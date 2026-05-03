"""Service-level task-attempt persistence tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import WorkspaceCreateV2Request
from awf.db.base import Base
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.session import make_engine, make_session_factory
from awf.service.workspaces import WorkspaceService


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


def _request(
    *,
    external_id: str | None = "TICKET-123",
    title: str = "Add task attempts",
    repo_url: str = "git@github.com:example/app.git",
    base_branch: str = "development",
    owned_paths: list[str] | None = None,
) -> WorkspaceCreateV2Request:
    return WorkspaceCreateV2Request(
        repo={"url": repo_url, "base_branch": base_branch},
        task={
            "title": title,
            "prompt": "Persist first-class task attempts.",
            "agent": "codex",
            "kind": "feature_branch_pr",
            "external_id": external_id,
            "task_class": "refactor_task",
            "owned_paths": ["src/awf/service/**"] if owned_paths is None else owned_paths,
        },
        workspace={"profile_ref": "auto", "profile": None},
        validation={"commands": ["uv run pytest -q"], "requested_tier": 1},
        resources={},
        preflight={
            "provider_readiness_override": True,
            "provider_readiness_override_reason": "task attempt test fixture",
        },
    )


@pytest.mark.unit
async def test_create_v2_creates_task_and_attempt(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.db.models import Task, TaskAttempt

    service = WorkspaceService(factory)

    created = await service.create_v2(_request())

    async with factory() as session:
        tasks = list((await session.execute(select(Task))).scalars())
        attempts = list((await session.execute(select(TaskAttempt))).scalars())

    assert len(tasks) == 1
    assert tasks[0].external_id == "TICKET-123"
    assert tasks[0].repo_url == "git@github.com:example/app.git"
    assert tasks[0].base_branch == "development"
    assert tasks[0].title == "Add task attempts"
    assert tasks[0].task_class == "refactor_task"
    assert tasks[0].owned_paths == ["src/awf/service/**"]

    assert len(attempts) == 1
    assert attempts[0].task_id == tasks[0].id
    assert attempts[0].workspace_id == created.id
    assert attempts[0].attempt_number == 1
    assert attempts[0].agent == AgentRuntime.codex.value
    assert attempts[0].status == WorkspaceStatus.requested.value
    assert attempts[0].repo_url == "git@github.com:example/app.git"
    assert attempts[0].base_branch == "development"
    assert attempts[0].title == "Add task attempts"


@pytest.mark.unit
async def test_create_v2_reuses_task_and_increments_attempt_for_same_external_id(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.db.models import Task, TaskAttempt

    service = WorkspaceService(factory)

    first = await service.create_v2(_request(title="same backlog slice", owned_paths=[]))
    second = await service.create_v2(_request(title="same backlog slice", owned_paths=[]))

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
    assert [attempt.attempt_number for attempt in attempts] == [1, 2]
    assert [attempt.workspace_id for attempt in attempts] == [first.id, second.id]
    assert {attempt.task_id for attempt in attempts} == {tasks[0].id}


@pytest.mark.unit
async def test_create_v2_rejects_external_id_reuse_for_different_owned_paths(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.db.repositories import TaskExternalIdConflictError

    service = WorkspaceService(factory)

    await service.create_v2(_request(external_id="WAVE-1", owned_paths=["docs/**"]))

    with pytest.raises(TaskExternalIdConflictError) as excinfo:
        await service.create_v2(
            _request(
                external_id="WAVE-1",
                title="different backlog slice",
                owned_paths=["src/awf/api/**"],
            )
        )

    assert excinfo.value.external_id == "WAVE-1"
    assert "already belongs to a different task scope" in str(excinfo.value)


@pytest.mark.unit
async def test_create_v2_rejects_external_id_reuse_for_different_title(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.db.repositories import TaskExternalIdConflictError

    service = WorkspaceService(factory)

    await service.create_v2(
        _request(
            external_id="WAVE-1",
            title="docs(onboarding): add prompts",
            owned_paths=["docs/**", "README.md"],
        )
    )

    with pytest.raises(TaskExternalIdConflictError) as excinfo:
        await service.create_v2(
            _request(
                external_id="WAVE-1",
                title="docs(install): document local install",
                owned_paths=["docs/**", "README.md"],
            )
        )

    assert excinfo.value.external_id == "WAVE-1"
    assert "already belongs to a different task scope" in str(excinfo.value)
