"""Broker-owned work-slot leases (slot-release, plan §6.2 A).

``acquire_work``/``release_work``/``renew_work`` take or delete a
``work_slot_leases`` row AND flip the workspace reservation phase steady<->peak in
one transaction under the local-capacity advisory lock. These tests run against
real Postgres so the lock, the partial unique index and the fenced CAS writes are
the production statements.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.ids import new_work_slot_invocation_id
from awf.control.worker import WorkerConfig
from awf.control.worker.resource_broker import (
    WorkSlotAcquireOutcome,
    WorkSlotLeaseRef,
    acquire_work,
    adopt_work,
    new_work_capacity,
    release_work,
    renew_work,
    transfer_work,
    work_slot_snapshot,
)
from awf.db.enums import ResourceReservationPhase, WorkSlotLeasePhase, WorkspaceStatus
from awf.db.models import QueueDecision, ResourceReservation, WorkSlotLease
from awf.db.repositories import (
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkSlotLeaseRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine

pytestmark = pytest.mark.unit

_LEASE_SECONDS = 300.0
_WORKER = "control-worker-a"
_OTHER_WORKER = "control-worker-b"


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def _workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    status: WorkspaceStatus = WorkspaceStatus.requested,
    steady: float = 1.0,
    peak: float = 2.0,
    reservation_phase: str | None = ResourceReservationPhase.workspace_lifecycle.value,
) -> str:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@example.com:org/repo.git",
            branch_base="main",
            task_title="slot",
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        task = await TaskRepository(session).create_or_get(
            repo_url=workspace.repo_url,
            base_branch=workspace.branch_base,
            title=workspace.task_title,
            prompt=workspace.task_prompt,
            external_id=None,
            idempotency_key=None,
            task_class=workspace.task_class,
            owned_paths=list(workspace.owned_paths),
        )
        attempt = await TaskAttemptRepository(session).create_for_workspace(
            task=task, workspace=workspace
        )
        if reservation_phase is not None:
            await ResourceReservationRepository(session).create(
                workspace_id=workspace.id,
                attempt_id=attempt.id,
                node_id="local",
                steady_cpu=steady,
                steady_memory_gb=steady,
                peak_cpu=peak,
                peak_memory_gb=peak,
                disk_mb=None,
                dind_slots=0,
                phase=reservation_phase,
            )
        if status is not WorkspaceStatus.requested:
            workspace.status = status.value
        await session.commit()
        return workspace.id


async def _reservation_phase(factory: async_sessionmaker[AsyncSession], workspace_id: str) -> str:
    async with factory() as session:
        reservation = await ResourceReservationRepository(session).active_for_workspace(
            workspace_id
        )
        assert reservation is not None
        return reservation.phase


async def _lease_row(
    factory: async_sessionmaker[AsyncSession], invocation_id: str
) -> WorkSlotLease | None:
    async with factory() as session:
        return await WorkSlotLeaseRepository(session).get(invocation_id)


async def _acquire(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    config: WorkerConfig,
    admission: str = "reacquire",
    invocation_id: str | None = None,
    holder: str = _WORKER,
    now: datetime | None = None,
    allow_grant: bool = True,
) -> tuple[str, WorkSlotAcquireOutcome, WorkSlotLeaseRef | None]:
    invocation_id = invocation_id or new_work_slot_invocation_id()
    async with factory() as session:
        result = await acquire_work(
            session,
            workspace_id=workspace_id,
            invocation_id=invocation_id,
            holder_worker_id=holder,
            config=config,
            now=now or datetime.now(UTC),
            lease_seconds=_LEASE_SECONDS,
            admission=admission,  # type: ignore[arg-type]
            allow_grant=allow_grant,
        )
        await session.commit()
    return invocation_id, result.outcome, result.lease


async def _release(
    factory: async_sessionmaker[AsyncSession],
    lease: WorkSlotLeaseRef,
    *,
    cleanup_unproven: bool = False,
) -> bool:
    async with factory() as session:
        released = await release_work(
            session,
            lease=lease,
            now=datetime.now(UTC),
            cleanup_unproven=cleanup_unproven,
        )
        await session.commit()
    return released


async def test_new_grant_inserts_held_lease_and_flips_reservation_to_peak(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _workspace(session_factory)
    config = WorkerConfig(max_concurrent_executions=2)

    invocation_id, outcome, lease = await _acquire(
        session_factory, workspace_id, config=config, admission="new"
    )

    assert outcome is WorkSlotAcquireOutcome.granted
    assert lease is not None
    assert lease.holder_epoch == 1
    row = await _lease_row(session_factory, invocation_id)
    assert row is not None
    assert row.phase == WorkSlotLeasePhase.held.value
    assert row.acquired_at is not None
    assert row.ticket_at == row.acquired_at
    assert await _reservation_phase(session_factory, workspace_id) == "peak"

    # Re-acquiring the same invocation is an idempotent no-op grant.
    _, again, again_lease = await _acquire(
        session_factory,
        workspace_id,
        config=config,
        invocation_id=invocation_id,
    )
    assert again is WorkSlotAcquireOutcome.granted
    assert again_lease == lease


async def test_grant_rollback_leaves_neither_lease_nor_phase_flip(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _workspace(session_factory)
    invocation_id = new_work_slot_invocation_id()
    async with session_factory() as session:
        result = await acquire_work(
            session,
            workspace_id=workspace_id,
            invocation_id=invocation_id,
            holder_worker_id=_WORKER,
            config=WorkerConfig(max_concurrent_executions=1),
            now=datetime.now(UTC),
            lease_seconds=_LEASE_SECONDS,
            admission="new",
        )
        assert result.outcome is WorkSlotAcquireOutcome.granted
        await session.rollback()

    assert await _lease_row(session_factory, invocation_id) is None
    assert await _reservation_phase(session_factory, workspace_id) == "workspace_lifecycle"


async def test_release_deletes_lease_and_flips_reservation_to_steady(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _workspace(session_factory)
    config = WorkerConfig(max_concurrent_executions=1)
    invocation_id, _, lease = await _acquire(
        session_factory, workspace_id, config=config, admission="new"
    )
    assert lease is not None

    assert await _release(session_factory, lease) is True

    assert await _lease_row(session_factory, invocation_id) is None
    assert await _reservation_phase(session_factory, workspace_id) == "steady"
    # Releasing twice is a fenced no-op.
    assert await _release(session_factory, lease) is False


async def test_release_of_a_waiting_row_keeps_the_reservation_phase(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    config = WorkerConfig(max_concurrent_executions=1)
    holder_ws = await _workspace(session_factory)
    await _acquire(session_factory, holder_ws, config=config, admission="new")
    waiter_ws = await _workspace(session_factory)
    invocation_id, outcome, _ = await _acquire(session_factory, waiter_ws, config=config)
    assert outcome is WorkSlotAcquireOutcome.waiting
    waiting = WorkSlotLeaseRef(
        invocation_id=invocation_id,
        workspace_id=waiter_ws,
        holder_worker_id=_WORKER,
        holder_epoch=1,
    )

    assert await _release(session_factory, waiting) is True

    assert await _lease_row(session_factory, invocation_id) is None
    assert await _reservation_phase(session_factory, waiter_ws) == "workspace_lifecycle"


async def test_cleanup_unproven_release_keeps_lease_flagged(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _workspace(session_factory)
    config = WorkerConfig(max_concurrent_executions=1)
    invocation_id, _, lease = await _acquire(
        session_factory, workspace_id, config=config, admission="new"
    )
    assert lease is not None

    assert await _release(session_factory, lease, cleanup_unproven=True) is True

    row = await _lease_row(session_factory, invocation_id)
    assert row is not None
    assert row.cleanup_unproven is True
    assert row.phase == WorkSlotLeasePhase.held.value
    # The slot stays occupied until the process is proven gone.
    async with session_factory() as session:
        assert await WorkSlotLeaseRepository(session).count_in_use(node_id="local") == 1
    assert await _reservation_phase(session_factory, workspace_id) == "peak"


async def test_stale_epoch_cannot_renew_or_release_after_transfer_or_adopt(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _workspace(session_factory, status=WorkspaceStatus.ready)
    config = WorkerConfig(max_concurrent_executions=1)
    invocation_id, _, original = await _acquire(
        session_factory, workspace_id, config=config, admission="new"
    )
    assert original is not None
    now = datetime.now(UTC)

    async with session_factory() as session:
        transferred = await transfer_work(
            session,
            workspace_id=workspace_id,
            holder_worker_id=_OTHER_WORKER,
            now=now + timedelta(seconds=_LEASE_SECONDS + 1),
            lease_seconds=_LEASE_SECONDS,
            fresh_worker_ids=frozenset(),
        )
        await session.commit()
    assert transferred is not None
    assert transferred.holder_worker_id == _OTHER_WORKER
    assert transferred.holder_epoch == original.holder_epoch + 1
    assert transferred.invocation_id == invocation_id

    async with session_factory() as session:
        assert not await renew_work(session, lease=original, now=now, lease_seconds=_LEASE_SECONDS)
        assert not await release_work(session, lease=original, now=now)
        await session.commit()
    row = await _lease_row(session_factory, invocation_id)
    assert row is not None
    assert row.holder_worker_id == _OTHER_WORKER

    async with session_factory() as session:
        adopted = await adopt_work(
            session,
            lease=transferred,
            holder_worker_id=_WORKER,
            now=now,
            lease_seconds=_LEASE_SECONDS,
        )
        await session.commit()
    assert adopted is not None
    assert adopted.holder_epoch == transferred.holder_epoch + 1
    row = await _lease_row(session_factory, invocation_id)
    assert row is not None
    assert row.phase == WorkSlotLeasePhase.adopted.value

    async with session_factory() as session:
        assert not await renew_work(
            session, lease=transferred, now=now, lease_seconds=_LEASE_SECONDS
        )
        assert not await release_work(session, lease=transferred, now=now)
        # A stale adopter cannot adopt again either.
        assert (
            await adopt_work(
                session,
                lease=transferred,
                holder_worker_id=_WORKER,
                now=now,
                lease_seconds=_LEASE_SECONDS,
            )
            is None
        )
        assert await renew_work(session, lease=adopted, now=now, lease_seconds=_LEASE_SECONDS)
        await session.commit()


async def test_transfer_refuses_fresh_foreign_holder_and_cleanup_unproven_leases(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _workspace(session_factory, status=WorkspaceStatus.ready)
    other_id = await _workspace(session_factory, status=WorkspaceStatus.ready)
    config = WorkerConfig(max_concurrent_executions=1)
    _, _, lease = await _acquire(
        session_factory, workspace_id, config=config, admission="new", holder=_OTHER_WORKER
    )
    assert lease is not None
    now = datetime.now(UTC)
    async with session_factory() as session:
        assert (
            await transfer_work(
                session,
                workspace_id=workspace_id,
                holder_worker_id=_WORKER,
                now=now,
                lease_seconds=_LEASE_SECONDS,
                fresh_worker_ids=frozenset({_OTHER_WORKER}),
            )
            is None
        )
    assert await _release(session_factory, lease, cleanup_unproven=True)
    async with session_factory() as session:
        assert (
            await transfer_work(
                session,
                workspace_id=workspace_id,
                holder_worker_id=_WORKER,
                now=now,
                lease_seconds=_LEASE_SECONDS,
                fresh_worker_ids=frozenset(),
            )
            is None
        )
        # No lease at all → nothing to transfer.
        assert (
            await transfer_work(
                session,
                workspace_id=other_id,
                holder_worker_id=_WORKER,
                now=now,
                lease_seconds=_LEASE_SECONDS,
                fresh_worker_ids=frozenset(),
            )
            is None
        )


async def test_reacquire_waiters_are_granted_in_ticket_order(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    config = WorkerConfig(max_concurrent_executions=1)
    blocker_ws = await _workspace(session_factory)
    _, _, blocker = await _acquire(session_factory, blocker_ws, config=config, admission="new")
    assert blocker is not None
    base = datetime.now(UTC)
    waiters: list[tuple[str, str]] = []
    for index in range(3):
        workspace_id = await _workspace(session_factory)
        invocation_id, outcome, _ = await _acquire(
            session_factory,
            workspace_id,
            config=config,
            now=base + timedelta(seconds=index),
        )
        assert outcome is WorkSlotAcquireOutcome.waiting
        waiters.append((workspace_id, invocation_id))

    # Release the blocker, then poll the waiters youngest-first each round: only
    # the oldest live ticket may take the freed slot.
    assert await _release(session_factory, blocker)
    granted_order: list[str] = []
    for _round in range(3):
        for workspace_id, invocation_id in reversed(waiters):
            if invocation_id in granted_order:
                continue
            _, outcome, lease = await _acquire(
                session_factory,
                workspace_id,
                config=config,
                invocation_id=invocation_id,
                now=base + timedelta(seconds=10),
            )
            if outcome is WorkSlotAcquireOutcome.granted:
                assert lease is not None
                granted_order.append(invocation_id)
                assert await _release(session_factory, lease)
                break

    assert granted_order == [invocation_id for _, invocation_id in waiters]


async def test_reacquire_refreshes_waiting_row_without_moving_its_ticket(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    config = WorkerConfig(max_concurrent_executions=1)
    holder_ws = await _workspace(session_factory)
    await _acquire(session_factory, holder_ws, config=config, admission="new")
    waiter_ws = await _workspace(session_factory)
    first = datetime.now(UTC)
    invocation_id, _, _ = await _acquire(session_factory, waiter_ws, config=config, now=first)
    later = first + timedelta(seconds=30)
    await _acquire(
        session_factory, waiter_ws, config=config, invocation_id=invocation_id, now=later
    )

    row = await _lease_row(session_factory, invocation_id)
    assert row is not None
    assert row.ticket_at == first
    assert row.expires_at == later + timedelta(seconds=_LEASE_SECONDS)


async def test_expired_waiter_does_not_block_the_head(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    config = WorkerConfig(max_concurrent_executions=1)
    old = datetime.now(UTC) - timedelta(seconds=_LEASE_SECONDS * 2)
    abandoned_ws = await _workspace(session_factory)
    holder_ws = await _workspace(session_factory)
    _, _, holder = await _acquire(session_factory, holder_ws, config=config, admission="new")
    assert holder is not None
    # The abandoned invocation queued long ago and then stopped re-checking.
    await _acquire(session_factory, abandoned_ws, config=config, now=old)
    assert await _release(session_factory, holder)
    live_ws = await _workspace(session_factory)

    _, outcome, lease = await _acquire(session_factory, live_ws, config=config)

    assert outcome is WorkSlotAcquireOutcome.granted
    assert lease is not None


async def test_live_reacquire_waiter_blocks_new_admission_even_with_free_slot(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    config = WorkerConfig(max_concurrent_executions=2)
    holder_ws = await _workspace(session_factory)
    await _acquire(session_factory, holder_ws, config=config, admission="new")
    # A waiter that is not yet granted (fail-closed admission keeps it queued).
    waiter_ws = await _workspace(session_factory)
    waiter_invocation, outcome, _ = await _acquire(
        session_factory, waiter_ws, config=config, allow_grant=False
    )
    assert outcome is WorkSlotAcquireOutcome.waiting
    requested_ws = await _workspace(session_factory)

    _, new_outcome, _ = await _acquire(
        session_factory, requested_ws, config=config, admission="new"
    )

    assert new_outcome is WorkSlotAcquireOutcome.refused
    async with session_factory() as session:
        assert await new_work_capacity(session, config=config, now=datetime.now(UTC)) == 0
    # The monitor-class waiter takes the free slot ahead of the new request.
    _, waiter_outcome, _ = await _acquire(
        session_factory, waiter_ws, config=config, invocation_id=waiter_invocation
    )
    assert waiter_outcome is WorkSlotAcquireOutcome.granted


async def test_new_work_capacity_reserves_leaseless_ready_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    config = WorkerConfig(max_concurrent_executions=3)
    await _workspace(session_factory, status=WorkspaceStatus.ready)
    held_ready = await _workspace(session_factory, status=WorkspaceStatus.ready)
    await _acquire(session_factory, held_ready, config=config, admission="new")
    now = datetime.now(UTC)

    async with session_factory() as session:
        assert await new_work_capacity(session, config=config, now=now) == 2
        assert (
            await new_work_capacity(session, config=config, now=now, reserve_leaseless_ready=True)
            == 1
        )
        assert (
            await new_work_capacity(
                session, config=replace(config, max_concurrent_executions=0), now=now
            )
            == 0
        )


async def test_new_admission_refused_when_slots_full(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    config = WorkerConfig(max_concurrent_executions=1)
    first = await _workspace(session_factory)
    second = await _workspace(session_factory)
    _, granted, _ = await _acquire(session_factory, first, config=config, admission="new")
    assert granted is WorkSlotAcquireOutcome.granted

    invocation_id, refused, lease = await _acquire(
        session_factory, second, config=config, admission="new"
    )

    assert refused is WorkSlotAcquireOutcome.refused
    assert lease is None
    # A refused new admission writes no ticket row.
    assert await _lease_row(session_factory, invocation_id) is None


async def test_different_invocation_never_shares_a_held_lease(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    config = WorkerConfig(max_concurrent_executions=3)
    workspace_id = await _workspace(session_factory)
    first_id, _, first = await _acquire(
        session_factory, workspace_id, config=config, admission="new"
    )
    assert first is not None

    _, new_outcome, _ = await _acquire(
        session_factory, workspace_id, config=config, admission="new"
    )
    second_id, reacquire_outcome, second = await _acquire(
        session_factory, workspace_id, config=config
    )

    assert new_outcome is WorkSlotAcquireOutcome.refused
    assert reacquire_outcome is WorkSlotAcquireOutcome.waiting
    assert second is None
    # Once the first invocation lets go, the waiting invocation is granted its own.
    assert await _release(session_factory, first)
    _, outcome, second = await _acquire(
        session_factory, workspace_id, config=config, invocation_id=second_id
    )
    assert outcome is WorkSlotAcquireOutcome.granted
    assert second is not None
    assert second.invocation_id != first_id
    # The DB backstop rejects a raw second held row for the same workspace.
    async with session_factory() as session:
        now = datetime.now(UTC)
        with pytest.raises(IntegrityError):
            await WorkSlotLeaseRepository(session).add(
                WorkSlotLease(
                    invocation_id=new_work_slot_invocation_id(),
                    workspace_id=workspace_id,
                    holder_worker_id=_WORKER,
                    holder_epoch=1,
                    ticket_at=now,
                    acquired_at=now,
                    expires_at=now + timedelta(seconds=_LEASE_SECONDS),
                    phase=WorkSlotLeasePhase.held.value,
                    cleanup_unproven=False,
                )
            )


async def test_waiter_blocked_by_its_own_workspace_lease_does_not_stall_the_queue(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    config = WorkerConfig(max_concurrent_executions=3)
    base = datetime.now(UTC)
    leftover_ws = await _workspace(session_factory)
    _, _, leftover = await _acquire(
        session_factory, leftover_ws, config=config, admission="new", now=base
    )
    assert leftover is not None
    assert await _release(session_factory, leftover, cleanup_unproven=True)
    # The oldest live ticket belongs to the workspace whose leftover lease is held.
    blocked_id, blocked_outcome, _ = await _acquire(
        session_factory, leftover_ws, config=config, now=base + timedelta(seconds=1)
    )
    assert blocked_outcome is WorkSlotAcquireOutcome.waiting
    other_ws = await _workspace(session_factory)
    requested_ws = await _workspace(session_factory)
    now = base + timedelta(seconds=2)

    _, other_outcome, _ = await _acquire(session_factory, other_ws, config=config, now=now)
    _, new_outcome, _ = await _acquire(
        session_factory, requested_ws, config=config, admission="new", now=now
    )

    assert other_outcome is WorkSlotAcquireOutcome.granted
    assert new_outcome is WorkSlotAcquireOutcome.granted
    # Once the leftover lease is gone the blocked ticket is the head again and
    # keeps its priority over new work.
    async with session_factory() as session:
        assert await WorkSlotLeaseRepository(session).delete_fenced(
            leftover.invocation_id,
            holder_worker_id=leftover.holder_worker_id,
            holder_epoch=leftover.holder_epoch,
        )
        await session.commit()
    async with session_factory() as session:
        assert await new_work_capacity(session, config=config, now=now) == 0
    _, outcome, lease = await _acquire(
        session_factory, leftover_ws, config=config, invocation_id=blocked_id, now=now
    )
    assert outcome is WorkSlotAcquireOutcome.granted
    assert lease is not None


async def test_foreign_holder_of_the_invocation_is_refused(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    config = WorkerConfig(max_concurrent_executions=2)
    workspace_id = await _workspace(session_factory)
    invocation_id, _, _ = await _acquire(
        session_factory, workspace_id, config=config, admission="new", holder=_OTHER_WORKER
    )

    _, outcome, lease = await _acquire(
        session_factory, workspace_id, config=config, invocation_id=invocation_id
    )

    assert outcome is WorkSlotAcquireOutcome.refused
    assert lease is None


async def test_acquire_ignores_reservations_when_local_capacity_unset(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # R6 regression: with ``local_capacity_*`` unset the phase logic gates nothing,
    # however large the reservations are; the work-slot count is the only bound.
    config = WorkerConfig(max_concurrent_executions=2)
    first = await _workspace(session_factory, steady=500.0, peak=4000.0)
    second = await _workspace(
        session_factory,
        steady=500.0,
        peak=4000.0,
        reservation_phase=ResourceReservationPhase.steady.value,
    )

    _, new_outcome, _ = await _acquire(session_factory, first, config=config, admission="new")
    _, reacquire_outcome, _ = await _acquire(session_factory, second, config=config)

    assert new_outcome is WorkSlotAcquireOutcome.granted
    assert reacquire_outcome is WorkSlotAcquireOutcome.granted
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(QueueDecision)) == 0


async def test_reacquire_peak_flip_gates_only_when_limits_configured(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    configured = WorkerConfig(max_concurrent_executions=5, local_capacity_memory_gb=6.0)
    # A held peak workspace already occupies 4 GB of the 6 GB peak budget.
    busy = await _workspace(session_factory, status=WorkspaceStatus.running, steady=1.0, peak=4.0)
    await _acquire(session_factory, busy, config=configured, admission="new")
    # A steady (released) monitor needs +3 GB to flip back to peak: deferred.
    idle = await _workspace(
        session_factory,
        status=WorkspaceStatus.monitoring_pr,
        steady=1.0,
        peak=4.0,
        reservation_phase=ResourceReservationPhase.steady.value,
    )
    invocation_id, deferred, _ = await _acquire(session_factory, idle, config=configured)
    assert deferred is WorkSlotAcquireOutcome.waiting
    row = await _lease_row(session_factory, invocation_id)
    assert row is not None and row.phase == WorkSlotLeasePhase.waiting.value

    # Without limits the same reacquire is granted.
    _, unset_outcome, _ = await _acquire(
        session_factory,
        idle,
        config=WorkerConfig(max_concurrent_executions=5),
        invocation_id=invocation_id,
    )
    assert unset_outcome is WorkSlotAcquireOutcome.granted
    assert await _reservation_phase(session_factory, idle) == "peak"


async def test_new_admission_of_steady_reservation_checks_peak_capacity(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Regression (PR #1194 review): a lease-less READY workspace whose parked lease
    # was released sits at steady; its fresh ``new`` admission must not flip it to
    # peak past the configured limit (4 GB + 4 GB peak under a 6 GB budget).
    configured = WorkerConfig(max_concurrent_executions=5, local_capacity_memory_gb=6.0)
    busy = await _workspace(session_factory, status=WorkspaceStatus.running, steady=1.0, peak=4.0)
    await _acquire(session_factory, busy, config=configured, admission="new")
    ready = await _workspace(
        session_factory,
        status=WorkspaceStatus.ready,
        steady=1.0,
        peak=4.0,
        reservation_phase=ResourceReservationPhase.steady.value,
    )

    invocation_id, refused, lease = await _acquire(
        session_factory, ready, config=configured, admission="new"
    )

    assert refused is WorkSlotAcquireOutcome.refused
    assert lease is None
    assert await _lease_row(session_factory, invocation_id) is None
    assert await _reservation_phase(session_factory, ready) == "steady"

    # Without limits the same admission is granted and flips to peak.
    _, unset_outcome, _ = await _acquire(
        session_factory,
        ready,
        config=WorkerConfig(max_concurrent_executions=5),
        admission="new",
    )
    assert unset_outcome is WorkSlotAcquireOutcome.granted
    assert await _reservation_phase(session_factory, ready) == "peak"


async def test_unsatisfiable_waiter_leaves_queue_but_stays_visible(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    configured = WorkerConfig(max_concurrent_executions=2, local_capacity_memory_gb=6.0)
    base = datetime.now(UTC)
    huge = await _workspace(
        session_factory,
        status=WorkspaceStatus.monitoring_pr,
        steady=1.0,
        peak=64.0,
        reservation_phase=ResourceReservationPhase.steady.value,
    )
    huge_invocation, huge_outcome, _ = await _acquire(
        session_factory, huge, config=configured, now=base
    )
    assert huge_outcome is WorkSlotAcquireOutcome.unsatisfiable
    row = await _lease_row(session_factory, huge_invocation)
    assert row is not None and row.phase == WorkSlotLeasePhase.unsatisfiable.value

    # A later ticket is not blocked by the unsatisfiable head.
    small = await _workspace(
        session_factory, status=WorkspaceStatus.monitoring_pr, steady=1.0, peak=2.0
    )
    _, small_outcome, _ = await _acquire(
        session_factory, small, config=configured, now=base + timedelta(seconds=1)
    )
    assert small_outcome is WorkSlotAcquireOutcome.granted

    async with session_factory() as session:
        snapshot = await work_slot_snapshot(session, config=configured, now=base)
    assert snapshot.in_use == 1
    assert snapshot.unsatisfiable == 1
    assert snapshot.waiting == 0
    assert snapshot.oldest_ticket_at == base
    assert {lease.workspace_id for lease in snapshot.leases} == {huge, small}

    # Re-evaluated on every re-check: once limits allow it, it returns to the
    # queue with its original ticket.
    _, outcome, _ = await _acquire(
        session_factory,
        huge,
        config=WorkerConfig(max_concurrent_executions=2),
        invocation_id=huge_invocation,
        now=base + timedelta(seconds=2),
    )
    assert outcome is WorkSlotAcquireOutcome.granted
    row = await _lease_row(session_factory, huge_invocation)
    assert row is not None and row.ticket_at == base


async def test_snapshot_counts_adopted_cleanup_unproven_and_waiters(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    config = WorkerConfig(max_concurrent_executions=2)
    now = datetime.now(UTC)
    first = await _workspace(session_factory)
    second = await _workspace(session_factory)
    _, _, first_lease = await _acquire(session_factory, first, config=config, admission="new")
    _, _, second_lease = await _acquire(session_factory, second, config=config, admission="new")
    assert first_lease is not None and second_lease is not None
    async with session_factory() as session:
        assert await adopt_work(
            session,
            lease=first_lease,
            holder_worker_id=_OTHER_WORKER,
            now=now,
            lease_seconds=_LEASE_SECONDS,
        )
        await session.commit()
    assert await _release(session_factory, second_lease, cleanup_unproven=True)
    waiter = await _workspace(session_factory)
    await _acquire(session_factory, waiter, config=config, now=now)

    async with session_factory() as session:
        snapshot = await work_slot_snapshot(session, config=config, now=now)

    assert snapshot.limit == 2
    assert snapshot.in_use == 2
    assert snapshot.free == 0
    assert snapshot.adopted == 1
    assert snapshot.cleanup_unproven == 1
    assert snapshot.waiting == 1
    assert snapshot.oldest_ticket_at == now


async def test_phase_aware_totals_count_steady_reservations_at_steady(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _workspace(
        session_factory,
        status=WorkspaceStatus.monitoring_pr,
        steady=1.0,
        peak=4.0,
        reservation_phase=ResourceReservationPhase.steady.value,
    )
    await _workspace(
        session_factory,
        status=WorkspaceStatus.running,
        steady=1.0,
        peak=4.0,
        reservation_phase=ResourceReservationPhase.workspace_lifecycle.value,
    )
    await _workspace(
        session_factory,
        status=WorkspaceStatus.running,
        steady=2.0,
        peak=8.0,
        reservation_phase=ResourceReservationPhase.peak.value,
    )
    async with session_factory() as session:
        totals = await ResourceReservationRepository(session).active_latest_totals()
        reservations = list((await session.execute(select(ResourceReservation))).scalars())

    assert len(reservations) == 3
    # Steady dimensions are unchanged; peak dimensions use the steady footprint for
    # the released (steady) workspace and peak for legacy/peak rows.
    assert totals["steady_cpu"] == pytest.approx(4.0)
    assert totals["peak_cpu"] == pytest.approx(1.0 + 4.0 + 8.0)
    assert totals["peak_memory_gb"] == pytest.approx(1.0 + 4.0 + 8.0)
