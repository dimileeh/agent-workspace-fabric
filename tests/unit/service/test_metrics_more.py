from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.ids import new_event_id
from awf.common.workspace_policy import (
    CURSOR_AUTO_MODE_POLICY_KEY,
    cursor_auto_model_selector,
)
from awf.db.enums import CursorAutoMode, WorkspaceStatus
from awf.db.models import WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
)
from awf.service.metrics import (
    _cached_failure_details_by_workspace_id,
    _cluster_root_causes,
    _failure_details_by_workspace_id,
    _provider_likely_cause,
)
from tests.postgres import postgres_test_engine


@pytest.mark.unit
def test_provider_likely_cause_not_string():
    assert _provider_likely_cause(None) == "Provider Capacity Exhausted"


@pytest.mark.unit
async def test_cached_failure_details_by_workspace_id_none_cache(session_factory):
    async with session_factory() as session:
        # no events
        res = await _cached_failure_details_by_workspace_id(session, {}, failure_details_cache=None)
        assert res == {}


@pytest.mark.unit
async def test_failure_details_ignore_duplicate_and_empty_failed_events(session_factory):
    now = datetime(2026, 5, 9, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        duplicate_ws = await repo.create(
            repo_url="git@github.com:example/repo.git",
            branch_base="main",
            task_title="duplicate failed events",
            task_prompt="prompt",
            agent="codex",
            task_policy={},
            test_commands=[],
            owned_paths=[],
        )
        empty_ws = await repo.create(
            repo_url="git@github.com:example/repo.git",
            branch_base="main",
            task_title="empty failed event",
            task_prompt="prompt",
            agent="codex",
            task_policy={},
            test_commands=[],
            owned_paths=[],
        )
        session.add_all(
            [
                WorkspaceEvent(
                    id=new_event_id(),
                    workspace_id=duplicate_ws.id,
                    event_type="workspace.state_changed",
                    new_state=WorkspaceStatus.failed.value,
                    reason_code="NEW_REASON",
                    payload={"message": "newer failure"},
                    occurred_at=now,
                ),
                WorkspaceEvent(
                    id=new_event_id(),
                    workspace_id=duplicate_ws.id,
                    event_type="workspace.state_changed",
                    new_state=WorkspaceStatus.failed.value,
                    reason_code="OLD_REASON",
                    payload={"message": "older failure"},
                    occurred_at=now - timedelta(minutes=1),
                ),
                WorkspaceEvent(
                    id=new_event_id(),
                    workspace_id=empty_ws.id,
                    event_type="workspace.state_changed",
                    new_state=WorkspaceStatus.failed.value,
                    reason_code=None,
                    payload=None,
                    occurred_at=now,
                ),
            ]
        )
        await session.commit()

        details = await _failure_details_by_workspace_id(
            session,
            {duplicate_ws.id: None, empty_ws.id: None},
        )

    assert details == {duplicate_ws.id: {"reason_code": "NEW_REASON", "message": "newer failure"}}


@pytest.mark.unit
async def test_cluster_root_causes_causes(session_factory):
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        # Create workspaces with specific failure reasons
        for reason in [
            "AGENT_PROVIDER_CAPACITY_EXHAUSTED",
            "AGENT_AUTH_FAILED",
            AGENT_PLAN_PHASE_SCOPE_VIOLATION,
        ]:
            ws = await repo.create(
                repo_url="git@github.com:example/repo.git",
                branch_base="main",
                task_title="title",
                task_prompt="prompt",
                agent="gemini",
                task_policy={},
                test_commands=[],
                owned_paths=[],
            )
            # Add state_changed event
            await repo.transition(
                ws, to=WorkspaceStatus.failed, reason_code=reason, payload={"details": {}}
            )

        await session.commit()

    async with session_factory() as session:
        causes = await _cluster_root_causes(session, datetime.now(UTC) - timedelta(hours=1))
        # ensure it runs the lines
        assert len(causes) > 0


@pytest.mark.unit
async def test_cluster_root_causes_derives_agent_model_from_cursor_auto_mode(
    session_factory,
):
    """Cursor Auto modes persist cursor_auto_mode without agent_model; clusters
    must key on the derived Router selector so Cost/Balance/Intelligence stay
    distinct instead of merging under agent_model=null.
    """

    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        cost_ws = await repo.create(
            repo_url="git@github.com:example/repo.git",
            branch_base="main",
            task_title="cost auto failure",
            task_prompt="prompt",
            agent="cursor",
            task_policy={CURSOR_AUTO_MODE_POLICY_KEY: CursorAutoMode.cost.value},
            test_commands=[],
            owned_paths=[],
        )
        balance_ws = await repo.create(
            repo_url="git@github.com:example/repo.git",
            branch_base="main",
            task_title="balance auto failure",
            task_prompt="prompt",
            agent="cursor",
            task_policy={CURSOR_AUTO_MODE_POLICY_KEY: CursorAutoMode.balance.value},
            test_commands=[],
            owned_paths=[],
        )
        cost_id = cost_ws.id
        balance_id = balance_ws.id
        await repo.transition(
            cost_ws,
            to=WorkspaceStatus.failed,
            reason_code="AGENT_FAILURE",
            payload={"message": "boom"},
        )
        await repo.transition(
            balance_ws,
            to=WorkspaceStatus.failed,
            reason_code="AGENT_FAILURE",
            payload={"message": "boom"},
        )
        await session.commit()

    async with session_factory() as session:
        causes = await _cluster_root_causes(session, datetime.now(UTC) - timedelta(hours=1))

    by_model = {c.agent_model: c for c in causes}
    cost_selector = cursor_auto_model_selector(CursorAutoMode.cost)
    balance_selector = cursor_auto_model_selector(CursorAutoMode.balance)
    assert cost_selector in by_model
    assert balance_selector in by_model
    assert by_model[cost_selector].count == 1
    assert by_model[balance_selector].count == 1
    assert cost_id in by_model[cost_selector].sample_workspace_ids
    assert balance_id in by_model[balance_selector].sample_workspace_ids


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)
