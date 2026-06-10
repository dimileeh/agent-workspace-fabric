"""ControlWorker superseded-Claude-base reaper sweep wiring (#509).

These tests cover the worker-side interval gating, kill-switch gating, and error
handling for the CAP_SYS_ADMIN-context shared-base reaper, plus one end-to-end
reap through the real ``reap_superseded_claude_bases`` engine. The reaper engine
itself (protection rules, dry-run/execute, permission handling) is covered in
``tests/unit/service/test_gc_claude_base.py``; here we prove the worker loop
actually drives a real reclaim when the capability probe is trustworthy.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.control.worker import ControlWorker, WorkerConfig
from awf.node.auth_mounts import _host_claude_signature, _shared_claude_base_dir
from awf.service.gc_claude_base import (
    CLAUDE_BASE_SUPERSEDED_REAPED,
    reap_superseded_claude_bases,
)

pytestmark = pytest.mark.unit


def _ok_report() -> dict[str, object]:
    """Build a successful (nothing-reaped) reaper report for worker tests."""
    return {"status": "skipped", "reason_code": "CLAUDE_BASE_GC_NOOP", "reaped": []}


def _reaped_report() -> dict[str, object]:
    """Build a report that reclaimed one superseded base."""
    return {
        "status": "ok",
        "reason_code": CLAUDE_BASE_SUPERSEDED_REAPED,
        "reaped": ["sigdsupersede000"],
    }


def _make_worker(
    reaper: object | None,
    *,
    claude_base_gc_enabled: bool,
    interval: float = 3600.0,
) -> ControlWorker:
    """Create a minimally wired worker with the Claude-base reaper dependency."""
    return ControlWorker(
        session_factory=SimpleNamespace(),  # type: ignore[arg-type]
        provisioner=SimpleNamespace(),  # type: ignore[arg-type]
        claude_base_reaper=reaper,  # type: ignore[arg-type]
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_provisions=0,
            claude_base_gc_enabled=claude_base_gc_enabled,
            claude_base_reap_scan_interval_seconds=interval,
        ),
    )


async def test_no_op_when_reaper_is_none() -> None:
    """The Claude-base loop is inert when no reaper callback is wired."""
    worker = _make_worker(None, claude_base_gc_enabled=True)
    worker._next_claude_base_reap_scan_at = 0.0  # noqa: SLF001

    await worker._maybe_reap_superseded_claude_bases()  # noqa: SLF001

    assert worker._next_claude_base_reap_scan_at == 0.0  # noqa: SLF001


async def test_does_not_fire_when_claude_base_gc_disabled() -> None:
    """The kill-switch prevents the reaper from running and keeps the cursor eligible."""
    calls = 0

    async def _reap() -> dict[str, object]:
        """Fail the test if the disabled kill-switch invokes the reaper."""
        nonlocal calls
        calls += 1
        raise AssertionError("reaper must not fire while claude_base_gc is disabled")

    worker = _make_worker(_reap, claude_base_gc_enabled=False)
    worker._next_claude_base_reap_scan_at = 0.0  # noqa: SLF001

    await worker._maybe_reap_superseded_claude_bases()  # noqa: SLF001

    assert calls == 0
    # Keep the cursor eligible so enabling the kill-switch does not wait out a
    # stale disabled-period interval.
    assert worker._next_claude_base_reap_scan_at == 0.0  # noqa: SLF001


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

    worker = _make_worker(_reap, claude_base_gc_enabled=True, interval=3600.0)
    worker._next_claude_base_reap_scan_at = 0.0  # noqa: SLF001

    await worker._maybe_reap_superseded_claude_bases()  # noqa: SLF001
    assert calls == 1
    assert worker._next_claude_base_reap_scan_at == current_time + 3600.0  # noqa: SLF001

    # A second call within the interval is gated out.
    await worker._maybe_reap_superseded_claude_bases()  # noqa: SLF001
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

    worker = _make_worker(_reap, claude_base_gc_enabled=True, interval=60.0)
    worker._next_claude_base_reap_scan_at = 0.0  # noqa: SLF001

    await worker._maybe_reap_superseded_claude_bases()  # noqa: SLF001

    assert [event for event, _ in logged] == ["worker.claude_base_reap_failed"]
    assert logged[0][1]["reason_code"] == "CLAUDE_BASE_REAP_SWEEP_FAILED"
    assert worker._next_claude_base_reap_scan_at == current_time + 60.0  # noqa: SLF001


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
        """Return a partial report with no bases reaped."""
        return {
            "status": "partial",
            "reason_code": "CLAUDE_BASE_REAP_LIVE_MOUNT_UNVERIFIABLE",
            "reaped": [],
            "errors": [],
            "unverifiable": ["sigdsupersede000"],
        }

    worker = _make_worker(_reap, claude_base_gc_enabled=True, interval=120.0)
    worker._next_claude_base_reap_scan_at = 0.0  # noqa: SLF001

    await worker._maybe_reap_superseded_claude_bases()  # noqa: SLF001

    assert [event for event, _ in warned] == ["worker.claude_base_reap_partial"]
    assert warned[0][1]["reason_code"] == "CLAUDE_BASE_REAP_LIVE_MOUNT_UNVERIFIABLE"
    assert warned[0][1]["unverifiable"] == ["sigdsupersede000"]
    assert worker._next_claude_base_reap_scan_at == current_time + 120.0  # noqa: SLF001


async def test_successful_reap_is_logged_at_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When bases are reclaimed the sweep logs an ``info`` summary with the names."""
    monkeypatch.setattr("awf.control.worker.cleanup.monotonic", lambda: 5_000.0)
    info: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "awf.control.worker.cleanup._log.info",
        lambda event, **fields: info.append((event, fields)),
    )

    async def _reap() -> dict[str, object]:
        """Return a report that reclaimed a superseded base."""
        return _reaped_report()

    worker = _make_worker(_reap, claude_base_gc_enabled=True)
    worker._next_claude_base_reap_scan_at = 0.0  # noqa: SLF001

    await worker._maybe_reap_superseded_claude_bases()  # noqa: SLF001

    assert [event for event, _ in info] == ["worker.claude_base_superseded_reaped"]
    assert info[0][1]["reaped"] == ["sigdsupersede000"]
    assert info[0][1]["reason_code"] == CLAUDE_BASE_SUPERSEDED_REAPED


async def test_run_once_invokes_claude_base_reap_loop() -> None:
    """The worker run-once path includes the Claude-base reaper loop."""
    calls = 0

    async def _reap() -> dict[str, object]:
        """Record run-once reaper invocation."""
        nonlocal calls
        calls += 1
        return _ok_report()

    worker = _make_worker(_reap, claude_base_gc_enabled=True)
    worker._next_secret_lease_expiration_scan_at = float("inf")  # noqa: SLF001
    worker._next_terminal_runtime_release_scan_at = float("inf")  # noqa: SLF001
    worker._next_orphan_reconcile_scan_at = float("inf")  # noqa: SLF001
    worker._next_classified_orphan_reap_scan_at = float("inf")  # noqa: SLF001

    dispatched = await worker.run_once()

    assert dispatched == 0
    assert calls == 1


async def test_worker_path_reaps_superseded_unmounted_unpinned_base(
    tmp_path: Path,
) -> None:
    """End-to-end: the worker loop drives a real reap when the capability probe is True.

    The injected closure calls the real ``reap_superseded_claude_bases`` with
    ``execute=True`` and a trustworthy ``capability_probe`` (the worker's
    CAP_SYS_ADMIN vantage), against a tmp layout with a current-signature base and
    a superseded, unmounted, unpinned base. The superseded base must be gone and
    the current-signature base must survive — proving the worker path actually
    reclaims disk (the leak fix for #509), unlike the API/no-CAP path.
    """
    work_dir = tmp_path / "work"
    host_home = tmp_path / "host-home"

    # Seed host ``~/.claude`` so the current signature is computable + protected.
    claude = host_home / ".claude"
    claude.mkdir(parents=True)
    (claude / "settings.json").write_text('{"theme": "dark"}\n')
    sig_current = _host_claude_signature(host_home)

    # A completed current-signature base (protected) and a superseded base (reaped).
    current_base = _shared_claude_base_dir(work_dir, sig_current)
    current_base.mkdir(parents=True)
    (current_base / "blob").write_text("x" * 1024)
    superseded_base = _shared_claude_base_dir(work_dir, "sigdsupersede000")
    superseded_base.mkdir(parents=True)
    (superseded_base / "blob").write_text("y" * 1024)

    # No overlay mounts: the superseded base is not a live lower.
    proc_mounts = tmp_path / "mounts"
    proc_mounts.write_text("proc /proc proc rw 0 0\n")

    captured: dict[str, object] = {}

    async def _reap() -> dict[str, object]:
        """Drive the real reaper as the worker (CAP_SYS_ADMIN) would."""
        report = reap_superseded_claude_bases(
            work_dir=work_dir,
            host_home=host_home,
            proc_mounts=proc_mounts,
            execute=True,
            capability_probe=lambda: True,
        )
        captured["report"] = report
        return report

    worker = _make_worker(_reap, claude_base_gc_enabled=True)
    worker._next_claude_base_reap_scan_at = 0.0  # noqa: SLF001

    await worker._maybe_reap_superseded_claude_bases()  # noqa: SLF001

    report = captured["report"]
    assert isinstance(report, dict)
    assert report["reaped"] == ["sigdsupersede000"]
    # The superseded base is reclaimed; the current-signature base survives.
    assert not superseded_base.parent.exists()
    assert current_base.is_dir()
