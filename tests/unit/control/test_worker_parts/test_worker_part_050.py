"""ControlWorker terminal-workspace auth-dir GC reaper sweep wiring (#513).

These tests cover the worker-side interval gating, kill-switch gating, and error
handling for the CAP_SYS_ADMIN-context terminal-workspace GC loop, plus an
end-to-end reap through the real ``run_service_workspace_gc`` engine (in the
sibling ``tests/unit/service/test_worker_terminal_gc_reaper.py``). The GC engine
itself (classification, path planning, ``preserves_failed_workspaces`` policy) is
covered under ``tests/unit/service/test_gc*``; here we prove the worker loop
drives it from the one context (CAP_SYS_ADMIN) where the overlay
unmount-before-remove actually succeeds, unlike the capability-less API path.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from awf.control.worker import ControlWorker, WorkerConfig

pytestmark = pytest.mark.unit


def _ok_report() -> dict[str, object]:
    """Build a successful (nothing-reclaimed) GC report for worker tests."""
    return {
        "status": "succeeded",
        "reason_code": "WORKSPACE_GC_OK",
        "deleted_path_count": 0,
        "candidate_count": 0,
        "preserved_count": 0,
        "delete_errors": [],
    }


def _reaped_report() -> dict[str, object]:
    """Build a report that reclaimed one terminal-workspace auth dir."""
    return {
        "status": "succeeded",
        "reason_code": "WORKSPACE_GC_OK",
        "deleted_path_count": 1,
        "candidate_count": 1,
        "preserved_count": 1,
        "delete_errors": [],
    }


def _make_worker(
    reaper: object | None,
    *,
    terminal_workspace_gc_enabled: bool,
    interval: float = 3600.0,
) -> ControlWorker:
    """Create a minimally wired worker with the terminal-workspace GC reaper."""
    return ControlWorker(
        session_factory=SimpleNamespace(),  # type: ignore[arg-type]
        provisioner=SimpleNamespace(),  # type: ignore[arg-type]
        terminal_gc_reaper=reaper,  # type: ignore[arg-type]
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_provisions=0,
            terminal_workspace_gc_enabled=terminal_workspace_gc_enabled,
            terminal_workspace_gc_scan_interval_seconds=interval,
        ),
    )


async def test_no_op_when_terminal_gc_reaper_is_none() -> None:
    """The terminal-GC loop is inert when no reaper callback is wired."""
    worker = _make_worker(None, terminal_workspace_gc_enabled=True)
    worker._next_terminal_gc_reap_scan_at = 0.0  # noqa: SLF001

    await worker._maybe_reap_terminal_workspace_gc()  # noqa: SLF001

    assert worker._next_terminal_gc_reap_scan_at == 0.0  # noqa: SLF001


async def test_does_not_fire_when_terminal_workspace_gc_disabled() -> None:
    """The kill-switch prevents the reaper from running and keeps the cursor eligible."""
    calls = 0

    async def _reap() -> dict[str, object]:
        """Fail the test if the disabled kill-switch invokes the reaper."""
        nonlocal calls
        calls += 1
        raise AssertionError("reaper must not fire while terminal_workspace_gc is disabled")

    worker = _make_worker(_reap, terminal_workspace_gc_enabled=False)
    worker._next_terminal_gc_reap_scan_at = 0.0  # noqa: SLF001

    await worker._maybe_reap_terminal_workspace_gc()  # noqa: SLF001

    assert calls == 0
    # Keep the cursor eligible so enabling the kill-switch does not wait out a
    # stale disabled-period interval.
    assert worker._next_terminal_gc_reap_scan_at == 0.0  # noqa: SLF001


async def test_fires_and_reschedules_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An eligible reaper run executes once and advances its cursor by the interval."""
    current_time = 1_000.0
    monkeypatch.setattr("awf.control.worker.cleanup.monotonic", lambda: current_time)
    calls = 0

    async def _reap() -> dict[str, object]:
        """Record successful reaper invocations."""
        nonlocal calls
        calls += 1
        return _ok_report()

    worker = _make_worker(_reap, terminal_workspace_gc_enabled=True, interval=3600.0)
    worker._next_terminal_gc_reap_scan_at = 0.0  # noqa: SLF001

    await worker._maybe_reap_terminal_workspace_gc()  # noqa: SLF001
    assert calls == 1
    assert worker._next_terminal_gc_reap_scan_at == current_time + 3600.0  # noqa: SLF001

    # A second call within the interval is gated out.
    await worker._maybe_reap_terminal_workspace_gc()  # noqa: SLF001
    assert calls == 1


async def test_fatal_error_logged_swallowed_and_reschedules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected reaper failures are logged loudly, swallowed, and rescheduled."""
    current_time = 3_000.0
    monkeypatch.setattr("awf.control.worker.cleanup.monotonic", lambda: current_time)
    logged: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "awf.control.worker.cleanup._log.exception",
        lambda event, **fields: logged.append((event, fields)),
    )

    async def _reap() -> dict[str, object]:
        """Raise the fatal reaper failure under test."""
        raise RuntimeError("rmtree blew up")

    worker = _make_worker(_reap, terminal_workspace_gc_enabled=True, interval=60.0)
    worker._next_terminal_gc_reap_scan_at = 0.0  # noqa: SLF001

    await worker._maybe_reap_terminal_workspace_gc()  # noqa: SLF001

    assert [event for event, _ in logged] == ["worker.terminal_workspace_gc_reap_failed"]
    assert logged[0][1]["reason_code"] == "TERMINAL_WORKSPACE_GC_SWEEP_FAILED"
    assert worker._next_terminal_gc_reap_scan_at == current_time + 60.0  # noqa: SLF001


async def test_partial_report_is_warned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A self-protecting ``partial`` report is surfaced as a warning, not an error."""
    current_time = 4_000.0
    monkeypatch.setattr("awf.control.worker.cleanup.monotonic", lambda: current_time)
    warned: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "awf.control.worker.cleanup._log.warning",
        lambda event, **fields: warned.append((event, fields)),
    )

    async def _reap() -> dict[str, object]:
        """Return a partial report with one delete error and no reclaimed paths."""
        return {
            "status": "partial",
            "reason_code": "WORKSPACE_GC_PARTIAL",
            "deleted_path_count": 0,
            "candidate_count": 1,
            "preserved_count": 0,
            "delete_errors": [
                {"workspace_id": "ws_x", "kind": "auth", "path": "/w/auth/ws_x", "error": "boom"}
            ],
        }

    worker = _make_worker(_reap, terminal_workspace_gc_enabled=True, interval=120.0)
    worker._next_terminal_gc_reap_scan_at = 0.0  # noqa: SLF001

    await worker._maybe_reap_terminal_workspace_gc()  # noqa: SLF001

    assert [event for event, _ in warned] == ["worker.terminal_workspace_gc_partial"]
    assert warned[0][1]["reason_code"] == "WORKSPACE_GC_PARTIAL"
    # Only the count is surfaced, never the auth-dir path/contents.
    assert warned[0][1]["delete_error_count"] == 1
    assert worker._next_terminal_gc_reap_scan_at == current_time + 120.0  # noqa: SLF001


async def test_successful_reclaim_is_logged_at_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When paths are reclaimed the sweep logs an ``info`` summary with counts only."""
    monkeypatch.setattr("awf.control.worker.cleanup.monotonic", lambda: 5_000.0)
    info: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "awf.control.worker.cleanup._log.info",
        lambda event, **fields: info.append((event, fields)),
    )

    async def _reap() -> dict[str, object]:
        """Return a report that reclaimed one terminal-workspace auth dir."""
        return _reaped_report()

    worker = _make_worker(_reap, terminal_workspace_gc_enabled=True)
    worker._next_terminal_gc_reap_scan_at = 0.0  # noqa: SLF001

    await worker._maybe_reap_terminal_workspace_gc()  # noqa: SLF001

    assert [event for event, _ in info] == ["worker.terminal_workspace_gc_reaped"]
    assert info[0][1]["deleted_path_count"] == 1
    assert info[0][1]["preserved_count"] == 1
    assert info[0][1]["reason_code"] == "WORKSPACE_GC_OK"


async def test_clean_no_op_report_is_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A succeeded run that reclaimed nothing logs neither info nor warning."""
    monkeypatch.setattr("awf.control.worker.cleanup.monotonic", lambda: 6_000.0)
    info: list[tuple[str, dict[str, object]]] = []
    warned: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "awf.control.worker.cleanup._log.info",
        lambda event, **fields: info.append((event, fields)),
    )
    monkeypatch.setattr(
        "awf.control.worker.cleanup._log.warning",
        lambda event, **fields: warned.append((event, fields)),
    )

    async def _reap() -> dict[str, object]:
        """Return a clean no-op report."""
        return _ok_report()

    worker = _make_worker(_reap, terminal_workspace_gc_enabled=True)
    worker._next_terminal_gc_reap_scan_at = 0.0  # noqa: SLF001

    await worker._maybe_reap_terminal_workspace_gc()  # noqa: SLF001

    assert info == []
    assert warned == []


async def test_run_once_invokes_terminal_gc_reap_loop() -> None:
    """The worker run-once path includes the terminal-workspace GC reaper loop."""
    calls = 0

    async def _reap() -> dict[str, object]:
        """Record run-once reaper invocation."""
        nonlocal calls
        calls += 1
        return _ok_report()

    worker = _make_worker(_reap, terminal_workspace_gc_enabled=True)
    worker._next_secret_lease_expiration_scan_at = float("inf")  # noqa: SLF001
    worker._next_terminal_runtime_release_scan_at = float("inf")  # noqa: SLF001
    worker._next_orphan_reconcile_scan_at = float("inf")  # noqa: SLF001
    worker._next_classified_orphan_reap_scan_at = float("inf")  # noqa: SLF001
    worker._next_claude_base_reap_scan_at = float("inf")  # noqa: SLF001

    dispatched = await worker.run_once()

    assert dispatched == 0
    assert calls == 1


async def test_run_once_consumes_gc_trigger_before_terminal_reap() -> None:
    """The budget-bound on-demand gc trigger is claimed before the periodic reap (#590).

    The API starts its worker-delegation deadline the moment it writes the pending
    ``service_gc_requests`` row. If the worker claimed that row only *after*
    ``_maybe_reap_terminal_workspace_gc`` (which can ``await`` a full multi-GB
    terminal GC sweep), a long periodic reap could push the claim past the deadline
    and surface a false SERVICE_GC_WORKER_DELEGATION_TIMEOUT with a healthy worker.
    Assert the on-demand consume runs first in the poll cycle.
    """
    order: list[str] = []

    worker = _make_worker(lambda: _ok_report(), terminal_workspace_gc_enabled=True)
    worker._next_secret_lease_expiration_scan_at = float("inf")  # noqa: SLF001
    worker._next_terminal_runtime_release_scan_at = float("inf")  # noqa: SLF001
    worker._next_orphan_reconcile_scan_at = float("inf")  # noqa: SLF001
    worker._next_classified_orphan_reap_scan_at = float("inf")  # noqa: SLF001
    worker._next_claude_base_reap_scan_at = float("inf")  # noqa: SLF001

    async def _consume() -> None:
        order.append("consume")

    async def _reap() -> None:
        order.append("reap")

    worker._maybe_consume_service_gc_trigger = _consume  # type: ignore[method-assign]  # noqa: SLF001
    worker._maybe_reap_terminal_workspace_gc = _reap  # type: ignore[method-assign]  # noqa: SLF001

    await worker.run_once()

    assert order.index("consume") < order.index("reap")


async def test_cancelled_error_propagates() -> None:
    """A cooperative-shutdown ``CancelledError`` propagates and does not reschedule."""
    import asyncio

    async def _reap() -> dict[str, object]:
        """Raise CancelledError to simulate shutdown mid-sweep."""
        raise asyncio.CancelledError

    worker = _make_worker(_reap, terminal_workspace_gc_enabled=True, interval=300.0)
    worker._next_terminal_gc_reap_scan_at = 0.0  # noqa: SLF001

    with pytest.raises(asyncio.CancelledError):
        await worker._maybe_reap_terminal_workspace_gc()  # noqa: SLF001

    # Cursor is not rescheduled when the sweep is cancelled.
    assert worker._next_terminal_gc_reap_scan_at == 0.0  # noqa: SLF001
