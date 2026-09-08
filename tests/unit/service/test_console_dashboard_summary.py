"""Service-level console dashboard summary tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.common.config import Settings
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.console_dashboard_summary import (
    _count_fleet_snapshot,
    summarize_console_dashboard,
    summarize_console_dashboard_for_session,
)
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
async def test_fleet_snapshot_pairs_live_and_windowed_counts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Live status, awaiting_human, and windowed terminals share one statement."""

    now = datetime(2026, 9, 6, 17, 0, tzinfo=UTC)
    flagged = await create_workspace(
        session_factory, status=WorkspaceStatus.monitoring_pr, updated_at=now
    )
    await create_workspace(session_factory, status=WorkspaceStatus.monitoring_pr, updated_at=now)
    await create_workspace(session_factory, status=WorkspaceStatus.running, updated_at=now)
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
    async with session_factory() as session:
        await WorkspaceRepository(session).set_workspace_attention(
            flagged, reason="merge blocked needs human", now=now
        )
        await session.commit()

    async with session_factory() as session:
        status_counts, awaiting_human, windowed, generated_at = await _count_fleet_snapshot(
            session, now=now
        )

    assert generated_at == now
    assert status_counts[WorkspaceStatus.monitoring_pr.value] == 2
    assert status_counts[WorkspaceStatus.running.value] == 1
    assert awaiting_human == 1
    assert awaiting_human <= status_counts[WorkspaceStatus.monitoring_pr.value]
    assert windowed[WorkspaceStatus.completed.value] == 1
    assert windowed[WorkspaceStatus.failed.value] == 1
    assert windowed[WorkspaceStatus.cancelled.value] == 0


@pytest.mark.unit
async def test_summary_uses_one_fleet_snapshot_for_live_and_window_counters(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: live + window counters must come from one SELECT, not two."""

    import awf.service.console_dashboard_summary as summary_mod

    calls: list[tuple[dict[str, int], int, dict[str, int], datetime]] = []
    original = summary_mod._count_fleet_snapshot

    async def _spy(
        session: AsyncSession, *, now: datetime | None, since_hours: int
    ) -> tuple[dict[str, int], int, dict[str, int], datetime]:
        result = await original(session, now=now, since_hours=since_hours)
        calls.append(result)
        return result

    monkeypatch.setattr(summary_mod, "_count_fleet_snapshot", _spy)

    settings = Settings(_env_file=None, work_dir="/tmp/awf-console-summary")
    now = datetime(2026, 9, 6, 17, 0, tzinfo=UTC)
    await create_workspace(session_factory, status=WorkspaceStatus.requested, updated_at=now)
    await create_workspace(session_factory, status=WorkspaceStatus.monitoring_pr, updated_at=now)
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=1),
    )

    summary = await summarize_console_dashboard(
        session_factory,
        settings=settings,
        now=now,
        since_hours=24,
    )
    assert len(calls) == 1
    status_counts, awaiting_human, windowed, generated_at = calls[0]
    assert summary.generated_at == generated_at
    assert summary.counts.queued == status_counts[WorkspaceStatus.requested.value]
    assert summary.counts.monitoring_pr == status_counts[WorkspaceStatus.monitoring_pr.value]
    assert summary.counts.awaiting_human == awaiting_human
    assert summary.counts.completed_last_window == windowed[WorkspaceStatus.completed.value]
    assert summary.counts.queued == 1
    assert summary.counts.monitoring_pr == 1
    assert summary.counts.awaiting_human == 0
    assert summary.counts.completed_last_window == 1
    # Live + window counters must stay in one module helper (no second SELECT helper).
    assert not hasattr(summary_mod, "_count_by_status")


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


@pytest.mark.unit
async def test_service_summary_window_excludes_terminals_after_generated_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """*_last_window is the closed interval [window.start, generated_at]."""

    settings = Settings(_env_file=None, work_dir="/tmp/awf-console-summary")
    now = datetime(2026, 9, 6, 17, 0, tzinfo=UTC)
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now,
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now + timedelta(seconds=1),
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.cancelled,
        updated_at=now + timedelta(minutes=5),
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=1),
    )

    summary = await summarize_console_dashboard(
        session_factory,
        settings=settings,
        now=now,
        since_hours=24,
    )

    assert summary.window.start == now - timedelta(hours=24)
    assert summary.generated_at == now
    assert summary.counts.completed_last_window == 1
    assert summary.counts.cancelled_last_window == 0
    assert summary.counts.failed_last_window == 1


@pytest.mark.unit
async def test_service_summary_counts_whole_control_plane_fleet_not_capacity_node(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Current and window counters must agree on fleet scope (not capacity node)."""

    settings = Settings(
        _env_file=None,
        work_dir="/tmp/awf-console-summary",
        worker_node_id="local-capacity-node",
    )
    now = datetime(2026, 9, 6, 17, 0, tzinfo=UTC)
    local_running = await create_workspace(
        session_factory, status=WorkspaceStatus.running, updated_at=now
    )
    remote_running = await create_workspace(
        session_factory, status=WorkspaceStatus.running, updated_at=now
    )
    remote_completed = await create_workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=2),
    )
    remote_queued = await create_workspace(
        session_factory, status=WorkspaceStatus.requested, updated_at=now
    )
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        local = await repo.get(local_running)
        remote = await repo.get(remote_running)
        completed = await repo.get(remote_completed)
        queued = await repo.get(remote_queued)
        assert local is not None and remote is not None
        assert completed is not None and queued is not None
        local.node_id = "local-capacity-node"
        remote.node_id = "other-worker-node"
        completed.node_id = "other-worker-node"
        queued.node_id = "other-worker-node"
        await session.commit()
        # ORM onupdate replaces updated_at on the node_id write. Pin the terminal
        # with a Core UPDATE so it stays inside [window.start, generated_at].
        await session.execute(
            update(Workspace)
            .where(Workspace.id == remote_completed)
            .values(updated_at=now - timedelta(hours=2))
        )
        await session.commit()

    summary = await summarize_console_dashboard(
        session_factory,
        settings=settings,
        now=now,
        since_hours=24,
    )
    assert summary.counts.executing == 2
    assert summary.counts.active == 3
    assert summary.counts.queued == 1
    assert summary.counts.completed_last_window == 1


@pytest.mark.unit
async def test_summary_anchor_includes_transition_committed_before_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await create_workspace(
        session_factory, status=WorkspaceStatus.running, updated_at=datetime.now(UTC)
    )
    transition_at: datetime | None = None
    async with session_factory() as session:
        execute = session.execute

        async def transition_before_execute(statement, *args, **kwargs):
            nonlocal transition_at
            async with session_factory() as writer:
                transition_at = (
                    await writer.execute(
                        update(Workspace)
                        .where(Workspace.id == workspace_id)
                        .values(status=WorkspaceStatus.completed, updated_at=func.clock_timestamp())
                        .returning(Workspace.updated_at)
                    )
                ).scalar_one()
                await writer.commit()
            return await execute(statement, *args, **kwargs)

        monkeypatch.setattr(session, "execute", transition_before_execute)
        summary = await summarize_console_dashboard_for_session(
            session, settings=Settings(_env_file=None)
        )

    assert summary.counts.active == 0
    assert summary.counts.completed_last_window == 1
    assert transition_at is not None
    assert summary.generated_at >= transition_at
    assert summary.as_of == summary.last_success_at == summary.generated_at
    assert summary.window.start == summary.generated_at - timedelta(hours=24)


@pytest.mark.unit
async def test_summary_empty_fleet_uses_database_statement_anchor(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    summary = await summarize_console_dashboard(session_factory, settings=Settings(_env_file=None))
    assert summary.generated_at.tzinfo is not None
    assert summary.window.start == summary.generated_at - timedelta(hours=24)
    assert summary.counts.active == summary.counts.completed_last_window == 0
