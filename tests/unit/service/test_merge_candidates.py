"""Service-level merge candidate lifecycle tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def _seed_attempted_workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    auto_merge: bool = True,
    task_class: str | None = None,
    owned_paths: list[str] | None = None,
) -> tuple[str, str]:
    from awf.db.repositories import TaskAttemptRepository, TaskRepository

    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/service.git",
            branch_base="development",
            task_title="Candidate lifecycle",
            task_prompt="Open a PR and monitor it.",
            task_external_id="TICKET-CANDIDATE",
            task_class=task_class,
            owned_paths=list(owned_paths or []),
            auto_merge=auto_merge,
            agent=AgentRuntime.codex.value,
            test_commands=[],
        )
        task = await TaskRepository(session).create_or_get(
            repo_url=workspace.repo_url,
            base_branch=workspace.branch_base,
            title=workspace.task_title,
            prompt=workspace.task_prompt,
            external_id=workspace.task_external_id,
            idempotency_key=None,
            task_class=task_class,
            owned_paths=list(owned_paths or []),
        )
        attempt = await TaskAttemptRepository(session).create_for_workspace(
            task=task,
            workspace=workspace,
        )
        for target in (
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
        ):
            await repo.transition(workspace, to=target, reason_code="TEST")
        workspace.branch_name = f"awf/{workspace.id}"
        workspace.remote_push_branch = workspace.branch_name
        workspace.base_commit = "a" * 40
        workspace.pr_url = "https://github.com/example/service/pull/31"
        workspace.pr_number = 31
        await session.commit()
        return workspace.id, attempt.id


@pytest.mark.unit
async def test_pr_opened_monitoring_transition_creates_open_canonical_candidate(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.db.repositories import MergeCandidateRepository, TaskAttemptRepository

    workspace_id, attempt_id = await _seed_attempted_workspace(factory)

    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(
            workspace,
            to=WorkspaceStatus.monitoring_pr,
            reason_code="PR_OPENED",
        )
        await session.commit()

    async with factory() as session:
        attempt = await TaskAttemptRepository(session).get_by_workspace_id(workspace_id)
        candidate = await MergeCandidateRepository(session).get_by_attempt_id(attempt_id)

    assert attempt is not None
    assert attempt.is_canonical_for_merge is True
    assert candidate is not None
    assert candidate.status == "open"
    assert candidate.workspace_id == workspace_id
    assert candidate.attempt_id == attempt_id
    assert candidate.ready is True
    assert candidate.manual_merge_required is False
    assert candidate.waiting_for_monitor is False


@pytest.mark.unit
async def test_docs_task_non_docs_owned_path_uses_scope_stale_reason(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.db.repositories import MergeCandidateRepository, TaskAttemptRepository

    workspace_id, attempt_id = await _seed_attempted_workspace(
        factory,
        task_class="docs_task",
        owned_paths=["docs/**", "src/config.py"],
    )

    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(
            workspace,
            to=WorkspaceStatus.monitoring_pr,
            reason_code="PR_OPENED",
        )
        await session.commit()

    async with factory() as session:
        attempt = await TaskAttemptRepository(session).get_by_workspace_id(workspace_id)
        candidate = await MergeCandidateRepository(session).get_by_attempt_id(attempt_id)

    assert attempt is not None
    assert attempt.is_canonical_for_merge is True
    assert candidate is not None
    assert candidate.ready is False
    assert candidate.stale is True
    assert candidate.stale_reason == "docs_task_scope_violation"


@pytest.mark.unit
async def test_docs_task_scope_staleness_clears_when_owned_paths_return_to_docs_only(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.db.repositories import (
        MergeCandidateRepository,
        TaskAttemptRepository,
        TaskRepository,
    )

    workspace_id, attempt_id = await _seed_attempted_workspace(
        factory,
        task_class="docs_task",
        owned_paths=["docs/**", "src/config.py"],
    )

    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(
            workspace,
            to=WorkspaceStatus.monitoring_pr,
            reason_code="PR_OPENED",
        )
        await session.commit()

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        attempt = await TaskAttemptRepository(session).get_by_workspace_id(workspace_id)
        assert workspace is not None
        assert attempt is not None
        task = await TaskRepository(session).get(attempt.task_id)
        assert task is not None

        workspace.owned_paths = ["docs/**"]
        attempt.owned_paths = ["docs/**"]
        task.owned_paths = ["docs/**"]
        candidate = await MergeCandidateRepository(session).create_or_update_open_for_attempt(
            task=task,
            attempt=attempt,
            workspace=workspace,
            head_sha="b" * 40,
            base_sha="a" * 40,
        )
        await session.commit()

    async with factory() as session:
        candidate = await MergeCandidateRepository(session).get_by_attempt_id(attempt_id)

    assert candidate is not None
    assert candidate.stale is False
    assert candidate.stale_reason is None
    assert candidate.ready is True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("terminal", "expected_reason"),
    [
        (WorkspaceStatus.failed, "WORKSPACE_FAILED"),
        (WorkspaceStatus.cancelled, "WORKSPACE_CANCELLED"),
    ],
)
async def test_failed_or_cancelled_workspace_closes_open_candidate_with_reason(
    factory: async_sessionmaker[AsyncSession],
    terminal: WorkspaceStatus,
    expected_reason: str,
) -> None:
    from awf.db.repositories import MergeCandidateRepository

    workspace_id, attempt_id = await _seed_attempted_workspace(factory)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.get(workspace_id)
        assert workspace is not None
        await repo.transition(workspace, to=WorkspaceStatus.monitoring_pr, reason_code="PR_OPENED")
        if terminal == WorkspaceStatus.failed:
            workspace.failure_reason = "infrastructure_failure"
            workspace.failure_message = "monitor failed"
        await repo.transition(workspace, to=terminal, reason_code="TEST_TERMINAL")
        await session.commit()

    async with factory() as session:
        candidate = await MergeCandidateRepository(session).get_by_attempt_id(attempt_id)

    assert candidate is not None
    assert candidate.status == "closed"
    assert candidate.close_reason == expected_reason
    assert candidate.failed_or_cancelled is True
    assert candidate.ready is False
