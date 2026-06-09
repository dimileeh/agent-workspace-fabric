"""Parallel merge-candidate stale refresh regression tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.enums import AgentRuntime, OperationStatus, OperationType, TaskClass, WorkspaceStatus
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    StaleReasonRepository,
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.merge_eligibility import (
    VALIDATION_INSUFFICIENT_TIER_STALE_REASON,
    stale_reason_required_action,
)
from awf.service.merge_queue import list_merge_queue_blockers_for_candidate
from awf.service.staleness import StalenessRefreshService, TargetBranchState
from tests.postgres import postgres_test_engine

pytestmark = pytest.mark.integration

REPO_URL = "git@github.com:example/parallel-candidates.git"
BASE_BRANCH = "development"
BASE_SHA = "a" * 40
ADVANCED_SHA = "b" * 40


@dataclass(frozen=True)
class _SeededCandidate:
    workspace_id: str
    attempt_id: str
    candidate_id: str


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def _seed_monitoring_candidate(
    session: AsyncSession,
    *,
    title: str,
    pr_number: int,
    created_at: datetime,
    task_class: str,
    owned_paths: list[str],
    head_sha: str,
    successful_validation_tier: int = 1,
) -> _SeededCandidate:
    workspace_repo = WorkspaceRepository(session)
    workspace = await workspace_repo.create(
        repo_url=REPO_URL,
        branch_base=BASE_BRANCH,
        task_title=title,
        task_prompt=f"Implement {title}.",
        task_external_id=f"PARALLEL-{pr_number}",
        task_class=task_class,
        owned_paths=owned_paths,
        auto_merge=True,
        agent=AgentRuntime.codex.value,
        test_commands=["pytest -q"],
    )
    workspace.created_at = created_at
    workspace.updated_at = created_at
    workspace.branch_name = f"awf/{workspace.id}"
    workspace.remote_push_branch = workspace.branch_name
    workspace.base_commit = BASE_SHA
    workspace.monitor_last_commit_sha = head_sha
    workspace.pr_url = f"https://github.com/example/parallel-candidates/pull/{pr_number}"
    workspace.pr_number = pr_number

    task = await TaskRepository(session).create_or_get(
        repo_url=workspace.repo_url,
        base_branch=workspace.branch_base,
        title=workspace.task_title,
        prompt=workspace.task_prompt,
        external_id=workspace.task_external_id,
        idempotency_key=None,
        task_class=task_class,
        owned_paths=owned_paths,
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
        WorkspaceStatus.monitoring_pr,
    ):
        await workspace_repo.transition(workspace, to=target, reason_code="TEST")

    candidate = await MergeCandidateRepository(session).get_by_attempt_id(attempt.id)
    assert candidate is not None
    candidate.created_at = created_at
    candidate.updated_at = created_at

    validation_repo = ValidationRunRepository(session)
    validation_run = await validation_repo.start(
        workspace_id=workspace.id,
        attempt_id=attempt.id,
        tier=successful_validation_tier,
        commands=[],
        base_commit=BASE_SHA,
        base_sha=BASE_SHA,
        workspace_head_sha=head_sha,
        target_branch=BASE_BRANCH,
        target_head_sha=BASE_SHA,
        log_stream_refs={},
        started_at=created_at + timedelta(seconds=1),
    )
    await validation_repo.finish(
        validation_run.id,
        status="succeeded",
        reason_code="VALIDATION_OK",
        finished_at=created_at + timedelta(seconds=2),
    )
    await session.flush()
    return _SeededCandidate(
        workspace_id=workspace.id,
        attempt_id=attempt.id,
        candidate_id=candidate.id,
    )


async def _candidate_by_attempt(
    session: AsyncSession,
    attempt_id: str,
):
    candidate = await MergeCandidateRepository(session).get_by_attempt_id(attempt_id)
    assert candidate is not None
    return candidate


async def test_parallel_candidate_stale_after_older_merge_requires_rebase_then_fresh_validation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
    async with factory() as session:
        older = await _seed_monitoring_candidate(
            session,
            title="Older candidate",
            pr_number=101,
            created_at=now,
            task_class=TaskClass.test_task.value,
            owned_paths=["tests/integration/shared_fixture_test.py"],
            head_sha="1" * 40,
        )
        later = await _seed_monitoring_candidate(
            session,
            title="Later candidate",
            pr_number=102,
            created_at=now + timedelta(minutes=5),
            task_class=TaskClass.test_task.value,
            owned_paths=["tests/integration/shared_fixture_test.py"],
            head_sha="2" * 40,
        )
        await session.commit()

    async with factory() as session:
        blockers = await list_merge_queue_blockers_for_candidate(
            session,
            candidate_id=later.candidate_id,
        )
        later_candidate = await _candidate_by_attempt(session, later.attempt_id)
        active_reasons = await StaleReasonRepository(session).list_active_for_candidate(
            later.candidate_id,
        )

    assert [blocker.candidate_id for blocker in blockers] == [older.candidate_id]
    assert later_candidate.ready is True
    assert later_candidate.stale is False
    assert active_reasons == []

    async with factory() as session:
        candidate_repo = MergeCandidateRepository(session)
        await candidate_repo.mark_workspace_merged(older.workspace_id)
        await StalenessRefreshService(session).refresh_candidate(
            later.candidate_id,
            target=TargetBranchState(
                branch=BASE_BRANCH,
                head_sha=ADVANCED_SHA,
                changed_paths=("tests/integration/shared_fixture_test.py",),
                advanced_commits=1,
            ),
        )
        await session.commit()

    async with factory() as session:
        blockers = await list_merge_queue_blockers_for_candidate(
            session,
            candidate_id=later.candidate_id,
        )
        later_candidate = await _candidate_by_attempt(session, later.attempt_id)
        active_reasons = await StaleReasonRepository(session).list_active_for_candidate(
            later.candidate_id,
        )
        stale_events = await WorkspaceEventRepository(session).list(
            workspace_id=later.workspace_id,
            event_type="merge_candidate.stale_detected",
            limit=20,
        )

    assert blockers == []
    assert later_candidate.ready is False
    assert later_candidate.stale is True
    assert len(active_reasons) == 1
    assert active_reasons[0].reason_code == "STALE_OVERLAP"
    assert active_reasons[0].trigger_type == "path_overlap"
    assert active_reasons[0].blocks_merge is True
    assert stale_reason_required_action(active_reasons[0].reason_code) == "rebase"
    assert [event.reason_code for event in stale_events] == ["STALE_OVERLAP"]

    rebase_time = now + timedelta(minutes=10)
    async with factory() as session:
        operation = await OperationRepository(session).create(
            workspace_id=later.workspace_id,
            operation_type=OperationType.rebase,
            status=OperationStatus.succeeded,
            payload={"source": "pr_monitor", "stale_reason": "STALE_OVERLAP"},
        )
        operation.created_at = rebase_time
        operation.started_at = rebase_time
        operation.finished_at = rebase_time + timedelta(seconds=30)
        later_candidate = await _candidate_by_attempt(session, later.attempt_id)
        later_candidate.base_sha = ADVANCED_SHA
        await session.commit()

    async with factory() as session:
        await StalenessRefreshService(session).refresh_candidate(
            later.candidate_id,
            target=TargetBranchState(
                branch=BASE_BRANCH,
                head_sha=ADVANCED_SHA,
                changed_paths=(),
                advanced_commits=0,
            ),
        )
        await session.commit()

    async with factory() as session:
        later_candidate = await _candidate_by_attempt(session, later.attempt_id)
        reasons_repo = StaleReasonRepository(session)
        active_reasons = await reasons_repo.list_active_for_candidate(later.candidate_id)
        all_reasons = await reasons_repo.list_for_candidate(later.candidate_id)

    assert active_reasons == []
    assert later_candidate.ready is False
    assert later_candidate.stale is True
    assert later_candidate.stale_reason == VALIDATION_INSUFFICIENT_TIER_STALE_REASON
    assert [reason.status for reason in all_reasons] == ["resolved"]
    assert all_reasons[0].resolved_at is not None

    async with factory() as session:
        validation_repo = ValidationRunRepository(session)
        validation_run = await validation_repo.start(
            workspace_id=later.workspace_id,
            attempt_id=later.attempt_id,
            tier=2,
            commands=[],
            base_commit=ADVANCED_SHA,
            base_sha=ADVANCED_SHA,
            workspace_head_sha="3" * 40,
            target_branch=BASE_BRANCH,
            target_head_sha=ADVANCED_SHA,
            log_stream_refs={},
            started_at=rebase_time + timedelta(minutes=1),
        )
        await validation_repo.finish(
            validation_run.id,
            status="succeeded",
            reason_code="VALIDATION_OK",
            finished_at=rebase_time + timedelta(minutes=2),
        )
        await session.commit()

    async with factory() as session:
        await StalenessRefreshService(session).refresh_candidate(
            later.candidate_id,
            target=TargetBranchState(
                branch=BASE_BRANCH,
                head_sha=ADVANCED_SHA,
                changed_paths=(),
                advanced_commits=0,
            ),
        )
        await session.commit()

    async with factory() as session:
        later_candidate = await _candidate_by_attempt(session, later.attempt_id)
        blockers = await list_merge_queue_blockers_for_candidate(
            session,
            candidate_id=later.candidate_id,
        )
        reasons_repo = StaleReasonRepository(session)
        active_reasons = await reasons_repo.list_active_for_candidate(later.candidate_id)
        all_reasons = await reasons_repo.list_for_candidate(later.candidate_id)

    assert active_reasons == []
    assert [reason.status for reason in all_reasons] == ["resolved"]
    assert all_reasons[0].resolved_at is not None
    assert later_candidate.stale is False
    assert later_candidate.stale_reason is None
    assert later_candidate.ready is True
    assert blockers == []


async def test_non_overlapping_docs_and_test_target_changes_remain_ready_when_policy_allows(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    now = datetime(2026, 4, 29, 13, 0, tzinfo=UTC)
    async with factory() as session:
        docs_candidate = await _seed_monitoring_candidate(
            session,
            title="Docs candidate",
            pr_number=201,
            created_at=now,
            task_class=TaskClass.docs_task.value,
            owned_paths=["docs/user-guide.md", "docs/awf-plans/**"],
            head_sha="4" * 40,
        )
        test_candidate = await _seed_monitoring_candidate(
            session,
            title="Test candidate",
            pr_number=202,
            created_at=now + timedelta(minutes=1),
            task_class=TaskClass.test_task.value,
            owned_paths=["tests/unit/service/test_status.py"],
            head_sha="5" * 40,
        )
        await session.commit()

    async with factory() as session:
        await StalenessRefreshService(session).refresh_candidate(
            docs_candidate.candidate_id,
            target=TargetBranchState(
                branch=BASE_BRANCH,
                head_sha=ADVANCED_SHA,
                changed_paths=(
                    "tests/unit/service/test_status.py",
                    "docs/awf-plans/ws_other.md",
                ),
                advanced_commits=1,
            ),
        )
        await StalenessRefreshService(session).refresh_candidate(
            test_candidate.candidate_id,
            target=TargetBranchState(
                branch=BASE_BRANCH,
                head_sha=ADVANCED_SHA,
                changed_paths=("docs/user-guide.md",),
                advanced_commits=1,
            ),
        )
        await session.commit()

    async with factory() as session:
        reasons_repo = StaleReasonRepository(session)
        docs_active = await reasons_repo.list_active_for_candidate(docs_candidate.candidate_id)
        test_active = await reasons_repo.list_active_for_candidate(test_candidate.candidate_id)
        docs_row = await _candidate_by_attempt(session, docs_candidate.attempt_id)
        test_row = await _candidate_by_attempt(session, test_candidate.attempt_id)

    assert docs_row.ready is True
    assert docs_row.stale is False
    assert [(reason.reason_code, reason.blocks_merge) for reason in docs_active] == [
        ("ADVISORY_PLAN_ARTIFACT_OVERLAP", False)
    ]
    assert test_row.ready is True
    assert test_row.stale is False
    assert test_active == []
