"""Service-level retry/requeue tests for terminal workspaces."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import WorkspaceCreateV2Request
from awf.db.base import Base
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import Operation, Task, TaskAttempt, WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
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


def _request(*, task_kind: str = "feature_branch_pr") -> WorkspaceCreateV2Request:
    return WorkspaceCreateV2Request(
        repo={"url": "git@github.com:example/retryable.git", "base_branch": "development"},
        task={
            "title": "Retry flaky validation",
            "prompt": "Fix the intermittent validation failure.",
            "agent": "codex",
            "kind": task_kind,
            "external_id": "TICKET-RETRY",
            "task_class": "test_task",
            "owned_paths": ["src/awf/retry/**"],
            "auto_merge": False,
            "initial_review_grace_period_seconds": 30,
        },
        workspace={"profile_ref": "python", "profile": None},
        validation={"commands": ["uv run pytest tests/unit -q"], "requested_tier": 2},
        resources={},
    )


async def _mark_failed(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    branch_name: str = "codex/old-attempt",
    remote_push_branch: str | None = None,
) -> dict[str, object]:
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST")
        workspace.failure_reason = "validation_failure"
        workspace.failure_message = "pytest failed"
        workspace.branch_name = branch_name
        workspace.remote_push_branch = remote_push_branch
        workspace.pr_url = "https://github.com/example/retryable/pull/10"
        workspace.compose_project_name = "awf_old_attempt"
        assert workspace.resolved_profile is not None
        frozen_profile = {
            **workspace.resolved_profile,
            "source": "frozen:test-profile",
        }
        workspace.resolved_profile = frozen_profile
        await repo.transition(workspace, to=WorkspaceStatus.failed, reason_code="TEST_FAIL")
        await session.commit()
        return frozen_profile


@pytest.mark.unit
async def test_retry_failed_workspace_clones_v2_metadata_and_increments_attempt(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    first = await service.create_v2(_request())
    frozen_profile = await _mark_failed(factory, first.id)

    retry = await service.retry_workspace(first.id)

    async with factory() as session:
        original = await WorkspaceRepository(session).get(first.id)
        retried = await WorkspaceRepository(session).get(retry.new_workspace_id)
        tasks = list((await session.execute(select(Task))).scalars())
        attempts = list(
            (
                await session.execute(
                    select(TaskAttempt).order_by(TaskAttempt.attempt_number.asc())
                )
            ).scalars()
        )
        operations = list(
            (
                await session.execute(
                    select(Operation).where(Operation.workspace_id == first.id)
                )
            ).scalars()
        )
        retry_events = list(
            (
                await session.execute(
                    select(WorkspaceEvent).where(
                        WorkspaceEvent.event_type.in_(
                            ["workspace.retry_requested", "workspace.retry_created"]
                        )
                    )
                )
            ).scalars()
        )

    assert original is not None
    assert retried is not None
    assert retry.source_workspace_id == first.id
    assert retry.new_workspace_id != first.id
    assert retry.status == WorkspaceStatus.requested
    assert retry.attempt_number == 2

    assert retried.status == WorkspaceStatus.requested.value
    assert retried.repo_url == original.repo_url
    assert retried.branch_base == original.branch_base
    assert retried.task_title == original.task_title
    assert retried.task_prompt == original.task_prompt
    assert retried.task_external_id == original.task_external_id
    assert retried.task_class == original.task_class
    assert retried.owned_paths == original.owned_paths
    assert retried.auto_merge is False
    assert retried.initial_review_grace_period_seconds == 30
    assert retried.agent == AgentRuntime.codex.value
    assert retried.profile_ref == "python"
    assert retried.resolved_profile == frozen_profile
    assert retried.test_commands == ["uv run pytest tests/unit -q"]
    assert retried.failure_reason is None
    assert retried.failure_message is None
    assert retried.pr_url is None
    assert retried.compose_project_name is None

    assert len(tasks) == 1
    assert [attempt.workspace_id for attempt in attempts] == [first.id, retried.id]
    assert [attempt.attempt_number for attempt in attempts] == [1, 2]
    assert {attempt.task_id for attempt in attempts} == {tasks[0].id}

    assert len(operations) == 1
    assert operations[0].type == "retry"
    assert operations[0].status == "succeeded"
    assert operations[0].payload == {"source_workspace_id": first.id}
    assert operations[0].result == {
        "new_workspace_id": retried.id,
        "attempt_number": 2,
        "status": "requested",
    }

    assert {
        (event.workspace_id, event.event_type, event.payload["source_workspace_id"])
        for event in retry_events
        if event.payload is not None
    } == {
        (first.id, "workspace.retry_requested", first.id),
        (retried.id, "workspace.retry_created", first.id),
    }


@pytest.mark.unit
async def test_retry_preserves_remote_push_branch_for_sync_workspace(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)
    first = await service.create_v2(_request(task_kind="sync_release_pr"))
    await _mark_failed(
        factory,
        first.id,
        branch_name="release-sync/ws_old",
        remote_push_branch="development",
    )

    retry = await service.retry_workspace(first.id)

    async with factory() as session:
        repo = WorkspaceRepository(session)
        original = await repo.get(first.id)
        retried = await repo.get(retry.new_workspace_id)

    assert original is not None
    assert retried is not None
    assert original.task_kind == "sync_release_pr"
    assert original.branch_name == "release-sync/ws_old"
    assert original.remote_push_branch == "development"

    assert retried.task_kind == "sync_release_pr"
    assert retried.branch_name is None
    assert retried.remote_push_branch == "development"


@pytest.mark.unit
async def test_retry_persists_task_kind_without_post_insert_update() -> None:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = make_session_factory(engine)
    service = WorkspaceService(factory)
    first = await service.create_v2(_request(task_kind="sync_release_pr"))
    await _mark_failed(factory, first.id)

    statements: list[str] = []

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

    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        await service.retry_workspace(first.id)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)
        await engine.dispose()

    task_kind_updates = [
        statement
        for statement in statements
        if statement.startswith("update workspaces") and "task_kind" in statement
    ]
    assert task_kind_updates == []
