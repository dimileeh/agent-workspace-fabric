"""Read-only operational metrics for workspace reliability."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql import expression

from awf.common.config import Settings
from awf.db.enums import FailureReason, OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Operation, Workspace
from awf.service.metrics_types import (
    FailureAction,
    SloMetricsSummary,
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
_RECOVERY_OPERATION_TYPES = frozenset(
    {
        OperationType.remonitor.value,
        OperationType.rebase.value,
        OperationType.retry.value,
    }
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
