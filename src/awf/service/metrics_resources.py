"""Read-only operational metrics for workspace reliability."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import expression

from awf.common.config import Settings
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import ResourceReservation, Workspace, WorkspaceEvent
from awf.db.repositories import (
    ALLOCATED_RESOURCE_RESERVATION_STATUSES,
    ProviderModelCircuitBreakerRepository,
    ResourceReservationRepository,
)
from awf.service.disk import DiskCheck, DiskUsage, check_disk_space
from awf.service.metrics_slo import _to_utc
from awf.service.metrics_types import (
    AdmissionSummary,
    CapacityQueueSummary,
    FailedWorkspaceExample,
    FailureAction,
    FailureReasonGroup,
    LocalCapacitySourceSummary,
    ProviderCircuitBreakerSummary,
    ProviderRecoveryStateSummary,
    ResourceConcurrency,
    ResourceSaturationSummary,
    WorkerConcurrencySettings,
    WorkspaceSaturationCounts,
    _AllocatedResourceAuxiliaryCounts,
)
from awf.service.orphan_resources import OrphanResourceSummary, summary_not_collected
from awf.service.provider_recovery import (
    PROVIDER_RECOVERY_NO_LOOP_REASON,
    PROVIDER_RECOVERY_STATE_KEY,
)
from awf.service.resource_capacity import (
    LocalCapacityLimits,
    ReservedResources,
    WorkspaceResourceDefaults,
    resource_capacity_summary,
)
from awf.service.workspace_runtime_health import WorkspaceRuntimeHealthSummary
from awf.service.workspaces import workspace_failure_details_payload


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


def _local_capacity_source_summary(
    settings: Settings,
    detected_local_capacity: LocalCapacityLimits | None,
) -> LocalCapacitySourceSummary:
    """Describe where local capacity values come from and normalize metadata.

    When operator overrides are provided, those values take precedence and the
    source is marked as ``operator_config``/``mixed`` so consumers can display
    the exact origin of runtime saturation inputs.
    """

    detected = detected_local_capacity or LocalCapacityLimits()
    cpu_configured = settings.local_capacity_cpu_cores is not None
    memory_configured = settings.local_capacity_memory_gb is not None
    source: str | None
    if cpu_configured and memory_configured:
        source = "operator_config"
    elif cpu_configured or memory_configured:
        source = "mixed"
    else:
        source = detected.source
    reason_code = detected.reason_code
    detail = detected.detail
    if source == "operator_config":
        reason_code = None
        detail = None

    return LocalCapacitySourceSummary(
        cpu_cores=(settings.local_capacity_cpu_cores if cpu_configured else detected.cpu_cores),
        memory_gb=(settings.local_capacity_memory_gb if memory_configured else detected.memory_gb),
        source=source,
        reason_code=reason_code,
        detail=detail,
    )


PROVISION_IN_USE_STATUSES = frozenset({WorkspaceStatus.provisioning.value})
PROVISION_QUEUE_STATUSES = frozenset({WorkspaceStatus.requested.value})
EXECUTION_IN_USE_STATUSES = frozenset(
    {
        WorkspaceStatus.running.value,
        WorkspaceStatus.validating.value,
        WorkspaceStatus.pushing.value,
        WorkspaceStatus.monitoring_pr.value,
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


def _local_capacity_node_id(settings: Settings) -> str:
    from awf.service.metrics_capacity import _local_capacity_node_id as impl

    return impl(settings)


def _workspace_node_scope_filter(node_id: str) -> Any:
    from awf.service.metrics_capacity import _workspace_node_scope_filter as impl

    return impl(node_id)


def _resource_concurrency(
    status_counts: dict[str, int],
    *,
    worker: WorkerConcurrencySettings,
) -> ResourceConcurrency:
    from awf.service.metrics_capacity import _resource_concurrency as impl

    return impl(status_counts, worker=worker)


async def _capacity_queue_summary(*args: Any, **kwargs: Any) -> CapacityQueueSummary:
    from awf.service.metrics_capacity import _capacity_queue_summary as impl

    return await impl(*args, **kwargs)


def _resource_admission_summary(
    *,
    disk_check: DiskCheck,
    concurrency: ResourceConcurrency,
) -> AdmissionSummary:
    from awf.service.metrics_capacity import _resource_admission_summary as impl

    return impl(disk_check=disk_check, concurrency=concurrency)


async def summarize_resource_saturation(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    disk_check: DiskCheck | None = None,
    disk_usage: DiskUsage | None = None,
    detected_local_capacity: LocalCapacityLimits | None = None,
    orphan_resources: OrphanResourceSummary | None = None,
    runtime_health: WorkspaceRuntimeHealthSummary | None = None,
    now: datetime | None = None,
) -> ResourceSaturationSummary:
    """Summarize local resource saturation using deterministic backend inputs."""

    async with session_factory() as session:
        return await summarize_resource_saturation_for_session(
            session,
            settings=settings,
            disk_check=disk_check,
            disk_usage=disk_usage,
            detected_local_capacity=detected_local_capacity,
            orphan_resources=orphan_resources,
            runtime_health=runtime_health,
            now=now,
        )


async def summarize_resource_saturation_for_session(
    session: AsyncSession,
    *,
    settings: Settings,
    disk_check: DiskCheck | None = None,
    disk_usage: DiskUsage | None = None,
    detected_local_capacity: LocalCapacityLimits | None = None,
    orphan_resources: OrphanResourceSummary | None = None,
    runtime_health: WorkspaceRuntimeHealthSummary | None = None,
    now: datetime | None = None,
) -> ResourceSaturationSummary:
    """Build the resource saturation payload for local console capacity views."""

    generated_at = _to_utc(now or datetime.now(UTC))
    # Status counts are scoped to the local node so workspace_counts,
    # reserved_resources, and concurrency metrics describe this node's workload.
    node_id = _local_capacity_node_id(settings)
    status_counts = await _count_current_by_status(session, node_id=node_id)
    workspace_counts = _workspace_saturation_counts(status_counts)
    worker = WorkerConcurrencySettings(
        max_concurrent_provisions=settings.worker_max_concurrent_provisions,
        max_concurrent_executions=settings.worker_max_concurrent_executions,
    )
    resource_defaults = WorkspaceResourceDefaults(
        steady_cpu=settings.workspace_steady_cpu,
        steady_memory_gb=settings.workspace_steady_memory_gb,
        peak_cpu=settings.workspace_peak_cpu,
        peak_memory_gb=settings.workspace_peak_memory_gb,
    )
    reserved_resources = await _reserved_resources_for_session(
        session,
        workspace_counts.active_total,
        node_id=node_id,
        resource_defaults=resource_defaults,
    )
    allocation_auxiliary_counts = await _allocated_resource_auxiliary_counts_for_session(
        session,
        node_id=node_id,
    )
    allocated_resources = await _allocated_resources_for_session(
        session,
        node_id=node_id,
        resource_defaults=resource_defaults,
        auxiliary_counts=allocation_auxiliary_counts,
    )
    concurrency = _resource_concurrency(status_counts, worker=worker)
    resolved_disk_check = disk_check
    if resolved_disk_check is None:
        resolved_disk_check = await asyncio.to_thread(
            check_disk_space,
            settings.work_dir,
            min_free_bytes=settings.min_free_disk_bytes,
            disk_usage=disk_usage,
        )
    capacity = resource_capacity_summary(
        settings=settings,
        reserved=reserved_resources,
        resource_defaults=resource_defaults,
        disk_check=resolved_disk_check,
        detected_local_capacity=detected_local_capacity,
    )
    allocated_capacity = resource_capacity_summary(
        settings=settings,
        reserved=allocated_resources,
        resource_defaults=resource_defaults,
        disk_check=resolved_disk_check,
        detected_local_capacity=detected_local_capacity,
    )
    capacity_queue = await _capacity_queue_summary(
        session,
        settings=settings,
        node_id=node_id,
        resource_defaults=resource_defaults,
        detected_local_capacity=detected_local_capacity,
        allocation_auxiliary_counts=allocation_auxiliary_counts,
        now=generated_at,
    )
    admission = _resource_admission_summary(
        disk_check=resolved_disk_check,
        concurrency=concurrency,
    )
    resolved_orphan_resources = orphan_resources or summary_not_collected()
    resolved_runtime_health = runtime_health or WorkspaceRuntimeHealthSummary(
        scanner_available=False,
        scanner_reason="RUNTIME_HEALTH_NOT_COLLECTED",
        scanner_detail="Runtime health inventory was not collected for this summary.",
    )
    provider_circuit_breakers = [
        ProviderCircuitBreakerSummary(
            provider=breaker.provider,
            model=breaker.model,
            state=breaker.state,
            failure_count=breaker.failure_count,
            cooldown_until=breaker.cooldown_until,
            last_reason_code=breaker.last_reason_code,
            last_workspace_id=breaker.last_workspace_id,
        )
        for breaker in await ProviderModelCircuitBreakerRepository(session).list_open(
            now=generated_at
        )
    ]
    provider_recovery_state_summary = await _provider_recovery_state_summary(
        session,
        circuit_breakers_open=len(provider_circuit_breakers),
        now=generated_at,
    )

    return ResourceSaturationSummary(
        generated_at=generated_at,
        workspace_counts=workspace_counts,
        worker=worker,
        local_capacity=_local_capacity_source_summary(settings, detected_local_capacity),
        resource_defaults=resource_defaults,
        reserved_resources=reserved_resources,
        capacity=capacity,
        allocated_resources=allocated_resources,
        allocated_capacity=allocated_capacity,
        capacity_queue=capacity_queue,
        concurrency=concurrency,
        disk=resolved_disk_check,
        orphan_resources=resolved_orphan_resources,
        runtime_health=resolved_runtime_health,
        admission=admission,
        provider_circuit_breakers=provider_circuit_breakers,
        provider_recovery_state_summary=provider_recovery_state_summary,
    )


async def _count_by_status(
    session: AsyncSession,
    *,
    window_start: datetime,
) -> dict[str, int]:
    counts = {status.value: 0 for status in WorkspaceStatus}
    stmt = (
        select(Workspace.status, func.count())
        .where(Workspace.updated_at >= window_start)
        .group_by(Workspace.status)
    )
    rows = await session.execute(stmt)
    for status, count in rows.all():
        counts[str(status)] = int(count)
    return counts


async def _count_current_by_status(
    session: AsyncSession,
    *,
    node_id: str | None = None,
) -> dict[str, int]:
    counts = {status.value: 0 for status in WorkspaceStatus}
    stmt = select(Workspace.status, func.count()).group_by(Workspace.status)
    if node_id is not None:
        stmt = stmt.where(_workspace_node_scope_filter(node_id))
    rows = await session.execute(stmt)
    for status, count in rows.all():
        counts[str(status)] = int(count)
    return counts


async def _count_by_failure_reason(
    session: AsyncSession,
    *,
    window_start: datetime,
) -> dict[str, int]:
    stmt = (
        select(Workspace.failure_reason, func.count())
        .where(Workspace.updated_at >= window_start)
        .where(Workspace.failure_reason.is_not(None))
        .group_by(Workspace.failure_reason)
    )
    rows = await session.execute(stmt)
    return {str(reason): int(count) for reason, count in rows.all()}


async def _count_failed_by_failure_reason(
    session: AsyncSession,
    *,
    window_start: datetime,
) -> dict[str, int]:
    stmt = (
        select(Workspace.failure_reason, func.count())
        .where(Workspace.status == WorkspaceStatus.failed.value)
        .where(Workspace.updated_at >= window_start)
        .group_by(Workspace.failure_reason)
    )
    rows = await session.execute(stmt)
    counts: dict[str, int] = {}
    for reason, count in rows.all():
        normalized_reason = _normalize_failure_reason(reason)
        counts[normalized_reason] = counts.get(normalized_reason, 0) + int(count)
    return counts


async def _latest_failed_workspace_examples(
    session: AsyncSession,
    *,
    window_start: datetime,
    limit: int,
    failure_details_cache: dict[str, dict[str, Any]] | None = None,
) -> list[FailedWorkspaceExample]:
    stmt = (
        select(
            Workspace.id,
            Workspace.task_title,
            Workspace.repo_url,
            Workspace.branch_base,
            Workspace.agent,
            Workspace.status,
            Workspace.failure_reason,
            Workspace.failure_message,
            Workspace.pr_url,
            Workspace.created_at,
            Workspace.updated_at,
        )
        .where(Workspace.status == WorkspaceStatus.failed.value)
        .where(Workspace.updated_at >= window_start)
        .order_by(
            Workspace.updated_at.desc(),
            Workspace.created_at.desc(),
            Workspace.id.desc(),
        )
        .limit(limit)
    )
    result = await session.execute(stmt)
    rows = result.all()
    details_by_id = await _cached_failure_details_by_workspace_id(
        session,
        {row.id: row.failure_message for row in rows},
        failure_details_cache=failure_details_cache,
    )
    examples: list[FailedWorkspaceExample] = []
    for row in rows:
        details_payload = details_by_id.get(row.id, {})
        examples.append(
            FailedWorkspaceExample(
                workspace_id=row.id,
                title=row.task_title,
                repo_url=row.repo_url,
                branch_base=row.branch_base,
                agent=row.agent,
                status=row.status,
                failure_reason=_normalize_failure_reason(row.failure_reason),
                failure_message=row.failure_message,
                pr_url=row.pr_url,
                created_at=_to_utc(row.created_at),
                updated_at=_to_utc(row.updated_at),
                reason_code=_details_reason_code(details_payload),
                details=_details_only(details_payload),
                salvage=_salvage_only(details_payload),
                provider_recovery=_provider_recovery_from_details(details_payload),
            )
        )
    return examples


async def _failure_details_by_workspace_id(
    session: AsyncSession,
    failure_messages: dict[str, str | None],
) -> dict[str, dict[str, Any]]:
    if not failure_messages:
        return {}
    stmt = (
        select(
            WorkspaceEvent.workspace_id,
            WorkspaceEvent.event_type,
            WorkspaceEvent.new_state,
            WorkspaceEvent.reason_code,
            WorkspaceEvent.payload,
            WorkspaceEvent.occurred_at,
        )
        .where(WorkspaceEvent.workspace_id.in_(failure_messages))
        .where(WorkspaceEvent.event_type == "workspace.state_changed")
        .where(WorkspaceEvent.new_state == WorkspaceStatus.failed.value)
        .order_by(WorkspaceEvent.workspace_id, WorkspaceEvent.occurred_at.desc())
    )
    rows = (await session.execute(stmt)).all()
    details: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.workspace_id in details:
            continue
        workspace = _workspace_event_view(
            workspace_id=row.workspace_id,
            event_type=row.event_type,
            new_state=row.new_state,
            reason_code=row.reason_code,
            payload=row.payload,
            failure_message=failure_messages.get(row.workspace_id),
        )
        payload = workspace_failure_details_payload(workspace)
        if payload is not None:
            details[row.workspace_id] = payload
    return details


async def _cached_failure_details_by_workspace_id(
    session: AsyncSession,
    failure_messages: dict[str, str | None],
    *,
    failure_details_cache: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    if failure_details_cache is None:
        return await _failure_details_by_workspace_id(session, failure_messages)

    missing_failure_messages = {
        workspace_id: failure_message
        for workspace_id, failure_message in failure_messages.items()
        if workspace_id not in failure_details_cache
    }
    fetched_details = await _failure_details_by_workspace_id(
        session,
        missing_failure_messages,
    )
    for workspace_id in missing_failure_messages:
        failure_details_cache[workspace_id] = fetched_details.get(workspace_id, {})
    return {workspace_id: failure_details_cache[workspace_id] for workspace_id in failure_messages}


def _workspace_event_view(
    *,
    workspace_id: str,
    event_type: str,
    new_state: str | None,
    reason_code: str | None,
    payload: dict[str, Any] | None,
    failure_message: str | None,
) -> Any:
    event = SimpleNamespace(
        workspace_id=workspace_id,
        event_type=event_type,
        new_state=new_state,
        reason_code=reason_code,
        payload=payload,
    )
    return SimpleNamespace(
        id=workspace_id,
        failure_message=failure_message,
        events=[event],
    )


async def _count_active_workspaces(session: AsyncSession) -> int:
    stmt = (
        select(func.count())
        .select_from(Workspace)
        .where(~Workspace.status.in_(TERMINAL_WORKSPACE_STATUSES))
    )
    return int(await session.scalar(stmt) or 0)


async def _count_workspaces_with_status(session: AsyncSession, status: WorkspaceStatus) -> int:
    stmt = select(func.count()).select_from(Workspace).where(Workspace.status == status.value)
    return int(await session.scalar(stmt) or 0)


async def _count_stuck_workspaces(
    session: AsyncSession,
    *,
    sla_seconds: float,
    now: datetime,
) -> int:
    cutoff = now - timedelta(seconds=2 * sla_seconds)
    stmt = (
        select(func.count())
        .select_from(Workspace)
        .where(
            ~Workspace.status.in_(TERMINAL_WORKSPACE_STATUSES),
            Workspace.status != WorkspaceStatus.destroying.value,
            Workspace.created_at < cutoff,
            Workspace.failure_reason.is_(None),
        )
    )
    return int(await session.scalar(stmt) or 0)


async def _count_reason_code_coverage(
    session: AsyncSession,
    *,
    window_start: datetime,
) -> tuple[int, int]:
    stmt = (
        select(Workspace.failure_reason, func.count())
        .where(
            Workspace.status.in_(
                [
                    WorkspaceStatus.failed.value,
                    WorkspaceStatus.cancelled.value,
                    WorkspaceStatus.destroying.value,
                ]
            ),
            Workspace.updated_at >= window_start,
        )
        .group_by(Workspace.failure_reason)
    )
    rows = await session.execute(stmt)
    actionable = 0
    unactionable = 0
    for reason, count in rows.all():
        if _normalize_failure_reason(reason) in _KNOWN_FAILURE_REASONS:
            actionable += int(count)
        else:
            unactionable += int(count)
    return actionable, unactionable


def _failure_reason_groups(reason_counts: dict[str, int]) -> list[FailureReasonGroup]:
    groups: list[FailureReasonGroup] = []
    for reason, count in sorted(reason_counts.items(), key=lambda item: (-item[1], item[0])):
        action = _failure_action(reason)
        groups.append(
            FailureReasonGroup(
                failure_reason=reason,
                count=count,
                retryable=action.retryable,
                recommended_action=action.recommended_action,
            )
        )
    return groups


def _failure_action(failure_reason: str) -> FailureAction:
    if failure_reason in _KNOWN_FAILURE_REASONS:
        return _FAILURE_ACTIONS[failure_reason]
    return _UNKNOWN_FAILURE_ACTION


def _normalize_failure_reason(reason: object) -> str:
    if not isinstance(reason, str):
        return UNKNOWN_FAILURE_REASON

    normalized = reason.strip()
    if normalized in _KNOWN_FAILURE_REASONS:
        return normalized
    return UNKNOWN_FAILURE_REASON


def _details_reason_code(details_payload: dict[str, Any]) -> str | None:
    value = details_payload.get("reason_code")
    return value if isinstance(value, str) else None


def _details_only(details_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in details_payload.items()
        if key not in {"reason_code", "message", "salvage"}
    }


async def _provider_recovery_state_summary(
    session: AsyncSession,
    *,
    circuit_breakers_open: int,
    now: datetime,
) -> ProviderRecoveryStateSummary:
    action = Workspace.task_policy[PROVIDER_RECOVERY_STATE_KEY]["action"].as_string()
    decision_reason = func.coalesce(
        Workspace.task_policy[PROVIDER_RECOVERY_STATE_KEY]["decision_reason_code"].as_string(),
        Workspace.task_policy[PROVIDER_RECOVERY_STATE_KEY]["source_reason_code"].as_string(),
    )
    not_before = Workspace.task_policy[PROVIDER_RECOVERY_STATE_KEY]["not_before"].as_string()
    not_before_ts = _IsoToTimestamp(not_before)
    now_ts = _IsoToTimestamp(now.astimezone(UTC).isoformat())

    stmt = select(
        func.coalesce(
            func.sum(case((and_(action == "retry", not_before.is_(None)), 1), else_=0)),
            0,
        ).label("pending_retry_no_not_before"),
        func.coalesce(
            func.sum(
                case(
                    (and_(action == "retry", not_before.isnot(None), not_before_ts <= now_ts), 1),
                    else_=0,
                )
            ),
            0,
        ).label("pending_retry_with_not_before"),
        func.coalesce(func.sum(case((action == "fallback", 1), else_=0)), 0).label(
            "pending_fallback"
        ),
        func.coalesce(
            func.sum(
                case(
                    (and_(action == "retry", not_before.isnot(None), not_before_ts > now_ts), 1),
                    else_=0,
                )
            ),
            0,
        ).label("in_cooldown"),
        func.coalesce(
            func.sum(
                case(
                    (
                        and_(
                            action == "terminal",
                            decision_reason == PROVIDER_RECOVERY_NO_LOOP_REASON,
                        ),
                        1,
                    ),
                    else_=0,
                )
            ),
            0,
        ).label("terminal_no_loop"),
        func.coalesce(
            func.sum(
                case(
                    (
                        and_(
                            action == "terminal",
                            decision_reason.is_(None)
                            | (decision_reason != PROVIDER_RECOVERY_NO_LOOP_REASON),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ),
            0,
        ).label("terminal_exhausted"),
    ).where(~Workspace.status.in_(TERMINAL_WORKSPACE_STATUSES) | (action == "terminal"))
    row = (await session.execute(stmt)).one()
    return ProviderRecoveryStateSummary(
        pending_retry=int(row.pending_retry_no_not_before + row.pending_retry_with_not_before),
        pending_fallback=int(row.pending_fallback),
        in_cooldown=int(row.in_cooldown),
        terminal_no_loop=int(row.terminal_no_loop),
        terminal_exhausted=int(row.terminal_exhausted),
        circuit_breakers_open=circuit_breakers_open,
    )


def _salvage_only(details_payload: dict[str, Any]) -> dict[str, Any] | None:
    value = details_payload.get("salvage")
    return value if isinstance(value, dict) else None


def _provider_recovery_from_details(details_payload: dict[str, Any]) -> dict[str, Any] | None:
    value = details_payload.get("provider_recovery")
    return value if isinstance(value, dict) else None


def _workspace_saturation_counts(status_counts: dict[str, int]) -> WorkspaceSaturationCounts:
    active_total = sum(
        count
        for status, count in status_counts.items()
        if status not in TERMINAL_WORKSPACE_STATUSES
    )
    return WorkspaceSaturationCounts(
        by_status=status_counts,
        active_total=active_total,
        requested=status_counts[WorkspaceStatus.requested.value],
        provisioning=status_counts[WorkspaceStatus.provisioning.value],
        ready=status_counts[WorkspaceStatus.ready.value],
        running=status_counts[WorkspaceStatus.running.value],
        validating=status_counts[WorkspaceStatus.validating.value],
        pushing=status_counts[WorkspaceStatus.pushing.value],
        monitoring_pr=status_counts[WorkspaceStatus.monitoring_pr.value],
        blocked=status_counts[WorkspaceStatus.blocked.value],
        destroying=status_counts[WorkspaceStatus.destroying.value],
        completed=status_counts[WorkspaceStatus.completed.value],
        failed=status_counts[WorkspaceStatus.failed.value],
        cancelled=status_counts[WorkspaceStatus.cancelled.value],
        destroyed=status_counts[WorkspaceStatus.destroyed.value],
    )


async def _reserved_resources_for_session(
    session: AsyncSession,
    active_workspace_count: int,
    *,
    node_id: str,
    resource_defaults: WorkspaceResourceDefaults,
) -> ReservedResources:
    persisted = await _active_latest_totals_for_workspace_scope(session, node_id=node_id)
    defaulted_dind_slots = await _defaulted_dind_slots_for_session(session, node_id=node_id)
    return _reserved_resources_from_totals(
        persisted,
        active_workspace_count,
        resource_defaults=resource_defaults,
        defaulted_dind_slots=defaulted_dind_slots,
    )


async def _allocated_resources_for_session(
    session: AsyncSession,
    *,
    node_id: str,
    resource_defaults: WorkspaceResourceDefaults,
    auxiliary_counts: _AllocatedResourceAuxiliaryCounts | None = None,
) -> ReservedResources:
    persisted = await _active_latest_totals_for_metrics_allocation_scope(
        session,
        statuses=ALLOCATED_RESOURCE_RESERVATION_STATUSES,
        node_id=node_id,
    )
    counts = auxiliary_counts
    if counts is None:
        counts = await _allocated_resource_auxiliary_counts_for_session(
            session,
            node_id=node_id,
        )
    return _reserved_resources_from_totals(
        persisted,
        int(persisted["workspace_count"]) + counts.unreserved_workspace_count,
        resource_defaults=resource_defaults,
        defaulted_dind_slots=counts.defaulted_dind_slots,
    )


async def _scheduler_allocated_resources_for_session(
    session: AsyncSession,
    *,
    node_id: str,
    resource_defaults: WorkspaceResourceDefaults,
    auxiliary_counts: _AllocatedResourceAuxiliaryCounts | None = None,
) -> ReservedResources:
    persisted = await _active_latest_totals_for_scheduler_allocation_scope(
        session,
        statuses=ALLOCATED_RESOURCE_RESERVATION_STATUSES,
        node_id=node_id,
    )
    counts = auxiliary_counts
    if counts is None:
        counts = await _allocated_resource_auxiliary_counts_for_session(
            session,
            node_id=node_id,
        )
    return _reserved_resources_from_totals(
        persisted,
        int(persisted["workspace_count"]) + counts.unreserved_workspace_count,
        resource_defaults=resource_defaults,
        defaulted_dind_slots=counts.defaulted_dind_slots,
    )


async def _allocated_resource_auxiliary_counts_for_session(
    session: AsyncSession,
    *,
    node_id: str,
) -> _AllocatedResourceAuxiliaryCounts:
    return _AllocatedResourceAuxiliaryCounts(
        unreserved_workspace_count=await _unreserved_workspace_count_for_session(
            session,
            statuses=ALLOCATED_RESOURCE_RESERVATION_STATUSES,
            node_id=node_id,
        ),
        defaulted_dind_slots=await _defaulted_dind_slots_for_session(
            session,
            statuses=ALLOCATED_RESOURCE_RESERVATION_STATUSES,
            node_id=node_id,
        ),
    )


async def _active_latest_totals_for_workspace_scope(
    session: AsyncSession,
    *,
    statuses: Iterable[WorkspaceStatus | str] | None = None,
    node_id: str | None = None,
) -> dict[str, float | int]:
    """Sum latest active reservations for workspaces routed to this metrics scope."""

    return await ResourceReservationRepository(session).active_latest_totals_for_workspace_scope(
        statuses=statuses,
        node_id=node_id,
    )


async def _active_latest_totals_for_scheduler_allocation_scope(
    session: AsyncSession,
    *,
    statuses: Iterable[WorkspaceStatus | str],
    node_id: str,
) -> dict[str, float | int]:
    """Sum latest active reservations using the scheduler's local allocation scope."""

    repo = ResourceReservationRepository(session)
    return await repo.active_latest_totals_for_scheduler_allocation_scope(
        statuses=statuses,
        node_id=node_id,
    )


async def _active_latest_totals_for_metrics_allocation_scope(
    session: AsyncSession,
    *,
    statuses: Iterable[WorkspaceStatus | str],
    node_id: str,
) -> dict[str, float | int]:
    """Sum latest active reservations in the local metrics allocation lane."""

    repo = ResourceReservationRepository(session)
    return await repo.active_latest_totals_for_metrics_allocation_scope(
        statuses=statuses,
        node_id=node_id,
    )


def _reserved_resources_from_totals(
    persisted: dict[str, float | int],
    workspace_count: int,
    *,
    resource_defaults: WorkspaceResourceDefaults,
    defaulted_dind_slots: int = 0,
) -> ReservedResources:
    fallback_count = max(0, workspace_count - int(persisted["workspace_count"]))
    return ReservedResources(
        active_workspace_count=workspace_count,
        steady_cpu=persisted["steady_cpu"] + fallback_count * resource_defaults.steady_cpu,
        steady_memory_gb=(
            persisted["steady_memory_gb"] + fallback_count * resource_defaults.steady_memory_gb
        ),
        peak_cpu=persisted["peak_cpu"] + fallback_count * resource_defaults.peak_cpu,
        peak_memory_gb=(
            persisted["peak_memory_gb"] + fallback_count * resource_defaults.peak_memory_gb
        ),
        disk_mb=int(persisted["disk_mb"]),
        dind_slots=int(persisted["dind_slots"]) + defaulted_dind_slots,
    )


async def _defaulted_dind_slots_for_session(
    session: AsyncSession,
    *,
    statuses: Iterable[WorkspaceStatus | str] | None = None,
    node_id: str | None = None,
) -> int:
    status_filter = _workspace_status_filter(statuses)
    if status_filter is None:
        return 0
    active_reservation_exists = (
        select(ResourceReservation.id)
        .where(
            ResourceReservation.workspace_id == Workspace.id,
            ResourceReservation.released_at.is_(None),
        )
        .exists()
    )
    stmt = select(func.coalesce(func.sum(_defaulted_dind_slots_sql_expression()), 0)).where(
        status_filter,
        ~active_reservation_exists,
    )
    if node_id is not None:
        stmt = stmt.where(_workspace_node_scope_filter(node_id))
    return int(await session.scalar(stmt) or 0)


def _defaulted_dind_slots_sql_expression() -> Any:
    resolved_profile: Any = Workspace.resolved_profile
    return case(
        (resolved_profile["docker"]["mode"].as_string() == "dind", 1),
        else_=0,
    )


async def _unreserved_workspace_count_for_session(
    session: AsyncSession,
    *,
    statuses: Iterable[WorkspaceStatus | str] | None = None,
    node_id: str | None = None,
) -> int:
    status_filter = _workspace_status_filter(statuses)
    if status_filter is None:
        return 0
    active_reservation_exists = (
        select(ResourceReservation.id)
        .where(
            ResourceReservation.workspace_id == Workspace.id,
            ResourceReservation.released_at.is_(None),
        )
        .exists()
    )
    stmt = select(func.count(Workspace.id)).where(
        status_filter,
        ~active_reservation_exists,
    )
    if node_id is not None:
        stmt = stmt.where(_workspace_node_scope_filter(node_id))
    return int(await session.scalar(stmt) or 0)


def _workspace_status_filter(
    statuses: Iterable[WorkspaceStatus | str] | None,
) -> Any | None:
    if statuses is None:
        return ~Workspace.status.in_(TERMINAL_WORKSPACE_STATUSES)
    status_values = tuple(
        status.value if isinstance(status, WorkspaceStatus) else str(status) for status in statuses
    )
    if not status_values:
        return None
    return Workspace.status.in_(status_values)
