"""PR monitor merge-gate tests for candidate-level merge safety."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.enums import (
    AgentRuntime,
    OperationStatus,
    OperationType,
    TaskClass,
    WorkspaceStatus,
)
from awf.db.models import MergeCandidate
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    StaleReasonCreate,
    StaleReasonRepository,
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
    WorkspaceRepository,
    sync_candidate_readiness,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import Merge, MonitorState, PRStatus
from awf.runtime.pr_monitor_operations import monitor_operation_idempotency_key
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import FakeAdapter, RecordedSleep, make_runner
from tests.unit.runtime.test_pr_monitor import _status

REPO_URL = "git@github.com:dimileeh/aira-web.git"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.fixture
def cmd() -> FakeCommandRunner:
    return FakeCommandRunner()


@pytest.fixture
def adapter() -> FakeAdapter:
    return FakeAdapter()


@pytest.fixture
def sleep_fn() -> RecordedSleep:
    return RecordedSleep()


@dataclass(frozen=True)
class CandidateSeed:
    workspace_id: str
    attempt_id: str
    candidate_id: str
    pr_number: int


class StaleBeforeMergeCoordinator:
    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        candidate_id: str,
    ) -> None:
        self._factory = factory
        self._candidate_id = candidate_id

    @asynccontextmanager
    async def serialized_merge(
        self,
        *,
        repo_url: str,
        base_branch: str,
    ) -> AsyncIterator[None]:
        del repo_url, base_branch
        async with self._factory() as session:
            candidate = await session.get(MergeCandidate, self._candidate_id)
            assert candidate is not None
            candidate.stale = True
            candidate.stale_reason = "STALE_TARGET_ADVANCED"
            await session.commit()
        yield


async def _finish_validation_run(
    validation_repo: ValidationRunRepository,
    *,
    workspace_id: str,
    attempt_id: str,
    tier: int,
    started_at: datetime,
    head_sha: str,
) -> None:
    run = await validation_repo.start(
        workspace_id=workspace_id,
        attempt_id=attempt_id,
        tier=tier,
        commands=[],
        base_commit="base",
        target_branch=f"awf/{workspace_id}",
        workspace_head_sha=head_sha,
        target_head_sha=head_sha,
        log_stream_refs={},
        started_at=started_at,
    )
    await validation_repo.finish(
        run.id,
        status="succeeded",
        reason_code="VALIDATION_OK",
        finished_at=started_at + timedelta(minutes=1),
    )


async def _seed_merge_candidate(
    factory: async_sessionmaker[AsyncSession],
    *,
    task_class: str = TaskClass.refactor_task.value,
    pr_number: int = 42,
    head_sha: str = "abc123",
    same_attempt_validation_tier: int | None = 2,
    other_attempt_validation_tier: int | None = None,
    candidate_stale_reason: str | None = None,
) -> CandidateSeed:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    async with factory() as session:
        workspace_repo = WorkspaceRepository(session)
        workspace = await workspace_repo.create(
            repo_url=REPO_URL,
            branch_base="development",
            task_title=f"Merge safety {pr_number}",
            task_prompt="Prove candidate merge safety.",
            task_external_id=f"MERGE-SAFETY-{pr_number}",
            task_class=task_class,
            auto_merge=True,
            agent=AgentRuntime.claude_code.value,
            test_commands=["pytest -q"],
            resolved_profile={"validation": {"requested_tier": 2}},
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
        workspace.branch_name = f"awf/{workspace.id}"
        workspace.remote_push_branch = workspace.branch_name
        workspace.base_commit = "a" * 40
        workspace.compose_project_name = f"awf_{workspace.id}"
        workspace.compose_file_path = "/tmp/compose.yml"
        workspace.pr_url = f"https://github.com/dimileeh/aira-web/pull/{pr_number}"
        workspace.pr_number = pr_number

        task = await TaskRepository(session).create_or_get(
            repo_url=workspace.repo_url,
            base_branch=workspace.branch_base,
            title=workspace.task_title,
            prompt=workspace.task_prompt,
            external_id=workspace.task_external_id,
            idempotency_key=None,
            task_class=workspace.task_class,
            owned_paths=[],
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
            head_sha=head_sha,
            base_sha="base",
        )

        validation_repo = ValidationRunRepository(session)
        if same_attempt_validation_tier is not None:
            await _finish_validation_run(
                validation_repo,
                workspace_id=workspace.id,
                attempt_id=attempt.id,
                tier=same_attempt_validation_tier,
                started_at=now,
                head_sha=head_sha,
            )
        if other_attempt_validation_tier is not None:
            other_workspace = await workspace_repo.create(
                repo_url=REPO_URL,
                branch_base="development",
                task_title=f"Other attempt {pr_number}",
                task_prompt="Different attempt.",
                agent=AgentRuntime.claude_code.value,
                test_commands=["pytest -q"],
            )
            other_attempt = await TaskAttemptRepository(session).create_for_workspace(
                task=task,
                workspace=other_workspace,
            )
            await _finish_validation_run(
                validation_repo,
                workspace_id=workspace.id,
                attempt_id=other_attempt.id,
                tier=other_attempt_validation_tier,
                started_at=now + timedelta(minutes=2),
                head_sha=head_sha,
            )

        sync_candidate_readiness(candidate, workspace=workspace, attempt=attempt)
        if candidate_stale_reason is not None:
            candidate.stale = True
            candidate.stale_reason = candidate_stale_reason
            sync_candidate_readiness(
                candidate,
                workspace=workspace,
                attempt=attempt,
                sync_validation_staleness=False,
            )
        await session.commit()
        return CandidateSeed(
            workspace_id=workspace.id,
            attempt_id=attempt.id,
            candidate_id=candidate.id,
            pr_number=pr_number,
        )


async def _seed_active_target_advanced_reason(
    factory: async_sessionmaker[AsyncSession],
    seed: CandidateSeed,
) -> None:
    async with factory() as session:
        candidate = await session.get(MergeCandidate, seed.candidate_id)
        assert candidate is not None
        await StaleReasonRepository(session).replace_active_findings(
            workspace_id=seed.workspace_id,
            candidate_id=seed.candidate_id,
            attempt_id=seed.attempt_id,
            task_id=candidate.task_id,
            findings=[
                StaleReasonCreate(
                    reason_code="STALE_TARGET_ADVANCED",
                    trigger_type="target_advanced",
                    trigger_ref="b" * 40,
                    explanation="Target branch advanced past this candidate.",
                )
            ],
        )
        await session.commit()


async def _execute_merge(
    *,
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
    seed: CandidateSeed,
    merge_coordinator: object | None = None,
    status: PRStatus | None = None,
) -> bool:
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
        merge_coordinator=merge_coordinator,
    )
    return await runner._execute(
        action=Merge(),
        workspace_id=seed.workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef.from_url(REPO_URL),
        pr_number=seed.pr_number,
        status=status or _status(),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{seed.workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )


@pytest.mark.unit
async def test_auto_merge_blocks_when_required_validation_is_only_on_other_attempt(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    seed = await _seed_merge_candidate(
        factory,
        pr_number=501,
        same_attempt_validation_tier=1,
        other_attempt_validation_tier=2,
    )

    terminal = await _execute_merge(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        tmp_path=tmp_path,
        seed=seed,
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(seed.workspace_id)
        operations = await OperationRepository(session).list_all(
            workspace_id=seed.workspace_id,
        )

    assert terminal is True
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.ready.value
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    recovery_operations = [op for op in operations if op.type == OperationType.validate.value]
    monitor_operations = [op for op in operations if op.type == OperationType.monitor_state.value]
    assert [(op.type, op.payload) for op in recovery_operations] == [
        (
            OperationType.validate.value,
            {
                "owner": "pr_monitor",
                "source": "pr_monitor",
                "action": "validate_only",
                "requested_action": "validate",
                "reason": "Required validation tier has not passed for this merge candidate.",
                "reason_code": "VALIDATION_INSUFFICIENT_TIER",
                "stale_reason": "validation_insufficient_tier",
                "recovery_mode": "validate_only",
                "pr_number": seed.pr_number,
                "pr_url": f"https://github.com/dimileeh/aira-web/pull/{seed.pr_number}",
                "source_head_sha": "abc123",
                "source_base_sha": "a" * 40,
                "target_branch": "development",
                "remote_branch": f"awf/{seed.workspace_id}",
            },
        )
    ]
    assert monitor_operations == []


@pytest.mark.unit
async def test_pr_166_regression_auto_merge_blocks_when_pr_head_changed_after_validation(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    seed = await _seed_merge_candidate(
        factory,
        pr_number=508,
        head_sha="pre-monitor-fix-head",
        same_attempt_validation_tier=2,
    )

    terminal = await _execute_merge(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        tmp_path=tmp_path,
        seed=seed,
        status=replace(_status(), number=508, head_sha="post-review-fix-head"),
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(seed.workspace_id)
        operations = await OperationRepository(session).list_all(
            workspace_id=seed.workspace_id,
        )

    assert terminal is True
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.ready.value
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    recovery_operations = [op for op in operations if op.type == OperationType.validate.value]
    assert len(recovery_operations) == 1
    assert recovery_operations[0].payload["reason_code"] == "VALIDATION_MISSING_FOR_CURRENT_HEAD"
    assert recovery_operations[0].payload["requested_action"] == "validate"
    assert recovery_operations[0].payload["source_head_sha"] == "post-review-fix-head"


@pytest.mark.unit
async def test_auto_merge_blocks_persisted_stale_candidate(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    seed = await _seed_merge_candidate(
        factory,
        pr_number=502,
        candidate_stale_reason="STALE_TARGET_ADVANCED",
    )

    terminal = await _execute_merge(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        tmp_path=tmp_path,
        seed=seed,
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(seed.workspace_id)
        operations = await OperationRepository(session).list_all(
            workspace_id=seed.workspace_id,
        )

    assert terminal is True
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.ready.value
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    recovery_operations = [op for op in operations if op.type == OperationType.validate.value]
    monitor_operations = [op for op in operations if op.type == OperationType.monitor_state.value]
    assert [(op.type, op.payload) for op in recovery_operations] == [
        (
            OperationType.validate.value,
            {
                "owner": "pr_monitor",
                "source": "pr_monitor",
                "action": "rebase_only",
                "requested_action": "rebase",
                "reason": "Target branch advanced after this merge candidate was validated.",
                "reason_code": "STALE_TARGET_ADVANCED",
                "stale_reason": "STALE_TARGET_ADVANCED",
                "recovery_mode": "rebase_only",
                "pr_number": seed.pr_number,
                "pr_url": f"https://github.com/dimileeh/aira-web/pull/{seed.pr_number}",
                "source_head_sha": "abc123",
                "source_base_sha": "a" * 40,
                "target_branch": "development",
                "remote_branch": f"awf/{seed.workspace_id}",
            },
        )
    ]
    assert monitor_operations == []


@pytest.mark.unit
async def test_auto_merge_materializes_active_stale_reason(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    seed = await _seed_merge_candidate(factory, pr_number=510)
    await _seed_active_target_advanced_reason(factory, seed)

    terminal = await _execute_merge(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        tmp_path=tmp_path,
        seed=seed,
    )

    async with factory() as session:
        candidate = await session.get(MergeCandidate, seed.candidate_id)
        workspace = await WorkspaceRepository(session).get(seed.workspace_id)
        operations = await OperationRepository(session).list_all(
            workspace_id=seed.workspace_id,
        )

    assert terminal is True
    assert candidate is not None
    assert candidate.stale is True
    assert candidate.stale_reason == "STALE_TARGET_ADVANCED"
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.ready.value
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    assert [(op.type, op.payload) for op in operations] == [
        (
            OperationType.validate.value,
            {
                "owner": "pr_monitor",
                "source": "pr_monitor",
                "action": "rebase_only",
                "requested_action": "rebase",
                "reason": "Target branch advanced after this merge candidate was validated.",
                "reason_code": "STALE_TARGET_ADVANCED",
                "stale_reason": "STALE_TARGET_ADVANCED",
                "recovery_mode": "rebase_only",
                "pr_number": seed.pr_number,
                "pr_url": f"https://github.com/dimileeh/aira-web/pull/{seed.pr_number}",
                "source_head_sha": "abc123",
                "source_base_sha": "a" * 40,
                "target_branch": "development",
                "remote_branch": f"awf/{seed.workspace_id}",
            },
        )
    ]


@pytest.mark.unit
async def test_auto_merge_allows_eligible_canonical_candidate(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    seed = await _seed_merge_candidate(factory, pr_number=503)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="MERGESHA\n")

    terminal = await _execute_merge(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        tmp_path=tmp_path,
        seed=seed,
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(seed.workspace_id)
        candidate = await MergeCandidateRepository(session).get_by_attempt_id(seed.attempt_id)

    assert terminal is True
    assert any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.completed.value
    assert candidate is not None
    assert candidate.id == seed.candidate_id
    assert candidate.status == "merged"


@pytest.mark.unit
async def test_auto_merge_rechecks_candidate_gate_inside_merge_lock(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    seed = await _seed_merge_candidate(factory, pr_number=504)
    coordinator = StaleBeforeMergeCoordinator(
        factory=factory,
        candidate_id=seed.candidate_id,
    )

    terminal = await _execute_merge(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        tmp_path=tmp_path,
        seed=seed,
        merge_coordinator=coordinator,
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(seed.workspace_id)
        operations = await OperationRepository(session).list_all(
            workspace_id=seed.workspace_id,
        )

    assert terminal is True
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.ready.value
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    recovery_operations = [op for op in operations if op.type == OperationType.validate.value]
    monitor_operations = [op for op in operations if op.type == OperationType.monitor_state.value]
    assert [(op.type, op.payload) for op in recovery_operations] == [
        (
            OperationType.validate.value,
            {
                "owner": "pr_monitor",
                "source": "pr_monitor",
                "action": "rebase_only",
                "requested_action": "rebase",
                "reason": "Target branch advanced after this merge candidate was validated.",
                "reason_code": "STALE_TARGET_ADVANCED",
                "stale_reason": "STALE_TARGET_ADVANCED",
                "recovery_mode": "rebase_only",
                "pr_number": seed.pr_number,
                "pr_url": f"https://github.com/dimileeh/aira-web/pull/{seed.pr_number}",
                "source_head_sha": "abc123",
                "source_base_sha": "a" * 40,
                "target_branch": "development",
                "remote_branch": f"awf/{seed.workspace_id}",
            },
        )
    ]
    assert [op.payload["action"] for op in monitor_operations] == ["merge_ready"]


@pytest.mark.unit
async def test_auto_merge_rechecks_candidate_gate_after_policy_refresh(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    seed = await _seed_merge_candidate(factory, pr_number=509)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
    )

    async def mark_stale_after_policy_refresh(
        *,
        workspace_id: str,
        changed_paths: tuple[str, ...],
    ) -> bool:
        del changed_paths
        async with factory() as session:
            candidate = await session.get(MergeCandidate, seed.candidate_id)
            assert candidate is not None
            assert candidate.workspace_id == workspace_id
            candidate.stale = True
            candidate.stale_reason = "STALE_TARGET_ADVANCED"
            await session.commit()
        return False

    runner._refresh_scope_policy_for_merge = mark_stale_after_policy_refresh  # type: ignore[method-assign]

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=seed.workspace_id,
        repo_url=REPO_URL,
        repo=RepoRef.from_url(REPO_URL),
        pr_number=seed.pr_number,
        status=_status(),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{seed.workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(seed.workspace_id)

    assert terminal is True
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.ready.value
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)


@pytest.mark.unit
async def test_auto_merge_does_not_duplicate_active_monitor_recovery(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    seed = await _seed_merge_candidate(
        factory,
        pr_number=505,
        same_attempt_validation_tier=1,
    )
    async with factory() as session:
        await OperationRepository(session).create(
            workspace_id=seed.workspace_id,
            operation_type=OperationType.validate,
            payload={
                "owner": "pr_monitor",
                "source": "pr_monitor",
                "reason": "Required validation tier has not passed for this merge candidate.",
                "reason_code": "VALIDATION_INSUFFICIENT_TIER",
                "stale_reason": "validation_insufficient_tier",
                "requested_action": "validate",
                "recovery_mode": "validate_only",
            },
        )
        await session.commit()

    terminal = await _execute_merge(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        tmp_path=tmp_path,
        seed=seed,
    )

    async with factory() as session:
        operations = await OperationRepository(session).list_all(
            workspace_id=seed.workspace_id,
        )

    assert terminal is False
    assert sleep_fn.calls == [60]
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    recovery_operations = [op for op in operations if op.type == OperationType.validate.value]
    wait_operations = [
        op
        for op in operations
        if op.type == OperationType.monitor_state.value
        and op.payload.get("reason_code") == "RECOVERY_IN_PROGRESS"
    ]
    assert len(recovery_operations) == 1
    assert len(wait_operations) == 1
    assert recovery_operations[0].payload == {
        "owner": "pr_monitor",
        "source": "pr_monitor",
        "reason": "Required validation tier has not passed for this merge candidate.",
        "reason_code": "VALIDATION_INSUFFICIENT_TIER",
        "stale_reason": "validation_insufficient_tier",
        "requested_action": "validate",
        "recovery_mode": "validate_only",
    }
    assert recovery_operations[0].status == OperationStatus.pending.value
    assert wait_operations[0].status == OperationStatus.succeeded.value
    assert wait_operations[0].payload["action"] == "recovery_wait"
    assert wait_operations[0].payload["requested_action"] == "validate"
    assert wait_operations[0].payload["wait_seconds"] == 60
    assert wait_operations[0].payload["recovery_mode"] == "validate_only"
    assert wait_operations[0].payload["stale_reason"] == "validation_insufficient_tier"
    assert wait_operations[0].result == {
        "status": "succeeded",
        "outcome": "wait_elapsed",
        "slept_seconds": 60,
    }


@pytest.mark.unit
async def test_auto_merge_dispatches_recovery_despite_active_monitor_non_recovery_operation(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    """Active non-recovery monitor work must not block stale validation recovery."""
    seed = await _seed_merge_candidate(
        factory,
        pr_number=511,
        same_attempt_validation_tier=1,
    )
    async with factory() as session:
        await OperationRepository(session).create(
            workspace_id=seed.workspace_id,
            operation_type=OperationType.comment_repair,
            status=OperationStatus.running,
            payload={
                "owner": "pr_monitor",
                "source": "pr_monitor",
                "action": "comment_repair",
                "requested_action": "address_comments",
                "reason": "Unresolved PR review comments required repair.",
                "reason_code": "COMMENT_REPAIR",
                "pr_number": seed.pr_number,
            },
        )
        await session.commit()

    terminal = await _execute_merge(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        tmp_path=tmp_path,
        seed=seed,
    )

    async with factory() as session:
        operations = await OperationRepository(session).list_all(
            workspace_id=seed.workspace_id,
        )

    assert terminal is True
    assert sleep_fn.calls == []
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    recovery_operations = [op for op in operations if op.type == OperationType.validate.value]
    wait_operations = [
        op
        for op in operations
        if op.type == OperationType.monitor_state.value
        and op.payload.get("reason_code") == "RECOVERY_IN_PROGRESS"
    ]
    assert len(recovery_operations) == 1
    assert wait_operations == []
    assert recovery_operations[0].status == OperationStatus.pending.value
    assert recovery_operations[0].payload["recovery_mode"] == "validate_only"
    assert recovery_operations[0].payload["reason_code"] == "VALIDATION_INSUFFICIENT_TIER"


@pytest.mark.unit
async def test_auto_merge_retries_failed_monitor_recovery_with_new_operation(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    seed = await _seed_merge_candidate(
        factory,
        pr_number=507,
        same_attempt_validation_tier=1,
    )
    base_key = monitor_operation_idempotency_key(
        workspace_id=seed.workspace_id,
        action="validate_only",
        pr_number=seed.pr_number,
        reason_code="VALIDATION_INSUFFICIENT_TIER",
        source_head_sha="abc123",
        source_base_sha="a" * 40,
    )
    async with factory() as session:
        repo = OperationRepository(session)
        failed = await repo.create(
            workspace_id=seed.workspace_id,
            operation_type=OperationType.validate,
            status=OperationStatus.running,
            payload={
                "owner": "pr_monitor",
                "source": "pr_monitor",
                "action": "validate_only",
                "requested_action": "validate",
                "reason": "Required validation tier has not passed for this merge candidate.",
                "reason_code": "VALIDATION_INSUFFICIENT_TIER",
                "stale_reason": "validation_insufficient_tier",
                "recovery_mode": "validate_only",
                "pr_number": seed.pr_number,
                "pr_url": f"https://github.com/dimileeh/aira-web/pull/{seed.pr_number}",
                "source_head_sha": "abc123",
                "source_base_sha": "a" * 40,
                "target_branch": "development",
                "remote_branch": f"awf/{seed.workspace_id}",
            },
            idempotency_key=base_key,
        )
        await repo.finish(
            failed,
            status=OperationStatus.failed,
            result={"reason_code": "COMMAND_FAILED"},
            error_code="COMMAND_FAILED",
            error_message="recovery validation failed",
        )
        await session.commit()

    terminal = await _execute_merge(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        tmp_path=tmp_path,
        seed=seed,
    )

    async with factory() as session:
        operations = await OperationRepository(session).list_all(
            workspace_id=seed.workspace_id,
        )

    assert terminal is True
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    assert len(operations) == 2
    retry = next(operation for operation in operations if operation.id != failed.id)
    assert retry.status == OperationStatus.pending.value
    assert retry.idempotency_key is not None
    assert retry.idempotency_key != base_key
    assert retry.idempotency_key.startswith("pr_monitor:validate_only:")
    assert retry.payload == {
        "owner": "pr_monitor",
        "source": "pr_monitor",
        "action": "validate_only",
        "requested_action": "validate",
        "reason": "Required validation tier has not passed for this merge candidate.",
        "reason_code": "VALIDATION_INSUFFICIENT_TIER",
        "stale_reason": "validation_insufficient_tier",
        "recovery_mode": "validate_only",
        "pr_number": seed.pr_number,
        "pr_url": f"https://github.com/dimileeh/aira-web/pull/{seed.pr_number}",
        "source_head_sha": "abc123",
        "source_base_sha": "a" * 40,
        "target_branch": "development",
        "remote_branch": f"awf/{seed.workspace_id}",
    }


@pytest.mark.unit
async def test_auto_merge_notifies_when_candidate_is_not_canonical(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    seed = await _seed_merge_candidate(factory, pr_number=506)
    async with factory() as session:
        attempt = await TaskAttemptRepository(session).get_by_workspace_id(seed.workspace_id)
        assert attempt is not None
        attempt.is_canonical_for_merge = False
        await session.commit()
    cmd.queue_result(returncode=0)

    terminal = await _execute_merge(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        tmp_path=tmp_path,
        seed=seed,
    )

    assert terminal is False
    assert sleep_fn.calls == [60]
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    assert any(call.args[:3] == ["gh", "pr", "comment"] for call in cmd.calls)


@pytest.mark.unit
async def test_auto_merge_notifies_when_scope_policy_blocks_candidate(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    seed = await _seed_merge_candidate(factory, pr_number=507)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(seed.workspace_id)
        assert workspace is not None
        workspace.owned_paths = ["src/owned.py"]
        workspace.task_policy = {
            "out_of_scope_changes": {
                "mode": "block",
            },
        }
        await session.commit()
    cmd.queue_result(returncode=0)
    status = replace(_status(), changed_paths=("src/outside.py",))

    terminal = await _execute_merge(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        tmp_path=tmp_path,
        seed=seed,
        status=status,
    )

    assert terminal is False
    assert sleep_fn.calls == [60]
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    assert any(call.args[:3] == ["gh", "pr", "comment"] for call in cmd.calls)


@pytest.mark.unit
async def test_merge_gate_reports_persisted_policy_blocker(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    seed = await _seed_merge_candidate(factory, pr_number=508)
    async with factory() as session:
        candidate = await session.get(MergeCandidate, seed.candidate_id)
        assert candidate is not None
        candidate.policy_blocked = True
        await session.commit()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
    )

    gate = await runner._merge_gate_for_workspace(seed.workspace_id, check_policy=True)

    assert gate.notify_message is not None
    assert "OUT_OF_SCOPE_CHANGE" in gate.notify_message
