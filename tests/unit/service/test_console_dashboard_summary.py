"""Service-level console dashboard summary tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.common.config import Settings
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.console_dashboard_summary import summarize_console_dashboard
from tests.unit.helpers import create_workspace


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


@pytest.mark.unit
async def test_service_summary_executing_excludes_blocked_and_recovering(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(_env_file=None, work_dir="/tmp/awf-console-summary")
    now = datetime(2026, 9, 6, 17, 0, tzinfo=UTC)
    for status in (
        WorkspaceStatus.running,
        WorkspaceStatus.validating,
        WorkspaceStatus.pushing,
        WorkspaceStatus.blocked,
        WorkspaceStatus.recovering,
        WorkspaceStatus.monitoring_pr,
    ):
        await create_workspace(session_factory, status=status, updated_at=now)

    summary = await summarize_console_dashboard(
        session_factory,
        settings=settings,
        now=now,
    )
    assert summary.counts.executing == 3
    assert summary.counts.awaiting_operator == 1
    assert summary.counts.retrying == 1
    assert summary.counts.active == 6
    assert summary.counts.monitoring_pr == 1


@pytest.mark.unit
async def test_service_summary_awaiting_human_overlap(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(_env_file=None, work_dir="/tmp/awf-console-summary")
    now = datetime(2026, 9, 6, 17, 0, tzinfo=UTC)
    flagged = await create_workspace(
        session_factory, status=WorkspaceStatus.monitoring_pr, updated_at=now
    )
    await create_workspace(session_factory, status=WorkspaceStatus.monitoring_pr, updated_at=now)
    async with session_factory() as session:
        await WorkspaceRepository(session).set_workspace_attention(
            flagged, reason="merge blocked needs human", now=now
        )
        await session.commit()

    summary = await summarize_console_dashboard(
        session_factory,
        settings=settings,
        now=now,
    )
    assert summary.counts.monitoring_pr == 2
    assert summary.counts.awaiting_human == 1
    assert summary.overlap.awaiting_human_subset_of_monitoring_pr is True


@pytest.mark.unit
async def test_service_summary_window_terminal_counts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(_env_file=None, work_dir="/tmp/awf-console-summary")
    now = datetime(2026, 9, 6, 17, 0, tzinfo=UTC)
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=1),
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=30),
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=2),
    )
    summary = await summarize_console_dashboard(
        session_factory,
        settings=settings,
        now=now,
        since_hours=24,
    )
    assert summary.counts.completed_last_window == 1
    assert summary.counts.failed_last_window == 1
    assert summary.window.start == now - timedelta(hours=24)
