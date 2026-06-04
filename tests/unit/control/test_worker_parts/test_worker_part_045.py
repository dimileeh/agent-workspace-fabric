"""ControlWorker orphan-directory reconcile sweep wiring (WS-B2, issue #361).

These cover only the worker-side interval gating and transient-vs-fatal error
handling for ``_maybe_reconcile_orphan_dirs``; the reconcile engine itself is
tested in ``tests/unit/service/test_gc_reconcile.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.exc import InterfaceError

from awf.control.worker import ControlWorker, WorkerConfig
from awf.service.gc_reconcile import ORPHAN_DIR_REAP_DRY_RUN, OrphanDirReconcileResult

pytestmark = pytest.mark.unit


def _closed_connection_error() -> InterfaceError:
    return InterfaceError(
        "SELECT 1",
        {},
        RuntimeError("connection is closed"),
        connection_invalidated=True,
    )


def _dry_run_result() -> OrphanDirReconcileResult:
    return OrphanDirReconcileResult(
        status="dry_run",
        reason_code=ORPHAN_DIR_REAP_DRY_RUN,
        scanned_count=0,
        orphan_count=0,
        reaped_count=0,
        cap_limit=50,
        capped=False,
        dropped_count=0,
    )


def _make_worker(
    reconciler: object | None,
    *,
    interval: float = 300.0,
) -> ControlWorker:
    return ControlWorker(
        session_factory=SimpleNamespace(),  # type: ignore[arg-type]
        provisioner=SimpleNamespace(),  # type: ignore[arg-type]
        orphan_dir_reconciler=reconciler,  # type: ignore[arg-type]
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            orphan_reconcile_scan_interval_seconds=interval,
        ),
    )


async def test_no_op_when_reconciler_is_none() -> None:
    worker = _make_worker(None)
    worker._next_orphan_reconcile_scan_at = 0.0  # noqa: SLF001

    await worker._maybe_reconcile_orphan_dirs()  # noqa: SLF001

    # Cursor is untouched when no callback is wired.
    assert worker._next_orphan_reconcile_scan_at == 0.0  # noqa: SLF001


async def test_interval_gating_invokes_and_reschedules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_time = 1_000.0
    monkeypatch.setattr("awf.control.worker.cleanup.monotonic", lambda: current_time)
    calls = 0

    async def _reconcile() -> OrphanDirReconcileResult:
        nonlocal calls
        calls += 1
        return _dry_run_result()

    worker = _make_worker(_reconcile, interval=300.0)
    worker._next_orphan_reconcile_scan_at = 0.0  # noqa: SLF001

    await worker._maybe_reconcile_orphan_dirs()  # noqa: SLF001
    assert calls == 1
    assert worker._next_orphan_reconcile_scan_at == current_time + 300.0  # noqa: SLF001

    # Within the interval: not invoked again.
    await worker._maybe_reconcile_orphan_dirs()  # noqa: SLF001
    assert calls == 1


async def test_transient_db_error_warns_and_reschedules_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_time = 2_000.0
    monkeypatch.setattr("awf.control.worker.cleanup.monotonic", lambda: current_time)
    calls = 0

    async def _reconcile() -> OrphanDirReconcileResult:
        nonlocal calls
        calls += 1
        raise _closed_connection_error()

    worker = _make_worker(_reconcile, interval=120.0)
    worker._next_orphan_reconcile_scan_at = 0.0  # noqa: SLF001

    # Must not raise on a transient closed-connection error.
    await worker._maybe_reconcile_orphan_dirs()  # noqa: SLF001
    assert calls == 1
    assert worker._next_orphan_reconcile_scan_at == current_time + 120.0  # noqa: SLF001

    # Still gated by the rescheduled cursor.
    await worker._maybe_reconcile_orphan_dirs()  # noqa: SLF001
    assert calls == 1


async def test_fatal_error_is_logged_swallowed_and_reschedules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_time = 3_000.0
    monkeypatch.setattr("awf.control.worker.cleanup.monotonic", lambda: current_time)
    logged: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "awf.control.worker.cleanup._log.exception",
        lambda event, **fields: logged.append((event, fields)),
    )

    async def _reconcile() -> OrphanDirReconcileResult:
        raise RuntimeError("disk exploded")

    worker = _make_worker(_reconcile, interval=60.0)
    worker._next_orphan_reconcile_scan_at = 0.0  # noqa: SLF001

    # A non-transient error is logged loudly but NOT re-raised: re-raising
    # would skip workspace dispatch for the iteration and double-log through
    # run_forever's last-resort run_once_failed handler.
    await worker._maybe_reconcile_orphan_dirs()  # noqa: SLF001

    assert [event for event, _ in logged] == ["worker.orphan_dir_reconcile_failed"]
    assert logged[0][1]["reason_code"] == "ORPHAN_DIR_RECONCILE_FAILED"

    # The sweep still reschedules so the worker does not hot-loop on it.
    assert worker._next_orphan_reconcile_scan_at == current_time + 60.0  # noqa: SLF001
