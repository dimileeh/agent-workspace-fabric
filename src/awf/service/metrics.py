"""Read-only operational metrics for workspace reliability."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import expression

from awf.adapters.provider_failures import AGENT_AUTH_FAILED, AGENT_PROVIDER_CAPACITY_EXHAUSTED
from awf.common.config import Settings
from awf.db.enums import FailureReason, OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Operation, ResourceReservation, Workspace, WorkspaceEvent
from awf.db.repositories import (
    ALLOCATED_RESOURCE_RESERVATION_STATUSES,
    ProviderModelCircuitBreakerRepository,
    ResourceReservationRepository,
)
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    PLAN_CONFORMANCE_UNSATISFIED,
)
from awf.service.config import DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID
from awf.service.disk import DiskCheck, DiskUsage, check_disk_space
from awf.service.orphan_resources import OrphanResourceSummary, summary_not_collected
from awf.service.provider_recovery import (
    PROVIDER_RECOVERY_NO_LOOP_REASON,
    PROVIDER_RECOVERY_STATE_KEY,
)
from awf.service.resource_capacity import (
    LOCAL_CAPACITY_CONSTRAINTS,
    LocalCapacityLimits,
    ReservedResources,
    ResourceCapacitySummary,
    WorkspaceResourceDefaults,
    default_dind_slots_from_profile,
    local_capacity_blocker,
    local_capacity_limit,
    resource_capacity_summary,
)
from awf.service.scheduler import scheduler_order_key, scheduler_score_from_workspace
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
    }
)
EXECUTION_QUEUE_STATUSES = frozenset({WorkspaceStatus.ready.value})

ADMISSION_OK_REASON = "ADMISSION_OK"
WORKER_PROVISION_CONCURRENCY_SATURATED_REASON = "WORKER_PROVISION_CONCURRENCY_SATURATED"
WORKER_EXECUTION_CONCURRENCY_SATURATED_REASON = "WORKER_EXECUTION_CONCURRENCY_SATURATED"
WORKER_PROVISION_AND_EXECUTION_CONCURRENCY_SATURATED_REASON = (
    "WORKER_PROVISION_AND_EXECUTION_CONCURRENCY_SATURATED"
)


@dataclass(frozen=True)
class WorkspaceReliabilitySummary:
    generated_at: datetime
    window_start: datetime
    since_hours: int
    status_counts: dict[str, int]
    failure_reason_counts: dict[str, int]
    active_count: int
    destroying_count: int
    completed_count: int
    failed_count: int
    cancelled_count: int
    destroyed_count: int
    cleanup_failure_count: int
    stuck_count: int
    actionable_reason_count: int
    unactionable_reason_count: int


@dataclass(frozen=True)
class FailureAction:
    retryable: bool
    recommended_action: str


@dataclass(frozen=True)
class FailureReasonGroup:
    failure_reason: str
    count: int
    retryable: bool
    recommended_action: str


@dataclass(frozen=True)
class FailedWorkspaceExample:
    workspace_id: str
    title: str
    repo_url: str
    branch_base: str
    agent: str
    status: str
    failure_reason: str
    failure_message: str | None
    pr_url: str | None
    created_at: datetime
    updated_at: datetime
    reason_code: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    salvage: dict[str, Any] | None = None
    provider_recovery: dict[str, Any] | None = None


@dataclass(frozen=True)
class RootCauseCluster:
    agent: str
    agent_model: str | None
    failure_reason: str
    reason_code: str | None
    likely_cause: str
    actionable_next_action: str
    count: int
    sample_workspace_ids: tuple[str, ...]
    details: dict[str, Any] = field(default_factory=dict)
    salvage: dict[str, Any] | None = None
    provider_recovery: dict[str, Any] | None = None


@dataclass(frozen=True)
class FailureAnalysisSummary:
    generated_at: datetime
    window_start: datetime
    since_hours: int
    total_failed_workspaces: int
    failure_groups: list[FailureReasonGroup]
    latest_examples: list[FailedWorkspaceExample]
    root_cause_clusters: list[RootCauseCluster] = field(default_factory=list)


@dataclass(frozen=True)
class SloMetricsSummary:
    generated_at: datetime
    window_start: datetime
    since_hours: int

    creation_total: int
    creation_succeeded: int
    creation_failed: int
    creation_cancelled: int

    cleanup_total: int
    cleanup_succeeded: int
    cleanup_failure_count: int

    stuck_running_count: int
    stuck_with_reason_count: int

    recovery_total: int
    recovery_succeeded: int
    recovery_failed_count: int

    monitor_completed_total: int
    completed_after_monitor_count: int
    monitor_stuck_count: int

    actionable_failure_count: int
    unactionable_failure_count: int


@dataclass(frozen=True)
class WorkspaceSaturationCounts:
    by_status: dict[str, int]
    active_total: int
    requested: int
    provisioning: int
    ready: int
    running: int
    validating: int
    pushing: int
    monitoring_pr: int
    destroying: int
    completed: int
    failed: int
    cancelled: int
    destroyed: int


@dataclass(frozen=True)
class WorkerConcurrencySettings:
    max_concurrent_provisions: int
    max_concurrent_executions: int


@dataclass(frozen=True)
class ConcurrencyLane:
    limit: int
    in_use: int
    queued: int
    available: int


@dataclass(frozen=True)
class ResourceConcurrency:
    provision: ConcurrencyLane
    execution: ConcurrencyLane


@dataclass(frozen=True)
class AdmissionSummary:
    ok: bool
    status: str
    reason: str
    detail: str | None = None


@dataclass(frozen=True)
class CapacityQueueSummary:
    queued_workspace_count: int
    oldest_workspace_id: str | None
    oldest_wait_seconds: int | None
    planned_resources: ReservedResources
    blocked_reason_counts: dict[str, int]


@dataclass(frozen=True)
class _CapacityQueueWorkspace:
    id: str
    task_class: str | None
    task_policy: dict[str, Any]
    created_at: datetime
    resolved_profile: object


@dataclass(frozen=True)
class _CapacityQueueCandidate:
    workspace: _CapacityQueueWorkspace
    demand: ReservedResources


@dataclass
class _CapacityQueueAllocated:
    active_workspace_count: int
    steady_cpu: float
    steady_memory_gb: float
    peak_cpu: float
    peak_memory_gb: float
    disk_mb: int = 0
    dind_slots: int = 0

    @classmethod
    def from_reserved(cls, resources: ReservedResources) -> _CapacityQueueAllocated:
        return cls(
            active_workspace_count=resources.active_workspace_count,
            steady_cpu=resources.steady_cpu,
            steady_memory_gb=resources.steady_memory_gb,
            peak_cpu=resources.peak_cpu,
            peak_memory_gb=resources.peak_memory_gb,
            disk_mb=resources.disk_mb,
            dind_slots=resources.dind_slots,
        )

    def add(self, demand: ReservedResources) -> None:
        self.active_workspace_count += demand.active_workspace_count
        self.steady_cpu += demand.steady_cpu
        self.steady_memory_gb += demand.steady_memory_gb
        self.peak_cpu += demand.peak_cpu
        self.peak_memory_gb += demand.peak_memory_gb
        self.disk_mb += demand.disk_mb
        self.dind_slots += demand.dind_slots


@dataclass(frozen=True)
class ProviderCircuitBreakerSummary:
    provider: str
    model: str
    state: str
    failure_count: int
    cooldown_until: datetime | None
    last_reason_code: str | None
    last_workspace_id: str | None


@dataclass(frozen=True)
class ProviderRecoveryStateSummary:
    pending_retry: int
    pending_fallback: int
    in_cooldown: int
    terminal_no_loop: int
    terminal_exhausted: int
    circuit_breakers_open: int


@dataclass(frozen=True)
class ResourceSaturationSummary:
    generated_at: datetime
    workspace_counts: WorkspaceSaturationCounts
    worker: WorkerConcurrencySettings
    resource_defaults: WorkspaceResourceDefaults
    reserved_resources: ReservedResources
    capacity: ResourceCapacitySummary
    allocated_resources: ReservedResources
    allocated_capacity: ResourceCapacitySummary
    capacity_queue: CapacityQueueSummary
    concurrency: ResourceConcurrency
    disk: DiskCheck
    orphan_resources: OrphanResourceSummary
    runtime_health: WorkspaceRuntimeHealthSummary
    admission: AdmissionSummary
    provider_circuit_breakers: list[ProviderCircuitBreakerSummary] = field(default_factory=list)
    provider_recovery_state_summary: ProviderRecoveryStateSummary | None = None


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


def _validate_failure_action_coverage() -> None:
    missing_actions = _KNOWN_FAILURE_REASONS.difference(_FAILURE_ACTIONS)
    if missing_actions:
        missing = ", ".join(sorted(missing_actions))
        raise RuntimeError(f"Missing failure analysis actions for FailureReason values: {missing}")


_validate_failure_action_coverage()


async def summarize_workspace_reliability(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    since_hours: int = DEFAULT_SUMMARY_WINDOW_HOURS,
    now: datetime | None = None,
) -> WorkspaceReliabilitySummary:
    """Summarize workspace reliability over a recent ``updated_at`` window."""

    async with session_factory() as session:
        return await summarize_workspace_reliability_for_session(
            session,
            settings=settings,
            since_hours=since_hours,
            now=now,
        )


async def summarize_workspace_reliability_for_session(
    session: AsyncSession,
    *,
    settings: Settings,
    since_hours: int = DEFAULT_SUMMARY_WINDOW_HOURS,
    now: datetime | None = None,
) -> WorkspaceReliabilitySummary:
    """Summarize workspace reliability using an existing request session.

    Status and failure-reason rollups are windowed by ``updated_at``. Current
    counters such as ``active_count`` and ``destroying_count`` are not windowed.
    """

    _validate_since_hours(since_hours)
    generated_at = _to_utc(now or datetime.now(UTC))
    window_start = generated_at - timedelta(hours=since_hours)

    status_counts = await _count_by_status(session, window_start=window_start)
    failure_reason_counts = await _count_by_failure_reason(session, window_start=window_start)
    active_count = await _count_active_workspaces(session)
    destroying_count = await _count_workspaces_with_status(session, WorkspaceStatus.destroying)
    stuck_count = await _count_stuck_workspaces(
        session,
        sla_seconds=settings.agent_wall_timeout_seconds,
        now=generated_at,
    )
    actionable_count, unactionable_count = await _count_reason_code_coverage(
        session,
        window_start=window_start,
    )

    completed_count = status_counts[WorkspaceStatus.completed.value]
    failed_count = status_counts[WorkspaceStatus.failed.value]
    cancelled_count = status_counts[WorkspaceStatus.cancelled.value]
    destroyed_count = status_counts[WorkspaceStatus.destroyed.value]

    return WorkspaceReliabilitySummary(
        generated_at=generated_at,
        window_start=window_start,
        since_hours=since_hours,
        status_counts=status_counts,
        failure_reason_counts=failure_reason_counts,
        active_count=active_count,
        destroying_count=destroying_count,
        completed_count=completed_count,
        failed_count=failed_count,
        cancelled_count=cancelled_count,
        destroyed_count=destroyed_count,
        cleanup_failure_count=failure_reason_counts.get(
            FailureReason.cleanup_failure.value,
            0,
        ),
        stuck_count=stuck_count,
        actionable_reason_count=actionable_count,
        unactionable_reason_count=unactionable_count,
    )


async def summarize_failure_analysis(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    since_hours: int = DEFAULT_SUMMARY_WINDOW_HOURS,
    failure_example_limit: int = DEFAULT_FAILURE_EXAMPLE_LIMIT,
    now: datetime | None = None,
) -> FailureAnalysisSummary:
    """Summarize failed workspaces by deterministic failure class and latest examples."""

    async with session_factory() as session:
        return await summarize_failure_analysis_for_session(
            session,
            since_hours=since_hours,
            failure_example_limit=failure_example_limit,
            now=now,
        )


async def summarize_failure_analysis_for_session(
    session: AsyncSession,
    *,
    since_hours: int = DEFAULT_SUMMARY_WINDOW_HOURS,
    failure_example_limit: int = DEFAULT_FAILURE_EXAMPLE_LIMIT,
    now: datetime | None = None,
) -> FailureAnalysisSummary:
    """Build a console-oriented failure analysis summary from workspace rows."""

    _validate_since_hours(since_hours)
    _validate_failure_example_limit(failure_example_limit)
    generated_at = _to_utc(now or datetime.now(UTC))
    window_start = generated_at - timedelta(hours=since_hours)

    reason_counts = await _count_failed_by_failure_reason(session, window_start=window_start)
    failure_details_cache: dict[str, dict[str, Any]] = {}
    clusters = await _cluster_root_causes(
        session,
        window_start=window_start,
        failure_details_cache=failure_details_cache,
    )
    latest_examples = await _latest_failed_workspace_examples(
        session,
        window_start=window_start,
        limit=failure_example_limit,
        failure_details_cache=failure_details_cache,
    )

    return FailureAnalysisSummary(
        generated_at=generated_at,
        window_start=window_start,
        since_hours=since_hours,
        total_failed_workspaces=sum(reason_counts.values()),
        failure_groups=_failure_reason_groups(reason_counts),
        latest_examples=latest_examples,
        root_cause_clusters=clusters,
    )


async def _cluster_root_causes(
    session: AsyncSession,
    window_start: datetime,
    *,
    failure_details_cache: dict[str, dict[str, Any]] | None = None,
) -> list[RootCauseCluster]:
    stmt = (
        select(
            Workspace.id,
            Workspace.agent,
            Workspace.task_policy,
            Workspace.failure_reason,
            Workspace.failure_message,
        )
        .where(
            Workspace.status == WorkspaceStatus.failed.value,
            Workspace.updated_at >= window_start,
        )
        .order_by(
            Workspace.updated_at.desc(),
            Workspace.id,
        )
    )
    result = await session.execute(stmt)
    rows = result.all()
    details_by_id = await _cached_failure_details_by_workspace_id(
        session,
        {row.id: row.failure_message for row in rows},
        failure_details_cache=failure_details_cache,
    )

    clusters: dict[tuple[str, str | None, str, str | None, str, str], list[str]] = {}
    cluster_details: dict[
        tuple[str, str | None, str, str | None, str, str],
        dict[str, Any],
    ] = {}
    cluster_salvage: dict[
        tuple[str, str | None, str, str | None, str, str],
        dict[str, Any] | None,
    ] = {}

    for row in rows:
        agent = row.agent or "unknown"
        agent_model = row.task_policy.get("agent_model") if row.task_policy else None
        reason = row.failure_reason or UNKNOWN_FAILURE_REASON
        msg = row.failure_message or ""
        details_payload = details_by_id.get(row.id, {})
        specific_reason_code = _details_reason_code(details_payload)

        likely_cause = "Unknown Validation Failure"
        action = "Review validation logs"

        provider_recovery = details_payload.get("provider_recovery")
        provider_failure_type = (
            provider_recovery.get("failure_type")
            if isinstance(provider_recovery, dict)
            else details_payload.get("failure_type")
        )
        provider_action = (
            provider_recovery.get("recommended_action")
            if isinstance(provider_recovery, dict)
            else details_payload.get("recommended_action")
        )

        if specific_reason_code == AGENT_PROVIDER_CAPACITY_EXHAUSTED:
            likely_cause = _provider_likely_cause(provider_failure_type)
            action = (
                provider_action
                if isinstance(provider_action, str) and provider_action
                else "Retry after provider cooldown or dispatch an approved fallback model."
            )
        elif specific_reason_code == AGENT_AUTH_FAILED:
            likely_cause = "Provider Auth Failed"
            action = (
                provider_action
                if isinstance(provider_action, str) and provider_action
                else "Refresh provider credentials or dispatch an approved fallback provider."
            )
        elif specific_reason_code == AGENT_PLAN_PHASE_SCOPE_VIOLATION:
            likely_cause = "Planning Scope Violation"
            action = (
                "Retry planning from a clean workspace; salvage the preserved branch only "
                "after explicit operator approval."
            )
        elif specific_reason_code == PLAN_CONFORMANCE_UNSATISFIED:
            likely_cause = "Plan Conformance Unsatisfied"
            action = "Retry with the final conformance gaps and finish the remaining planned work."
        elif AGENT_AUTH_FAILED in msg:
            likely_cause = "Agent Auth Failed"
            action = "Check agent credentials"
        elif "GitHub auth/PR creation failed" in msg:
            likely_cause = "GitHub Transient/Auth Error"
            action = "Check GitHub App token or retry"
        elif "model not found" in msg or "404" in msg:
            likely_cause = "Model Not Found / 404"
            action = "Verify model configuration or availability"
        elif "missing managed worktree during fix loop" in msg:
            likely_cause = "Missing Managed Worktree"
            action = "Review fix loop configuration or git identity"
        elif "coverage threshold failure" in msg:
            likely_cause = "Coverage Threshold Failure"
            action = "Update tests to improve coverage or adjust baseline"
        elif "SyntaxError" in msg or "ImportError" in msg:
            likely_cause = "Syntax or Import Error"
            action = "Fix syntax/import issues in generated code"
        elif reason == FailureReason.agent_failure.value:
            likely_cause = "Unknown Agent Failure"
            action = "Review agent logs"

        key = (agent, agent_model, reason, specific_reason_code, likely_cause, action)
        clusters.setdefault(key, []).append(row.id)
        cluster_details.setdefault(key, _details_only(details_payload))
        cluster_salvage.setdefault(key, _salvage_only(details_payload))

    return [
        RootCauseCluster(
            agent=k[0],
            agent_model=k[1],
            failure_reason=k[2],
            reason_code=k[3],
            likely_cause=k[4],
            actionable_next_action=k[5],
            count=len(wids),
            sample_workspace_ids=tuple(wids[:DEFAULT_ROOT_CAUSE_SAMPLE_LIMIT]),
            details=cluster_details.get(k, {}),
            salvage=cluster_salvage.get(k),
            provider_recovery=_provider_recovery_from_details(cluster_details.get(k, {})),
        )
        for k, wids in clusters.items()
    ]


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
    allocated_resources = await _allocated_resources_for_session(
        session,
        node_id=node_id,
        resource_defaults=resource_defaults,
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
        allocated_resources=allocated_resources,
        resource_defaults=resource_defaults,
        detected_local_capacity=detected_local_capacity,
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
) -> ReservedResources:
    persisted = await _active_latest_totals_for_scheduler_allocation_scope(
        session,
        statuses=ALLOCATED_RESOURCE_RESERVATION_STATUSES,
        node_id=node_id,
    )
    unreserved_workspace_count = await _unreserved_workspace_count_for_session(
        session,
        statuses=ALLOCATED_RESOURCE_RESERVATION_STATUSES,
        node_id=node_id,
    )
    defaulted_dind_slots = await _defaulted_dind_slots_for_session(
        session,
        statuses=ALLOCATED_RESOURCE_RESERVATION_STATUSES,
        node_id=node_id,
    )
    return _reserved_resources_from_totals(
        persisted,
        int(persisted["workspace_count"]) + unreserved_workspace_count,
        resource_defaults=resource_defaults,
        defaulted_dind_slots=defaulted_dind_slots,
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
    stmt = select(Workspace.resolved_profile).where(
        status_filter,
        ~active_reservation_exists,
    )
    if node_id is not None:
        stmt = stmt.where(_workspace_node_scope_filter(node_id))
    profiles = await session.scalars(stmt)
    return sum(default_dind_slots_from_profile(profile) for profile in profiles)


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


async def _capacity_queue_summary(
    session: AsyncSession,
    *,
    settings: Settings,
    node_id: str,
    allocated_resources: ReservedResources,
    resource_defaults: WorkspaceResourceDefaults,
    detected_local_capacity: LocalCapacityLimits | None,
    now: datetime,
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
    blockers = await _capacity_queue_blocked_reason_counts(
        session,
        settings=settings,
        node_id=node_id,
        allocated_resources=allocated_resources,
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

    candidates = await _capacity_queue_candidates(
        session,
        node_id=node_id,
        resource_defaults=resource_defaults,
    )
    if not candidates:
        return {}

    scoring_time = scoring_at or datetime.now(UTC)
    ordered_candidates = sorted(
        candidates,
        key=lambda candidate: scheduler_order_key(
            scheduler_score_from_workspace(candidate.workspace, now=scoring_time)
        ),
    )
    allocated = _CapacityQueueAllocated.from_reserved(allocated_resources)
    counts: dict[str, int] = {}
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


async def _capacity_queue_candidates(
    session: AsyncSession,
    *,
    node_id: str,
    resource_defaults: WorkspaceResourceDefaults,
) -> list[_CapacityQueueCandidate]:
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
        .where(ResourceReservation.released_at.is_(None))
        .subquery()
    )
    stmt = (
        select(
            Workspace.id.label("queue_workspace_id"),
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
        .order_by(Workspace.created_at.asc(), Workspace.id.asc())
    )
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
    configured = settings.worker_node_id.strip() if settings.worker_node_id else ""
    return configured or DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID


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


_RECOVERY_OPERATION_TYPES = frozenset(
    {OperationType.remonitor.value, OperationType.rebase.value, OperationType.retry.value}
)


async def summarize_slo_metrics(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    since_hours: int = DEFAULT_SUMMARY_WINDOW_HOURS,
    now: datetime | None = None,
) -> SloMetricsSummary:
    async with session_factory() as session:
        return await summarize_slo_metrics_for_session(
            session,
            settings=settings,
            since_hours=since_hours,
            now=now,
        )


async def summarize_slo_metrics_for_session(
    session: AsyncSession,
    *,
    settings: Settings,
    since_hours: int = DEFAULT_SUMMARY_WINDOW_HOURS,
    now: datetime | None = None,
) -> SloMetricsSummary:
    _validate_since_hours(since_hours)
    generated_at = _to_utc(now or datetime.now(UTC))
    window_start = generated_at - timedelta(hours=since_hours)
    sla_seconds = settings.agent_wall_timeout_seconds

    creation = await _count_creation_metrics(session, window_start=window_start)
    cleanup = await _count_cleanup_metrics(session, window_start=window_start)
    stuck_running, stuck_with_reason = await _count_stuck_detailed(
        session, sla_seconds=sla_seconds, now=generated_at
    )
    recovery = await _count_recovery_operations(session, window_start=window_start)
    monitor_completed, completed_after_monitor, monitor_stuck = await _count_monitor_completions(
        session, window_start=window_start, sla_seconds=sla_seconds, now=generated_at
    )
    actionable, unactionable = await _count_slo_reason_code_coverage(
        session, window_start=window_start
    )

    return SloMetricsSummary(
        generated_at=generated_at,
        window_start=window_start,
        since_hours=since_hours,
        creation_total=creation["total"],
        creation_succeeded=creation["succeeded"],
        creation_failed=creation["failed"],
        creation_cancelled=creation["cancelled"],
        cleanup_total=cleanup["total"],
        cleanup_succeeded=cleanup["succeeded"],
        cleanup_failure_count=cleanup["failed"],
        stuck_running_count=stuck_running,
        stuck_with_reason_count=stuck_with_reason,
        recovery_total=recovery["total"],
        recovery_succeeded=recovery["succeeded"],
        recovery_failed_count=recovery["failed"],
        monitor_completed_total=monitor_completed,
        completed_after_monitor_count=completed_after_monitor,
        monitor_stuck_count=monitor_stuck,
        actionable_failure_count=actionable,
        unactionable_failure_count=unactionable,
    )


async def _count_creation_metrics(
    session: AsyncSession,
    *,
    window_start: datetime,
) -> dict[str, int]:
    stmt = select(
        func.count().label("total"),
        func.sum(case((Workspace.status == WorkspaceStatus.completed.value, 1), else_=0)).label(
            "succeeded"
        ),
        func.sum(case((Workspace.status == WorkspaceStatus.failed.value, 1), else_=0)).label(
            "failed"
        ),
        func.sum(case((Workspace.status == WorkspaceStatus.cancelled.value, 1), else_=0)).label(
            "cancelled"
        ),
    ).where(Workspace.created_at >= window_start)
    row = (await session.execute(stmt)).one()
    return {
        "total": int(row.total or 0),
        "succeeded": int(row.succeeded or 0),
        "failed": int(row.failed or 0),
        "cancelled": int(row.cancelled or 0),
    }


async def _count_cleanup_metrics(
    session: AsyncSession,
    *,
    window_start: datetime,
) -> dict[str, int]:
    stmt = select(
        func.count().label("total"),
        func.sum(case((Operation.status == OperationStatus.succeeded.value, 1), else_=0)).label(
            "succeeded"
        ),
        func.sum(case((Operation.status == OperationStatus.failed.value, 1), else_=0)).label(
            "failed"
        ),
    ).where(
        Operation.type == OperationType.destroy.value,
        Operation.finished_at >= window_start,
    )
    row = (await session.execute(stmt)).one()
    return {
        "total": int(row.total or 0),
        "succeeded": int(row.succeeded or 0),
        "failed": int(row.failed or 0),
    }


async def _count_stuck_detailed(
    session: AsyncSession,
    *,
    sla_seconds: float,
    now: datetime,
) -> tuple[int, int]:
    cutoff = now - timedelta(seconds=2 * sla_seconds)
    stmt = (
        select(
            func.sum(case((Workspace.failure_reason.is_(None), 1), else_=0)).label("stuck_running"),
            func.sum(case((Workspace.failure_reason.is_not(None), 1), else_=0)).label(
                "stuck_with_reason"
            ),
        )
        .select_from(Workspace)
        .where(
            ~Workspace.status.in_(TERMINAL_WORKSPACE_STATUSES),
            Workspace.status != WorkspaceStatus.destroying.value,
            Workspace.status != WorkspaceStatus.monitoring_pr.value,
            Workspace.created_at < cutoff,
        )
    )
    row = (await session.execute(stmt)).one()
    stuck_running = int(row.stuck_running or 0)
    stuck_with_reason = int(row.stuck_with_reason or 0)
    return stuck_running, stuck_with_reason


async def _count_recovery_operations(
    session: AsyncSession,
    *,
    window_start: datetime,
) -> dict[str, int]:
    stmt = select(
        func.count().label("total"),
        func.sum(case((Operation.status == OperationStatus.succeeded.value, 1), else_=0)).label(
            "succeeded"
        ),
        func.sum(case((Operation.status == OperationStatus.failed.value, 1), else_=0)).label(
            "failed"
        ),
    ).where(
        Operation.type.in_(_RECOVERY_OPERATION_TYPES),
        Operation.created_at >= window_start,
    )
    row = (await session.execute(stmt)).one()
    return {
        "total": int(row.total or 0),
        "succeeded": int(row.succeeded or 0),
        "failed": int(row.failed or 0),
    }


async def _count_monitor_completions(
    session: AsyncSession,
    *,
    window_start: datetime,
    sla_seconds: float,
    now: datetime,
) -> tuple[int, int, int]:
    cutoff = now - timedelta(seconds=2 * sla_seconds)

    recent_pr_workspace = (Workspace.updated_at >= window_start) & Workspace.pr_url.is_not(None)
    completed_recent_pr_workspace = (
        Workspace.status == WorkspaceStatus.completed.value
    ) & recent_pr_workspace
    stuck_monitor_workspace = (Workspace.status == WorkspaceStatus.monitoring_pr.value) & (
        Workspace.created_at < cutoff
    )

    stmt = (
        select(
            func.sum(case((recent_pr_workspace, 1), else_=0)).label("monitor_completed_total"),
            func.sum(case((completed_recent_pr_workspace, 1), else_=0)).label(
                "completed_after_monitor"
            ),
            func.sum(case((stuck_monitor_workspace, 1), else_=0)).label("monitor_stuck"),
        )
        .select_from(Workspace)
        .where(recent_pr_workspace | stuck_monitor_workspace)
    )

    row = (await session.execute(stmt)).one()

    return (
        int(row.monitor_completed_total or 0),
        int(row.completed_after_monitor or 0),
        int(row.monitor_stuck or 0),
    )


async def _count_slo_reason_code_coverage(
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
        if reason is not None and reason in _KNOWN_FAILURE_REASONS:
            actionable += int(count)
        else:
            unactionable += int(count)
    return actionable, unactionable


def _validate_since_hours(since_hours: int) -> None:
    if not MIN_SUMMARY_WINDOW_HOURS <= since_hours <= MAX_SUMMARY_WINDOW_HOURS:
        raise ValueError(
            f"since_hours must be between {MIN_SUMMARY_WINDOW_HOURS} and {MAX_SUMMARY_WINDOW_HOURS}"
        )


def _validate_failure_example_limit(failure_example_limit: int) -> None:
    if not MIN_FAILURE_EXAMPLE_LIMIT <= failure_example_limit <= MAX_FAILURE_EXAMPLE_LIMIT:
        raise ValueError(
            "failure_example_limit must be between "
            f"{MIN_FAILURE_EXAMPLE_LIMIT} and {MAX_FAILURE_EXAMPLE_LIMIT}"
        )


def _provider_likely_cause(failure_type: object) -> str:
    if not isinstance(failure_type, str):
        return "Provider Capacity Exhausted"
    return {
        "auth": "Provider Auth Failed",
        "quota": "Provider Quota Exhausted",
        "capacity": "Provider Capacity Exhausted",
        "usage_limit": "Provider Usage Limit Exhausted",
        "timeout": "Provider Timeout",
        "idle_timeout": "Provider Idle Timeout",
    }.get(failure_type, "Provider Capacity Exhausted")


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
