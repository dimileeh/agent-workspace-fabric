"""Resource broker models and capacity boundary checks."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from awf.control.worker.scheduling import _CapacityQueueDecisionContext
from awf.common.logging import get_logger
from awf.control.worker.config import (
    DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID,
    WorkerConfig,
    effective_worker_config_node_id,
)
from awf.db.enums import ResourceReservationPhase, WorkSlotLeasePhase
from awf.db.models import QueueDecision, ResourceReservation, WorkSlotLease, Workspace
from awf.db.repositories import (
    ALLOCATED_RESOURCE_RESERVATION_STATUSES,
    IN_USE_WORK_SLOT_LEASE_PHASES,
    QueueDecisionRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
    WorkSlotLeaseRepository,
    WorkSlotSnapshot,
    summarize_work_slot_leases,
)
from awf.service.resource_capacity import (
    LOCAL_CAPACITY_CONSTRAINTS,
    LocalCapacityBlocker,
    default_dind_slots_from_profile,
    local_capacity_blocker,
    local_capacity_limit,
)
from awf.service.scheduler import (
    scheduler_score_from_workspace,
    score_summary_with_suppression,
)

_log = get_logger(__name__)

QUEUE_DECISION_DEFERRED = "deferred"
LOCAL_CAPACITY_RESERVATION_DEFAULTED_REASON = "LOCAL_CAPACITY_RESERVATION_DEFAULTED"
_CAPACITY_BLOCKER_SIGNATURE_FIELDS: tuple[str, ...] = (
    "dimension",
    "reason_code",
    "limit",
    "requested",
    "unsatisfiable",
)


@dataclass(frozen=True)
class _ReservationDemand:
    workspace_id: str
    steady_cpu: float
    steady_memory_gb: float
    peak_cpu: float
    peak_memory_gb: float
    disk_mb: int
    dind_slots: int
    defaulted: bool = False


@dataclass
class _AllocatedReservationTotals:
    workspace_count: int = 0
    steady_cpu: float = 0.0
    steady_memory_gb: float = 0.0
    peak_cpu: float = 0.0
    peak_memory_gb: float = 0.0
    disk_mb: int = 0
    dind_slots: int = 0

    def add(self: Any, demand: _ReservationDemand) -> None:
        self.workspace_count += 1
        self.steady_cpu += demand.steady_cpu
        self.steady_memory_gb += demand.steady_memory_gb
        self.peak_cpu += demand.peak_cpu
        self.peak_memory_gb += demand.peak_memory_gb
        self.disk_mb += demand.disk_mb
        self.dind_slots += demand.dind_slots


def _reservation_demand_for_workspace(
    workspace: Workspace,
    *,
    reservation: ResourceReservation | None,
    config: WorkerConfig,
) -> _ReservationDemand:
    if reservation is not None:
        return _reservation_demand_from_reservation(reservation)
    return _default_reservation_demand_for_workspace(
        workspace.id,
        resolved_profile=workspace.resolved_profile,
        config=config,
    )


def _reservation_demand_from_reservation(
    reservation: ResourceReservation,
) -> _ReservationDemand:
    return _ReservationDemand(
        workspace_id=reservation.workspace_id,
        steady_cpu=reservation.steady_cpu,
        steady_memory_gb=reservation.steady_memory_gb,
        peak_cpu=reservation.peak_cpu,
        peak_memory_gb=reservation.peak_memory_gb,
        disk_mb=int(reservation.disk_mb or 0),
        dind_slots=int(reservation.dind_slots or 0),
    )


def _default_reservation_demand_for_workspace(
    workspace_id: str,
    *,
    resolved_profile: object,
    config: WorkerConfig,
) -> _ReservationDemand:
    return _ReservationDemand(
        workspace_id=workspace_id,
        steady_cpu=config.workspace_steady_cpu,
        steady_memory_gb=config.workspace_steady_memory_gb,
        peak_cpu=config.workspace_peak_cpu,
        peak_memory_gb=config.workspace_peak_memory_gb,
        disk_mb=0,
        dind_slots=default_dind_slots_from_profile(resolved_profile),
        defaulted=True,
    )


def _local_capacity_blockers(
    *,
    allocated: _AllocatedReservationTotals,
    demand: _ReservationDemand,
    config: WorkerConfig,
) -> list[LocalCapacityBlocker]:
    blockers: list[LocalCapacityBlocker] = []
    for constraint in LOCAL_CAPACITY_CONSTRAINTS:
        blocker = local_capacity_blocker(
            constraint=constraint,
            limit=local_capacity_limit(
                constraint,
                cpu_limit=config.local_capacity_cpu_cores,
                memory_limit=config.local_capacity_memory_gb,
                dind_slots=config.local_capacity_dind_slots,
            ),
            allocated=getattr(allocated, constraint.dimension),
            requested=getattr(demand, constraint.dimension),
        )
        if blocker is not None:
            blockers.append(blocker)
    return blockers


def _local_capacity_configured(config: WorkerConfig) -> bool:
    return (
        config.local_capacity_cpu_cores is not None
        or config.local_capacity_memory_gb is not None
        or config.local_capacity_dind_slots is not None
    )


def _capacity_resource_summary(
    *,
    allocated: _AllocatedReservationTotals,
    demand: _ReservationDemand,
    blockers: list[LocalCapacityBlocker],
) -> dict[str, Any]:
    return {
        "allocated": {
            "workspace_count": allocated.workspace_count,
            "steady_cpu": allocated.steady_cpu,
            "steady_memory_gb": allocated.steady_memory_gb,
            "peak_cpu": allocated.peak_cpu,
            "peak_memory_gb": allocated.peak_memory_gb,
            "disk_mb": allocated.disk_mb,
            "dind_slots": allocated.dind_slots,
        },
        "requested": {
            "workspace_id": demand.workspace_id,
            "steady_cpu": demand.steady_cpu,
            "steady_memory_gb": demand.steady_memory_gb,
            "peak_cpu": demand.peak_cpu,
            "peak_memory_gb": demand.peak_memory_gb,
            "disk_mb": demand.disk_mb,
            "dind_slots": demand.dind_slots,
            "defaulted": demand.defaulted,
        },
        "blockers": [_capacity_blocker_payload(blocker) for blocker in blockers],
    }


def _capacity_blocker_payload(blocker: LocalCapacityBlocker) -> dict[str, Any]:
    return {
        "dimension": blocker.dimension,
        "reason_code": blocker.reason_code,
        "limit": blocker.limit,
        "allocated": blocker.allocated,
        "requested": blocker.requested,
        "after": blocker.after,
        "unsatisfiable": blocker.unsatisfiable,
    }


def _capacity_blocker_payload_signature(
    payload: Mapping[str, Any],
) -> tuple[Any, ...]:
    return tuple(payload.get(field) for field in _CAPACITY_BLOCKER_SIGNATURE_FIELDS)


def _capacity_blocker_signatures(
    blockers: list[LocalCapacityBlocker],
) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        _capacity_blocker_payload_signature(_capacity_blocker_payload(blocker))
        for blocker in blockers
    )


def _capacity_blocker_signatures_from_summary(
    resource_summary: Mapping[str, Any],
) -> tuple[tuple[Any, ...], ...] | None:
    blockers = resource_summary.get("blockers")
    if not isinstance(blockers, list):
        return None
    signatures: list[tuple[Any, ...]] = []
    for blocker in blockers:
        if not isinstance(blocker, Mapping):
            return None
        signatures.append(_capacity_blocker_payload_signature(blocker))
    return tuple(signatures)


def _capacity_deferred_decision_matches(
    decision: QueueDecision | None,
    *,
    attempt_id: str,
    reason_code: str,
    blockers: list[LocalCapacityBlocker],
) -> bool:
    if decision is None:
        return False
    if (
        decision.attempt_id != attempt_id
        or decision.decision != QUEUE_DECISION_DEFERRED
        or decision.reason_code != reason_code
    ):
        return False
    stored_signatures = _capacity_blocker_signatures_from_summary(decision.resource_summary)
    if stored_signatures is None:
        return False
    return stored_signatures == _capacity_blocker_signatures(blockers)


def _capacity_previous_resource_summary(
    resource_summary: Mapping[str, Any],
) -> dict[str, Any]:
    previous = dict(resource_summary)
    previous.pop("previous", None)
    return previous


async def _record_capacity_queue_decision(
    session: AsyncSession,
    workspace: Workspace,
    *,
    decision: str,
    reason_code: str,
    decided_at: Any,
    allocated: _AllocatedReservationTotals,
    demand: _ReservationDemand,
    blockers: list[LocalCapacityBlocker],
    context: _CapacityQueueDecisionContext | None = None,
) -> None:
    latest_decision: QueueDecision | None
    if context is None:
        attempt = await TaskAttemptRepository(session).get_by_workspace_id(workspace.id)
    else:
        attempt = context.attempt
    if attempt is None:
        _log.warning(
            "worker.capacity_queue_decision_missing_attempt",
            workspace_id=workspace.id,
            workspace_status=workspace.status,
            decision=decision,
            reason_code=reason_code,
        )
        return

    queue_repo = QueueDecisionRepository(session)
    if context is None:
        latest = await queue_repo.list_for_workspace(workspace.id, limit=1)
        latest_decision = latest[0] if latest else None
    else:
        latest_decision = context.latest_decision
    if decision == QUEUE_DECISION_DEFERRED and _capacity_deferred_decision_matches(
        latest_decision,
        attempt_id=attempt.id,
        reason_code=reason_code,
        blockers=blockers,
    ):
        return

    score = scheduler_score_from_workspace(workspace, now=decided_at)
    score_summary = (
        score_summary_with_suppression(
            score,
            reason_code=reason_code,
            detail={
                "blockers": [_capacity_blocker_payload(blocker) for blocker in blockers],
            },
        )
        if decision == QUEUE_DECISION_DEFERRED
        else score.score_summary
    )
    resource_summary = _capacity_resource_summary(
        allocated=allocated,
        demand=demand,
        blockers=blockers,
    )
    if latest_decision is not None:
        resource_summary["previous"] = _capacity_previous_resource_summary(
            latest_decision.resource_summary
        )
    await queue_repo.create(
        workspace_id=workspace.id,
        task_id=attempt.task_id,
        attempt_id=attempt.id,
        decision=decision,
        reason_code=reason_code,
        class_priority=score.class_priority,
        computed_priority=score.effective_score,
        age_boost=score.age_boost,
        retry_bonus=score.retry_bonus,
        resource_summary=resource_summary,
        overlap_risk_summary=(
            dict(latest_decision.overlap_risk_summary) if latest_decision is not None else {}
        ),
        score_summary=score_summary,
        decided_at=decided_at,
    )


async def _allocated_totals_for_capacity_gate(
    session: AsyncSession,
    *,
    reservation_repo: ResourceReservationRepository,
    config: WorkerConfig,
) -> _AllocatedReservationTotals:
    node_id = effective_worker_config_node_id(config)
    allocated = _allocated_totals_from_repository(
        await reservation_repo.active_latest_totals_for_scheduler_allocation_scope(
            statuses=ALLOCATED_RESOURCE_RESERVATION_STATUSES,
            node_id=node_id,
        )
    )
    await _add_unreserved_active_workspace_defaults(
        session,
        allocated=allocated,
        config=config,
        node_id=node_id,
    )
    return allocated


def _active_reservation_workspace_ids_subquery() -> Any:
    return (
        select(ResourceReservation.workspace_id.label("workspace_id"))
        .where(ResourceReservation.released_at.is_(None))
        .distinct()
        .subquery()
    )


async def _add_unreserved_active_workspace_defaults(
    session: AsyncSession,
    *,
    allocated: _AllocatedReservationTotals,
    config: WorkerConfig,
    node_id: str,
) -> None:
    active_reservation_workspace_ids = _active_reservation_workspace_ids_subquery()
    stmt = (
        select(Workspace.id, Workspace.resolved_profile)
        .outerjoin(
            active_reservation_workspace_ids,
            active_reservation_workspace_ids.c.workspace_id == Workspace.id,
        )
        .where(
            Workspace.status.in_(ALLOCATED_RESOURCE_RESERVATION_STATUSES),
            or_(Workspace.node_id == node_id, Workspace.node_id.is_(None)),
            active_reservation_workspace_ids.c.workspace_id.is_(None),
        )
    )
    for workspace_id, resolved_profile in await session.execute(stmt):
        allocated.add(
            _default_reservation_demand_for_workspace(
                workspace_id,
                resolved_profile=resolved_profile,
                config=config,
            )
        )


def _allocated_totals_from_repository(
    totals: Mapping[str, float | int],
) -> _AllocatedReservationTotals:
    return _AllocatedReservationTotals(
        workspace_count=int(totals.get("workspace_count", 0) or 0),
        steady_cpu=float(totals.get("steady_cpu", 0.0) or 0.0),
        steady_memory_gb=float(totals.get("steady_memory_gb", 0.0) or 0.0),
        peak_cpu=float(totals.get("peak_cpu", 0.0) or 0.0),
        peak_memory_gb=float(totals.get("peak_memory_gb", 0.0) or 0.0),
        disk_mb=int(totals.get("disk_mb", 0) or 0),
        dind_slots=int(totals.get("dind_slots", 0) or 0),
    )


_ALLOCATED_RESERVATION_SIGNATURE_SCALE = 1_000_000_000
type _AllocatedReservationSignature = tuple[int, int, int, int, int, int, int]


def _allocated_reservation_signature(
    allocated: _AllocatedReservationTotals,
) -> _AllocatedReservationSignature:
    return (
        allocated.workspace_count,
        _capacity_signature_units(allocated.steady_cpu),
        _capacity_signature_units(allocated.steady_memory_gb),
        _capacity_signature_units(allocated.peak_cpu),
        _capacity_signature_units(allocated.peak_memory_gb),
        allocated.disk_mb,
        allocated.dind_slots,
    )


def _capacity_signature_units(value: float) -> int:
    return round(value * _ALLOCATED_RESERVATION_SIGNATURE_SCALE)


async def _acquire_local_capacity_scheduler_lock(
    session: AsyncSession,
    *,
    node_id: str,
) -> None:
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    lock_key = _postgres_advisory_lock_key(f"awf:local-capacity:{node_id}")
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": lock_key},
    )


def _postgres_advisory_lock_key(value: str) -> int:
    raw = int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:8], "big")
    return raw if raw < 2**63 else raw - 2**64


# --- Work-slot leases (slot-release) ----------------------------------------
#
# A work slot means provisioning, an agent run or a validation run is in flight
# right now. The broker owns the ``work_slot_leases`` rows: every acquisition and
# release takes the local-capacity advisory lock and, in the same transaction,
# writes the lease row and flips the workspace reservation phase steady<->peak.
# ``pg_advisory_xact_lock`` is re-entrant inside one transaction, so the
# requested claim can call ``acquire_work`` after taking the lock itself.
#
# Lock order: requested-admission locks -> capacity lock. Nothing takes an
# admission lock while holding the capacity lock, and no lock is held across a
# wait (waiters re-check in a fresh short transaction).

WorkSlotAdmission = Literal["new", "reacquire"]
"""``new``: requested claims and lease-less READY dispatch, never ahead of a live
waiter. ``reacquire``: hook acquisitions (monitor actions, blocked/recovering/
paused resumes), served strictly FIFO by persisted ticket before any new work."""

_PEAK_TO_STEADY_DIMENSION: dict[str, str] = {
    "peak_cpu": "steady_cpu",
    "peak_memory_gb": "steady_memory_gb",
}


class WorkSlotAcquireOutcome(StrEnum):
    granted = "granted"
    waiting = "waiting"
    unsatisfiable = "unsatisfiable"
    refused = "refused"


@dataclass(frozen=True)
class WorkSlotLeaseRef:
    """Fencing identity of one invocation's lease (owner + epoch, like claims)."""

    invocation_id: str
    workspace_id: str
    holder_worker_id: str
    holder_epoch: int
    node_id: str = DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID

    @classmethod
    def from_row(cls, row: WorkSlotLease, *, node_id: str) -> WorkSlotLeaseRef:
        return cls(
            invocation_id=row.invocation_id,
            workspace_id=row.workspace_id,
            holder_worker_id=row.holder_worker_id,
            holder_epoch=row.holder_epoch,
            node_id=node_id,
        )


@dataclass(frozen=True)
class WorkSlotAcquireResult:
    outcome: WorkSlotAcquireOutcome
    lease: WorkSlotLeaseRef | None
    in_use: int
    limit: int
    ticket_at: datetime | None = None
    blockers: tuple[LocalCapacityBlocker, ...] = ()

    @property
    def granted(self) -> bool:
        return self.outcome is WorkSlotAcquireOutcome.granted


def work_slot_limit(config: WorkerConfig) -> int:
    """The work-slot count is ``max_concurrent_executions`` (R6: the only bound)."""
    return max(0, int(config.max_concurrent_executions))


async def new_work_capacity(
    session: AsyncSession,
    *,
    config: WorkerConfig,
    now: datetime,
    reserve_leaseless_ready: bool = False,
) -> int:
    """Free slots new work may take: zero while any live reacquire waiter queues.

    ``reserve_leaseless_ready`` (requested admission) further holds back one slot
    per lease-less ``ready`` row so already-provisioned work is not starved.
    """
    limit = work_slot_limit(config)
    if limit <= 0:
        return 0
    node_id = effective_worker_config_node_id(config)
    repo = WorkSlotLeaseRepository(session)
    if await repo.count_live_waiters(node_id=node_id, now=now) > 0:
        return 0
    free = limit - await repo.count_in_use(node_id=node_id)
    if reserve_leaseless_ready and free > 0:
        free -= await repo.count_leaseless_ready(node_id=node_id)
    return max(0, free)


async def acquire_work(
    session: AsyncSession,
    *,
    workspace_id: str,
    invocation_id: str,
    holder_worker_id: str,
    config: WorkerConfig,
    now: datetime,
    lease_seconds: float,
    admission: WorkSlotAdmission,
    allow_grant: bool = True,
    reserve_leaseless_ready: bool = False,
) -> WorkSlotAcquireResult:
    """Take a work slot for ``invocation_id`` or record why it must wait.

    Re-acquiring a lease this invocation already holds is an idempotent grant
    (nested acquisition). A lease of the same invocation held by another worker
    is refused: only the adopter owns it now. ``allow_grant=False`` (fail-closed
    admission) still registers or refreshes a reacquire ticket but never grants.
    """
    node_id = effective_worker_config_node_id(config)
    await _acquire_local_capacity_scheduler_lock(session, node_id=node_id)
    repo = WorkSlotLeaseRepository(session)
    limit = work_slot_limit(config)
    in_use = await repo.count_in_use(node_id=node_id)
    existing = await repo.get(invocation_id)
    if existing is not None and (
        existing.holder_worker_id != holder_worker_id or existing.workspace_id != workspace_id
    ):
        return WorkSlotAcquireResult(
            outcome=WorkSlotAcquireOutcome.refused, lease=None, in_use=in_use, limit=limit
        )
    if existing is not None and existing.phase in IN_USE_WORK_SLOT_LEASE_PHASES:
        return WorkSlotAcquireResult(
            outcome=WorkSlotAcquireOutcome.granted,
            lease=WorkSlotLeaseRef.from_row(existing, node_id=node_id),
            in_use=in_use,
            limit=limit,
            ticket_at=existing.ticket_at,
        )
    expires_at = now + timedelta(seconds=lease_seconds)
    if admission == "new":
        if (
            existing is not None
            or not allow_grant
            or await repo.held_for_workspace(workspace_id) is not None
            or await new_work_capacity(
                session,
                config=config,
                now=now,
                reserve_leaseless_ready=reserve_leaseless_ready,
            )
            <= 0
        ):
            return WorkSlotAcquireResult(
                outcome=WorkSlotAcquireOutcome.refused, lease=None, in_use=in_use, limit=limit
            )
        row = await repo.add(
            WorkSlotLease(
                invocation_id=invocation_id,
                workspace_id=workspace_id,
                holder_worker_id=holder_worker_id,
                holder_epoch=1,
                ticket_at=now,
                acquired_at=now,
                expires_at=expires_at,
                phase=WorkSlotLeasePhase.held.value,
                cleanup_unproven=False,
            )
        )
        await _flip_reservation_phase(session, workspace_id, ResourceReservationPhase.peak)
        return WorkSlotAcquireResult(
            outcome=WorkSlotAcquireOutcome.granted,
            lease=WorkSlotLeaseRef.from_row(row, node_id=node_id),
            in_use=in_use + 1,
            limit=limit,
            ticket_at=now,
        )

    if existing is None:
        row = await repo.add(
            WorkSlotLease(
                invocation_id=invocation_id,
                workspace_id=workspace_id,
                holder_worker_id=holder_worker_id,
                holder_epoch=1,
                ticket_at=now,
                acquired_at=None,
                expires_at=expires_at,
                phase=WorkSlotLeasePhase.waiting.value,
                cleanup_unproven=False,
            )
        )
    else:
        # Keep the original ticket; an ``unsatisfiable`` row is re-evaluated on
        # every re-check and returns to the queue if the limits now allow it.
        row = existing
        row.expires_at = expires_at
        row.phase = WorkSlotLeasePhase.waiting.value
        await session.flush()
    blockers = await _reacquire_peak_blockers(session, workspace_id=workspace_id, config=config)
    waiting = WorkSlotAcquireResult(
        outcome=WorkSlotAcquireOutcome.waiting,
        lease=None,
        in_use=in_use,
        limit=limit,
        ticket_at=row.ticket_at,
        blockers=tuple(blockers),
    )
    if any(blocker.unsatisfiable for blocker in blockers):
        row.phase = WorkSlotLeasePhase.unsatisfiable.value
        await session.flush()
        return WorkSlotAcquireResult(
            outcome=WorkSlotAcquireOutcome.unsatisfiable,
            lease=None,
            in_use=in_use,
            limit=limit,
            ticket_at=row.ticket_at,
            blockers=tuple(blockers),
        )
    if not allow_grant or in_use >= limit or blockers:
        return waiting
    if await repo.held_for_workspace(workspace_id) is not None:
        # A different invocation never shares another invocation's lease.
        return waiting
    head = await repo.fifo_head(node_id=node_id, now=now)
    if head is None or head.invocation_id != invocation_id:
        return waiting
    row.phase = WorkSlotLeasePhase.held.value
    row.acquired_at = now
    row.expires_at = expires_at
    await session.flush()
    await _flip_reservation_phase(session, workspace_id, ResourceReservationPhase.peak)
    return WorkSlotAcquireResult(
        outcome=WorkSlotAcquireOutcome.granted,
        lease=WorkSlotLeaseRef.from_row(row, node_id=node_id),
        in_use=in_use + 1,
        limit=limit,
        ticket_at=row.ticket_at,
    )


async def release_work(
    session: AsyncSession,
    *,
    lease: WorkSlotLeaseRef,
    now: datetime,
    cleanup_unproven: bool = False,
) -> bool:
    """Delete ``lease`` and flip its workspace reservation to steady (fenced).

    With ``cleanup_unproven`` the held row is only flagged and kept: the slot
    stays occupied until no ``awf-exec`` remains for the invocation. A waiting row
    is simply deleted. Returns ``False`` when a newer holder/epoch fenced us.
    """
    del now
    await _acquire_local_capacity_scheduler_lock(session, node_id=lease.node_id)
    repo = WorkSlotLeaseRepository(session)
    row = await repo.get(lease.invocation_id)
    if (
        row is None
        or row.holder_worker_id != lease.holder_worker_id
        or row.holder_epoch != lease.holder_epoch
    ):
        return False
    held = row.phase in IN_USE_WORK_SLOT_LEASE_PHASES
    if cleanup_unproven and held:
        row.cleanup_unproven = True
        await session.flush()
        return True
    deleted = await repo.delete_fenced(
        lease.invocation_id,
        holder_worker_id=lease.holder_worker_id,
        holder_epoch=lease.holder_epoch,
    )
    if deleted and held:
        await _flip_reservation_phase(session, lease.workspace_id, ResourceReservationPhase.steady)
    return deleted


async def renew_work(
    session: AsyncSession,
    *,
    lease: WorkSlotLeaseRef,
    now: datetime,
    lease_seconds: float,
) -> bool:
    """Extend ``lease``; ``False`` means a newer holder/epoch fenced us out."""
    return await WorkSlotLeaseRepository(session).update_fenced(
        lease.invocation_id,
        holder_worker_id=lease.holder_worker_id,
        holder_epoch=lease.holder_epoch,
        values={"expires_at": now + timedelta(seconds=lease_seconds)},
    )


async def transfer_work(
    session: AsyncSession,
    *,
    workspace_id: str,
    holder_worker_id: str,
    now: datetime,
    lease_seconds: float,
    fresh_worker_ids: frozenset[str] | set[str],
    node_id: str = DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID,
) -> WorkSlotLeaseRef | None:
    """Move a parked held lease to ``holder_worker_id`` with ``holder_epoch + 1``.

    READY dispatch continues the provisioning invocation on its claim lease.
    Allowed when the holder is this worker, is not a fresh worker, or let the
    lease expire; never for adopted or cleanup-unproven leases (those belong to
    the reconciler until their process is proven gone).
    """
    repo = WorkSlotLeaseRepository(session)
    row = await repo.held_for_workspace(workspace_id)
    if row is None or row.phase != WorkSlotLeasePhase.held.value or row.cleanup_unproven:
        return None
    if (
        row.holder_worker_id != holder_worker_id
        and row.holder_worker_id in fresh_worker_ids
        and row.expires_at > now
    ):
        return None
    current = WorkSlotLeaseRef.from_row(row, node_id=node_id)
    return await _take_over(
        repo,
        current=current,
        holder_worker_id=holder_worker_id,
        values={"expires_at": now + timedelta(seconds=lease_seconds)},
    )


async def adopt_work(
    session: AsyncSession,
    *,
    lease: WorkSlotLeaseRef,
    holder_worker_id: str,
    now: datetime,
    lease_seconds: float,
) -> WorkSlotLeaseRef | None:
    """Adopt a lease whose ``awf-exec`` is still in flight (new holder, epoch+1)."""
    return await _take_over(
        WorkSlotLeaseRepository(session),
        current=lease,
        holder_worker_id=holder_worker_id,
        values={
            "phase": WorkSlotLeasePhase.adopted.value,
            "expires_at": now + timedelta(seconds=lease_seconds),
        },
    )


async def work_slot_snapshot(
    session: AsyncSession,
    *,
    config: WorkerConfig,
    now: datetime,
) -> WorkSlotSnapshot:
    """Slots in use, waiters, unsatisfiable waiters and per-lease detail for the node."""
    leases = await WorkSlotLeaseRepository(session).list_for_node(
        node_id=effective_worker_config_node_id(config)
    )
    return summarize_work_slot_leases(leases, limit=work_slot_limit(config), now=now)


async def _take_over(
    repo: WorkSlotLeaseRepository,
    *,
    current: WorkSlotLeaseRef,
    holder_worker_id: str,
    values: Mapping[str, Any],
) -> WorkSlotLeaseRef | None:
    new_epoch = current.holder_epoch + 1
    taken = await repo.update_fenced(
        current.invocation_id,
        holder_worker_id=current.holder_worker_id,
        holder_epoch=current.holder_epoch,
        values={**values, "holder_worker_id": holder_worker_id, "holder_epoch": new_epoch},
    )
    if not taken:
        return None
    return WorkSlotLeaseRef(
        invocation_id=current.invocation_id,
        workspace_id=current.workspace_id,
        holder_worker_id=holder_worker_id,
        holder_epoch=new_epoch,
        node_id=current.node_id,
    )


async def _flip_reservation_phase(
    session: AsyncSession,
    workspace_id: str,
    phase: ResourceReservationPhase,
) -> None:
    await ResourceReservationRepository(session).set_active_phase_for_workspace(
        workspace_id, phase=phase.value
    )


async def _reacquire_peak_blockers(
    session: AsyncSession,
    *,
    workspace_id: str,
    config: WorkerConfig,
) -> list[LocalCapacityBlocker]:
    """Peak-dimension blockers for flipping a steady workspace back to peak.

    Gates nothing unless ``local_capacity_*`` limits are configured (R6), and only
    applies to a ``steady`` reservation: legacy/peak reservations already count at
    peak, and an unreserved workspace is counted at default peak conservatively.
    """
    if not _local_capacity_configured(config):
        return []
    reservation_repo = ResourceReservationRepository(session)
    reservation = await reservation_repo.active_for_workspace(workspace_id)
    if reservation is None or reservation.phase != ResourceReservationPhase.steady.value:
        return []
    allocated = await _allocated_totals_for_capacity_gate(
        session,
        reservation_repo=reservation_repo,
        config=config,
    )
    blockers: list[LocalCapacityBlocker] = []
    for constraint in LOCAL_CAPACITY_CONSTRAINTS:
        steady_dimension = _PEAK_TO_STEADY_DIMENSION.get(constraint.dimension)
        if steady_dimension is None:
            continue
        blocker = local_capacity_blocker(
            constraint=constraint,
            limit=local_capacity_limit(
                constraint,
                cpu_limit=config.local_capacity_cpu_cores,
                memory_limit=config.local_capacity_memory_gb,
                dind_slots=config.local_capacity_dind_slots,
            ),
            allocated=max(
                0.0,
                getattr(allocated, constraint.dimension) - getattr(reservation, steady_dimension),
            ),
            requested=getattr(reservation, constraint.dimension),
        )
        if blocker is not None:
            blockers.append(blocker)
    return blockers
