"""Read-only operational metrics for workspace reliability."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import expression

from awf.adapters.provider_failures import AGENT_AUTH_FAILED, AGENT_PROVIDER_CAPACITY_EXHAUSTED
from awf.common.config import Settings
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import Workspace
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    PLAN_CONFORMANCE_UNSATISFIED,
)
from awf.service.metrics_types import (
    AdmissionSummary,
    CapacityQueueSummary,
    ConcurrencyLane,
    FailedWorkspaceExample,
    FailureAction,
    FailureAnalysisSummary,
    FailureReasonGroup,
    ProviderCircuitBreakerSummary,
    ProviderRecoveryStateSummary,
    ResourceConcurrency,
    ResourceSaturationSummary,
    RootCauseCluster,
    SloMetricsSummary,
    WorkerConcurrencySettings,
    WorkspaceReliabilitySummary,
    WorkspaceSaturationCounts,
)


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


from awf.service.metrics_capacity import (  # noqa: E402
    _capacity_queue_blocked_reason_counts,
    _capacity_queue_candidates,
    _capacity_queue_demand_from_row,
    _capacity_queue_summary,
    _capacity_queue_workspace_from_row,
    _float_or_default,
    _local_capacity_node_id,
    _provider_recovery_eligible_capacity_queue_candidates,
    _provider_recovery_eligible_capacity_queue_scan_candidates,
    _resource_admission_summary,
    _resource_concurrency,
    _sum_status_counts,
    _workspace_node_scope_filter,
)
from awf.service.metrics_resources import (  # noqa: E402
    _active_latest_totals_for_metrics_allocation_scope,
    _active_latest_totals_for_scheduler_allocation_scope,
    _active_latest_totals_for_workspace_scope,
    _allocated_resource_auxiliary_counts_for_session,
    _allocated_resources_for_session,
    _cached_failure_details_by_workspace_id,
    _count_active_workspaces,
    _count_by_failure_reason,
    _count_by_status,
    _count_current_by_status,
    _count_failed_by_failure_reason,
    _count_reason_code_coverage,
    _count_stuck_workspaces,
    _count_workspaces_with_status,
    _defaulted_dind_slots_for_session,
    _defaulted_dind_slots_sql_expression,
    _details_only,
    _details_reason_code,
    _failure_action,
    _failure_details_by_workspace_id,
    _failure_reason_groups,
    _latest_failed_workspace_examples,
    _normalize_failure_reason,
    _provider_recovery_from_details,
    _provider_recovery_state_summary,
    _reserved_resources_for_session,
    _reserved_resources_from_totals,
    _salvage_only,
    _scheduler_allocated_resources_for_session,
    _unreserved_workspace_count_for_session,
    _workspace_event_view,
    _workspace_saturation_counts,
    _workspace_status_filter,
    summarize_resource_saturation,
    summarize_resource_saturation_for_session,
)
from awf.service.metrics_slo import (  # noqa: E402
    _count_cleanup_metrics,
    _count_creation_metrics,
    _count_monitor_completions,
    _count_recovery_operations,
    _count_slo_reason_code_coverage,
    _count_stuck_detailed,
    _provider_likely_cause,
    _to_utc,
    _validate_failure_example_limit,
    _validate_since_hours,
    summarize_slo_metrics,
    summarize_slo_metrics_for_session,
)

__all__ = [
    "AdmissionSummary",
    "CapacityQueueSummary",
    "ConcurrencyLane",
    "FailedWorkspaceExample",
    "FailureAction",
    "FailureAnalysisSummary",
    "FailureReasonGroup",
    "ProviderCircuitBreakerSummary",
    "ProviderRecoveryStateSummary",
    "ResourceConcurrency",
    "ResourceSaturationSummary",
    "RootCauseCluster",
    "SloMetricsSummary",
    "WorkerConcurrencySettings",
    "WorkspaceReliabilitySummary",
    "WorkspaceSaturationCounts",
    "summarize_resource_saturation",
    "summarize_resource_saturation_for_session",
    "_count_by_status",
    "_count_current_by_status",
    "_count_by_failure_reason",
    "_count_failed_by_failure_reason",
    "_latest_failed_workspace_examples",
    "_failure_details_by_workspace_id",
    "_cached_failure_details_by_workspace_id",
    "_workspace_event_view",
    "_count_active_workspaces",
    "_count_workspaces_with_status",
    "_count_stuck_workspaces",
    "_count_reason_code_coverage",
    "_failure_reason_groups",
    "_failure_action",
    "_normalize_failure_reason",
    "_details_reason_code",
    "_details_only",
    "_provider_recovery_state_summary",
    "_salvage_only",
    "_provider_recovery_from_details",
    "_workspace_saturation_counts",
    "_reserved_resources_for_session",
    "_allocated_resources_for_session",
    "_scheduler_allocated_resources_for_session",
    "_allocated_resource_auxiliary_counts_for_session",
    "_active_latest_totals_for_workspace_scope",
    "_active_latest_totals_for_scheduler_allocation_scope",
    "_active_latest_totals_for_metrics_allocation_scope",
    "_reserved_resources_from_totals",
    "_defaulted_dind_slots_for_session",
    "_defaulted_dind_slots_sql_expression",
    "_unreserved_workspace_count_for_session",
    "_workspace_status_filter",
    "_capacity_queue_summary",
    "_capacity_queue_blocked_reason_counts",
    "_provider_recovery_eligible_capacity_queue_scan_candidates",
    "_provider_recovery_eligible_capacity_queue_candidates",
    "_capacity_queue_candidates",
    "_capacity_queue_workspace_from_row",
    "_capacity_queue_demand_from_row",
    "_float_or_default",
    "_local_capacity_node_id",
    "_workspace_node_scope_filter",
    "_resource_concurrency",
    "_resource_admission_summary",
    "_sum_status_counts",
    "summarize_slo_metrics",
    "summarize_slo_metrics_for_session",
    "_count_creation_metrics",
    "_count_cleanup_metrics",
    "_count_stuck_detailed",
    "_count_recovery_operations",
    "_count_monitor_completions",
    "_count_slo_reason_code_coverage",
    "_validate_since_hours",
    "_validate_failure_example_limit",
    "_provider_likely_cause",
    "_to_utc",
]
