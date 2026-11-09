"""Work-slot restart adoption and the periodic reconciler (slot-release, plan §6.2 E).

At startup, before any dispatch, a worker checks every lease of a dead holder:
an ``awf-exec`` still in flight is ADOPTED (new holder and epoch), otherwise the
lease is RELEASED; a lease that cannot be proven either way is kept for the
reconciler. If the runtime cannot be queried every lease is kept and new
admission refused until a check succeeds. A reconciler then releases adopted and
cleanup-unproven leases once their process is gone.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog.testing
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import manager as worker_manager
from awf.control.worker import work_slots as worker_work_slots
from awf.control.worker.constants import (
    WORK_SLOT_ADOPTED,
    WORK_SLOT_RELEASED_BY_RECONCILER,
    WORK_SLOT_STARTUP_CHECK_FAILED,
)
from awf.db.enums import ResourceReservationPhase, WorkSlotLeasePhase, WorkspaceStatus
from awf.db.repositories import (
    ResourceReservationRepository,
    WorkerHeartbeatRepository,
    WorkSlotLeaseRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.service import worker_heartbeat
from tests.postgres import postgres_test_engine
from tests.unit.control._work_slot_support import create_slot_workspace
from tests.unit.control._worker_work_slot_support import (
    GatedSlotExecutor,
    RecordingProvisioner,
    SlotAwareExecutor,
    add_lease,
    create_in_status,
    create_requested,
    held_leases,
    make_worker,
    wait_until,
)

pytestmark = pytest.mark.unit

_DEAD = "control-worker-dead"
_FRESH = "control-worker-alive"
_TIMEOUT = 30.0


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class FakeProbe:
    def __init__(self, default: str = "absent") -> None:
        self.default = default
        self.results: dict[str, Any] = {}
        self.calls: list[str] = []

    async def probe(
        self,
        *,
        workspace_id: str,
        invocation_id: str,
        compose_project: str | None,
        compose_file: object,
    ) -> str:
        del workspace_id, compose_project, compose_file
        self.calls.append(invocation_id)
        result = self.results.get(invocation_id, self.default)
        if isinstance(result, BaseException):
            raise result
        return str(result)


async def _lease(session_factory: async_sessionmaker[AsyncSession], invocation_id: str) -> Any:
    async with session_factory() as session:
        return await WorkSlotLeaseRepository(session).get(invocation_id)


async def _record_fresh_heartbeat(
    session_factory: async_sessionmaker[AsyncSession], worker_id: str
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as session:
        await WorkerHeartbeatRepository(session).record_heartbeat(
            worker_id=worker_id,
            node_id="local",
            started_at=now,
            last_heartbeat_at=now + timedelta(minutes=10),
            poll_interval_seconds=60.0,
        )
        await session.commit()


def _reconcile_now(worker: Any) -> None:
    worker._work_slot_startup_checked = True  # noqa: SLF001
    worker._next_work_slot_reconcile_at = 0.0  # noqa: SLF001


async def test_startup_adopts_in_flight_and_releases_finished_leases_before_any_claim(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    in_flight = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    finished = await create_slot_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        reservation_phase=ResourceReservationPhase.peak.value,
    )
    queued = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    in_flight_lease = await add_lease(session_factory, in_flight, holder=_DEAD)
    finished_lease = await add_lease(session_factory, finished, holder=_DEAD)
    queued_ticket = await add_lease(
        session_factory, queued, holder=_DEAD, phase=WorkSlotLeasePhase.waiting
    )
    requested = await create_requested(session_factory)
    probe = FakeProbe()
    probe.results[in_flight_lease] = "present"
    provisioner = RecordingProvisioner(session_factory, to_ready=False)
    worker = make_worker(
        session_factory,
        executor=SlotAwareExecutor(),
        provisioner=provisioner,
        max_concurrent_executions=2,
        work_slot_probe=probe,
    )

    with structlog.testing.capture_logs() as logs:
        await asyncio.wait_for(worker.run_once(), timeout=_TIMEOUT)

    adopted = await _lease(session_factory, in_flight_lease)
    assert adopted is not None
    assert adopted.holder_worker_id == worker._worker_id  # noqa: SLF001
    assert adopted.holder_epoch == 2
    assert adopted.phase == WorkSlotLeasePhase.adopted.value
    assert await _lease(session_factory, finished_lease) is None
    assert await _lease(session_factory, queued_ticket) is None
    async with session_factory() as session:
        reservation = await ResourceReservationRepository(session).active_for_workspace(finished)
    assert reservation is not None and reservation.phase == "steady"
    # The released slot was admitted in the very same cycle: the check ran first.
    assert provisioner.calls == [requested]
    assert any(entry.get("reason_code") == WORK_SLOT_ADOPTED for entry in logs)
    assert any(entry.get("reason_code") == WORK_SLOT_RELEASED_BY_RECONCILER for entry in logs)
    assert worker._work_slot_startup_checked  # noqa: SLF001


async def test_one_unknown_probe_keeps_its_lease_without_stalling_the_node(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Docker answers, but one lease's process cannot be proven either way: only
    # that lease is kept (fail closed for it alone). The check still completes,
    # the other dead holders are adopted/released, and admission stays open.
    unknown_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    in_flight = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    finished = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    unknown_lease = await add_lease(session_factory, unknown_ws, holder=_DEAD)
    in_flight_lease = await add_lease(session_factory, in_flight, holder=_DEAD)
    finished_lease = await add_lease(session_factory, finished, holder=_DEAD)
    requested = await create_requested(session_factory)
    probe = FakeProbe()
    probe.results.update({unknown_lease: "unknown", in_flight_lease: "present"})
    provisioner = RecordingProvisioner(session_factory, to_ready=False)
    worker = make_worker(
        session_factory,
        executor=SlotAwareExecutor(),
        provisioner=provisioner,
        max_concurrent_executions=3,
        work_slot_probe=probe,
    )

    with structlog.testing.capture_logs() as logs:
        await asyncio.wait_for(worker.run_once(), timeout=_TIMEOUT)

    assert worker._work_slot_startup_checked  # noqa: SLF001
    assert not worker._work_slot_admission_blocked  # noqa: SLF001
    assert not any(entry.get("reason_code") == WORK_SLOT_STARTUP_CHECK_FAILED for entry in logs)
    kept = await _lease(session_factory, unknown_lease)
    assert kept is not None
    assert (kept.holder_worker_id, kept.holder_epoch) == (_DEAD, 1)
    adopted = await _lease(session_factory, in_flight_lease)
    assert adopted is not None and adopted.holder_worker_id == worker._worker_id  # noqa: SLF001
    assert await _lease(session_factory, finished_lease) is None
    assert provisioner.calls == [requested]

    # The kept lease is re-probed on every reconciler pass until it resolves.
    probes_before = probe.calls.count(unknown_lease)
    _reconcile_now(worker)
    await worker._maybe_reconcile_work_slot_leases()  # noqa: SLF001
    assert probe.calls.count(unknown_lease) == probes_before + 1
    assert await _lease(session_factory, unknown_lease) is not None
    probe.results[unknown_lease] = "absent"
    _reconcile_now(worker)
    await worker._maybe_reconcile_work_slot_leases()  # noqa: SLF001
    assert await _lease(session_factory, unknown_lease) is None


async def test_unreachable_docker_keeps_every_lease_and_refuses_all_admission(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    held = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    held_lease = await add_lease(session_factory, held, holder=_DEAD)
    ready = await create_in_status(session_factory, WorkspaceStatus.ready, "ready")
    requested = await create_requested(session_factory)
    blocked_hook = await create_in_status(session_factory, WorkspaceStatus.running, "hook")
    probe = FakeProbe(default="unavailable")
    executor = SlotAwareExecutor(session_factory=session_factory)
    provisioner = RecordingProvisioner(session_factory, to_ready=False)
    worker = make_worker(
        session_factory,
        executor=executor,
        provisioner=provisioner,
        max_concurrent_executions=3,
        work_slot_probe=probe,
    )

    with structlog.testing.capture_logs() as logs:
        assert await asyncio.wait_for(worker.run_once(), timeout=_TIMEOUT) == 0

    assert worker._work_slot_admission_blocked  # noqa: SLF001
    assert any(entry.get("reason_code") == WORK_SLOT_STARTUP_CHECK_FAILED for entry in logs)
    unchanged = await _lease(session_factory, held_lease)
    assert unchanged is not None
    assert (unchanged.holder_worker_id, unchanged.holder_epoch) == (_DEAD, 1)
    assert provisioner.calls == []
    assert executor.execute_calls == []
    # Hooks queue a ticket but are never granted while fail-closed.
    scope = worker._new_invocation_work_slot(blocked_hook)  # noqa: SLF001
    hook = asyncio.create_task(_hold(scope))
    await wait_until(lambda: _phase_of(session_factory, blocked_hook, "waiting"))
    await asyncio.sleep(0.2)
    assert not hook.done()

    # The next successful check (once per heartbeat interval) unblocks everything:
    # the queued hook is woken and granted, then new work is admitted again.
    probe.default = "present"
    worker._next_work_slot_startup_check_at = 0.0  # noqa: SLF001
    try:
        await asyncio.wait_for(worker.run_once(), timeout=_TIMEOUT)
        assert not worker._work_slot_admission_blocked  # noqa: SLF001
        await asyncio.wait_for(hook, timeout=_TIMEOUT)
        await asyncio.wait_for(worker.run_once(), timeout=_TIMEOUT)
        await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=_TIMEOUT)
        assert executor.execute_calls == [ready]
        assert provisioner.calls == [requested]
    finally:
        hook.cancel()


async def _hold(scope: Any) -> None:
    async with scope, scope.hold("agent"):
        pass


async def _phase_of(
    session_factory: async_sessionmaker[AsyncSession], workspace_id: str, phase: str
) -> bool:
    async with session_factory() as session:
        rows = await WorkSlotLeaseRepository(session).list_for_workspace(workspace_id)
    return phase in {row.phase for row in rows}


async def test_startup_check_retries_at_most_once_per_heartbeat_interval(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    held = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    await add_lease(session_factory, held, holder=_DEAD)
    probe = FakeProbe(default="unavailable")
    worker = make_worker(session_factory, executor=SlotAwareExecutor(), work_slot_probe=probe)

    await worker._ensure_work_slot_startup_check()  # noqa: SLF001
    await worker._ensure_work_slot_startup_check()  # noqa: SLF001

    assert len(probe.calls) == 1
    assert worker._work_slot_admission_blocked  # noqa: SLF001


async def test_startup_lease_read_failure_fails_closed(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = make_worker(session_factory, executor=SlotAwareExecutor())

    async def _db_down() -> object:
        raise OperationalError("SELECT", {}, Exception("db down"))

    monkeypatch.setattr(worker, "_load_work_slot_leases_for_reconcile", _db_down)

    await worker._ensure_work_slot_startup_check()  # noqa: SLF001

    assert worker._work_slot_admission_blocked  # noqa: SLF001
    assert not worker._work_slot_startup_checked  # noqa: SLF001
    assert await worker._requested_provision_slots() == 0  # noqa: SLF001
    assert await worker._claim_ready_work_slots(["ws_any"]) == {}  # noqa: SLF001


async def test_startup_leaves_fresh_workers_leases_alone_and_skips_provision_only_workers(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    held = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    lease_id = await add_lease(session_factory, held, holder=_FRESH)
    await _record_fresh_heartbeat(session_factory, _FRESH)
    probe = FakeProbe()
    worker = make_worker(session_factory, executor=SlotAwareExecutor(), work_slot_probe=probe)

    await worker._ensure_work_slot_startup_check()  # noqa: SLF001

    assert probe.calls == []
    lease = await _lease(session_factory, lease_id)
    assert lease is not None and lease.holder_worker_id == _FRESH
    provision_only = make_worker(session_factory, executor=None, work_slot_probe=probe)
    await provision_only._ensure_work_slot_startup_check()  # noqa: SLF001
    assert not provision_only._work_slot_startup_checked  # noqa: SLF001


async def test_without_a_probe_nothing_is_adoptable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    held = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    lease_id = await add_lease(session_factory, held, holder=_DEAD)
    worker = make_worker(session_factory, executor=SlotAwareExecutor())

    await worker._ensure_work_slot_startup_check()  # noqa: SLF001

    assert await _lease(session_factory, lease_id) is None


async def test_reconciler_renews_present_and_releases_absent_own_leases(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    probe = FakeProbe(default="present")
    worker = make_worker(session_factory, executor=SlotAwareExecutor(), work_slot_probe=probe)
    me = worker._worker_id  # noqa: SLF001
    adopted_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    unproven_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    expired_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.ready)
    live_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    adopted = await add_lease(
        session_factory, adopted_ws, holder=me, phase=WorkSlotLeasePhase.adopted, expires_in=5
    )
    unproven = await add_lease(session_factory, unproven_ws, holder=me, cleanup_unproven=True)
    expired = await add_lease(session_factory, expired_ws, holder=me, expires_in=-1)
    live = await add_lease(session_factory, live_ws, holder=me)
    before = await _lease(session_factory, adopted)
    # An expired, untracked own lease (a parked claim nobody continued) is gone.
    probe.results[expired] = "absent"

    _reconcile_now(worker)
    await worker._maybe_reconcile_work_slot_leases()  # noqa: SLF001

    # Present: kept and renewed. A plain unexpired own lease is not probed.
    renewed = await _lease(session_factory, adopted)
    assert renewed is not None and renewed.expires_at > before.expires_at
    assert await _lease(session_factory, unproven) is not None
    assert await _lease(session_factory, expired) is None
    assert live not in probe.calls
    assert sorted(probe.calls) == sorted([adopted, unproven, expired])

    probe.default = "absent"
    _reconcile_now(worker)
    with structlog.testing.capture_logs() as logs:
        await worker._maybe_reconcile_work_slot_leases()  # noqa: SLF001
    for invocation_id in (adopted, unproven):
        assert await _lease(session_factory, invocation_id) is None
    assert await _lease(session_factory, live) is not None
    assert (
        sum(1 for entry in logs if entry.get("reason_code") == WORK_SLOT_RELEASED_BY_RECONCILER)
        == 2
    )


async def test_reconciler_adopts_or_releases_dead_holders_and_keeps_unknown(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    probe = FakeProbe()
    worker = make_worker(session_factory, executor=SlotAwareExecutor(), work_slot_probe=probe)
    present_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    absent_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    unknown_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    fresh_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    present = await add_lease(session_factory, present_ws, holder=_DEAD)
    absent = await add_lease(session_factory, absent_ws, holder=_DEAD)
    unknown = await add_lease(session_factory, unknown_ws, holder=_DEAD)
    fresh = await add_lease(session_factory, fresh_ws, holder=_FRESH)
    await _record_fresh_heartbeat(session_factory, _FRESH)
    probe.results.update({present: "present", unknown: OSError("docker gone")})
    expired_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.monitoring_pr)
    expired_ticket = await add_lease(
        session_factory,
        expired_ws,
        holder=_DEAD,
        phase=WorkSlotLeasePhase.waiting,
        expires_in=-1,
    )

    _reconcile_now(worker)
    await worker._maybe_reconcile_work_slot_leases()  # noqa: SLF001

    taken = await _lease(session_factory, present)
    assert taken is not None
    assert taken.holder_worker_id == worker._worker_id  # noqa: SLF001
    assert taken.phase == WorkSlotLeasePhase.adopted.value
    assert await _lease(session_factory, absent) is None
    kept = await _lease(session_factory, unknown)
    assert kept is not None and kept.holder_worker_id == _DEAD
    assert fresh not in probe.calls
    assert await _lease(session_factory, expired_ticket) is None


async def test_reconciler_skips_leases_tracked_by_a_running_scope_and_honours_cadence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    probe = FakeProbe()
    workspace_id = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    worker = make_worker(session_factory, executor=SlotAwareExecutor(), work_slot_probe=probe)
    scope = worker._new_invocation_work_slot(workspace_id)  # noqa: SLF001

    # Not reconciled before the startup check succeeds.
    await worker._maybe_reconcile_work_slot_leases()  # noqa: SLF001
    async with scope, scope.hold("agent"):
        lease = scope.lease
        assert lease is not None
        async with session_factory() as session:
            await WorkSlotLeaseRepository(session).update_fenced(
                lease.invocation_id,
                holder_worker_id=lease.holder_worker_id,
                holder_epoch=lease.holder_epoch,
                values={"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
            )
            await session.commit()
        _reconcile_now(worker)
        await worker._maybe_reconcile_work_slot_leases()  # noqa: SLF001
        # The second call within the interval is a no-op.
        await worker._maybe_reconcile_work_slot_leases()  # noqa: SLF001
        assert probe.calls == []


async def test_reconciler_survives_read_and_write_failures(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = FakeProbe()
    worker = make_worker(session_factory, executor=SlotAwareExecutor(), work_slot_probe=probe)
    held = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    lease_id = await add_lease(session_factory, held, holder=_DEAD)

    async def _read_down() -> object:
        raise OperationalError("SELECT", {}, Exception("db down"))

    with monkeypatch.context() as patch:
        patch.setattr(worker, "_load_work_slot_leases_for_reconcile", _read_down)
        _reconcile_now(worker)
        await worker._maybe_reconcile_work_slot_leases()  # noqa: SLF001
    assert probe.calls == []

    async def _write_down(*_args: object, **_kwargs: object) -> object:
        raise OperationalError("DELETE", {}, Exception("db down"))

    monkeypatch.setattr(worker_work_slots, "release_work", _write_down)
    monkeypatch.setattr(WorkSlotLeaseRepository, "delete_expired_waiters", _write_down)
    _reconcile_now(worker)
    with structlog.testing.capture_logs() as logs:
        await worker._maybe_reconcile_work_slot_leases()  # noqa: SLF001

    # The probe said absent, but the release write failed: the lease survives
    # until a later pass instead of pretending the slot was freed.
    assert probe.calls == [lease_id]
    assert await _lease(session_factory, lease_id) is not None
    events = {entry["event"] for entry in logs}
    assert "worker.work_slot_reconcile_write_failed" in events
    assert "worker.work_slot_expired_waiter_cleanup_failed" in events


async def test_draining_monitor_lease_goes_to_the_reconciler_and_never_blocks_others(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # #276 regression on the lease model: a wedged monitor that held a slot when
    # its workspace left monitoring_pr is cancelled and reclassified draining; its
    # lease is handed to the reconciler (cleanup unproven) and released once the
    # probe proves its process gone, so other workspaces are admitted again.
    monitor = await create_in_status(session_factory, WorkspaceStatus.monitoring_pr, "wedged")
    probe = FakeProbe(default="present")
    executor = GatedSlotExecutor(monitor_agent=True, wedge=True)
    worker = make_worker(
        session_factory, executor=executor, max_concurrent_executions=1, work_slot_probe=probe
    )
    executor.monitor_release.set()
    try:
        await asyncio.wait_for(worker.run_once(), timeout=_TIMEOUT)
        await wait_until(lambda: executor.hold_order == [monitor])
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get(monitor)
            assert ws is not None
            # Leaves monitoring_pr (paused without an operator directive).
            ws.status = WorkspaceStatus.blocked.value
            await session.commit()
        requested = await create_requested(session_factory)

        await asyncio.wait_for(worker.run_once(), timeout=_TIMEOUT)

        assert worker._tracked_draining_workspace_ids() == [monitor]  # noqa: SLF001
        assert executor.cancellations >= 1
        async with session_factory() as session:
            (lease,) = await WorkSlotLeaseRepository(session).list_for_workspace(monitor)
        assert lease.cleanup_unproven is True
        # The wedged process is still alive: the slot stays occupied and the
        # same workspace is not re-dispatched while it drains.
        _reconcile_now(worker)
        await asyncio.wait_for(worker.run_once(), timeout=_TIMEOUT)
        assert monitor not in executor.execute_calls
        assert await _status(session_factory, requested) == WorkspaceStatus.requested.value

        executor.unwedge.set()
        await wait_until(lambda: not worker._tracked_draining_workspace_ids())  # noqa: SLF001
        probe.default = "absent"
        _reconcile_now(worker)
        await asyncio.wait_for(worker.run_once(), timeout=_TIMEOUT)
        assert await _status(session_factory, requested) != WorkspaceStatus.requested.value
    finally:
        executor.unwedge.set()
        for gate_id in list(executor.gates):
            executor.gate(gate_id).set()
        await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=_TIMEOUT)


@pytest.mark.parametrize("startup_checked", [True, False])
async def test_once_drain_reconciles_so_a_retry_behind_an_unproven_lease_finishes(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    startup_checked: bool,
) -> None:
    # A once=True worker never calls run_once again while its tasks drain, so a
    # recovery retry waiting for its cleanup-unproven row to be reconciled away
    # would wait forever unless the drain itself keeps reconciling (and first
    # completes a startup check that run_once could not finish).
    for module in (worker_manager, worker_work_slots):
        monkeypatch.setattr(
            module, "worker_heartbeat_write_interval_seconds", lambda _poll_interval: 0.01
        )
    workspace_id = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    probe = FakeProbe(default="absent")
    worker = make_worker(session_factory, executor=SlotAwareExecutor(), work_slot_probe=probe)
    if startup_checked:
        _reconcile_now(worker)
    unproven = await add_lease(
        session_factory, workspace_id, holder=worker._worker_id, cleanup_unproven=True
    )
    scope = worker._new_invocation_work_slot(workspace_id)  # noqa: SLF001

    async def _retry() -> str:
        async with scope, scope.hold("agent") as handle:
            return handle.invocation_id

    retry = asyncio.create_task(_retry())
    worker._execution_tasks[workspace_id] = retry  # noqa: SLF001
    try:
        await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=5.0)
    finally:
        retry.cancel()
        await asyncio.gather(retry, return_exceptions=True)

    assert worker._work_slot_startup_checked is True  # noqa: SLF001
    assert retry.result() != unproven
    assert unproven in probe.calls
    assert await _lease(session_factory, unproven) is None


class _GatedProvisioner(RecordingProvisioner):
    """Provisioning that blocks until released, then stays ``provisioning``."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory, to_ready=False)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def provision_claimed(
        self, workspace_id: str, execution_claim_epoch: int | None = None
    ) -> None:
        self.entered.set()
        await self.release.wait()
        await super().provision_claimed(workspace_id, execution_claim_epoch)


async def test_once_run_keeps_heartbeating_so_a_peer_never_frees_a_live_provision_lease(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # run_once awaits provisioning inline, so a once=True worker never reaches the
    # drain heartbeat meanwhile. A stale heartbeat would let a peer's reconciler
    # probe the provisioning lease (no agent container yet: ``absent``) and free a
    # slot provisioning still uses.
    monkeypatch.setattr(worker_heartbeat, "WORKER_HEARTBEAT_MIN_STALE_AFTER_SECONDS", 0.3)
    monkeypatch.setattr(
        worker_manager, "worker_heartbeat_write_interval_seconds", lambda _poll_interval: 0.05
    )
    workspace_id = await create_requested(session_factory)
    provisioner = _GatedProvisioner(session_factory)
    worker = make_worker(session_factory, executor=SlotAwareExecutor(), provisioner=provisioner)
    probe = FakeProbe(default="absent")
    peer = make_worker(session_factory, executor=SlotAwareExecutor(), work_slot_probe=probe)

    run = asyncio.create_task(worker.run_once_and_drain())
    try:
        await asyncio.wait_for(provisioner.entered.wait(), timeout=_TIMEOUT)
        await asyncio.sleep(0.9)  # three staleness windows into provisioning
        _reconcile_now(peer)
        await peer._maybe_reconcile_work_slot_leases()  # noqa: SLF001

        leases = await held_leases(session_factory)
        assert [(lease.workspace_id, lease.holder_worker_id) for lease in leases] == [
            (workspace_id, worker._worker_id)  # noqa: SLF001
        ]
        assert probe.calls == []
    finally:
        provisioner.release.set()
        await asyncio.wait_for(run, timeout=_TIMEOUT)

    assert provisioner.calls == [workspace_id]
    assert await held_leases(session_factory) == []


async def _status(session_factory: async_sessionmaker[AsyncSession], workspace_id: str) -> str:
    async with session_factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        return ws.status
