"""Workspace event broadcast tests."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.events import WORKSPACE_EVENT_BROADCASTER


@pytest.mark.unit
async def test_committed_workspace_events_are_broadcast(engine: AsyncEngine) -> None:
    factory = make_session_factory(engine)

    async with factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@example.com:repo/app.git",
            branch_base="main",
            task_title="Broadcast events",
            task_prompt="Exercise committed event notifications.",
            agent="codex",
            test_commands=[],
        )
        async with WORKSPACE_EVENT_BROADCASTER.subscribe(workspace.id) as queue:
            await session.commit()
            frame = await asyncio.wait_for(queue.get(), timeout=1)

    assert frame.workspace_id == workspace.id
    assert frame.event_type == "workspace.created"


@pytest.mark.unit
async def test_unsubscribe_keeps_workspace_entry_until_last_subscriber_exits() -> None:
    workspace_id = "ws_two_subscribers"

    async with WORKSPACE_EVENT_BROADCASTER.subscribe(workspace_id) as first:
        async with WORKSPACE_EVENT_BROADCASTER.subscribe(workspace_id) as second:
            assert first is not second
        assert workspace_id in WORKSPACE_EVENT_BROADCASTER._subscribers  # noqa: SLF001

    assert workspace_id not in WORKSPACE_EVENT_BROADCASTER._subscribers  # noqa: SLF001
