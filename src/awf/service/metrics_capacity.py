"""Read-only operational metrics for workspace reliability."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import aliased
from sqlalchemy.sql import expression

from awf.common.config import Settings
from awf.common.workspace_policy import agent_model_from_task_policy
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import ResourceReservation, Workspace
from awf.db.repositories import (
    ProviderModelCircuitBreakerRepository,
    resolve_session_dialect_name,
    scheduler_order_expressions,
)
from awf.service.disk import DiskCheck
from awf.service.metrics_resources import (
    _active_latest_totals_for_workspace_scope,
    _defaulted_dind_slots_for_session,
    _reserved_resources_from_totals,
    _scheduler_allocated_resources_for_session,
)
from awf.service.metrics_slo import _to_utc
from awf.service.metrics_types import (
    AdmissionSummary,
    CapacityQueueSummary,
    ConcurrencyLane,
    FailureAction,
    ResourceConcurrency,
    WorkerConcurrencySettings,
    _AllocatedResourceAuxiliaryCounts,
    _CapacityQueueAllocated,
    _CapacityQueueCandidate,
    _CapacityQueueWorkspace,
)
from awf.service.node_identity import effective_worker_node_id
from awf.service.provider_recovery import (
    provider_cooldown_not_before,
    provider_for_agent_model,
)
from awf.service.resource_capacity import (
    LOCAL_CAPACITY_CONSTRAINTS,
    LocalCapacityLimits,
    ReservedResources,
    WorkspaceResourceDefaults,
    default_dind_slots_from_profile,
    local_capacity_blocker,
    local_capacity_limit,
)
from awf.service.scheduler import scheduler_order_key, scheduler_score_from_workspace


class _IsoToTimestamp(expression.FunctionElement[Any]):  # noqa: N801
    """PostgreSQL ISO-8601 string to timestamp cast."""

    inherit_cache = True


_ISO8601_TS_PG = (
    r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])"
    r"T([01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    r"(\.\d+)?"
    r"([+-]([01]\d|2[0-3]):?[0-5]\d|Z)?$"
)


@compiles(_IsoToTimestamp, "postgresql")
def _pg_iso_to_timestamp(element: expression.FunctionElement[Any], compiler: Any, **kw: Any) -> str:
    arg = compiler.process(list(element.clauses)[0], **kw)
    return (
        f"CASE WHEN {arg} ~ '{_ISO8601_TS_PG}'"
        f" THEN CAST({arg} AS TIMESTAMP WITH TIME ZONE) ELSE NULL END"
    )


DEFAULT_SUMMARY_WINDOW_HOURS = 24
MIN_SUMMARY_WINDOW_HOURS = 1
MAX_SUMMARY_WINDOW_HOURS = 168
DEFAULT_FAILURE_EXAMPLE_LIMIT = 5
MIN_FAILURE_EXAMPLE_LIMIT = 1
MAX_FAILURE_EXAMPLE_LIMIT = 25
DEFAULT_ROOT_CAUSE_SAMPLE_LIMIT = 5
DEFAULT_CAPACITY_QUEUE_BLOCKER_SCAN_LIMIT = 500
DEFAULT_CAPACITY_QUEUE_BLOCKER_REFILL_PAGE_LIMIT = 3
UNKNOWN_FAILURE_REASON = "unknown"

TERMINAL_WORKSPACE_STATUSES = frozenset(
    {
        WorkspaceStatus.completed.value,
        WorkspaceStatus.failed.value,
        WorkspaceStatus.cancelled.value,
        WorkspaceStatus.destroyed.value,
    }
)

PROVISION_IN_USE_STATUSES = frozenset({WorkspaceStatus.provisioning.value})
PROVISION_QUEUE_STATUSES = frozenset({WorkspaceStatus.requested.value})
EXECUTION_IN_USE_STATUSES = frozenset(
    {
        WorkspaceStatus.running.value,
        WorkspaceStatus.validating.value,
        WorkspaceStatus.pushing.value,
        WorkspaceStatus.monitoring_pr.value,
        # A blocked workspace keeps its warm stack + execution claim while it
        # awaits an operator decision, so it still holds an execution slot.
        WorkspaceStatus.blocked.value,
        # A recovering workspace keeps its warm stack + execution claim during
        # the provider cooldown while it auto-retries in place, so it still holds
        # an execution slot (#612).
        WorkspaceStatus.recovering.value,
    }
)
EXECUTION_QUEUE_STATUSES = frozenset({WorkspaceStatus.ready.value})

ADMISSION_OK_REASON = "ADMISSION_OK"
WORKER_PROVISION_CONCURRENCY_SATURATED_REASON = "WORKER_PROVISION_CONCURRENCY_SATURATED"
WORKER_EXECUTION_CONCURRENCY_SATURATED_REASON = "WORKER_EXECUTION_CONCURRENCY_SATURATED"
WORKER_PROVISION_AND_EXECUTION_CONCURRENCY_SATURATED_REASON = (
    "WORKER_PROVISION_AND_EXECUTION_CONCURRENCY_SATURATED"
)


_UNKNOWN_FAILURE_ACTION = FailureAction(
    retryable=False,
    recommended_action="Inspect workspace logs and classify the failure_reason before retrying.",
)
_FAILURE_ACTIONS: dict[str, FailureAction] = {
    FailureReason.agent_failure.value: FailureAction(
        retryable=True,
        recommended_action="Retry the workspace; inspect agent logs if it fails again.",
    ),
    FailureReason.validation_failure.value: FailureAction(
        retryable=False,
        recommended_action="Review validation output and fix failing checks before retrying.",
    ),
    FailureReason.infrastructure_failure.value: FailureAction(
        retryable=True,
        recommended_action="Retry after confirming infrastructure health and worker capacity.",
    ),
    FailureReason.policy_failure.value: FailureAction(
        retryable=False,
        recommended_action="Update the request or policy inputs before retrying.",
    ),
    FailureReason.cleanup_failure.value: FailureAction(
        retryable=True,
        recommended_action="Retry cleanup or inspect node resources before creating replacements.",
    ),
    FailureReason.profile_resolution_failure.value: FailureAction(
        retryable=False,
        recommended_action="Fix the workspace profile configuration before retrying.",
    ),
    FailureReason.service_startup_failure.value: FailureAction(
        retryable=True,
        recommended_action="Retry after inspecting service startup logs and dependencies.",
    ),
    FailureReason.phase_timeout.value: FailureAction(
        retryable=True,
        recommended_action="Retry with attention to phase duration and worker capacity.",
    ),
    FailureReason.health_check_failure.value: FailureAction(
        retryable=True,
        recommended_action="Retry after checking service health probes and runtime logs.",
    ),
}
_KNOWN_FAILURE_REASONS = frozenset(reason.value for reason in FailureReason)


async def _capacity_queue_summary(
    session: AsyncSession,
    *,
    settings: Settings,
    node_id: str,
    resource_defaults: WorkspaceResourceDefaults,
    detected_local_capacity: LocalCapacityLimits | None,
    now: datetime,
    allocation_auxiliary_counts: _AllocatedResourceAuxiliaryCounts | None = None,
) -> CapacityQueueSummary:
    requested_filter = and_(
        Workspace.status == WorkspaceStatus.requested.value,
        _workspace_node_scope_filter(node_id),
    )
    queued_count = await session.scalar(select(func.count(Workspace.id)).where(requested_filter))
    requested_count = int(queued_count or 0)
    planned_totals = await _active_latest_totals_for_workspace_scope(
        session,
        statuses=(WorkspaceStatus.requested,),
        node_id=node_id,
    )
    planned_resources = _reserved_resources_from_totals(
        planned_totals,
        requested_count,
        resource_defaults=resource_defaults,
        defaulted_dind_slots=await _defaulted_dind_slots_for_session(
            session,
            statuses=(WorkspaceStatus.requested,),
            node_id=node_id,
        ),
    )
    oldest_row = (
        await session.execute(
            select(Workspace.id, Workspace.created_at)
            .where(requested_filter)
            .order_by(Workspace.created_at.asc(), Workspace.id.asc())
            .limit(1)
        )
    ).one_or_none()
    oldest_workspace_id: str | None = None
    oldest_wait_seconds: int | None = None
    if oldest_row is not None:
        oldest_workspace_id = oldest_row.id
        oldest_wait_seconds = max(
            0,
            int((_to_utc(now) - _to_utc(oldest_row.created_at)).total_seconds()),
        )
    has_configured_constraints = any(
        local_capacity_limit(
            constraint,
            cpu_limit=settings.local_capacity_cpu_cores,
            memory_limit=settings.local_capacity_memory_gb,
            dind_slots=settings.local_capacity_dind_slots,
        )
        is not None
        for constraint in LOCAL_CAPACITY_CONSTRAINTS
    )
    if has_configured_constraints:
        allocated_for_gate = await _scheduler_allocated_resources_for_session(
            session,
            node_id=node_id,
            resource_defaults=resource_defaults,
            auxiliary_counts=allocation_auxiliary_counts,
        )
    else:
        allocated_for_gate = ReservedResources(
            active_workspace_count=0,
            steady_cpu=0.0,
            steady_memory_gb=0.0,
            peak_cpu=0.0,
            peak_memory_gb=0.0,
        )
    blockers = await _capacity_queue_blocked_reason_counts(
        session,
        settings=settings,
        node_id=node_id,
        allocated_resources=allocated_for_gate,
        resource_defaults=resource_defaults,
        detected_local_capacity=detected_local_capacity,
        scoring_at=now,
    )
    return CapacityQueueSummary(
        queued_workspace_count=requested_count,
        oldest_workspace_id=oldest_workspace_id,
        oldest_wait_seconds=oldest_wait_seconds,
        planned_resources=planned_resources,
        blocked_reason_counts=blockers,
    )


async def _capacity_queue_blocked_reason_counts(
    session: AsyncSession,
    *,
    settings: Settings,
    node_id: str,
    allocated_resources: ReservedResources,
    resource_defaults: WorkspaceResourceDefaults,
    detected_local_capacity: LocalCapacityLimits | None,
    scoring_at: datetime | None = None,
) -> dict[str, int]:
    # Queue blockers must mirror scheduler enforcement, which only gates on
    # explicitly configured local capacity limits.
    del detected_local_capacity
    cpu_limit = settings.local_capacity_cpu_cores
    memory_limit = settings.local_capacity_memory_gb
    configured_constraints = []
    for constraint in LOCAL_CAPACITY_CONSTRAINTS:
        limit = local_capacity_limit(
            constraint,
            cpu_limit=cpu_limit,
            memory_limit=memory_limit,
            dind_slots=settings.local_capacity_dind_slots,
        )
        if limit is not None:
            configured_constraints.append((constraint, limit))
    if not configured_constraints:
        return {}

    scoring_time = scoring_at or datetime.now(UTC)
    candidates = await _provider_recovery_eligible_capacity_queue_scan_candidates(
        session,
        node_id=node_id,
        resource_defaults=resource_defaults,
        limit=DEFAULT_CAPACITY_QUEUE_BLOCKER_SCAN_LIMIT,
        max_refill_pages=DEFAULT_CAPACITY_QUEUE_BLOCKER_REFILL_PAGE_LIMIT,
        scoring_at=scoring_time,
    )
    if not candidates:
        return {}

    # SQL normally emits the same scheduler order, but keep this bounded
    # Python pass as a diagnostic guard if a dialect or future expression
    # change diverges from ``scheduler_order_key``.
    ordered_candidates = sorted(
        candidates,
        key=lambda candidate: scheduler_order_key(
            scheduler_score_from_workspace(candidate.workspace, now=scoring_time)
        ),
    )
    allocated = _CapacityQueueAllocated.from_reserved(allocated_resources)
    counts: dict[str, int] = {}
    # These counts are FIFO saturation frontiers, not every workspace waiting
    # behind a frontier. Unsatisfiable requests are counted individually because
    # they are independent of current allocated capacity.
    deferred_frontiers: set[str] = set()
    for candidate in ordered_candidates:
        blockers = []
        for constraint, limit in configured_constraints:
            blocker = local_capacity_blocker(
                constraint=constraint,
                limit=limit,
                allocated=getattr(allocated, constraint.dimension),
                requested=getattr(candidate.demand, constraint.dimension),
            )
            if blocker is not None:
                blockers.append(blocker)
        if blockers:
            for blocker in blockers:
                if blocker.unsatisfiable or blocker.reason_code not in deferred_frontiers:
                    counts[blocker.reason_code] = counts.get(blocker.reason_code, 0) + 1
                if not blocker.unsatisfiable:
                    deferred_frontiers.add(blocker.reason_code)
            continue
        allocated.add(candidate.demand)
    return dict(sorted(counts.items()))


async def _provider_recovery_eligible_capacity_queue_scan_candidates(
    session: AsyncSession,
    *,
    node_id: str,
    resource_defaults: WorkspaceResourceDefaults,
    limit: int,
    max_refill_pages: int,
    scoring_at: datetime,
) -> list[_CapacityQueueCandidate]:
    if limit <= 0 or max_refill_pages <= 0:
        return []

    # Blocker counts are a bounded scheduler-frontier diagnostic on the hot
    # metrics path; the queue totals above remain whole-queue aggregates. The
    # bound applies after provider-recovery suppression so cooldown/circuit
    # rows do not shrink the analyzed scheduler frontier. Refill is still
    # truncated after ``limit * max_refill_pages`` scanned rows so suppression-
    # heavy queues cannot turn the diagnostic into a whole-queue scan.
    eligible_candidates: list[_CapacityQueueCandidate] = []
    offset = 0
    pages_scanned = 0
    while len(eligible_candidates) < limit and pages_scanned < max_refill_pages:
        candidates = await _capacity_queue_candidates(
            session,
            node_id=node_id,
            resource_defaults=resource_defaults,
            limit=limit,
            offset=offset,
            scoring_at=scoring_at,
        )
        if not candidates:
            break
        pages_scanned += 1
        eligible_candidates.extend(
            await _provider_recovery_eligible_capacity_queue_candidates(
                session,
                candidates,
                scoring_at=scoring_at,
            )
        )
        if len(candidates) < limit:
            break
        offset += len(candidates)
    return eligible_candidates[:limit]


async def _provider_recovery_eligible_capacity_queue_candidates(
    session: AsyncSession,
    candidates: list[_CapacityQueueCandidate],
    *,
    scoring_at: datetime,
) -> list[_CapacityQueueCandidate]:
    if not candidates:
        return []

    cooldown_eligible: list[_CapacityQueueCandidate] = []
    circuit_candidate_pairs: dict[str, tuple[str, str]] = {}
    for candidate in candidates:
        workspace = candidate.workspace
        not_before = provider_cooldown_not_before(workspace.task_policy)
        if not_before is not None and not_before > scoring_at:
            continue
        model = agent_model_from_task_policy(workspace.task_policy)
        provider = provider_for_agent_model(workspace.agent, model)
        if provider is not None and model is not None:
            circuit_candidate_pairs[workspace.id] = (provider, model)
        cooldown_eligible.append(candidate)

    if not circuit_candidate_pairs:
        return cooldown_eligible

    open_breakers = await ProviderModelCircuitBreakerRepository(session).open_breakers_for_pairs(
        pairs=circuit_candidate_pairs.values(),
        now=scoring_at,
    )
    return [
        candidate
        for candidate in cooldown_eligible
        if circuit_candidate_pairs.get(candidate.workspace.id) not in open_breakers
    ]


async def _capacity_queue_candidates(
    session: AsyncSession,
    *,
    node_id: str,
    resource_defaults: WorkspaceResourceDefaults,
    limit: int,
    scoring_at: datetime,
    offset: int = 0,
) -> list[_CapacityQueueCandidate]:
    order_expressions = scheduler_order_expressions(
        scoring_at=scoring_at,
        dialect_name=resolve_session_dialect_name(session, None),
    )
    requested_reservation_workspace = aliased(Workspace, name="requested_reservation_workspace")
    latest_active_reservations = (
        select(
            ResourceReservation.workspace_id.label("workspace_id"),
            ResourceReservation.steady_cpu.label("reservation_steady_cpu"),
            ResourceReservation.steady_memory_gb.label("reservation_steady_memory_gb"),
            ResourceReservation.peak_cpu.label("reservation_peak_cpu"),
            ResourceReservation.peak_memory_gb.label("reservation_peak_memory_gb"),
            ResourceReservation.disk_mb.label("reservation_disk_mb"),
            ResourceReservation.dind_slots.label("reservation_dind_slots"),
            func.row_number()
            .over(
                partition_by=ResourceReservation.workspace_id,
                order_by=(
                    ResourceReservation.reserved_at.desc(),
                    ResourceReservation.id.desc(),
                ),
            )
            .label("reservation_rank"),
        )
        .join(
            requested_reservation_workspace,
            ResourceReservation.workspace_id == requested_reservation_workspace.id,
        )
        .where(
            ResourceReservation.released_at.is_(None),
            requested_reservation_workspace.status == WorkspaceStatus.requested.value,
            or_(
                requested_reservation_workspace.node_id == node_id,
                requested_reservation_workspace.node_id.is_(None),
            ),
        )
        .subquery()
    )
    stmt = (
        select(
            Workspace.id.label("queue_workspace_id"),
            Workspace.agent.label("queue_agent"),
            Workspace.task_class.label("queue_task_class"),
            Workspace.task_policy.label("queue_task_policy"),
            Workspace.created_at.label("queue_created_at"),
            Workspace.resolved_profile.label("queue_resolved_profile"),
            latest_active_reservations.c.reservation_steady_cpu,
            latest_active_reservations.c.reservation_steady_memory_gb,
            latest_active_reservations.c.reservation_peak_cpu,
            latest_active_reservations.c.reservation_peak_memory_gb,
            latest_active_reservations.c.reservation_disk_mb,
            latest_active_reservations.c.reservation_dind_slots,
        )
        # Workspace routing defines the local queue scope; reservation node_id can lag
        # during reassignment/backfill, but scheduler demand still uses this row.
        .outerjoin(
            latest_active_reservations,
            and_(
                latest_active_reservations.c.workspace_id == Workspace.id,
                latest_active_reservations.c.reservation_rank == 1,
            ),
        )
        .where(
            Workspace.status == WorkspaceStatus.requested.value,
            _workspace_node_scope_filter(node_id),
        )
        .order_by(
            order_expressions.class_priority.desc(),
            order_expressions.effective_score.desc(),
            Workspace.created_at.asc(),
            Workspace.id.asc(),
        )
        .limit(limit)
    )
    if offset > 0:
        stmt = stmt.offset(offset)
    rows = (await session.execute(stmt)).all()
    return [
        _CapacityQueueCandidate(
            workspace=_capacity_queue_workspace_from_row(row._mapping),
            demand=_capacity_queue_demand_from_row(
                values=row._mapping,
                resource_defaults=resource_defaults,
            ),
        )
        for row in rows
    ]


def _capacity_queue_workspace_from_row(values: Any) -> _CapacityQueueWorkspace:
    return _CapacityQueueWorkspace(
        id=str(values["queue_workspace_id"]),
        agent=values["queue_agent"],
        task_class=values["queue_task_class"],
        task_policy=dict(values["queue_task_policy"] or {}),
        created_at=values["queue_created_at"],
        resolved_profile=values["queue_resolved_profile"],
    )


def _capacity_queue_demand_from_row(
    *,
    values: Any,
    resource_defaults: WorkspaceResourceDefaults,
) -> ReservedResources:
    return ReservedResources(
        active_workspace_count=1,
        steady_cpu=_float_or_default(
            values["reservation_steady_cpu"],
            resource_defaults.steady_cpu,
        ),
        steady_memory_gb=_float_or_default(
            values["reservation_steady_memory_gb"],
            resource_defaults.steady_memory_gb,
        ),
        peak_cpu=_float_or_default(
            values["reservation_peak_cpu"],
            resource_defaults.peak_cpu,
        ),
        peak_memory_gb=_float_or_default(
            values["reservation_peak_memory_gb"],
            resource_defaults.peak_memory_gb,
        ),
        disk_mb=int(values["reservation_disk_mb"] or 0),
        dind_slots=(
            int(values["reservation_dind_slots"])
            if values["reservation_dind_slots"] is not None
            else default_dind_slots_from_profile(values["queue_resolved_profile"])
        ),
    )


def _float_or_default(value: Any, default: float) -> float:
    return default if value is None else float(value)


def _local_capacity_node_id(settings: Settings) -> str:
    return effective_worker_node_id(settings)


def _workspace_node_scope_filter(node_id: str) -> Any:
    return or_(Workspace.node_id == node_id, Workspace.node_id.is_(None))


def _resource_concurrency(
    status_counts: dict[str, int],
    *,
    worker: WorkerConcurrencySettings,
) -> ResourceConcurrency:
    provision_in_use = _sum_status_counts(status_counts, PROVISION_IN_USE_STATUSES)
    provision_queued = _sum_status_counts(status_counts, PROVISION_QUEUE_STATUSES)
    execution_in_use = _sum_status_counts(status_counts, EXECUTION_IN_USE_STATUSES)
    execution_queued = _sum_status_counts(status_counts, EXECUTION_QUEUE_STATUSES)
    return ResourceConcurrency(
        provision=ConcurrencyLane(
            limit=worker.max_concurrent_provisions,
            in_use=provision_in_use,
            queued=provision_queued,
            available=max(0, worker.max_concurrent_provisions - provision_in_use),
        ),
        execution=ConcurrencyLane(
            limit=worker.max_concurrent_executions,
            in_use=execution_in_use,
            queued=execution_queued,
            available=max(0, worker.max_concurrent_executions - execution_in_use),
        ),
    )


def _resource_admission_summary(
    *,
    disk_check: DiskCheck,
    concurrency: ResourceConcurrency,
) -> AdmissionSummary:
    if not disk_check.ok:
        return AdmissionSummary(
            ok=False,
            status="blocked",
            reason=disk_check.reason,
            detail=disk_check.detail
            or (
                "Free disk is below the configured workspace admission threshold. "
                f"free_bytes={disk_check.free_bytes} threshold_bytes={disk_check.threshold_bytes}"
            ),
        )

    provision_saturated = concurrency.provision.available <= 0
    execution_saturated = concurrency.execution.available <= 0

    if provision_saturated and execution_saturated:
        return AdmissionSummary(
            ok=True,
            status="saturated",
            reason=WORKER_PROVISION_AND_EXECUTION_CONCURRENCY_SATURATED_REASON,
            detail=(
                "Provisioning and execution workers are at their configured concurrency limits; "
                "new workspaces can be accepted but may wait for both provisioning and "
                "execution capacity."
            ),
        )

    if execution_saturated:
        return AdmissionSummary(
            ok=True,
            status="saturated",
            reason=WORKER_EXECUTION_CONCURRENCY_SATURATED_REASON,
            detail=(
                "Execution workers are at AWF_WORKER_MAX_CONCURRENT_EXECUTIONS; "
                "new workspaces can be accepted but may wait for execution capacity."
            ),
        )

    if provision_saturated:
        return AdmissionSummary(
            ok=True,
            status="saturated",
            reason=WORKER_PROVISION_CONCURRENCY_SATURATED_REASON,
            detail=(
                "Provisioning workers are at AWF_WORKER_MAX_CONCURRENT_PROVISIONS; "
                "new workspaces can be accepted but may wait for provisioning capacity."
            ),
        )

    return AdmissionSummary(ok=True, status="ok", reason=ADMISSION_OK_REASON)


def _sum_status_counts(status_counts: dict[str, int], statuses: Iterable[str]) -> int:
    return sum(status_counts[status] for status in statuses)
