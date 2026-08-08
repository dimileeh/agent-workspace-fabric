"""Clear-path race and episode-fence coverage for attention events.

Covers concurrent clear lost-races and identity-map stale clears for
``src/awf/db/repositories/workspace_repo_attention.py`` (AIRA-T490).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Update, update
from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.attention_events import (
    ATTENTION_CLEARED_EVENT_TYPE,
    ATTENTION_REQUIRED_EVENT_TYPE,
    ATTENTION_SOURCE_MONITORING_PR,
)
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from tests.unit.db.test_workspace_attention_events_parts.helpers import (
    _attention_events,
    _create_workspace,
)


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
