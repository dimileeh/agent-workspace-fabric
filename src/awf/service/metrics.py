"""Read-only operational metrics for workspace reliability."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.config import Settings
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import Workspace
from awf.service.disk import DiskCheck, DiskUsage, check_disk_space

DEFAULT_SUMMARY_WINDOW_HOURS = 24
MIN_SUMMARY_WINDOW_HOURS = 1
MAX_SUMMARY_WINDOW_HOURS = 168

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
    reserved_resources = _reserved_resources(
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


def _reserved_resources(
    active_workspace_count: int,
    *,
    resource_defaults: WorkspaceResourceDefaults,
) -> ReservedResources:
    return ReservedResources(
        active_workspace_count=active_workspace_count,
        steady_cpu=active_workspace_count * resource_defaults.steady_cpu,
        steady_memory_gb=active_workspace_count * resource_defaults.steady_memory_gb,
        peak_cpu=active_workspace_count * resource_defaults.peak_cpu,
        peak_memory_gb=active_workspace_count * resource_defaults.peak_memory_gb,
    )


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

    if concurrency.execution.available <= 0:
        return AdmissionSummary(
            ok=True,
            status="saturated",
            reason=WORKER_EXECUTION_CONCURRENCY_SATURATED_REASON,
            detail=(
                "Execution workers are at AWF_WORKER_MAX_CONCURRENT_EXECUTIONS; "
                "new workspaces can be accepted but may wait for execution capacity."
            ),
        )

    if concurrency.provision.available <= 0:
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


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
