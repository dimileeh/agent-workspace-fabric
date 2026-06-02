"""Planning-scope workspace retry tests split from test_workspace_retry."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.models import Operation, WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.workspaces import WorkspaceService
from tests.postgres import postgres_test_engine
from tests.unit.service.test_workspace_retry import (
    _mark_planning_scope_failed,
    _request,
    _retry_with_preflight_override,
)

pytestmark = pytest.mark.unit


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def test_retry_planning_scope_violation_applies_only_approved_fallback_model(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    first = await service.create(_request())
    await _mark_planning_scope_failed(
        factory,
        first.id,
        approved_fallback_model="gpt-5.5",
    )

    retry = await _retry_with_preflight_override(service, first.id)

    async with factory() as session:
        retried = await WorkspaceRepository(session).get(retry.new_workspace_id)
        operations = list(
            (
                await session.execute(
                    select(Operation).where(Operation.workspace_id == retry.new_workspace_id)
                )
            ).scalars()
        )
        retry_created = list(
            (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.workspace_id == retry.new_workspace_id,
                        WorkspaceEvent.event_type == "workspace.retry_created",
                    )
                )
            ).scalars()
        )

    assert retried is not None
    assert retried.task_policy["agent_model"] == "gpt-5.5"
    assert operations[0].payload["fallback_model"] == {
        "model": "gpt-5.5",
        "source": "task_policy.planning_scope_recovery.approved_fallback_model",
    }
    assert operations[0].result["fallback_model"]["model"] == "gpt-5.5"
    assert retry_created[0].payload["fallback_model"]["model"] == "gpt-5.5"
