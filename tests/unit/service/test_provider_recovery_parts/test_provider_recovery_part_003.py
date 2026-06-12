from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import WorkspaceCreateRequest
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.provider_recovery import create_provider_recovery_attempt_row
from awf.service.workspaces import WorkspaceService
from tests.postgres import postgres_test_engine

"""Provider/model recovery policy and fallback attempt tests."""


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_provider_recovery_preserves_source_task_tag(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """A tagged source workspace must keep its task_tag on the recovery clone."""
    service = WorkspaceService(factory)
    request = WorkspaceCreateRequest(
        repo={"url": "git@github.com:example/provider.git", "base_branch": "development"},
        task={
            "title": "Recover tagged provider outage",
            "prompt": "Implement the provider recovery behavior.",
            "agent": "gemini",
            "model": "gemini-2.5-pro",
            "external_id": "PROVIDER-TAG-1",
            "task_tag": "PROJ-77",
            "task_class": "test_task",
            "owned_paths": ["src/awf/provider/**"],
            "auto_merge": False,
            "initial_review_grace_period_seconds": 45,
        },
        workspace={"profile_ref": "python", "profile": None},
        validation={"commands": ["uv run pytest tests/unit -q"], "requested_tier": 2},
        resources={},
        preflight={
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "provider recovery test fixture",
        },
    )
    source_response = await service.create(request)

    async with factory() as session:
        result = await create_provider_recovery_attempt_row(
            session,
            source_response.id,
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
            metadata={"reason_code": "AGENT_TIMEOUT", "retryable": True},
        )
        await session.commit()

    assert result is not None
    assert result.action == "retry"
    async with factory() as session:
        repo = WorkspaceRepository(session)
        retried = await repo.get(result.new_workspace_id)
        assert retried is not None
        assert retried.task_tag == "PROJ-77"
