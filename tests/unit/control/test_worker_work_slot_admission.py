"""Worker admission with broker-owned work-slot leases (slot-release, plan §6.2 D, §6.1).

Requested claims take a lease atomically with the requested->provisioning claim;
waiting monitors and blocked/recovering workspaces hold none, so they no longer
starve new work. READY dispatch stays bounded by slots, reacquire waiters get a
freed slot before any new admission, and the saturation log is split into work
and waiting.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import structlog.testing
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.work_slot import MONITOR_WAITING_FOR_WORK_SLOT_REASON_CODE
from awf.db.enums import WorkSlotLeasePhase, WorkspaceStatus
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine
from tests.unit.control._work_slot_support import create_slot_workspace, lease_rows
from tests.unit.control._worker_work_slot_support import (
    GatedSlotExecutor,
    RecordingProvisioner,
    SlotAwareExecutor,
    add_lease,
    create_in_status,
    create_requested,
    held_leases,
    make_worker,
    status_of,
    wait_until,
)

pytestmark = pytest.mark.unit

_TIMEOUT = 30.0
_OTHER_WORKER = "control-worker-elsewhere"


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def test_requested_claim_takes_its_lease_atomically(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await create_requested(session_factory)
    worker = make_worker(session_factory, executor=SlotAwareExecutor(), max_concurrent_executions=2)

    assert await worker._claim_requested_for_provisioning(workspace_id)  # noqa: SLF001

    leases = await held_leases(session_factory)
    assert [(lease.workspace_id, lease.holder_worker_id) for lease in leases] == [
        (workspace_id, worker._worker_id)  # noqa: SLF001
    ]
    assert await status_of(session_factory, workspace_id) == WorkspaceStatus.provisioning.value


async def test_failure_before_commit_leaves_neither_claim_nor_lease(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await create_requested(session_factory)
    worker = make_worker(session_factory, executor=SlotAwareExecutor())

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("claim stamp failed")

    monkeypatch.setattr(worker, "_apply_execution_claim", _boom)

    with pytest.raises(RuntimeError, match="claim stamp failed"):
        await worker._claim_requested_for_provisioning(workspace_id)  # noqa: SLF001

    assert await status_of(session_factory, workspace_id) == WorkspaceStatus.requested.value
    assert await lease_rows(session_factory) == []


async def test_race_refused_claim_lease_rolls_the_claim_back(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await create_requested(session_factory)
    worker = make_worker(session_factory, executor=SlotAwareExecutor())

    async def _refused(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(worker, "_requested_claim_work_slot_refused", _refused)

    assert not await worker._claim_requested_for_provisioning(workspace_id)  # noqa: SLF001
    assert await status_of(session_factory, workspace_id) == WorkspaceStatus.requested.value


async def test_capacity_claims_take_one_lease_per_claimed_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await create_slot_workspace(session_factory)
    second = await create_slot_workspace(session_factory)
    worker = make_worker(
        session_factory,
        executor=SlotAwareExecutor(),
        max_concurrent_executions=2,
        max_concurrent_provisions=2,
        local_capacity_cpu_cores=100.0,
    )

    claimed = await worker._claim_requested_ids(limit=2)  # noqa: SLF001

    assert sorted(claimed) == sorted([first, second])
    assert sorted(lease.workspace_id for lease in await held_leases(session_factory)) == sorted(
        [first, second]
    )


async def test_capacity_claim_rolls_back_a_race_refused_lease(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await create_slot_workspace(session_factory)
    worker = make_worker(
        session_factory,
        executor=SlotAwareExecutor(),
        local_capacity_cpu_cores=100.0,
    )

    async def _refused(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(worker, "_requested_claim_work_slot_refused", _refused)

    assert await worker._claim_requested_ids(limit=1) == []  # noqa: SLF001
    assert await status_of(session_factory, workspace_id) == WorkspaceStatus.requested.value
    assert await lease_rows(session_factory) == []


async def test_seven_idle_monitors_no_longer_block_provisioning(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    monitor_ids = [
        await create_in_status(session_factory, WorkspaceStatus.monitoring_pr, f"idle-{index}")
        for index in range(7)
    ]
    first = await create_requested(session_factory, "first")
    executor = SlotAwareExecutor()
    provisioner = RecordingProvisioner(session_factory)
    worker = make_worker(
        session_factory,
        executor=executor,
        provisioner=provisioner,
        max_concurrent_executions=3,
    )

    try:
        await asyncio.wait_for(worker.run_once(), timeout=_TIMEOUT)
        await wait_until(lambda: len(executor.monitor_calls) == 7)
        # Seven monitors are tracked and waiting (WaitForCI) — none holds a slot
        # once the first workspace's (short) execution finished.
        assert sorted(executor.monitor_calls) == sorted(monitor_ids)
        await wait_until(lambda: _no_leases(session_factory))
        second = await create_requested(session_factory, "second")

        await asyncio.wait_for(worker.run_once(), timeout=_TIMEOUT)

        assert provisioner.calls == [first, second]
        for workspace_id in (first, second):
            claim_leases = provisioner.leases_seen[workspace_id]
            assert [lease.phase for lease in claim_leases] == [WorkSlotLeasePhase.held.value]
    finally:
        executor.monitor_release.set()
        await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=_TIMEOUT)


async def test_ready_dispatch_never_exceeds_free_work_slots(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    for index in range(5):
        await create_in_status(session_factory, WorkspaceStatus.ready, f"ready-{index}")
    executor = GatedSlotExecutor()
    worker = make_worker(session_factory, executor=executor, max_concurrent_executions=2)

    try:
        assert await asyncio.wait_for(worker.run_once(), timeout=_TIMEOUT) == 2
        await wait_until(lambda: len(executor.hold_order) == 2)
        assert len(await held_leases(session_factory)) == 2
        # Both slots busy: nothing more is dispatched, however many rows wait.
        assert await asyncio.wait_for(worker.run_once(), timeout=_TIMEOUT) == 0
        assert len(executor.execute_calls) == 2
    finally:
        for gate_id in list(executor.execute_calls):
            executor.gate(gate_id).set()
        await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=_TIMEOUT)
    assert await held_leases(session_factory) == []


async def test_parked_claim_lease_and_one_free_slot_both_dispatch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    executor = GatedSlotExecutor()
    worker = make_worker(session_factory, executor=executor, max_concurrent_executions=2)
    parked = await create_in_status(session_factory, WorkspaceStatus.ready, "parked")
    parked_invocation = await add_lease(
        session_factory,
        parked,
        holder=worker._worker_id,  # noqa: SLF001
    )
    await create_in_status(session_factory, WorkspaceStatus.ready, "free-a")
    await create_in_status(session_factory, WorkspaceStatus.ready, "free-b")

    try:
        assert await asyncio.wait_for(worker.run_once(), timeout=_TIMEOUT) == 2
        await wait_until(lambda: len(executor.hold_order) == 2)
        assert parked in executor.execute_calls
        continued = executor.leases_at_start[parked]
        assert continued.invocation_id == parked_invocation
        assert continued.holder_epoch == 2
        assert len(await held_leases(session_factory)) == 2
    finally:
        for gate_id in list(executor.execute_calls):
            executor.gate(gate_id).set()
        await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=_TIMEOUT)


async def test_provisioning_parks_its_claim_lease_and_ready_dispatch_continues_it(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await create_requested(session_factory)
    provisioner = RecordingProvisioner(session_factory, to_ready=True)
    executor = SlotAwareExecutor()
    worker = make_worker(
        session_factory,
        executor=executor,
        provisioner=provisioner,
        max_concurrent_executions=1,
    )

    assert await asyncio.wait_for(worker.run_once(), timeout=_TIMEOUT) == 1
    await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=_TIMEOUT)

    (claim_lease,) = provisioner.leases_seen[workspace_id]
    continued = executor.leases_at_start[workspace_id]
    assert executor.execute_calls == [workspace_id]
    assert continued.invocation_id == claim_lease.invocation_id
    assert continued.holder_epoch == claim_lease.holder_epoch + 1
    assert await held_leases(session_factory) == []


async def test_provisioning_that_does_not_end_ready_releases_its_claim_lease(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await create_requested(session_factory)
    provisioner = RecordingProvisioner(session_factory, to_ready=False)
    worker = make_worker(session_factory, executor=SlotAwareExecutor(), provisioner=provisioner)

    await asyncio.wait_for(worker.run_once(), timeout=_TIMEOUT)

    assert [lease.workspace_id for lease in provisioner.leases_seen[workspace_id]] == [workspace_id]
    assert await held_leases(session_factory) == []


async def test_provision_status_read_failure_releases_the_claim_lease(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await create_requested(session_factory)
    worker = make_worker(
        session_factory,
        executor=SlotAwareExecutor(),
        provisioner=RecordingProvisioner(session_factory, to_ready=False),
    )
    original = worker._load_workspace_statuses  # noqa: SLF001
    calls = 0

    async def _flaky_statuses(workspace_ids: list[str]) -> dict[str, str]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OperationalError("SELECT", {}, Exception("db down"))
        return await original(workspace_ids)

    monkeypatch.setattr(worker, "_load_workspace_statuses", _flaky_statuses)

    await asyncio.wait_for(worker.run_once(), timeout=_TIMEOUT)

    assert await held_leases(session_factory) == []


async def test_leaseless_ready_rows_are_served_before_new_requested_admission(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    ready_id = await create_in_status(session_factory, WorkspaceStatus.ready, "leaseless")
    requested_id = await create_requested(session_factory)
    executor = GatedSlotExecutor()
    worker = make_worker(session_factory, executor=executor, max_concurrent_executions=1)

    try:
        await asyncio.wait_for(worker.run_once(), timeout=_TIMEOUT)
        await wait_until(lambda: executor.hold_order == [ready_id])
        assert await status_of(session_factory, requested_id) == WorkspaceStatus.requested.value
    finally:
        executor.gate(ready_id).set()
        await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=_TIMEOUT)


async def test_reacquire_waiter_takes_the_freed_slot_before_new_work(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    busy = await create_in_status(session_factory, WorkspaceStatus.ready, "busy")
    executor = GatedSlotExecutor(session_factory=session_factory)
    worker = make_worker(session_factory, executor=executor, max_concurrent_executions=1)

    try:
        await asyncio.wait_for(worker.run_once(), timeout=_TIMEOUT)
        await wait_until(lambda: executor.hold_order == [busy])
        blocked = await create_in_status(session_factory, WorkspaceStatus.blocked, "resume")
        ready = await create_in_status(session_factory, WorkspaceStatus.ready, "fresh")
        requested = await create_requested(session_factory, "new")

        await asyncio.wait_for(worker.run_once(), timeout=_TIMEOUT)
        # The blocked resume is dispatched lease-less and queues at its hook.
        await wait_until(
            lambda: _has_phase(session_factory, blocked, WorkSlotLeasePhase.waiting.value)
        )
        assert ready not in executor.execute_calls

        # Freeing the busy slot hands it to the FIFO waiter, not to new work.
        executor.gate(busy).set()
        await wait_until(lambda: executor.hold_order == [busy, blocked])
        await asyncio.wait_for(worker.run_once(), timeout=_TIMEOUT)
        assert ready not in executor.execute_calls
        assert await status_of(session_factory, requested) == WorkspaceStatus.requested.value

        executor.gate(blocked).set()
        await wait_until(lambda: _no_held_lease(session_factory, blocked))
        await asyncio.wait_for(worker.run_once(), timeout=_TIMEOUT)
        await wait_until(lambda: executor.hold_order == [busy, blocked, ready])
    finally:
        for gate_id in [*executor.execute_calls, *executor.resume_calls]:
            executor.gate(gate_id).set()
        await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=_TIMEOUT)


async def _no_leases(session_factory: async_sessionmaker[AsyncSession]) -> bool:
    return await held_leases(session_factory) == []


async def _has_phase(
    session_factory: async_sessionmaker[AsyncSession], workspace_id: str, phase: str
) -> bool:
    return phase in {
        row.phase for row in await lease_rows(session_factory, workspace_id=workspace_id)
    }


async def _no_held_lease(
    session_factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> bool:
    return all(lease.workspace_id != workspace_id for lease in await held_leases(session_factory))


async def test_parked_monitor_resumes_and_acquires_at_its_hook(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A monitor parked waiting on a human holds no slot; when it resumes into
    # AddressComments it acquires at the agent hook and queues FIFO if full.
    monitor = await create_in_status(session_factory, WorkspaceStatus.monitoring_pr, "parked")
    busy = await create_in_status(session_factory, WorkspaceStatus.ready, "busy")
    executor = GatedSlotExecutor(monitor_agent=True)
    worker = make_worker(session_factory, executor=executor, max_concurrent_executions=1)

    try:
        await asyncio.wait_for(worker.run_once(), timeout=_TIMEOUT)
        await wait_until(lambda: executor.hold_order == [busy] and executor.monitor_calls)
        assert [lease.workspace_id for lease in await held_leases(session_factory)] == [busy]

        executor.monitor_release.set()
        await wait_until(
            lambda: _has_phase(session_factory, monitor, WorkSlotLeasePhase.waiting.value)
        )
        executor.gate(busy).set()
        await wait_until(lambda: executor.hold_order == [busy, monitor])
    finally:
        for gate_id in [busy, monitor]:
            executor.gate(gate_id).set()
        await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=_TIMEOUT)


async def test_split_saturation_logs_report_holders_and_waiters(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    busy = await create_in_status(session_factory, WorkspaceStatus.running, "busy")
    waiter = await create_in_status(session_factory, WorkspaceStatus.monitoring_pr, "waiter")
    await add_lease(session_factory, busy, holder=_OTHER_WORKER)
    await add_lease(session_factory, waiter, holder=_OTHER_WORKER, phase=WorkSlotLeasePhase.waiting)
    worker = make_worker(session_factory, executor=SlotAwareExecutor(), max_concurrent_executions=1)

    with structlog.testing.capture_logs() as logs:
        for _ in range(10):
            await worker._update_work_slot_saturation(dispatched=0)  # noqa: SLF001

    saturated = [entry for entry in logs if entry["event"] == "worker.work_slots_saturated"]
    waiters = [entry for entry in logs if entry["event"] == "worker.work_slot_waiters"]
    assert len(saturated) == 1
    assert saturated[0]["holders"] == [
        {"workspace_id": busy, "phase": "held", "cleanup_unproven": False}
    ]
    assert saturated[0]["waiting"] == 1
    assert len(waiters) == 1
    assert waiters[0]["waiting_workspace_ids"] == [waiter]
    assert waiters[0]["reason_code"] == MONITOR_WAITING_FOR_WORK_SLOT_REASON_CODE
    assert not any(entry["event"] == "worker.execution_slots_saturated" for entry in logs)

    # A cycle that dispatched work is not saturated; waiting still streaks.
    await worker._update_work_slot_saturation(dispatched=1)  # noqa: SLF001
    assert worker._consecutive_saturated_cycles == 0  # noqa: SLF001
    assert worker._consecutive_work_slot_waiting_cycles == 11  # noqa: SLF001


async def test_saturation_streaks_reset_when_execution_is_disabled_or_slots_idle(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled = make_worker(
        session_factory, executor=SlotAwareExecutor(), max_concurrent_executions=0
    )
    disabled._consecutive_saturated_cycles = 4  # noqa: SLF001
    disabled._consecutive_work_slot_waiting_cycles = 4  # noqa: SLF001
    await disabled._update_work_slot_saturation(dispatched=0)  # noqa: SLF001
    assert disabled._consecutive_saturated_cycles == 0  # noqa: SLF001
    assert disabled._consecutive_work_slot_waiting_cycles == 0  # noqa: SLF001

    idle = make_worker(session_factory, executor=SlotAwareExecutor(), max_concurrent_executions=2)
    idle._consecutive_work_slot_waiting_cycles = 3  # noqa: SLF001
    await idle._update_work_slot_saturation(dispatched=0)  # noqa: SLF001
    assert idle._consecutive_work_slot_waiting_cycles == 0  # noqa: SLF001

    async def _snapshot_down() -> object:
        raise OperationalError("SELECT", {}, Exception("db down"))

    monkeypatch.setattr(idle, "_work_slot_snapshot", _snapshot_down)
    idle._consecutive_saturated_cycles = 2  # noqa: SLF001
    await idle._update_work_slot_saturation(dispatched=0)  # noqa: SLF001
    assert idle._consecutive_saturated_cycles == 2  # noqa: SLF001
