"""Merge queue ordering policy tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.base import Base
from awf.db.enums import AgentRuntime, OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_engine, make_session_factory
from awf.service.merge_queue import list_merge_queue_blockers_for_candidate


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


async def _seed_candidate(
    session: AsyncSession,
    *,
    title: str,
    pr_number: int,
    created_at: datetime,
    status: WorkspaceStatus = WorkspaceStatus.monitoring_pr,
    repo_url: str = "git@github.com:example/service.git",
    base_branch: str = "development",
    canonical: bool = True,
) -> tuple[str, str, str]:
    workspace_repo = WorkspaceRepository(session)
    workspace = await workspace_repo.create(
        repo_url=repo_url,
        branch_base=base_branch,
        task_title=title,
        task_prompt=f"Implement {title}.",
        task_external_id=f"QUEUE-{pr_number}",
        auto_merge=True,
        agent=AgentRuntime.codex.value,
        test_commands=[],
    )
    workspace.status = status.value
    workspace.branch_name = f"awf/{workspace.id}"
    workspace.remote_push_branch = workspace.branch_name
    workspace.pr_url = f"https://github.com/example/service/pull/{pr_number}"
    workspace.pr_number = pr_number

    task = await TaskRepository(session).create_or_get(
        repo_url=repo_url,
        base_branch=base_branch,
        title=title,
        prompt=f"Implement {title}.",
        external_id=f"QUEUE-{pr_number}",
        idempotency_key=None,
        task_class=None,
        owned_paths=[],
    )
    attempt = await TaskAttemptRepository(session).create_for_workspace(
        task=task,
        workspace=workspace,
    )
    attempt.is_canonical_for_merge = canonical
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
@pytest.mark.parametrize(
    ("older_status", "operation_payload", "expected_state"),
    [
        (WorkspaceStatus.monitoring_pr, None, "merge_eligible"),
        (
            WorkspaceStatus.ready,
            {"source": "pr_monitor", "reason": "validation_insufficient_tier"},
            "monitor_owned_recovery",
        ),
    ],
)
async def test_older_open_candidate_blocks_later_same_repo_base_candidate(
    factory: async_sessionmaker[AsyncSession],
    older_status: WorkspaceStatus,
    operation_payload: dict[str, str] | None,
    expected_state: str,
) -> None:
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    async with factory() as session:
        older_workspace_id, _older_attempt_id, older_candidate_id = await _seed_candidate(
            session,
            title="Older candidate",
            pr_number=11,
            created_at=now,
            status=older_status,
        )
        _later_workspace_id, _later_attempt_id, later_candidate_id = await _seed_candidate(
            session,
            title="Later candidate",
            pr_number=12,
            created_at=now + timedelta(minutes=5),
        )
        if operation_payload is not None:
            await OperationRepository(session).create(
                workspace_id=older_workspace_id,
                operation_type=OperationType.validate,
                status=OperationStatus.pending,
                payload=operation_payload,
            )
        await session.commit()

    async with factory() as session:
        blockers = await list_merge_queue_blockers_for_candidate(
            session,
            candidate_id=later_candidate_id,
        )

    assert len(blockers) == 1
    assert blockers[0].candidate_id == older_candidate_id
    assert blockers[0].workspace_id == older_workspace_id
    assert blockers[0].blocker_state == expected_state
    assert blockers[0].reason_code == "MERGE_QUEUE_WAITING_FOR_OLDER_CANDIDATE"


@pytest.mark.unit
async def test_non_monitor_recovery_operation_does_not_block_later_candidate(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    async with factory() as session:
        older_workspace_id, _older_attempt_id, _older_candidate_id = await _seed_candidate(
            session,
            title="Older manual recovery",
            pr_number=13,
            created_at=now,
            status=WorkspaceStatus.ready,
        )
        _later_workspace_id, _later_attempt_id, later_candidate_id = await _seed_candidate(
            session,
            title="Later candidate",
            pr_number=14,
            created_at=now + timedelta(minutes=5),
        )
        await OperationRepository(session).create(
            workspace_id=older_workspace_id,
            operation_type=OperationType.validate,
            status=OperationStatus.pending,
            payload={"source": "operator", "reason": "manual_validation"},
        )
        await session.commit()

    async with factory() as session:
        blockers = await list_merge_queue_blockers_for_candidate(
            session,
            candidate_id=later_candidate_id,
        )

    assert blockers == []


@pytest.mark.unit
@pytest.mark.parametrize("clearing_state", ["merged", "closed", "non_canonical"])
async def test_blocker_clears_when_older_candidate_is_not_open_canonical(
    factory: async_sessionmaker[AsyncSession],
    clearing_state: str,
) -> None:
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    async with factory() as session:
        older_workspace_id, older_attempt_id, _older_candidate_id = await _seed_candidate(
            session,
            title="Older candidate",
            pr_number=21,
            created_at=now,
        )
        _later_workspace_id, _later_attempt_id, later_candidate_id = await _seed_candidate(
            session,
            title="Later candidate",
            pr_number=22,
            created_at=now + timedelta(minutes=5),
        )
        candidate_repo = MergeCandidateRepository(session)
        if clearing_state == "merged":
            await candidate_repo.mark_workspace_merged(older_workspace_id)
        elif clearing_state == "closed":
            await candidate_repo.close_open_for_workspace(
                older_workspace_id,
                close_reason="TEST_CLOSED",
            )
        else:
            attempt = await TaskAttemptRepository(session).get_by_workspace_id(
                older_workspace_id,
            )
            assert attempt is not None
            assert attempt.id == older_attempt_id
            attempt.is_canonical_for_merge = False
        await session.commit()

    async with factory() as session:
        blockers = await list_merge_queue_blockers_for_candidate(
            session,
            candidate_id=later_candidate_id,
        )

    assert blockers == []


@pytest.mark.unit
async def test_candidates_on_other_repo_or_base_do_not_block(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    async with factory() as session:
        await _seed_candidate(
            session,
            title="Other repo",
            pr_number=31,
            created_at=now,
            repo_url="git@github.com:example/other.git",
        )
        await _seed_candidate(
            session,
            title="Other base",
            pr_number=32,
            created_at=now + timedelta(minutes=1),
            base_branch="main",
        )
        _later_workspace_id, _later_attempt_id, later_candidate_id = await _seed_candidate(
            session,
            title="Later candidate",
            pr_number=33,
            created_at=now + timedelta(minutes=5),
        )
        await session.commit()

    async with factory() as session:
        blockers = await list_merge_queue_blockers_for_candidate(
            session,
            candidate_id=later_candidate_id,
        )

    assert blockers == []
