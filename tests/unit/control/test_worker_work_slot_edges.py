"""Edge paths of the worker work-slot delegates (slot-release)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import structlog.testing
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.ids import new_work_slot_invocation_id
from awf.control.worker import WorkerConfig
from awf.control.worker import work_slots as worker_work_slots
from awf.control.worker.resource_broker import WorkSlotAcquireOutcome, acquire_work
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkerHeartbeatRepository, WorkspaceRepository
from awf.db.session import make_session_factory
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
    status_of,
)

pytestmark = pytest.mark.unit

_TIMEOUT = 30.0
_FRESH = "control-worker-busy-elsewhere"


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def test_provision_only_worker_runs_tasks_without_a_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = make_worker(session_factory, executor=None)

    async with worker._work_slot_task_scope("ws_any") as scope:  # noqa: SLF001
        assert scope is None


async def test_ready_dispatch_starts_on_its_transferred_lease_without_rereading_it(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = SlotAwareExecutor()
    worker = make_worker(session_factory, executor=executor)
    workspace_id = await create_in_status(session_factory, WorkspaceStatus.ready, "parked")
    await add_lease(session_factory, workspace_id, holder=worker._worker_id)  # noqa: SLF001
    leases = await worker._claim_ready_work_slots([workspace_id])  # noqa: SLF001

    async def _db_down(*_args: object, **_kwargs: object) -> object:
        raise OperationalError("SELECT", {}, Exception("db down"))

    with monkeypatch.context() as patch:
        # A work-slot DB failure after the pre-step cannot strand the transferred
        # lease (held, unrenewed, the task's hook reacquire queued behind it): the
        # task is handed the lease directly instead of looking it up again.
        patch.setattr(worker_work_slots, "_work_slot_db", _db_down)
        dispatched = worker._dispatch_ready_executions(  # noqa: SLF001
            [workspace_id], limit=1, leases=leases
        )
        assert dispatched == {workspace_id}
        await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=_TIMEOUT)

    assert executor.leases_at_start[workspace_id] == leases[workspace_id]
    assert await held_leases(session_factory) == []


async def test_provisioning_aborts_when_its_claim_lease_cannot_be_read(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await create_requested(session_factory)
    provisioner = RecordingProvisioner(session_factory)
    worker = make_worker(session_factory, executor=SlotAwareExecutor(), provisioner=provisioner)
    assert await worker._claim_requested_for_provisioning(workspace_id)  # noqa: SLF001

    async def _db_down(*_args: object, **_kwargs: object) -> object:
        raise OperationalError("SELECT", {}, Exception("db down"))

    with monkeypatch.context() as patch:
        patch.setattr(worker_work_slots, "_work_slot_db", _db_down)
        with structlog.testing.capture_logs() as logs:
            await worker._safely_provision_claimed(workspace_id)  # noqa: SLF001

    # Provisioning has no hook to reacquire at, so it never runs on a claim lease
    # nobody renews (the reconciler would free the slot mid-provision).
    assert provisioner.calls == []
    assert "worker.provision_claim_setup_failed" in {entry["event"] for entry in logs}
    # The unrenewed claim lease is left for the reconciler, the row for stale recovery.
    assert [lease.workspace_id for lease in await held_leases(session_factory)] == [workspace_id]
    assert await status_of(session_factory, workspace_id) == WorkspaceStatus.provisioning.value


class _BlockingProvisioner(RecordingProvisioner):
    """Provisioning that runs until cancelled (worker shutdown)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory, to_ready=False)
        self.entered = asyncio.Event()

    async def provision_claimed(
        self, workspace_id: str, execution_claim_epoch: int | None = None
    ) -> None:
        del workspace_id, execution_claim_epoch
        self.entered.set()
        await asyncio.Event().wait()


async def test_second_cancel_on_the_provision_status_read_still_closes_and_releases(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The work-slot cleanup runs in the provisioning task's finally while a
    # shutdown cancel propagates. A second cancel landing on its status read must
    # not skip scope.close() (the lease would stay held with a leaked renewal) nor
    # the shielded execution-claim release that follows it.
    workspace_id = await create_requested(session_factory)
    provisioner = _BlockingProvisioner(session_factory)
    worker = make_worker(session_factory, executor=SlotAwareExecutor(), provisioner=provisioner)
    assert await worker._claim_requested_for_provisioning(workspace_id)  # noqa: SLF001
    reading = asyncio.Event()
    read_gate = asyncio.Event()
    load_statuses = worker._load_workspace_statuses  # noqa: SLF001

    async def _slow_status_read(workspace_ids: list[str]) -> dict[str, str]:
        reading.set()
        await read_gate.wait()
        return await load_statuses(workspace_ids)

    monkeypatch.setattr(worker, "_load_workspace_statuses", _slow_status_read)
    task = asyncio.create_task(worker._safely_provision_claimed(workspace_id))  # noqa: SLF001
    await asyncio.wait_for(provisioner.entered.wait(), timeout=_TIMEOUT)
    scope = worker._work_slot_scopes[workspace_id]  # noqa: SLF001
    assert scope.lease is not None

    task.cancel()  # worker shutdown
    await asyncio.wait_for(reading.wait(), timeout=_TIMEOUT)
    task.cancel()  # a second cancellation lands on the status read
    read_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=_TIMEOUT)

    assert scope.lease is None
    assert scope._renewal is None  # noqa: SLF001
    assert workspace_id not in worker._work_slot_scopes  # noqa: SLF001
    assert await held_leases(session_factory) == []
    async with session_factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
    assert ws is not None and ws.execution_claimed_by is None
    assert workspace_id not in worker._execution_claim_epochs  # noqa: SLF001


async def test_ready_row_whose_lease_a_live_worker_holds_is_not_dispatched(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    busy = await create_in_status(session_factory, WorkspaceStatus.ready, "busy-elsewhere")
    await add_lease(session_factory, busy, holder=_FRESH)
    now = datetime.now(UTC)
    async with session_factory() as session:
        await WorkerHeartbeatRepository(session).record_heartbeat(
            worker_id=_FRESH,
            node_id="local",
            started_at=now,
            last_heartbeat_at=now,
            poll_interval_seconds=60.0,
        )
        await session.commit()
    free = await create_in_status(session_factory, WorkspaceStatus.ready, "free")
    executor = GatedSlotExecutor(session_factory=session_factory)
    worker = make_worker(session_factory, executor=executor, max_concurrent_executions=3)

    try:
        await asyncio.wait_for(worker.run_once(), timeout=_TIMEOUT)
        assert executor.execute_calls == [free]
    finally:
        for gate_id in list(executor.execute_calls):
            executor.gate(gate_id).set()
        await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=_TIMEOUT)


async def test_new_admission_for_an_invocation_already_queued_is_refused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await create_slot_workspace(session_factory)
    invocation_id = new_work_slot_invocation_id()
    config = WorkerConfig(max_concurrent_executions=2)
    async with session_factory() as session:
        queued = await acquire_work(
            session,
            workspace_id=workspace_id,
            invocation_id=invocation_id,
            holder_worker_id="control-worker-a",
            config=config,
            now=datetime.now(UTC),
            lease_seconds=300.0,
            admission="reacquire",
            allow_grant=False,
        )
        await session.commit()
    assert queued.outcome is WorkSlotAcquireOutcome.waiting

    async with session_factory() as session:
        new = await acquire_work(
            session,
            workspace_id=workspace_id,
            invocation_id=invocation_id,
            holder_worker_id="control-worker-a",
            config=config,
            now=datetime.now(UTC),
            lease_seconds=300.0,
            admission="new",
        )

    assert new.outcome is WorkSlotAcquireOutcome.refused
    assert await held_leases(session_factory) == []
