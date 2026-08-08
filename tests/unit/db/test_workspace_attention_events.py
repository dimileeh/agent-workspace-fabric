"""Once-per-flip WorkspaceEvent emission for attention set/clear (AIRA-T490)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Update, event, update
from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.attention_events import (
    ATTENTION_CLEARED_EVENT_TYPE,
    ATTENTION_REQUIRED_EVENT_TYPE,
    ATTENTION_SOURCE_MONITORING_PR,
)
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceEventRepository, WorkspaceRepository
from tests.postgres import postgres_test_session

# selectin child tables loaded by a full Workspace entity get(). Hot-path
# already-clear attention clears must not materialize this graph (PRRT_kwDOSJAM6s6XdqPs).
_SELECTIN_GRAPH_TABLES = (
    "workspace_events",
    "operations",
    "workspace_log_streams",
    "task_attempts",
    "queue_decisions",
    "resource_reservations",
    "merge_candidates",
    "policy_findings",
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


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
async def test_clear_workspace_attention_skips_event_when_update_matches_zero_rows(
    session: AsyncSession,
) -> None:
    """Lost race: stale in-memory episode must not emit a second cleared event."""
    repo = WorkspaceRepository(session)
    workspace = await _create_workspace(repo, session)
    await repo.set_workspace_attention(
        workspace.id,
        reason="blocking",
        now=datetime(2026, 6, 22, tzinfo=UTC),
    )
    assert workspace.awaiting_human_since is not None

    # Simulate another session clearing first without refreshing the identity map.
    await session.execute(
        update(Workspace)
        .where(Workspace.id == workspace.id)
        .values(awaiting_human_since=None, awaiting_human_reason=None)
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    assert workspace.awaiting_human_since is not None

    await repo.clear_workspace_attention(workspace.id)

    events = await _attention_events(session, workspace.id)
    assert [e.event_type for e in events] == [ATTENTION_REQUIRED_EVENT_TYPE]
    assert workspace.awaiting_human_since is None
    assert workspace.awaiting_human_reason is None


@pytest.mark.unit
async def test_clear_workspace_attention_lost_race_does_not_dirty_replacement_episode(
    session: AsyncSession,
) -> None:
    """Lost clear must not dirty ORM so a replacement episode survives flush.

    After refresh sees episode E1, a concurrent clear can make the guarded UPDATE
    match zero rows. Assigning None onto the still-stale E1 snapshot dirties the
    row; if E2 is entered before the next flush, SQLAlchemy can erase it without
    attention_cleared (PRRT_kwDOSJAM6s6XYw7b).
    """
    repo = WorkspaceRepository(session)
    workspace = await _create_workspace(repo, session)
    episode_e1 = datetime(2026, 6, 22, 11, 0, tzinfo=UTC)
    episode_e2 = datetime(2026, 6, 22, 11, 30, tzinfo=UTC)
    await repo.set_workspace_attention(
        workspace.id,
        reason="episode-e1",
        now=episode_e1,
    )
    assert workspace.awaiting_human_since == episode_e1

    real_execute = session.execute
    clear_updates = 0

    async def execute_clear_then_replace(statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal clear_updates
        if isinstance(statement, Update):
            clear_updates += 1
            if clear_updates > 1:
                return await real_execute(statement, *args, **kwargs)
            # Concurrent clear of E1 so this session's guarded clear matches 0.
            await real_execute(
                update(Workspace)
                .where(Workspace.id == workspace.id)
                .values(awaiting_human_since=None, awaiting_human_reason=None)
                .execution_options(synchronize_session=False)
            )
            await session.flush()
            result = await real_execute(statement, *args, **kwargs)
            # Replacement episode entered before lost-race handling returns.
            await real_execute(
                update(Workspace)
                .where(Workspace.id == workspace.id)
                .values(
                    awaiting_human_since=episode_e2,
                    awaiting_human_reason="episode-e2",
                )
                .execution_options(synchronize_session=False)
            )
            await session.flush()
            return result
        return await real_execute(statement, *args, **kwargs)

    session.execute = execute_clear_then_replace  # type: ignore[method-assign]

    await repo.clear_workspace_attention(workspace.id)
    # Caller (or unit-of-work boundary) may flush dirty ORM state after return.
    await session.flush()

    refreshed = await repo.get(workspace.id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.awaiting_human_since == episode_e2
    assert refreshed.awaiting_human_reason == "episode-e2"
    events = await _attention_events(session, workspace.id)
    assert [e.event_type for e in events] == [ATTENTION_REQUIRED_EVENT_TYPE]
    assert events[0].payload.get("reason") == "episode-e1"


@pytest.mark.unit
async def test_clear_workspace_attention_emits_reason_at_clear_not_stale_snapshot(
    session: AsyncSession,
) -> None:
    """Cleared event must carry the reason present when the guarded clear flips.

    After clear snapshots episode start (and previously the reason), a concurrent
    reason-only refresh can commit a newer reason for the same awaiting_human_since
    fence. Emitting the unlocked pre-refresh snapshot would make attention_cleared
    disagree with the episode state immediately before clear (PRRT_kwDOSJAM6s6XZEFA).
    """
    repo = WorkspaceRepository(session)
    workspace = await _create_workspace(
        repo,
        session,
        pr_url="https://github.com/example/app/pull/11",
    )
    episode_start = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
    await repo.set_workspace_attention(
        workspace.id,
        reason="stale-snapshot-reason",
        now=episode_start,
    )
    assert workspace.awaiting_human_since == episode_start

    real_execute = session.execute

    async def execute_refresh_before_clear(statement: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(statement, Update):
            await real_execute(
                update(Workspace)
                .where(
                    Workspace.id == workspace.id,
                    Workspace.awaiting_human_since == episode_start,
                )
                .values(awaiting_human_reason="refreshed-reason")
                .execution_options(synchronize_session=False)
            )
            await session.flush()
        return await real_execute(statement, *args, **kwargs)

    session.execute = execute_refresh_before_clear  # type: ignore[method-assign]

    await repo.clear_workspace_attention(workspace.id)

    events = await _attention_events(session, workspace.id)
    assert [e.event_type for e in events] == [
        ATTENTION_CLEARED_EVENT_TYPE,
        ATTENTION_REQUIRED_EVENT_TYPE,
    ]
    assert events[0].payload == {
        "reason": "refreshed-reason",
        "source": ATTENTION_SOURCE_MONITORING_PR,
        "pr_url": "https://github.com/example/app/pull/11",
    }
    refreshed = await repo.get(workspace.id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.awaiting_human_since is None
    assert refreshed.awaiting_human_reason is None


@pytest.mark.unit
async def test_clear_workspace_attention_fenced_to_snapshotted_episode(
    session: AsyncSession,
) -> None:
    """Stale clear must not wipe a replacement episode committed before UPDATE.

    After this session snapshots E1, another session can clear E1 and enter E2
    before the clear UPDATE takes the row lock. A predicate that only requires
    non-null awaiting_human_since then clears E2 and emits attention_cleared
    with E1's reason (PRRT_kwDOSJAM6s6XY5JC).
    """
    repo = WorkspaceRepository(session)
    workspace = await _create_workspace(repo, session)
    episode_e1 = datetime(2026, 6, 22, 11, 0, tzinfo=UTC)
    episode_e2 = datetime(2026, 6, 22, 11, 30, tzinfo=UTC)
    await repo.set_workspace_attention(
        workspace.id,
        reason="episode-e1",
        now=episode_e1,
    )
    assert workspace.awaiting_human_since == episode_e1

    real_execute = session.execute
    clear_updates = 0

    async def execute_replace_before_clear(statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal clear_updates
        if isinstance(statement, Update):
            clear_updates += 1
            if clear_updates == 1:
                # Concurrent clear of E1 + enter of E2 before this session's UPDATE.
                await real_execute(
                    update(Workspace)
                    .where(Workspace.id == workspace.id)
                    .values(awaiting_human_since=None, awaiting_human_reason=None)
                    .execution_options(synchronize_session=False)
                )
                await session.flush()
                await real_execute(
                    update(Workspace)
                    .where(Workspace.id == workspace.id)
                    .values(
                        awaiting_human_since=episode_e2,
                        awaiting_human_reason="episode-e2",
                    )
                    .execution_options(synchronize_session=False)
                )
                await session.flush()
        return await real_execute(statement, *args, **kwargs)

    session.execute = execute_replace_before_clear  # type: ignore[method-assign]

    await repo.clear_workspace_attention(workspace.id)
    await session.flush()

    refreshed = await repo.get(workspace.id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.awaiting_human_since == episode_e2
    assert refreshed.awaiting_human_reason == "episode-e2"
    events = await _attention_events(session, workspace.id)
    assert [e.event_type for e in events] == [ATTENTION_REQUIRED_EVENT_TYPE]
    assert events[0].payload.get("reason") == "episode-e1"


@pytest.mark.unit
async def test_clear_workspace_attention_clears_when_identity_map_stale_clear(
    session: AsyncSession,
) -> None:
    """Stale identity-map clear must not skip a concurrently committed episode.

    guide/remonitor may cache the workspace while attention is clear; another
    transaction can enter attention before clear runs. Skipping on the cached
    flag would leave the persisted episode active (PRRT_kwDOSJAM6s6XYG7K).
    """
    repo = WorkspaceRepository(session)
    workspace = await _create_workspace(
        repo,
        session,
        pr_url="https://github.com/example/app/pull/9",
    )
    assert workspace.awaiting_human_since is None

    episode_start = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
    await session.execute(
        update(Workspace)
        .where(Workspace.id == workspace.id)
        .values(
            awaiting_human_since=episode_start,
            awaiting_human_reason="blocking review",
        )
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    assert workspace.awaiting_human_since is None

    await repo.clear_workspace_attention(workspace.id)

    refreshed = await repo.get(workspace.id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.awaiting_human_since is None
    assert refreshed.awaiting_human_reason is None
    events = await _attention_events(session, workspace.id)
    assert len(events) == 1
    assert events[0].event_type == ATTENTION_CLEARED_EVENT_TYPE
    assert events[0].payload == {
        "reason": "blocking review",
        "source": ATTENTION_SOURCE_MONITORING_PR,
        "pr_url": "https://github.com/example/app/pull/9",
    }


@pytest.mark.unit
async def test_set_workspace_attention_skips_enter_when_status_left_monitoring_pr(
    session: AsyncSession,
) -> None:
    """Terminal cancel/stop/destroy race must not reopen attention after clear.

    Control can clear ``awaiting_human_since`` and leave ``monitoring_pr`` while a
    blocked enter UPDATE waits; on re-evaluation it must not match a terminal
    row (PRRT_kwDOSJAM6s6XYelT).
    """
    repo = WorkspaceRepository(session)
    workspace = await _create_workspace(repo, session)
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    assert workspace.awaiting_human_since is None

    await session.execute(
        update(Workspace)
        .where(Workspace.id == workspace.id)
        .values(
            status=WorkspaceStatus.cancelled.value,
            awaiting_human_since=None,
            awaiting_human_reason=None,
        )
        .execution_options(synchronize_session=False)
    )
    await session.flush()

    await repo.set_workspace_attention(
        workspace.id,
        reason="blocking review after cancel race",
        now=datetime(2026, 6, 22, 12, 0, tzinfo=UTC),
    )

    refreshed = await repo.get(workspace.id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.status == WorkspaceStatus.cancelled.value
    assert refreshed.awaiting_human_since is None
    assert refreshed.awaiting_human_reason is None
    assert await _attention_events(session, workspace.id) == []


@pytest.mark.unit
async def test_set_workspace_attention_skips_event_when_enter_update_matches_zero_rows(
    session: AsyncSession,
) -> None:
    """Lost race: stale in-memory clear must not emit a second required event."""
    repo = WorkspaceRepository(session)
    workspace = await _create_workspace(repo, session)
    episode_start = datetime(2026, 6, 22, 11, 0, tzinfo=UTC)
    assert workspace.awaiting_human_since is None

    # Simulate another session entering first without refreshing the identity map.
    await session.execute(
        update(Workspace)
        .where(Workspace.id == workspace.id)
        .values(
            awaiting_human_since=episode_start,
            awaiting_human_reason="first",
        )
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    assert workspace.awaiting_human_since is None

    await repo.set_workspace_attention(
        workspace.id,
        reason="second",
        now=datetime(2026, 6, 22, 12, 0, tzinfo=UTC),
    )

    refreshed = await repo.get(workspace.id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.awaiting_human_since == episode_start
    assert refreshed.awaiting_human_reason == "second"
    assert await _attention_events(session, workspace.id) == []


@pytest.mark.unit
async def test_set_workspace_attention_reason_refresh_skips_when_episode_cleared(
    session: AsyncSession,
) -> None:
    """Lost enter + concurrent clear must not orphan awaiting_human_reason.

    After the guarded enter UPDATE matches zero rows, another transaction may
    clear awaiting_human_since before the reason-only refresh. Refreshing by id
    alone would write reason while attention is clear (PRRT_kwDOSJAM6s6XYFS3).
    """
    repo = WorkspaceRepository(session)
    workspace = await _create_workspace(repo, session)
    episode_start = datetime(2026, 6, 22, 11, 0, tzinfo=UTC)
    await session.execute(
        update(Workspace)
        .where(Workspace.id == workspace.id)
        .values(
            awaiting_human_since=episode_start,
            awaiting_human_reason="first",
        )
        .execution_options(synchronize_session=False)
    )
    await session.flush()

    real_execute = session.execute
    update_calls = 0

    async def execute_clearing_after_enter(statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal update_calls
        result = await real_execute(statement, *args, **kwargs)
        if isinstance(statement, Update):
            update_calls += 1
            if update_calls == 1:
                # Concurrent clear between lost-enter and reason refresh.
                await real_execute(
                    update(Workspace)
                    .where(Workspace.id == workspace.id)
                    .values(awaiting_human_since=None, awaiting_human_reason=None)
                    .execution_options(synchronize_session=False)
                )
                await session.flush()
        return result

    session.execute = execute_clearing_after_enter  # type: ignore[method-assign]

    await repo.set_workspace_attention(
        workspace.id,
        reason="second",
        now=datetime(2026, 6, 22, 12, 0, tzinfo=UTC),
    )

    refreshed = await repo.get(workspace.id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.awaiting_human_since is None
    assert refreshed.awaiting_human_reason is None
    assert await _attention_events(session, workspace.id) == []


@pytest.mark.unit
async def test_set_workspace_attention_reason_refresh_fenced_to_observed_episode(
    session: AsyncSession,
) -> None:
    """Lost enter must not refresh a replacement episode's reason.

    After setter A loses enter against episode E1, E1 can be cleared and setter B
    can open E2 before A's reason refresh. A refresh guarded only by
    ``awaiting_human_since IS NOT NULL`` overwrites B's reason so the required
    event and read model disagree (PRRT_kwDOSJAM6s6XYlxN).
    """
    repo = WorkspaceRepository(session)
    workspace = await _create_workspace(repo, session)
    episode_e1 = datetime(2026, 6, 22, 11, 0, tzinfo=UTC)
    episode_e2 = datetime(2026, 6, 22, 11, 30, tzinfo=UTC)
    await repo.set_workspace_attention(
        workspace.id,
        reason="episode-e1",
        now=episode_e1,
    )
    assert workspace.awaiting_human_since == episode_e1

    real_execute = session.execute
    update_calls = 0

    async def execute_replace_episode_after_enter(statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal update_calls
        result = await real_execute(statement, *args, **kwargs)
        if isinstance(statement, Update):
            update_calls += 1
            if update_calls == 1:
                # Concurrent clear of E1 + enter of E2 (as setter B) before refresh.
                await real_execute(
                    update(Workspace)
                    .where(Workspace.id == workspace.id)
                    .values(awaiting_human_since=None, awaiting_human_reason=None)
                    .execution_options(synchronize_session=False)
                )
                await real_execute(
                    update(Workspace)
                    .where(Workspace.id == workspace.id)
                    .values(
                        awaiting_human_since=episode_e2,
                        awaiting_human_reason="episode-e2-from-b",
                    )
                    .execution_options(synchronize_session=False)
                )
                await session.flush()
        return result

    session.execute = execute_replace_episode_after_enter  # type: ignore[method-assign]

    await repo.set_workspace_attention(
        workspace.id,
        reason="stale-reason-from-a",
        now=datetime(2026, 6, 22, 12, 0, tzinfo=UTC),
    )

    refreshed = await repo.get(workspace.id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.awaiting_human_since == episode_e2
    assert refreshed.awaiting_human_reason == "episode-e2-from-b"
    events = await _attention_events(session, workspace.id)
    assert len(events) == 1
    assert events[0].event_type == ATTENTION_REQUIRED_EVENT_TYPE
    assert events[0].payload.get("reason") == "episode-e1"


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


@pytest.mark.unit
async def test_set_workspace_attention_lost_enter_after_null_projection_refreshes_winner(
    session: AsyncSession,
) -> None:
    """Null projection + concurrent enter must refresh the winner's reason.

    The scalar projection can observe ``awaiting_human_since IS NULL`` while
    another session opens the episode before the guarded enter UPDATE runs.
    Enter matches zero rows with ``observed_since`` still None; the setter must
    re-fetch the winner's episode start and fence the reason-only refresh to it
    (coverage branch 206→215) without emitting a second attention_required.
    """
    repo = WorkspaceRepository(session)
    workspace = await _create_workspace(repo, session)
    workspace_id = workspace.id
    # Fresh monitor session: projection is not polluted by an identity-map episode.
    session.expunge_all()
    winner_since = datetime(2026, 6, 22, 11, 0, tzinfo=UTC)
    real_execute = session.execute
    update_calls = 0

    async def execute_enter_after_projection(statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal update_calls
        if isinstance(statement, Update):
            update_calls += 1
            if update_calls == 1:
                # Concurrent enter between null projection and this enter UPDATE.
                await real_execute(
                    update(Workspace)
                    .where(Workspace.id == workspace_id)
                    .values(
                        awaiting_human_since=winner_since,
                        awaiting_human_reason="winner-first",
                    )
                    .execution_options(synchronize_session=False)
                )
                await session.flush()
        return await real_execute(statement, *args, **kwargs)

    session.execute = execute_enter_after_projection  # type: ignore[method-assign]

    await repo.set_workspace_attention(
        workspace_id,
        reason="loser-latest",
        now=datetime(2026, 6, 22, 12, 0, tzinfo=UTC),
    )

    refreshed = await repo.get(workspace_id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.awaiting_human_since == winner_since
    assert refreshed.awaiting_human_reason == "loser-latest"
    assert await _attention_events(session, workspace_id) == []


@pytest.mark.unit
async def test_clear_workspace_attention_lost_race_without_identity_map_is_noop(
    session: AsyncSession,
) -> None:
    """Lost clear with no cached Workspace must not emit attention_cleared.

    Monitor poll sessions often have no identity-mapped row. When a concurrent
    clear wins after the scalar episode snapshot, the local UPDATE returns no
    row; ``_refresh_cached_attention_columns`` must early-return without loading
    the selectin graph or emitting a cleared event.
    """
    repo = WorkspaceRepository(session)
    workspace = await _create_workspace(repo, session)
    workspace_id = workspace.id
    await repo.set_workspace_attention(
        workspace_id,
        reason="blocking",
        now=datetime(2026, 6, 22, 11, 0, tzinfo=UTC),
    )
    await session.flush()
    session.expunge_all()

    real_execute = session.execute
    clear_updates = 0

    async def execute_clear_before_guarded(statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal clear_updates
        if isinstance(statement, Update):
            clear_updates += 1
            if clear_updates == 1:
                await real_execute(
                    update(Workspace)
                    .where(Workspace.id == workspace_id)
                    .values(awaiting_human_since=None, awaiting_human_reason=None)
                    .execution_options(synchronize_session=False)
                )
                await session.flush()
        return await real_execute(statement, *args, **kwargs)

    session.execute = execute_clear_before_guarded  # type: ignore[method-assign]

    await repo.clear_workspace_attention(workspace_id)

    refreshed = await repo.get(workspace_id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.awaiting_human_since is None
    assert refreshed.awaiting_human_reason is None
    events = await _attention_events(session, workspace_id)
    assert [e.event_type for e in events] == [ATTENTION_REQUIRED_EVENT_TYPE]
