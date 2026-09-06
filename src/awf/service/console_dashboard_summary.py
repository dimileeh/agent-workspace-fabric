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

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.config import Settings
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.service.metrics_resources import (
    _count_awaiting_human,
    _count_by_status,
    _count_current_by_status,
    _workspace_saturation_counts,
)
from awf.service.metrics_slo import _to_utc

CONSOLE_SCHEMA_VERSION = 1
DEFAULT_SUMMARY_WINDOW_HOURS = 24
CoverageStatus = Literal["complete", "partial", "unknown"]
SummaryScope = Literal["local", "tenant"]


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
    awaiting_human_subset_of_monitoring_pr: bool
    awaiting_operator_in_active_not_executing: bool
    retrying_in_active_not_executing: bool


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

    # Fleet-wide: omit capacity-node filtering so current and window counters agree.
    status_counts = await _count_current_by_status(session, node_id=None)
    awaiting_human = await _count_awaiting_human(session, node_id=None)
    saturation = _workspace_saturation_counts(status_counts, awaiting_human=awaiting_human)
    queued = await _count_queued_workspaces(session)

    windowed = await _count_by_status(session, window_start=window_start)

    executing = saturation.running + saturation.validating + saturation.pushing
    counts = ConsoleDashboardCounts(
        active=saturation.active_total,
        executing=executing,
        monitoring_pr=saturation.monitoring_pr,
        awaiting_operator=saturation.blocked,
        awaiting_human=saturation.awaiting_human,
        retrying=saturation.recovering,
        queued=queued,
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


async def _count_queued_workspaces(session: AsyncSession) -> int:
    """Count persisted queue evidence (requested status) without Docker probes."""

    stmt = select(func.count(Workspace.id)).where(
        Workspace.status == WorkspaceStatus.requested.value,
    )
    return int((await session.execute(stmt)).scalar_one())
