"""Focused service-level merge queue regressions."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.enums import AgentRuntime, TaskClass, WorkspaceStatus
from awf.db.repositories import (
    MergeCandidateRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
    sync_candidate_readiness,
)
from awf.db.session import make_session_factory
from awf.service.merge_queue import list_merge_queue_blockers_for_candidate
from tests.postgres import postgres_test_engine


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def _seed_candidate(
    session: AsyncSession,
    *,
    title: str,
    pr_number: int,
    created_at: datetime,
) -> tuple[str, str, str]:
    workspace_repo = WorkspaceRepository(session)
    workspace = await workspace_repo.create(
        repo_url="git@github.com:example/service.git",
        branch_base="development",
        task_title=title,
        task_prompt=f"Implement {title}.",
        task_external_id=f"MERGE-QUEUE-{pr_number}",
        task_class=TaskClass.test_task.value,
        owned_paths=[f"tests/unit/service/test_{pr_number}.py"],
        auto_merge=True,
        agent=AgentRuntime.codex.value,
        test_commands=["pytest -q"],
    )
    workspace.status = WorkspaceStatus.monitoring_pr.value
    workspace.branch_name = f"awf/{workspace.id}"
    workspace.remote_push_branch = workspace.branch_name
    workspace.pr_url = f"https://github.com/example/service/pull/{pr_number}"
    workspace.pr_number = pr_number

    task = await TaskRepository(session).create_or_get(
        repo_url=workspace.repo_url,
        base_branch=workspace.branch_base,
        title=workspace.task_title,
        prompt=workspace.task_prompt,
        external_id=workspace.task_external_id,
        idempotency_key=None,
        task_class=workspace.task_class,
        owned_paths=list(workspace.owned_paths),
    )
    attempt = await TaskAttemptRepository(session).create_for_workspace(
        task=task,
        workspace=workspace,
    )
    attempt.is_canonical_for_merge = True
    candidate = await MergeCandidateRepository(session).create_or_update_open_for_attempt(
        task=task,
        attempt=attempt,
        workspace=workspace,
        head_sha=f"head-{pr_number}",
        base_sha="base",
    )
    candidate.created_at = created_at
    candidate.updated_at = created_at
    await session.flush()
    return workspace.id, attempt.id, candidate.id


@pytest.mark.unit
async def test_stale_candidate_drops_out_of_merge_ready_queue_blocker_lookup(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 4, 29, 14, 0, tzinfo=UTC)
    async with factory() as session:
        await _seed_candidate(
            session,
            title="Older candidate",
            pr_number=301,
            created_at=now,
        )
        _later_workspace_id, later_attempt_id, later_candidate_id = await _seed_candidate(
            session,
            title="Later stale candidate",
            pr_number=302,
            created_at=now + timedelta(minutes=5),
        )
        later = await MergeCandidateRepository(session).get_by_attempt_id(later_attempt_id)
        assert later is not None
        later.stale = True
        later.stale_reason = "STALE_OVERLAP"
        sync_candidate_readiness(
            later,
            workspace=later.workspace,
            attempt=later.attempt,
            sync_validation_staleness=False,
        )
        await session.commit()

    async with factory() as session:
        later = await MergeCandidateRepository(session).get_by_attempt_id(later_attempt_id)
        blockers = await list_merge_queue_blockers_for_candidate(
            session,
            candidate_id=later_candidate_id,
        )

    assert later is not None
    assert later.ready is False
    assert later.stale is True
    assert blockers == []
