"""Focused attention-clear terminate coverage for PR monitor runner (split part)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.attention_events import (
    ATTENTION_CLEARED_EVENT_TYPE,
    ATTENTION_SOURCE_MONITORING_PR,
)
from awf.common.commands import FakeCommandRunner
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_terminate_failed_clears_persisted_human_attention(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """PRRT_kwDOSJAM6s6XY5JH: ``NotifyHuman`` stamps attention before posting the
    human notification; a permanent forge fault then ``_terminate_failed``s.
    That terminal exit must null attention columns and emit
    ``workspace.attention_cleared`` in the same commit — otherwise the failed
    workspace strands callback consumers with an active episode forever.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        await repo.set_workspace_attention(
            workspace_id,
            reason="notify human: forge will fail permanently",
            now=datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
        )
        await session.commit()
        ws = await repo.get(workspace_id)
        assert ws is not None
        assert ws.awaiting_human_since is not None
        assert ws.awaiting_human_reason is not None

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    await runner._terminate_failed(
        workspace_id,
        message="monitor: github error: permanent notification failure",
        reason_code="GITHUB_ERROR",
    )

    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        assert ws.status == "failed"
        assert ws.awaiting_human_since is None
        assert ws.awaiting_human_reason is None
        cleared = [
            event
            for event in ws.events
            if event.event_type == ATTENTION_CLEARED_EVENT_TYPE
            and (event.payload or {}).get("source") == ATTENTION_SOURCE_MONITORING_PR
        ]
    assert len(cleared) == 1


@pytest.mark.unit
async def test_terminate_completed_clears_persisted_human_attention(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Externally merged PRs can terminate while a NotifyHuman episode is
    still active (release/manual monitors). ``_terminate_completed`` must null
    attention columns and emit ``workspace.attention_cleared`` in the same
    commit — otherwise completed workspaces strand subscribers with an
    active episode forever (Greptile issuecomment-5225662425 / PR #805).
    """
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        await repo.set_workspace_attention(
            workspace_id,
            reason="notify human: release PR awaits manual merge",
            now=datetime(2026, 8, 8, 10, 0, tzinfo=UTC),
        )
        await session.commit()
        ws = await repo.get(workspace_id)
        assert ws is not None
        assert ws.awaiting_human_since is not None
        assert ws.awaiting_human_reason is not None

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    await runner._terminate_completed(workspace_id, pr_merge_sha="deadbeef")

    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        assert ws.status == "completed"
        assert ws.pr_merge_sha == "deadbeef"
        assert ws.awaiting_human_since is None
        assert ws.awaiting_human_reason is None
        cleared = [
            event
            for event in ws.events
            if event.event_type == ATTENTION_CLEARED_EVENT_TYPE
            and (event.payload or {}).get("source") == ATTENTION_SOURCE_MONITORING_PR
        ]
    assert len(cleared) == 1
