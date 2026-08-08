"""Shared helpers for workspace attention event repository tests.

Covers ``src/awf/db/repositories/workspace_repo_attention.py`` once-per-flip
WorkspaceEvent emission for attention set/clear (AIRA-T490).
"""

from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.attention_events import (
    ATTENTION_CLEARED_EVENT_TYPE,
    ATTENTION_REQUIRED_EVENT_TYPE,
)
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceEventRepository, WorkspaceRepository

# selectin child tables loaded by a full Workspace entity get(). Hot-path
# already-clear attention clears must not materialize this graph (PRRT_kwDOSJAM6s6XdqPs).
# Derived from the mapper so newly added selectin relationships stay covered.
_SELECTIN_GRAPH_TABLES = tuple(
    rel.target.name for rel in inspect(Workspace).relationships if rel.lazy == "selectin"
)


async def _create_workspace(
    repo: WorkspaceRepository,
    session: AsyncSession,
    *,
    pr_url: str | None = None,
    status: WorkspaceStatus = WorkspaceStatus.monitoring_pr,
) -> Workspace:
    workspace = await repo.create(
        repo_url="git@github.com:example/app.git",
        branch_base="development",
        task_title="attention event test",
        task_prompt="x",
        agent=AgentRuntime.codex.value,
        test_commands=[],
    )
    # Attention enter is fenced to monitoring_pr; create defaults to requested.
    workspace.status = status.value
    if pr_url is not None:
        workspace.pr_url = pr_url
    await session.flush()
    return workspace


async def _attention_events(session: AsyncSession, workspace_id: str) -> list:
    events = await WorkspaceEventRepository(session).list(workspace_id=workspace_id, limit=50)
    return [
        e
        for e in events
        if e.event_type in {ATTENTION_REQUIRED_EVENT_TYPE, ATTENTION_CLEARED_EVENT_TYPE}
    ]
