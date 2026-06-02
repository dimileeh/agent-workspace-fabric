"""Legacy workspace retry tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import Task, TaskAttempt
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.service.workspaces import WorkspaceService
from tests.postgres import postgres_test_engine

pytestmark = pytest.mark.unit


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def _retry_with_preflight_override(
    service: WorkspaceService,
    workspace_id: str,
) -> object:
    return await service.retry_workspace(
        workspace_id,
        provider_readiness_override=True,
        provider_readiness_override_reason="retry service test fixture",
    )


async def test_retry_legacy_workspace_without_attempt_reuses_fallback_task(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        source = await repo.create(
            repo_url="git@github.com:example/retryable.git",
            branch_base="development",
            task_title="Retry legacy validation",
            task_prompt="Fix a legacy workspace without task attempts.",
            task_external_id=None,
            task_class="test_task",
            owned_paths=[],
            auto_merge=False,
            initial_review_grace_period_seconds=30,
            agent=AgentRuntime.codex.value,
            profile_ref="python",
            requested_profile={"source": "legacy-test-profile"},
            resolved_profile={"source": "legacy-test-profile"},
            test_commands=["uv run pytest tests/unit -q"],
        )
        await repo.transition(source, to=WorkspaceStatus.provisioning, reason_code="TEST")
        await repo.transition(source, to=WorkspaceStatus.failed, reason_code="TEST_FAIL")
        await session.commit()
        source_id = source.id

    service = WorkspaceService(factory)
    first_retry = await _retry_with_preflight_override(service, source_id)
    second_retry = await _retry_with_preflight_override(service, source_id)

    async with factory() as session:
        tasks = list((await session.execute(select(Task))).scalars())
        attempts = list(
            (
                await session.execute(
                    select(TaskAttempt).order_by(TaskAttempt.attempt_number.asc())
                )
            ).scalars()
        )

    assert len(tasks) == 1
    assert tasks[0].idempotency_key == f"retry-source-workspace:{source_id}"
    assert [attempt.workspace_id for attempt in attempts] == [
        first_retry.new_workspace_id,
        second_retry.new_workspace_id,
    ]
    assert [attempt.attempt_number for attempt in attempts] == [1, 2]
    assert {attempt.task_id for attempt in attempts} == {tasks[0].id}
