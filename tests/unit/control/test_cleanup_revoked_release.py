"""Regression tests for cleanup worker revocation-aware candidate listing.

When ``stop_project_containers`` fails during the provisioner's orphan
cleanup (``_launch_lost_to_terminal_cleanup``), the provisioner records a
``terminal_runtime_release_revoked`` event.  Before this fix, the cleanup
worker's candidate query used a simple existence check for any
``terminal_runtime_released`` event, so a revoked release prevented the
workspace from ever being re-swept.  The fix makes the candidate query
compare release and revocation timestamps so that a revoked release
re-includes the workspace in the sweep.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import WorkerConfig
from awf.control.worker import cleanup as worker_cleanup
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.db.repositories.base import (
    TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
    TERMINAL_RUNTIME_RELEASE_REASON_CODE,
    TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
    TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
)
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine

_H2 = "git@github.com:example/revoked-release.git"
_B = "main"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def _make_workspace(
    session: AsyncSession,
    repo: WorkspaceRepository,
    *,
    status: WorkspaceStatus = WorkspaceStatus.destroyed,
    compose_project_name: str | None = "awf_ws_revoked_test",
    node_id: str = "node-1",
) -> Workspace:
    ws = await repo.create(
        repo_url=_H2,
        branch_base=_B,
        task_title="revoked release regression",
        task_prompt="test",
        agent="codex",
        test_commands=[],
        task_policy={},
    )
    ws.status = status.value
    if compose_project_name is not None:
        ws.compose_project_name = compose_project_name
    ws.node_id = node_id
    await session.commit()
    return ws


def _sweeper(factory: async_sessionmaker[AsyncSession], node_id: str = "node-1"):

    cfg = WorkerConfig(node_id=node_id)
    return type(
        "Sweeper",
        (),
        {
            "_config": cfg,
            "_session_factory": factory,
            "_runtime_cleaner": None,
            "_log_transient_db_retry": lambda *_: None,
            "_list_terminal_runtime_candidates": worker_cleanup._list_terminal_runtime_candidates,
            "_has_terminal_runtime_release_event": worker_cleanup._has_terminal_runtime_release_event,
        },
    )()


@pytest.mark.asyncio
async def test_revoked_release_reincludes_workspace_in_candidate_list(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sweeper = _sweeper(factory)
    ws_id: str | None = None
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        ws_id = ws.id
        await repo.add_event(
            ws,
            event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
            reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
        )
        released_ev = (
            await session.execute(
                sa.select(WorkspaceEvent)
                .where(WorkspaceEvent.workspace_id == ws.id)
                .where(WorkspaceEvent.event_type == TERMINAL_RUNTIME_RELEASE_EVENT_TYPE)
            )
        ).scalar_one()
        released_ev.occurred_at = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)

        await repo.add_event(
            ws,
            event_type=TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
            reason_code=TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
        )
        revoked_ev = (
            await session.execute(
                sa.select(WorkspaceEvent)
                .where(WorkspaceEvent.workspace_id == ws.id)
                .where(WorkspaceEvent.event_type == TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE)
            )
        ).scalar_one()
        revoked_ev.occurred_at = datetime(2026, 5, 31, 12, 0, 1, tzinfo=UTC)
        await session.commit()

    candidates = await sweeper._list_terminal_runtime_candidates()  # noqa: SLF001
    candidate_ids = [c.workspace_id for c in candidates]
    assert ws_id in candidate_ids, (
        "workspace with revoked release must be re-included in candidate sweep"
    )


@pytest.mark.asyncio
async def test_effective_release_excludes_workspace_from_candidate_list(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sweeper = _sweeper(factory)
    ws_id: str | None = None
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        ws_id = ws.id
        await repo.add_event(
            ws,
            event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
            reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
        )
        await session.commit()

    candidates = await sweeper._list_terminal_runtime_candidates()  # noqa: SLF001
    candidate_ids = [c.workspace_id for c in candidates]
    assert ws_id not in candidate_ids, (
        "workspace with effective (non-revoked) release must not be swept again"
    )


@pytest.mark.asyncio
async def test_has_terminal_runtime_release_event_returns_false_when_revoked_newer(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sweeper = _sweeper(factory)
    ws_id: str | None = None
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        ws_id = ws.id
        await repo.add_event(
            ws,
            event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
            reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
        )
        released_ev = (
            await session.execute(
                sa.select(WorkspaceEvent)
                .where(WorkspaceEvent.workspace_id == ws.id)
                .where(WorkspaceEvent.event_type == TERMINAL_RUNTIME_RELEASE_EVENT_TYPE)
            )
        ).scalar_one()
        released_ev.occurred_at = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)

        await repo.add_event(
            ws,
            event_type=TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
            reason_code=TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
        )
        revoked_ev = (
            await session.execute(
                sa.select(WorkspaceEvent)
                .where(WorkspaceEvent.workspace_id == ws.id)
                .where(WorkspaceEvent.event_type == TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE)
            )
        ).scalar_one()
        revoked_ev.occurred_at = datetime(2026, 5, 31, 12, 0, 1, tzinfo=UTC)
        await session.commit()

    async with factory() as session:
        result = await sweeper._has_terminal_runtime_release_event(  # noqa: SLF001
            session,
            ws_id,
        )
    assert result is False, (
        "revoked release must cause _has_terminal_runtime_release_event to return False"
    )


@pytest.mark.asyncio
async def test_has_terminal_runtime_release_event_returns_true_when_release_newer(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    sweeper = _sweeper(factory)
    ws_id: str | None = None
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await _make_workspace(session, repo)
        ws_id = ws.id
        await repo.add_event(
            ws,
            event_type=TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE,
            reason_code=TERMINAL_RUNTIME_RELEASE_REVOKED_REASON_CODE,
        )
        revoked_ev = (
            await session.execute(
                sa.select(WorkspaceEvent)
                .where(WorkspaceEvent.workspace_id == ws.id)
                .where(WorkspaceEvent.event_type == TERMINAL_RUNTIME_RELEASE_REVOKED_EVENT_TYPE)
            )
        ).scalar_one()
        revoked_ev.occurred_at = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)

        await repo.add_event(
            ws,
            event_type=TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
            reason_code=TERMINAL_RUNTIME_RELEASE_REASON_CODE,
        )
        released_ev = (
            await session.execute(
                sa.select(WorkspaceEvent)
                .where(WorkspaceEvent.workspace_id == ws.id)
                .where(WorkspaceEvent.event_type == TERMINAL_RUNTIME_RELEASE_EVENT_TYPE)
            )
        ).scalar_one()
        released_ev.occurred_at = datetime(2026, 5, 31, 12, 0, 1, tzinfo=UTC)
        await session.commit()

    async with factory() as session:
        result = await sweeper._has_terminal_runtime_release_event(  # noqa: SLF001
            session,
            ws_id,
        )
    assert result is True, (
        "release newer than revocation must cause _has_terminal_runtime_release_event to return True"
    )
