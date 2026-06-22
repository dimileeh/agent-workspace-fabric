"""Repository tests for the awaiting-human attention flag (#657)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import AgentRuntime
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from tests.postgres import postgres_test_session


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


async def _create_workspace(repo: WorkspaceRepository, session: AsyncSession) -> Workspace:
    workspace = await repo.create(
        repo_url="git@github.com:example/app.git",
        branch_base="development",
        task_title="attention test",
        task_prompt="x",
        agent=AgentRuntime.codex.value,
        test_commands=[],
    )
    await session.flush()
    return workspace


@pytest.mark.unit
async def test_set_workspace_attention_sets_since_and_reason(session: AsyncSession) -> None:
    repo = WorkspaceRepository(session)
    workspace = await _create_workspace(repo, session)
    now = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)

    await repo.set_workspace_attention(workspace.id, reason="blocking review", now=now)

    refreshed = await repo.get(workspace.id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.awaiting_human_since == now
    assert refreshed.awaiting_human_reason == "blocking review"


@pytest.mark.unit
async def test_set_workspace_attention_keeps_since_stable_refreshes_reason(
    session: AsyncSession,
) -> None:
    repo = WorkspaceRepository(session)
    workspace = await _create_workspace(repo, session)
    first = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
    second = first + timedelta(minutes=5)

    await repo.set_workspace_attention(workspace.id, reason="first", now=first)
    await repo.set_workspace_attention(workspace.id, reason="second", now=second)

    refreshed = await repo.get(workspace.id, populate_existing=True)
    assert refreshed is not None
    # COALESCE keeps the original episode start stable across repeats.
    assert refreshed.awaiting_human_since == first
    # The reason always tracks the latest escalation.
    assert refreshed.awaiting_human_reason == "second"


@pytest.mark.unit
async def test_clear_workspace_attention_nulls_both_columns(session: AsyncSession) -> None:
    repo = WorkspaceRepository(session)
    workspace = await _create_workspace(repo, session)
    await repo.set_workspace_attention(
        workspace.id, reason="blocked", now=datetime(2026, 6, 22, tzinfo=UTC)
    )

    await repo.clear_workspace_attention(workspace.id)

    refreshed = await repo.get(workspace.id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.awaiting_human_since is None
    assert refreshed.awaiting_human_reason is None


@pytest.mark.unit
async def test_clear_workspace_attention_is_noop_when_already_clear(session: AsyncSession) -> None:
    repo = WorkspaceRepository(session)
    workspace = await _create_workspace(repo, session)
    before = await repo.get(workspace.id, populate_existing=True)
    assert before is not None
    updated_at_before = before.updated_at

    # Never flagged: the guarded WHERE matches 0 rows, so the clear is a DB-level
    # no-op — no error, no row churn (updated_at unchanged).
    await repo.clear_workspace_attention(workspace.id)

    after = await repo.get(workspace.id, populate_existing=True)
    assert after is not None
    assert after.awaiting_human_since is None
    assert after.awaiting_human_reason is None
    assert after.updated_at == updated_at_before
