"""Workspace reliability summary service tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


async def _workspace(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: WorkspaceStatus,
    updated_at: datetime,
    failure_reason: FailureReason | None = None,
) -> None:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/metrics.git",
            branch_base="main",
            task_title=f"{status.value} workspace",
            task_prompt="Collect workspace reliability metrics.",
            agent="codex",
            test_commands=[],
        )
        workspace.status = status.value
        workspace.updated_at = updated_at
        workspace.failure_reason = failure_reason.value if failure_reason is not None else None
        await session.commit()


def _zero_status_counts() -> dict[str, int]:
    return {status.value: 0 for status in WorkspaceStatus}


@pytest.mark.unit
async def test_empty_db_returns_zero_workspace_reliability_summary(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_workspace_reliability

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)

    summary = await summarize_workspace_reliability(session_factory, now=now)

    assert summary.generated_at == now
    assert summary.window_start == now - timedelta(hours=24)
    assert summary.status_counts == _zero_status_counts()
    assert summary.failure_reason_counts == {}
    assert summary.active_count == 0
    assert summary.completed_count == 0
    assert summary.failed_count == 0
    assert summary.cancelled_count == 0
    assert summary.destroyed_count == 0
    assert summary.cleanup_failure_count == 0


@pytest.mark.unit
async def test_mixed_statuses_and_failure_reasons_roll_up_counts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_workspace_reliability

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    for status in (
        WorkspaceStatus.requested,
        WorkspaceStatus.running,
        WorkspaceStatus.monitoring_pr,
        WorkspaceStatus.destroying,
        WorkspaceStatus.completed,
        WorkspaceStatus.cancelled,
        WorkspaceStatus.destroyed,
    ):
        await _workspace(session_factory, status=status, updated_at=now - timedelta(minutes=10))
    await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=10),
        failure_reason=FailureReason.agent_failure,
    )
    await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=10),
        failure_reason=FailureReason.cleanup_failure,
    )

    summary = await summarize_workspace_reliability(session_factory, now=now)

    expected_status_counts = _zero_status_counts()
    expected_status_counts.update(
        {
            WorkspaceStatus.requested.value: 1,
            WorkspaceStatus.running.value: 1,
            WorkspaceStatus.monitoring_pr.value: 1,
            WorkspaceStatus.destroying.value: 1,
            WorkspaceStatus.completed.value: 1,
            WorkspaceStatus.failed.value: 2,
            WorkspaceStatus.cancelled.value: 1,
            WorkspaceStatus.destroyed.value: 1,
        }
    )
    assert summary.status_counts == expected_status_counts
    assert summary.failure_reason_counts == {
        FailureReason.agent_failure.value: 1,
        FailureReason.cleanup_failure.value: 1,
    }
    assert summary.active_count == 4
    assert summary.completed_count == 1
    assert summary.failed_count == 2
    assert summary.cancelled_count == 1
    assert summary.destroyed_count == 1
    assert summary.cleanup_failure_count == 1


@pytest.mark.unit
async def test_since_hours_filters_by_workspace_updated_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_workspace_reliability

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=7),
    )
    await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=5),
        failure_reason=FailureReason.validation_failure,
    )

    summary = await summarize_workspace_reliability(session_factory, since_hours=6, now=now)

    expected_status_counts = _zero_status_counts()
    expected_status_counts[WorkspaceStatus.failed.value] = 1
    assert summary.window_start == now - timedelta(hours=6)
    assert summary.status_counts == expected_status_counts
    assert summary.failure_reason_counts == {FailureReason.validation_failure.value: 1}
    assert summary.completed_count == 0
    assert summary.failed_count == 1
