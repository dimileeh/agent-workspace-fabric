from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.base import Base
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
)
from awf.service.metrics import (
    _cached_failure_details_by_workspace_id,
    _cluster_root_causes,
    _provider_likely_cause,
)


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
async def test_cluster_root_causes_causes(session_factory):
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        # Create workspaces with specific failure reasons
        for reason in [
            "AGENT_PROVIDER_CAPACITY_EXHAUSTED",
            "AGENT_AUTH_FAILED",
            AGENT_PLAN_PHASE_SCOPE_VIOLATION
        ]:
            ws = await repo.create(
                repo_url="git@github.com:example/repo.git",
                branch_base="main",
                task_title="title",
                task_prompt="prompt",
                agent="gemini",
                task_policy={},
                test_commands=[],
                owned_paths=[]
            )
            # Add state_changed event
            await repo.transition(ws, to=WorkspaceStatus.failed, reason_code=reason, payload={"details": {}})

        await session.commit()

    async with session_factory() as session:
        causes = await _cluster_root_causes(session, datetime.now(UTC) - timedelta(hours=1))
        # ensure it runs the lines
        assert len(causes) > 0


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()
