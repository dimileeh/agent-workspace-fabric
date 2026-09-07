"""Local-scope console dashboard summary (schema_version=1).

Fleet counters are independent of resource capacity / Docker probes. Counts come
from persisted workspace status, attention flags, and reliability window SQL.
``scope=local`` means the whole authorized control-plane fleet for this Core
instance — not the capacity worker node filter used by Docker saturation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.config import Settings
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.service.metrics_resources import _workspace_saturation_counts
from awf.service.metrics_slo import _to_utc

CONSOLE_SCHEMA_VERSION = 1
DEFAULT_SUMMARY_WINDOW_HOURS = 24
CoverageStatus = Literal["complete", "partial", "unknown"]
SummaryScope = Literal["local", "tenant"]

_WINDOWED_TERMINAL_STATUSES = (
    WorkspaceStatus.completed,
    WorkspaceStatus.cancelled,
    WorkspaceStatus.failed,
)


@dataclass(frozen=True)
class ConsoleDashboardWindow:
    anchor: Literal["generated_at"]
    since_hours: int
    start: datetime


@dataclass(frozen=True)
class ConsoleDashboardCoverage:
    status: CoverageStatus
    notes: tuple[str, ...]


@dataclass(frozen=True)
class ConsoleDashboardCounts:
    active: int | None
    executing: int | None
    monitoring_pr: int | None
    awaiting_operator: int | None
    awaiting_human: int | None
    retrying: int | None
    queued: int | None
    completed_last_window: int | None
    cancelled_last_window: int | None
    failed_last_window: int | None


@dataclass(frozen=True)
class ConsoleDashboardOverlap:
    awaiting_human_subset_of_monitoring_pr: Literal[True]
    awaiting_operator_in_active_not_executing: Literal[True]
    retrying_in_active_not_executing: Literal[True]


@dataclass(frozen=True)
class ConsoleDashboardSummary:
    schema_version: int
    scope: SummaryScope
    generated_at: datetime
    as_of: datetime
    last_success_at: datetime
    window: ConsoleDashboardWindow
    coverage: ConsoleDashboardCoverage
    counts: ConsoleDashboardCounts
    overlap: ConsoleDashboardOverlap


async def summarize_console_dashboard_for_session(
    session: AsyncSession,
    *,
    settings: Settings,
    now: datetime | None = None,
    since_hours: int = DEFAULT_SUMMARY_WINDOW_HOURS,
) -> ConsoleDashboardSummary:
    """Build fleet-wide local-scope dashboard summary without Docker/capacity probes."""

    del settings  # Settings retained for call-site symmetry; fleet scope is DB-wide.
    generated_at = _to_utc(now or datetime.now(UTC))
    window_start = generated_at - timedelta(hours=since_hours)

    # One statement so live status, awaiting_human, and windowed terminals share
    # a READ COMMITTED snapshot (a workspace cannot be both executing and
    # completed_last_window under the published as_of).
    status_counts, awaiting_human, windowed = await _count_fleet_snapshot(
        session, window_start=window_start
    )
    saturation = _workspace_saturation_counts(status_counts, awaiting_human=awaiting_human)

    executing = saturation.running + saturation.validating + saturation.pushing
    counts = ConsoleDashboardCounts(
        active=saturation.active_total,
        executing=executing,
        monitoring_pr=saturation.monitoring_pr,
        awaiting_operator=saturation.blocked,
        awaiting_human=saturation.awaiting_human,
        retrying=saturation.recovering,
        queued=saturation.requested,
        completed_last_window=int(windowed.get(WorkspaceStatus.completed.value, 0)),
        cancelled_last_window=int(windowed.get(WorkspaceStatus.cancelled.value, 0)),
        failed_last_window=int(windowed.get(WorkspaceStatus.failed.value, 0)),
    )
    return ConsoleDashboardSummary(
        schema_version=CONSOLE_SCHEMA_VERSION,
        scope="local",
        generated_at=generated_at,
        as_of=generated_at,
        last_success_at=generated_at,
        window=ConsoleDashboardWindow(
            anchor="generated_at",
            since_hours=since_hours,
            start=window_start,
        ),
        coverage=ConsoleDashboardCoverage(status="complete", notes=()),
        counts=counts,
        overlap=ConsoleDashboardOverlap(
            awaiting_human_subset_of_monitoring_pr=True,
            awaiting_operator_in_active_not_executing=True,
            retrying_in_active_not_executing=True,
        ),
    )


async def summarize_console_dashboard(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    now: datetime | None = None,
    since_hours: int = DEFAULT_SUMMARY_WINDOW_HOURS,
) -> ConsoleDashboardSummary:
    async with session_factory() as session:
        return await summarize_console_dashboard_for_session(
            session,
            settings=settings,
            now=now,
            since_hours=since_hours,
        )


async def _count_fleet_snapshot(
    session: AsyncSession,
    *,
    window_start: datetime,
) -> tuple[dict[str, int], int, dict[str, int]]:
    """Count live fleet status, awaiting_human, and windowed terminals in one SELECT.

    Separate SELECTs under READ COMMITTED can observe different committed states
    (e.g. awaiting_human=1 with monitoring_pr=0, or the same row as both running
    and completed_last_window). One statement keeps the published snapshot coherent.
    """

    status_exprs = [
        func.coalesce(
            func.sum(case((Workspace.status == status.value, 1), else_=0)),
            0,
        ).label(status.value)
        for status in WorkspaceStatus
    ]
    awaiting_expr = func.coalesce(
        func.sum(
            case(
                (
                    and_(
                        Workspace.status == WorkspaceStatus.monitoring_pr.value,
                        Workspace.awaiting_human_since.is_not(None),
                    ),
                    1,
                ),
                else_=0,
            )
        ),
        0,
    ).label("awaiting_human")
    windowed_exprs = [
        func.coalesce(
            func.sum(
                case(
                    (
                        and_(
                            Workspace.status == status.value,
                            Workspace.updated_at >= window_start,
                        ),
                        1,
                    ),
                    else_=0,
                )
            ),
            0,
        ).label(f"window_{status.value}")
        for status in _WINDOWED_TERMINAL_STATUSES
    ]
    row = (
        await session.execute(
            select(*status_exprs, awaiting_expr, *windowed_exprs).select_from(Workspace)
        )
    ).one()
    status_counts = {
        status.value: int(getattr(row, status.value) or 0) for status in WorkspaceStatus
    }
    windowed = {
        status.value: int(getattr(row, f"window_{status.value}") or 0)
        for status in _WINDOWED_TERMINAL_STATUSES
    }
    return status_counts, int(row.awaiting_human or 0), windowed
