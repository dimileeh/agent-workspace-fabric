"""Set-path race and episode-fence coverage for attention events.

Covers concurrent enter/refresh lost-races for
``src/awf/db/repositories/workspace_repo_attention.py`` (AIRA-T490).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Update, update
from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.attention_events import ATTENTION_REQUIRED_EVENT_TYPE
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from tests.unit.db.test_workspace_attention_events_parts.helpers import (
    _attention_events,
    _create_workspace,
)


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
async def test_set_workspace_attention_lost_enter_after_null_projection_refreshes_winner(
    session: AsyncSession,
) -> None:
    """Null projection + concurrent enter must refresh the winner's reason.

    The scalar projection can observe ``awaiting_human_since IS NULL`` while
    another session opens the episode before the guarded enter UPDATE runs.
    Enter matches zero rows with ``observed_since`` still None; the setter must
    take the stale-null-projection re-fetch branch — re-fetch the winner's
    episode start and fence the reason-only refresh to it — without emitting a
    second attention_required.
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
