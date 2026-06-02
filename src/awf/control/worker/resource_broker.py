"""Resource broker models and capacity boundary checks."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from awf.control.worker.scheduling import _CapacityQueueDecisionContext
from awf.common.logging import get_logger
from awf.control.worker.config import WorkerConfig, effective_worker_config_node_id
from awf.db.models import QueueDecision, ResourceReservation, Workspace
from awf.db.repositories import (
    ALLOCATED_RESOURCE_RESERVATION_STATUSES,
    QueueDecisionRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
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
