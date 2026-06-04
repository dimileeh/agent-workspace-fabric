"""ControlWorker classified-orphan reaper sweep wiring.

These tests cover the worker-side interval gating and error handling for the
readiness-driven classified-orphan reaper. The scan/classification/reap engine
itself is covered in ``tests/unit/service/test_orphan_resources_parts``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.exc import InterfaceError

from awf.control.worker import ControlWorker, WorkerConfig
from awf.service.orphan_resources import ORPHAN_REAP_OK, OrphanReapResult

pytestmark = pytest.mark.unit


def _closed_connection_error() -> InterfaceError:
    return InterfaceError(
        "SELECT 1",
        {},
        RuntimeError("connection is closed"),
        connection_invalidated=True,
    )


def _reap_result() -> OrphanReapResult:
    return OrphanReapResult(
        enabled=True,
        status="ok",
        reason_code=ORPHAN_REAP_OK,
    )


def _make_worker(
    reaper: object | None,
    *,
    auto_cleanup_orphans: bool,
    interval: float = 300.0,
) -> ControlWorker:
    return ControlWorker(
        session_factory=SimpleNamespace(),  # type: ignore[arg-type]
        provisioner=SimpleNamespace(),  # type: ignore[arg-type]
        classified_orphan_reaper=reaper,  # type: ignore[arg-type]
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_provisions=0,
            auto_cleanup_orphans=auto_cleanup_orphans,
            classified_orphan_reap_scan_interval_seconds=interval,
        ),
    )


async def test_no_op_when_reaper_is_none() -> None:
    worker = _make_worker(None, auto_cleanup_orphans=True)
    worker._next_classified_orphan_reap_scan_at = 0.0  # noqa: SLF001

    await worker._maybe_reap_classified_orphans()  # noqa: SLF001

    assert worker._next_classified_orphan_reap_scan_at == 0.0  # noqa: SLF001


async def test_classified_orphan_reaper_does_not_fire_when_auto_cleanup_off() -> None:
    calls = 0

    async def _reap() -> OrphanReapResult:
        nonlocal calls
        calls += 1
        raise AssertionError("reaper must not fire while cleanup is disabled")

    worker = _make_worker(_reap, auto_cleanup_orphans=False)
    worker._next_classified_orphan_reap_scan_at = 0.0  # noqa: SLF001

    await worker._maybe_reap_classified_orphans()  # noqa: SLF001

    assert calls == 0
    # Keep the cursor eligible so enabling the kill-switch does not wait out a
    # stale disabled-period interval.
    assert worker._next_classified_orphan_reap_scan_at == 0.0  # noqa: SLF001


async def test_classified_orphan_reaper_fires_and_reschedules_when_auto_cleanup_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_time = 1_000.0
    monkeypatch.setattr("awf.control.worker.cleanup.monotonic", lambda: current_time)
    calls = 0

    async def _reap() -> OrphanReapResult:
        nonlocal calls
        calls += 1
        return _reap_result()

    worker = _make_worker(_reap, auto_cleanup_orphans=True, interval=300.0)
    worker._next_classified_orphan_reap_scan_at = 0.0  # noqa: SLF001

    await worker._maybe_reap_classified_orphans()  # noqa: SLF001
    assert calls == 1
    assert worker._next_classified_orphan_reap_scan_at == current_time + 300.0  # noqa: SLF001

    await worker._maybe_reap_classified_orphans()  # noqa: SLF001
    assert calls == 1


async def test_classified_orphan_reaper_transient_db_error_warns_and_reschedules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_time = 2_000.0
    monkeypatch.setattr("awf.control.worker.cleanup.monotonic", lambda: current_time)
    logged: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "awf.control.worker.cleanup._log.warning",
        lambda event, **fields: logged.append((event, fields)),
    )

    async def _reap() -> OrphanReapResult:
        raise _closed_connection_error()

    worker = _make_worker(_reap, auto_cleanup_orphans=True, interval=120.0)
    worker._next_classified_orphan_reap_scan_at = 0.0  # noqa: SLF001

    await worker._maybe_reap_classified_orphans()  # noqa: SLF001

    assert worker._next_classified_orphan_reap_scan_at == current_time + 120.0  # noqa: SLF001
    assert [event for event, _ in logged] == ["worker.classified_orphan_reap_db_connection_closed"]
    assert logged[0][1]["reason_code"] == "DB_CONNECTION_CLOSED"


async def test_classified_orphan_reaper_fatal_error_is_logged_swallowed_and_reschedules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_time = 3_000.0
    monkeypatch.setattr("awf.control.worker.cleanup.monotonic", lambda: current_time)
    logged: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        "awf.control.worker.cleanup._log.exception",
        lambda event, **fields: logged.append((event, fields)),
    )

    async def _reap() -> OrphanReapResult:
        raise RuntimeError("docker inventory failed")

    worker = _make_worker(_reap, auto_cleanup_orphans=True, interval=60.0)
    worker._next_classified_orphan_reap_scan_at = 0.0  # noqa: SLF001

    await worker._maybe_reap_classified_orphans()  # noqa: SLF001

    assert [event for event, _ in logged] == ["worker.classified_orphan_reap_failed"]
    assert logged[0][1]["reason_code"] == "CLASSIFIED_ORPHAN_REAP_FAILED"
    assert worker._next_classified_orphan_reap_scan_at == current_time + 60.0  # noqa: SLF001


async def test_run_once_invokes_classified_orphan_reaper_loop() -> None:
    calls = 0

    async def _reap() -> OrphanReapResult:
        nonlocal calls
        calls += 1
        return _reap_result()

    worker = _make_worker(_reap, auto_cleanup_orphans=True, interval=300.0)
    worker._next_secret_lease_expiration_scan_at = float("inf")  # noqa: SLF001
    worker._next_terminal_runtime_release_scan_at = float("inf")  # noqa: SLF001
    worker._next_orphan_reconcile_scan_at = float("inf")  # noqa: SLF001

    dispatched = await worker.run_once()

    assert dispatched == 0
    assert calls == 1
