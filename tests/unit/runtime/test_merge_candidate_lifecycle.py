"""PR monitor merge-candidate lifecycle wiring tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import ValidationRunRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import MonitorState, NotifyHuman
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
)
from tests.unit.runtime.test_pr_monitor import _status


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


async def _seed_monitoring_candidate_workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    auto_merge: bool = True,
) -> tuple[str, str]:
    from awf.db.repositories import TaskAttemptRepository, TaskRepository

    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:dimileeh/aira-web.git",
            branch_base="development",
            task_title="candidate monitor test",
            task_prompt="x",
            auto_merge=auto_merge,
            agent=AgentRuntime.claude_code.value,
            test_commands=["pytest -q"],
        )
        task = await TaskRepository(session).create_or_get(
            repo_url=workspace.repo_url,
            base_branch=workspace.branch_base,
            title=workspace.task_title,
            prompt=workspace.task_prompt,
            external_id="TICKET-MONITOR-CANDIDATE",
            idempotency_key=None,
            task_class=None,
            owned_paths=[],
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
        workspace.compose_project_name = f"awf_{workspace.id}"
        workspace.pr_url = "https://github.com/dimileeh/aira-web/pull/42"
        workspace.pr_number = 42
        await repo.transition(workspace, to=WorkspaceStatus.monitoring_pr, reason_code="PR_OPENED")
        validation_repo = ValidationRunRepository(session)
        validation_run = await validation_repo.start(
            workspace_id=workspace.id,
            attempt_id=attempt.id,
            tier=1,
            commands=[],
            base_commit=workspace.base_commit,
            target_branch=workspace.remote_push_branch,
            workspace_head_sha="abc1234567890def",
            target_head_sha="abc1234567890def",
            log_stream_refs={},
        )
        await validation_repo.finish(
            validation_run.id,
            status="succeeded",
            reason_code="VALIDATION_OK",
        )
        await session.commit()
        return workspace.id, attempt.id


@pytest.mark.unit
async def test_completed_pr_monitor_marks_candidate_merged(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    from awf.db.repositories import MergeCandidateRepository

    workspace_id, attempt_id = await _seed_monitoring_candidate_workspace(factory)
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload())  # mergeable PR state
    cmd.queue_result(returncode=0)  # gh pr merge
    cmd.queue_result(returncode=0, stdout="MERGESHA\n")  # merge SHA lookup

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
    )

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    async with factory() as session:
        candidate = await MergeCandidateRepository(session).get_by_attempt_id(attempt_id)
        workspace = await WorkspaceRepository(session).get(workspace_id)

    assert workspace is not None
    assert workspace.status == WorkspaceStatus.completed.value
    assert candidate is not None
    assert candidate.status == "merged"
    assert candidate.closed_at is None
    assert candidate.merged_at is not None
    assert candidate.completed is True
    assert candidate.ready is False


@pytest.mark.unit
async def test_completed_pr_monitor_sets_merge_sha_before_transition_hooks(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_transition = WorkspaceRepository.transition
    completed_transition_merge_shas: list[str | None] = []

    async def transition_spy(self, workspace, *, to, reason_code, payload=None):
        if to == WorkspaceStatus.completed:
            completed_transition_merge_shas.append(workspace.pr_merge_sha)
        return await original_transition(
            self,
            workspace,
            to=to,
            reason_code=reason_code,
            payload=payload,
        )

    monkeypatch.setattr(WorkspaceRepository, "transition", transition_spy)
    workspace_id, _attempt_id = await _seed_monitoring_candidate_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
    )

    await runner._terminate_completed(workspace_id, pr_merge_sha="MERGESHA")

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)

    assert completed_transition_merge_shas == ["MERGESHA"]
    assert workspace is not None
    assert workspace.pr_merge_sha == "MERGESHA"


@pytest.mark.unit
async def test_manual_merge_candidate_stays_open_until_external_merge_is_observed(
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    tmp_path: Path,
) -> None:
    from awf.db.repositories import MergeCandidateRepository

    workspace_id, attempt_id = await _seed_monitoring_candidate_workspace(
        factory,
        auto_merge=False,
    )
    cmd.queue_result(returncode=0)  # gh pr comment

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        auto_merge=False,
    )
    terminal = await runner._execute(
        action=NotifyHuman(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef.from_url("git@github.com:dimileeh/aira-web.git"),
        pr_number=42,
        status=_status(head_sha="abc1234567890def"),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    async with factory() as session:
        candidate = await MergeCandidateRepository(session).get_by_attempt_id(attempt_id)
        workspace = await WorkspaceRepository(session).get(workspace_id)

    assert terminal is False
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    assert candidate is not None
    assert candidate.status == "open"
    assert candidate.manual_merge_required is True
    assert candidate.completed is False
