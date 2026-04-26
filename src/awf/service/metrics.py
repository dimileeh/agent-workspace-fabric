"""Read-only operational metrics for workspace reliability."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import Workspace

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


def _validate_since_hours(since_hours: int) -> None:
    if not MIN_SUMMARY_WINDOW_HOURS <= since_hours <= MAX_SUMMARY_WINDOW_HOURS:
        raise ValueError(
            f"since_hours must be between {MIN_SUMMARY_WINDOW_HOURS} and {MAX_SUMMARY_WINDOW_HOURS}"
        )


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
