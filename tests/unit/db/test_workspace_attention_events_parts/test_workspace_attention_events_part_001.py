"""Happy-path and selectin-graph skip coverage for attention events.

Covers once-per-flip set/clear emission and unknown-id noops for
``src/awf/db/repositories/workspace_repo_attention.py`` (AIRA-T490).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.attention_events import (
    ATTENTION_CLEARED_EVENT_TYPE,
    ATTENTION_REQUIRED_EVENT_TYPE,
    ATTENTION_SOURCE_MONITORING_PR,
)
from awf.db.repositories import WorkspaceRepository
from tests.unit.db.test_workspace_attention_events_parts.helpers import (
    _SELECTIN_GRAPH_TABLES,
    _attention_events,
    _create_workspace,
)


@pytest.mark.unit
async def test_set_workspace_attention_emits_required_once_with_pr_url(
    session: AsyncSession,
) -> None:
    repo = WorkspaceRepository(session)
    workspace = await _create_workspace(
        repo,
        session,
        pr_url="https://github.com/example/app/pull/42",
    )
    now = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)

    await repo.set_workspace_attention(workspace.id, reason="blocking review", now=now)

    events = await _attention_events(session, workspace.id)
    assert len(events) == 1
    event = events[0]
    assert event.event_type == ATTENTION_REQUIRED_EVENT_TYPE
    assert event.payload == {
        "reason": "blocking review",
        "source": ATTENTION_SOURCE_MONITORING_PR,
        "pr_url": "https://github.com/example/app/pull/42",
    }


@pytest.mark.unit
async def test_set_workspace_attention_reason_refresh_does_not_reemit(
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
    assert refreshed.awaiting_human_since == first
    assert refreshed.awaiting_human_reason == "second"

    events = await _attention_events(session, workspace.id)
    assert len(events) == 1
    assert events[0].event_type == ATTENTION_REQUIRED_EVENT_TYPE
    assert events[0].payload["reason"] == "first"


@pytest.mark.unit
async def test_set_workspace_attention_reason_refresh_skips_selectin_graph(
    session: AsyncSession,
) -> None:
    """Reason-only refresh must not eager-load the workspace selectin graph.

    While NotifyHuman keeps polling, each cycle refreshes one scalar reason.
    A full ``get()`` would SELECT events/operations/… on every poll
    (PRRT_kwDOSJAM6s6Xd0TF).
    """
    repo = WorkspaceRepository(session)
    workspace = await _create_workspace(repo, session)
    workspace_id = workspace.id
    first = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
    second = first + timedelta(minutes=5)
    await repo.set_workspace_attention(workspace_id, reason="first", now=first)
    await session.flush()
    # Mirror a fresh monitor session: no identity-mapped Workspace to reuse.
    session.expunge_all()

    statements: list[str] = []
    bind = session.get_bind()

    def record_sql(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del conn, cursor, parameters, context, executemany
        statements.append(" ".join(statement.lower().split()))

    event.listen(bind, "before_cursor_execute", record_sql)
    try:
        await repo.set_workspace_attention(workspace_id, reason="second", now=second)
    finally:
        event.remove(bind, "before_cursor_execute", record_sql)

    refreshed = await repo.get(workspace_id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.awaiting_human_since == first
    assert refreshed.awaiting_human_reason == "second"
    graph_hits = [
        statement
        for statement in statements
        if statement.startswith("select")
        and any(f"from {table}" in statement for table in _SELECTIN_GRAPH_TABLES)
    ]
    assert graph_hits == []
    attention_projection = [
        statement
        for statement in statements
        if statement.startswith("select")
        and "awaiting_human_since" in statement
        and "pr_url" in statement
    ]
    assert attention_projection, "expected a scalar pr_url/attention-column projection"


@pytest.mark.unit
async def test_clear_workspace_attention_emits_cleared_once(session: AsyncSession) -> None:
    repo = WorkspaceRepository(session)
    workspace = await _create_workspace(
        repo,
        session,
        pr_url="https://github.com/example/app/pull/7",
    )
    await repo.set_workspace_attention(
        workspace.id,
        reason="merge BLOCKED",
        now=datetime(2026, 6, 22, tzinfo=UTC),
    )

    await repo.clear_workspace_attention(workspace.id)
    await repo.clear_workspace_attention(workspace.id)

    events = await _attention_events(session, workspace.id)
    assert [e.event_type for e in events] == [
        ATTENTION_CLEARED_EVENT_TYPE,
        ATTENTION_REQUIRED_EVENT_TYPE,
    ]
    cleared = events[0]
    assert cleared.payload == {
        "reason": "merge BLOCKED",
        "source": ATTENTION_SOURCE_MONITORING_PR,
        "pr_url": "https://github.com/example/app/pull/7",
    }


@pytest.mark.unit
async def test_clear_workspace_attention_when_already_clear_emits_nothing(
    session: AsyncSession,
) -> None:
    repo = WorkspaceRepository(session)
    workspace = await _create_workspace(repo, session)

    await repo.clear_workspace_attention(workspace.id)

    assert await _attention_events(session, workspace.id) == []


@pytest.mark.unit
async def test_clear_workspace_attention_already_clear_skips_selectin_graph(
    session: AsyncSession,
) -> None:
    """Already-clear clear must not eager-load the workspace selectin graph.

    Monitor polls call clear on every non-NotifyHuman action; the common case is
    already clear. A full ``get(..., populate_existing=True)`` would SELECT
    events/operations/… on every poll (PRRT_kwDOSJAM6s6XdqPs).
    """
    repo = WorkspaceRepository(session)
    workspace = await _create_workspace(repo, session)
    workspace_id = workspace.id
    await session.flush()
    # Mirror a fresh monitor session: no identity-mapped Workspace to reuse.
    session.expunge_all()

    statements: list[str] = []
    bind = session.get_bind()

    def record_sql(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del conn, cursor, parameters, context, executemany
        statements.append(" ".join(statement.lower().split()))

    event.listen(bind, "before_cursor_execute", record_sql)
    try:
        await repo.clear_workspace_attention(workspace_id)
    finally:
        event.remove(bind, "before_cursor_execute", record_sql)

    assert await _attention_events(session, workspace_id) == []
    graph_hits = [
        statement
        for statement in statements
        if statement.startswith("select")
        and any(f"from {table}" in statement for table in _SELECTIN_GRAPH_TABLES)
    ]
    assert graph_hits == []
    attention_projection = [
        statement
        for statement in statements
        if statement.startswith("select") and "awaiting_human_since" in statement
    ]
    assert attention_projection, "expected a scalar attention-column refresh"


@pytest.mark.unit
async def test_set_workspace_attention_unknown_id_is_noop(session: AsyncSession) -> None:
    """Missing workspace id must not raise or emit attention_required."""
    repo = WorkspaceRepository(session)

    await repo.set_workspace_attention(
        "ws_missing_attention_set",
        reason="should never persist",
        now=datetime(2026, 6, 22, 12, 0, tzinfo=UTC),
    )

    assert await _attention_events(session, "ws_missing_attention_set") == []


@pytest.mark.unit
async def test_clear_workspace_attention_unknown_id_is_noop(session: AsyncSession) -> None:
    """Missing workspace id must not raise or emit attention_cleared."""
    repo = WorkspaceRepository(session)

    await repo.clear_workspace_attention("ws_missing_attention_clear")

    assert await _attention_events(session, "ws_missing_attention_clear") == []
