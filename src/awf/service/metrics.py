"""Read-only operational metrics for workspace reliability."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.config import Settings
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import ResourceReservation, Workspace
from awf.service.disk import DiskCheck, DiskUsage, check_disk_space

DEFAULT_SUMMARY_WINDOW_HOURS = 24
MIN_SUMMARY_WINDOW_HOURS = 1
MAX_SUMMARY_WINDOW_HOURS = 168
DEFAULT_FAILURE_EXAMPLE_LIMIT = 5
MIN_FAILURE_EXAMPLE_LIMIT = 1
MAX_FAILURE_EXAMPLE_LIMIT = 25
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


@dataclass(frozen=True)
class FailureAnalysisSummary:
    generated_at: datetime
    window_start: datetime
    since_hours: int
    total_failed_workspaces: int
    failure_groups: list[FailureReasonGroup]
    latest_examples: list[FailedWorkspaceExample]


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
class WorkspaceResourceDefaults:
    steady_cpu: float
    steady_memory_gb: float
    peak_cpu: float
    peak_memory_gb: float


@dataclass(frozen=True)
class ReservedResources:
    active_workspace_count: int
    steady_cpu: float
    steady_memory_gb: float
    peak_cpu: float
    peak_memory_gb: float


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
class ResourceSaturationSummary:
    generated_at: datetime
    workspace_counts: WorkspaceSaturationCounts
    worker: WorkerConcurrencySettings
    resource_defaults: WorkspaceResourceDefaults
    reserved_resources: ReservedResources
    concurrency: ResourceConcurrency
    disk: DiskCheck
    admission: AdmissionSummary


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
    since_hours: int = DEFAULT_SUMMARY_WINDOW_HOURS,
    now: datetime | None = None,
) -> WorkspaceReliabilitySummary:
    """Summarize workspace reliability over a recent ``updated_at`` window."""

    async with session_factory() as session:
        return await summarize_workspace_reliability_for_session(
            session,
            since_hours=since_hours,
            now=now,
        )


async def summarize_workspace_reliability_for_session(
    session: AsyncSession,
    *,
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
    latest_examples = await _latest_failed_workspace_examples(
        session,
        window_start=window_start,
        limit=failure_example_limit,
    )

    return FailureAnalysisSummary(
        generated_at=generated_at,
        window_start=window_start,
        since_hours=since_hours,
        total_failed_workspaces=sum(reason_counts.values()),
        failure_groups=_failure_reason_groups(reason_counts),
        latest_examples=latest_examples,
    )


async def summarize_resource_saturation(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    disk_check: DiskCheck | None = None,
    disk_usage: DiskUsage | None = None,
    now: datetime | None = None,
) -> ResourceSaturationSummary:
    """Summarize local resource saturation using deterministic backend inputs."""

    async with session_factory() as session:
        return await summarize_resource_saturation_for_session(
            session,
            settings=settings,
            disk_check=disk_check,
            disk_usage=disk_usage,
            now=now,
        )


async def summarize_resource_saturation_for_session(
    session: AsyncSession,
    *,
    settings: Settings,
    disk_check: DiskCheck | None = None,
    disk_usage: DiskUsage | None = None,
    now: datetime | None = None,
) -> ResourceSaturationSummary:
    """Build the resource saturation payload for local console capacity views."""

    generated_at = _to_utc(now or datetime.now(UTC))
    status_counts = await _count_current_by_status(session)
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
    admission = _resource_admission_summary(
        disk_check=resolved_disk_check,
        concurrency=concurrency,
    )

    return ResourceSaturationSummary(
        generated_at=generated_at,
        workspace_counts=workspace_counts,
        worker=worker,
        resource_defaults=resource_defaults,
        reserved_resources=reserved_resources,
        concurrency=concurrency,
        disk=resolved_disk_check,
        admission=admission,
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


async def _count_current_by_status(session: AsyncSession) -> dict[str, int]:
    counts = {status.value: 0 for status in WorkspaceStatus}
    stmt = select(Workspace.status, func.count()).group_by(Workspace.status)
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
    rows = await session.execute(stmt)
    return [
        FailedWorkspaceExample(
            workspace_id=workspace_id,
            title=title,
            repo_url=repo_url,
            branch_base=branch_base,
            agent=agent,
            status=status,
            failure_reason=_normalize_failure_reason(failure_reason),
            failure_message=failure_message,
            pr_url=pr_url,
            created_at=_to_utc(created_at),
            updated_at=_to_utc(updated_at),
        )
        for (
            workspace_id,
            title,
            repo_url,
            branch_base,
            agent,
            status,
            failure_reason,
            failure_message,
            pr_url,
            created_at,
            updated_at,
        ) in rows.all()
    ]


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
    resource_defaults: WorkspaceResourceDefaults,
) -> ReservedResources:
    persisted = await _active_reservation_totals(session)
    fallback_count = max(0, active_workspace_count - persisted["workspace_count"])
    return ReservedResources(
        active_workspace_count=active_workspace_count,
        steady_cpu=persisted["steady_cpu"] + fallback_count * resource_defaults.steady_cpu,
        steady_memory_gb=(
            persisted["steady_memory_gb"]
            + fallback_count * resource_defaults.steady_memory_gb
        ),
        peak_cpu=persisted["peak_cpu"] + fallback_count * resource_defaults.peak_cpu,
        peak_memory_gb=(
            persisted["peak_memory_gb"] + fallback_count * resource_defaults.peak_memory_gb
        ),
    )


async def _active_reservation_totals(session: AsyncSession) -> dict[str, float | int]:
    latest_active_reservations = (
        select(
            ResourceReservation.workspace_id.label("workspace_id"),
            ResourceReservation.steady_cpu.label("steady_cpu"),
            ResourceReservation.steady_memory_gb.label("steady_memory_gb"),
            ResourceReservation.peak_cpu.label("peak_cpu"),
            ResourceReservation.peak_memory_gb.label("peak_memory_gb"),
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
        .join(Workspace, ResourceReservation.workspace_id == Workspace.id)
        .where(
            ResourceReservation.released_at.is_(None),
            ~Workspace.status.in_(TERMINAL_WORKSPACE_STATUSES),
        )
        .subquery()
    )
    stmt = (
        select(
            func.count(latest_active_reservations.c.workspace_id),
            func.coalesce(func.sum(latest_active_reservations.c.steady_cpu), 0.0),
            func.coalesce(func.sum(latest_active_reservations.c.steady_memory_gb), 0.0),
            func.coalesce(func.sum(latest_active_reservations.c.peak_cpu), 0.0),
            func.coalesce(func.sum(latest_active_reservations.c.peak_memory_gb), 0.0),
        )
        .select_from(latest_active_reservations)
        .where(latest_active_reservations.c.reservation_rank == 1)
    )
    row = (await session.execute(stmt)).one()
    return {
        "workspace_count": int(row[0] or 0),
        "steady_cpu": float(row[1] or 0.0),
        "steady_memory_gb": float(row[2] or 0.0),
        "peak_cpu": float(row[3] or 0.0),
        "peak_memory_gb": float(row[4] or 0.0),
    }


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


def _sum_status_counts(status_counts: dict[str, int], statuses: frozenset[str]) -> int:
    return sum(status_counts[status] for status in statuses)


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


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
