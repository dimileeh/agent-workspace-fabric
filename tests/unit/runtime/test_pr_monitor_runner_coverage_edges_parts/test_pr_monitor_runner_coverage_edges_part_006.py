"""Focused branch-coverage tests for PR monitor runner edge behavior."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.common.github_client import RepoRef
from awf.control.quality_gates import QualityGateViolation
from awf.db.enums import OperationStatus, OperationType, TaskClass, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    OperationRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckFailure,
    CheckState,
    CheckTiming,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReportCiFailure,
    ReviewComment,
    ReviewThread,
)
from awf.runtime.pr_monitor_runner import (
    PullRequestMonitorRunner,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _changed_paths_from_name_only_z,
    _changed_paths_from_name_status_z,
)
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
)
from awf.service.merge_queue import MergeQueueBlocker
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _status_for_helpers(
    *,
    head_sha: str = "abc1234567890def",
    threads: tuple[ReviewThread, ...] = (),
    reviews: tuple[ReviewComment, ...] = (),
    blocking_reviews: tuple[ReviewComment, ...] | None = None,
    checks: tuple[CheckTiming, ...] = (),
) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=threads,
        unresolved_review_comments=reviews,
        blocking_reviews=(
            tuple(review for review in reviews if review.blocks_merge)
            if blocking_reviews is None
            else blocking_reviews
        ),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        checks=checks,
    )


_PROTECTED_WORKFLOW_OLD = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
  lint:
    runs-on: ubuntu-latest
    steps:
      - name: Run ruff
        run: uv run ruff check
""".strip()
_PROTECTED_WORKFLOW_BLOCKED = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
        continue-on-error: true
""".strip()


def _queue_protected_workflow_diff(
    cmd: FakeCommandRunner,
    *,
    old_text: str = _PROTECTED_WORKFLOW_OLD,
    new_text: str = _PROTECTED_WORKFLOW_BLOCKED,
) -> None:
    cmd.queue_result(returncode=0)  # cat-file base:path
    cmd.queue_result(returncode=0, stdout=old_text)
    cmd.queue_result(returncode=0)  # cat-file HEAD:path
    cmd.queue_result(returncode=0, stdout=new_text)


class _FailingLogSink:
    async def write(self, data: str) -> None:
        del data
        raise RuntimeError("log sink unavailable")


class _RecordingLogSink:
    stream_id = "monitor.log"

    def __init__(self) -> None:
        self.lines: list[str] = []

    async def write(self, data: str) -> None:
        self.lines.append(data)


class _ExplodingRunner:
    async def run(self, args: list[str], **_kwargs: object) -> object:
        del args
        raise RuntimeError("runner unavailable")


class _CleanupFailingAdapter(FakeAdapter):
    async def run(self, **_kwargs: object) -> object:  # type: ignore[override]
        raise ComposeExecCleanupError(
            invocation_id="awf_monitor_cleanup_failed",
            source="agent",
            label="monitor",
            message="tagged process still alive",
        )


class _QueueAfterLockRunner(PullRequestMonitorRunner):
    def __init__(self, *, blocker: MergeQueueBlocker, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._blocker = blocker
        self.blocker_calls = 0

    async def _merge_queue_blockers_for_workspace(
        self,
        workspace_id: str,
    ) -> list[MergeQueueBlocker]:
        assert workspace_id
        self.blocker_calls += 1
        return [] if self.blocker_calls == 1 else [self._blocker]


class _StopAfterRetryError(RuntimeError):
    pass


class _StopAfterRetrySleep(RecordedSleep):
    async def __call__(self, seconds: float) -> None:
        await super().__call__(seconds)
        raise _StopAfterRetryError


def _retry_events(ws: Workspace) -> list:
    return [
        event
        for event in ws.events
        if event.event_type == "monitor.github_transient_error_retrying"
    ]


def _assert_committed_diff_phase_ran(
    cmd: FakeCommandRunner,
    *,
    worktree_path: Path,
    remote_branch: str,
    remote: str = "origin",
) -> None:
    call_args = [call.args for call in cmd.calls]
    assert (
        _git_worktree_command(
            worktree_path,
            "fetch",
            remote,
            f"refs/heads/{remote_branch}",
        )
        in call_args
    )
    assert (
        _git_worktree_command(
            worktree_path,
            "merge-base",
            "FETCH_HEAD",
            "HEAD",
        )
        in call_args
    )


def _git_worktree_command(worktree_path: Path, *args: str) -> list[str]:
    return ["git", "-c", f"safe.directory={worktree_path}", "-C", str(worktree_path), *args]


def _name_status_z(*paths: str) -> str:
    return "".join(f"M\0{path}\0" for path in paths)


async def _mark_refactor_task(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    auto_merge: bool,
) -> None:
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        ws.task_class = TaskClass.refactor_task.value
        ws.auto_merge = auto_merge
        await s.commit()


async def _seed_running_operation(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> str:
    async with factory() as s:
        operation = await OperationRepository(s).create(
            workspace_id=workspace_id,
            operation_type=OperationType.refresh,
            status=OperationStatus.running,
            payload={"source": "test", "keep": True},
            idempotency_key=f"op:{workspace_id}",
        )
        await s.commit()
        return operation.id


async def _update_workspace(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    **values: object,
) -> None:
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        for key, value in values.items():
            setattr(ws, key, value)
        await s.commit()


async def _force_workspace_status(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    status: WorkspaceStatus,
) -> None:
    async with factory() as s:
        await s.execute(
            sa_update(Workspace).where(Workspace.id == workspace_id).values(status=status.value)
        )
        await s.commit()


@pytest.mark.unit
async def test_protected_scope_revert_keeps_untracked_path_missing_from_remote(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=128, stderr="not in tree")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    violation = QualityGateViolation(
        path=".github/workflows/ci.yml",
        protected_pattern=".github/**",
    )

    remaining = await runner._protected_scope_violations_not_restored_to_remote_branch(
        workspace_id=workspace_id,
        status_stdout="?? .github/workflows/ci.yml\n",
        violations=[violation],
        remote_branch=f"awf/{workspace_id}",
    )

    assert remaining == [violation]


@pytest.mark.unit
async def test_protected_scope_revert_raises_when_untracked_hash_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="remote-blob\n")
    cmd.queue_result(returncode=128, stdout="", stderr="cannot hash")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    with pytest.raises(ProtectedScopeDiffError, match="hash-object"):
        await runner._protected_scope_violations_not_restored_to_remote_branch(
            workspace_id=workspace_id,
            status_stdout="?? .github/workflows/ci.yml\n",
            violations=[
                QualityGateViolation(
                    path=".github/workflows/ci.yml",
                    protected_pattern=".github/**",
                )
            ],
            remote_branch=f"awf/{workspace_id}",
        )


@pytest.mark.unit
async def test_protected_scope_revert_keeps_untracked_restore_with_mismatched_blob(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # fetch remote branch
    cmd.queue_result(returncode=0, stdout="remote-blob\n")
    cmd.queue_result(returncode=0, stdout="local-blob\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    violation = QualityGateViolation(
        path=".github/workflows/ci.yml",
        protected_pattern=".github/**",
    )

    remaining = await runner._protected_scope_violations_not_restored_to_remote_branch(
        workspace_id=workspace_id,
        status_stdout="?? .github/workflows/ci.yml\n",
        violations=[violation],
        remote_branch=f"awf/{workspace_id}",
    )

    assert remaining == [violation]
    assert [call.args for call in cmd.calls] == [
        _git_worktree_command(
            worktree,
            "fetch",
            "origin",
            f"refs/heads/awf/{workspace_id}",
        ),
        _git_worktree_command(
            worktree,
            "rev-parse",
            "--verify",
            "FETCH_HEAD:.github/workflows/ci.yml^{blob}",
        ),
        _git_worktree_command(
            worktree,
            "hash-object",
            "--path",
            ".github/workflows/ci.yml",
            "--",
            ".github/workflows/ci.yml",
        ),
    ]


@pytest.mark.unit
async def test_protected_scope_revert_keeps_tracked_diff_and_raises_on_diff_error(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    violation = QualityGateViolation(
        path=".github/workflows/ci.yml",
        protected_pattern=".github/**",
    )

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=1, stdout="", stderr="")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert await runner._protected_scope_violations_not_restored_to_remote_branch(
        workspace_id=workspace_id,
        status_stdout=" M .github/workflows/ci.yml\n",
        violations=[violation],
        remote_branch=f"awf/{workspace_id}",
    ) == [violation]

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=2, stdout="bad diff", stderr="fatal")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    with pytest.raises(ProtectedScopeDiffError, match="diff FETCH_HEAD"):
        await runner._protected_scope_violations_not_restored_to_remote_branch(
            workspace_id=workspace_id,
            status_stdout=" M .github/workflows/ci.yml\n",
            violations=[violation],
            remote_branch=f"awf/{workspace_id}",
        )


@pytest.mark.unit
async def test_ci_fix_rolls_back_instead_of_committing_verified_protected_revert(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)
        assert workspace is not None
        workspace.owned_paths = ["src/**"]
        await s.commit()

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # clean worktree before repair
    cmd.queue_result(returncode=0, stdout="abc1234567890def\n")  # operation start HEAD
    cmd.queue_result(returncode=0, stdout="")  # clean worktree: agent committed locally itself
    cmd.queue_result(returncode=0, stdout="")  # fetch remote branch for committed diff
    cmd.queue_result(returncode=0, stdout="merge-base-sha\n")
    cmd.queue_result(returncode=0, stdout=_name_status_z(".github/workflows/ci.yml", "src/fix.py"))
    _queue_protected_workflow_diff(cmd)
    cmd.queue_result(returncode=0, stdout="blocked-head-sha\n")  # attempted HEAD
    cmd.queue_result(returncode=0, stdout=_name_status_z(".github/workflows/ci.yml", "src/fix.py"))
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="HEAD is now at abc1234\n")
    cmd.queue_result(returncode=0, stdout="")
    adapter = FakeAdapter()
    adapter.queue(stdout="Committed locally.")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)

    push_result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="ci", conclusion="FAILURE", log_excerpt="failing check"),),
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
        status=_status_for_helpers(),
    )

    assert push_result.pushed is False
    assert push_result.failed is True
    assert push_result.reason_code == "PROTECTED_SCOPE_PUSH_BLOCKED"
    assert len(adapter.calls) == 1
    call_args = [call.args for call in cmd.calls]
    assert _git_worktree_command(worktree, "reset", "--hard", "abc1234567890def") in call_args
    assert not any(args[:1] == ["git"] and "commit" in args for args in call_args)
    assert not any(args[:1] == ["git"] and "push" in args for args in call_args)


@pytest.mark.unit
async def test_ci_fix_rolls_back_before_protected_revert_baseline_fetch(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)
        assert workspace is not None
        workspace.owned_paths = ["src/**"]
        await s.commit()

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # clean worktree before repair
    cmd.queue_result(returncode=0, stdout="abc1234567890def\n")  # operation start HEAD
    cmd.queue_result(returncode=0, stdout="")  # clean worktree: agent committed locally itself
    cmd.queue_result(returncode=0, stdout="")  # fetch remote branch for committed diff
    cmd.queue_result(returncode=0, stdout="merge-base-sha\n")
    cmd.queue_result(returncode=0, stdout=_name_status_z(".github/workflows/ci.yml", "src/fix.py"))
    _queue_protected_workflow_diff(cmd)
    cmd.queue_result(returncode=0, stdout="blocked-head-sha\n")  # attempted HEAD
    cmd.queue_result(returncode=0, stdout=_name_status_z(".github/workflows/ci.yml", "src/fix.py"))
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="HEAD is now at abc1234\n")
    cmd.queue_result(returncode=0, stdout="")
    adapter = FakeAdapter()
    adapter.queue(stdout="Committed locally.")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)

    push_result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="ci", conclusion="FAILURE", log_excerpt="failing check"),),
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
        status=_status_for_helpers(),
    )

    assert push_result.failed is True
    assert push_result.pushed is False
    assert push_result.reason_code == "PROTECTED_SCOPE_PUSH_BLOCKED"
    assert push_result.details is not None
    assert push_result.details["branch_restored"] is True
    call_args = [call.args for call in cmd.calls]
    assert not any(args[:1] == ["git"] and "add" in args for args in call_args)
    assert not any(args[:1] == ["git"] and "commit" in args for args in call_args)
    assert not any(args[:1] == ["git"] and "push" in args for args in call_args)
    async with factory() as s:
        events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.monitor_protected_scope_push_blocked",
            limit=10,
        )

    assert len(events) == 1
    assert events[0].reason_code == "PROTECTED_SCOPE_PUSH_BLOCKED"


@pytest.mark.unit
async def test_execute_ci_fix_diff_baseline_unavailable_terminates_with_diff_reason(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # clean worktree before repair
    cmd.queue_result(returncode=0, stdout="abc1234567890def\n")  # operation start HEAD
    cmd.queue_result(returncode=0, stdout="")  # clean worktree: agent committed locally itself
    cmd.queue_result(returncode=128, stderr="network reset")  # committed-diff baseline fetch
    adapter = FakeAdapter()
    adapter.queue(stdout="Committed locally.")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    state = MonitorState()

    terminal = await runner._execute(
        action=ReportCiFailure(
            failures=(CheckFailure(name="ci", conclusion="FAILURE", log_excerpt="failing check"),)
        ),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True
    assert state.iter_count == 0
    assert not any(call.args[:1] == ["git"] and "push" in call.args for call in cmd.calls)
    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id, limit=20)
        push_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )
        scope_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.monitor_protected_scope_push_blocked",
            limit=10,
        )

    assert workspace is not None
    assert workspace.status == WorkspaceStatus.failed.value
    assert workspace.events[-1].reason_code == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
    ci_operation = next(operation for operation in operations if operation.type == "ci_repair")
    assert ci_operation.status == OperationStatus.failed.value
    assert ci_operation.error_code == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
    assert ci_operation.result is not None
    assert ci_operation.result["outcome"] == "protected_scope_diff_unavailable"
    assert ci_operation.result["reason_code"] == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
    assert len(push_events) == 1
    assert push_events[0].reason_code == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
    assert push_events[0].payload is not None
    assert push_events[0].payload["action"] == "ci_repair_push"
    assert push_events[0].payload["outcome"] == "failed"
    assert push_events[0].payload["evidence"]["reason_code"] == ("PROTECTED_SCOPE_DIFF_UNAVAILABLE")
    assert len(scope_events) == 1
    assert scope_events[0].reason_code == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
    assert scope_events[0].payload is not None
    assert scope_events[0].payload["reason"] == "diff_baseline_unavailable"


@pytest.mark.unit
async def test_execute_ci_fix_workflow_scope_push_failure_is_terminal(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Verify CI repair workflow-scope push failures are terminal."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(
        returncode=1,
        stderr=(
            "remote: refusing to allow a Personal Access Token to create or update workflow "
            "`.github/workflows/publish.yml` without `workflow` scope"
        ),
    )
    cmd.queue_result(returncode=0)  # gh pr comment notification
    adapter = FakeAdapter()
    adapter.queue(stdout="Updated workflow repair.")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=ReportCiFailure(
            failures=(CheckFailure(name="ci", conclusion="FAILURE", log_excerpt="failing check"),)
        ),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True
    assert state.iter_count == 0
    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id, limit=20)
        push_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )

    assert workspace is not None
    assert workspace.status == WorkspaceStatus.failed.value
    ci_operation = next(operation for operation in operations if operation.type == "ci_repair")
    assert ci_operation.status == OperationStatus.failed.value
    assert ci_operation.error_code == "GITHUB_WORKFLOW_SCOPE_REQUIRED"
    assert ci_operation.result is not None
    assert ci_operation.result["outcome"] == "github_workflow_scope_required"
    assert ci_operation.result["reason_code"] == "GITHUB_WORKFLOW_SCOPE_REQUIRED"
    assert ci_operation.result["failure_evidence"]["reason_code"] == (
        "GITHUB_WORKFLOW_SCOPE_REQUIRED"
    )
    assert len(push_events) == 1
    assert push_events[0].reason_code == "GITHUB_WORKFLOW_SCOPE_REQUIRED"
    assert push_events[0].payload is not None
    assert push_events[0].payload["action"] == "ci_repair_push"
    assert push_events[0].payload["outcome"] == "failed"
    assert push_events[0].payload["evidence"]["reason_code"] == "GITHUB_WORKFLOW_SCOPE_REQUIRED"
    comment_calls = [call for call in cmd.calls if call.args[:3] == ["gh", "pr", "comment"]]
    assert len(comment_calls) == 1
    body = comment_calls[0].args[comment_calls[0].args.index("--body") + 1]
    assert "GitHub rejected the workflow-file push" in body
    assert "`workflow` scope for .github/workflows/publish.yml" in body


@pytest.mark.unit
async def test_execute_ci_fix_workflow_scope_notification_failure_still_terminates(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Notification failures must not skip terminal workflow-scope handling."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(
        returncode=1,
        stderr=(
            "remote: refusing to allow a Personal Access Token to create or update workflow "
            "`.github/workflows/publish.yml` without `workflow` scope"
        ),
    )
    cmd.queue_result(returncode=1, stderr="bad credentials")  # gh pr comment notification
    adapter = FakeAdapter()
    adapter.queue(stdout="Updated workflow repair.")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=ReportCiFailure(
            failures=(CheckFailure(name="ci", conclusion="FAILURE", log_excerpt="failing check"),)
        ),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True
    assert state.iter_count == 0
    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id, limit=20)
        push_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )

    assert workspace is not None
    assert workspace.status == WorkspaceStatus.failed.value
    ci_operation = next(operation for operation in operations if operation.type == "ci_repair")
    assert ci_operation.status == OperationStatus.failed.value
    assert ci_operation.error_code == "GITHUB_WORKFLOW_SCOPE_REQUIRED"
    assert len(push_events) == 1
    assert push_events[0].reason_code == "GITHUB_WORKFLOW_SCOPE_REQUIRED"
    comment_calls = [call for call in cmd.calls if call.args[:3] == ["gh", "pr", "comment"]]
    assert len(comment_calls) == 1


@pytest.mark.unit
async def test_protected_scope_push_check_skips_missing_worktree_without_git_diff(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    block = await runner._protected_scope_push_block(
        workspace_id="ws_missing_worktree",
        worktree_path=tmp_path / "worktrees" / "ws_missing_worktree",
        remote_branch="awf/ws_missing_worktree",
    )

    assert block is None
    assert cmd.calls == []


@pytest.mark.unit
async def test_protected_scope_unpushed_commit_check_fails_closed_for_missing_workspace(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    worktree = tmp_path / "worktrees" / "ws_missing_row"
    worktree.mkdir(parents=True)

    with pytest.raises(ProtectedScopeDiffError, match="Workspace row ws_missing_row"):
        await runner._protected_scope_violations_for_unpushed_commits(
            workspace_id="ws_missing_row",
            worktree_path=worktree,
            remote_branch="awf/ws_missing_row",
        )
    assert cmd.calls == []


@pytest.mark.unit
async def test_changed_paths_since_remote_branch_fetches_real_push_remote(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="merge-base-sha\n")
    cmd.queue_result(returncode=0, stdout=_name_status_z("src/fix.py", "tests/test_fix.py"))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    paths = await runner._changed_paths_since_remote_branch(
        workspace_id=workspace_id,
        worktree_path=tmp_path / "worktree",
        remote_branch="awf/ws_remote_missing",
        remote_push_url="https://github.com/org/fork.git",
    )

    assert paths == ("src/fix.py", "tests/test_fix.py")
    worktree = tmp_path / "worktree"
    assert cmd.calls[0].args == _git_worktree_command(
        worktree,
        "fetch",
        "https://github.com/org/fork.git",
        "refs/heads/awf/ws_remote_missing",
    )
    assert cmd.calls[1].args == _git_worktree_command(
        worktree,
        "merge-base",
        "FETCH_HEAD",
        "HEAD",
    )
    assert cmd.calls[2].args == _git_worktree_command(
        worktree,
        "diff",
        "--name-status",
        "-z",
        "merge-base-sha..HEAD",
        "--",
    )


@pytest.mark.unit
async def test_remote_branch_diff_base_repairs_orphaned_broken_awf_ref(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Regression: broken AWF ref in the local mirror is repaired before retrying the baseline fetch."""
    workspace_id = await seed_monitoring_workspace(factory)
    terminal_id = await seed_monitoring_workspace(factory)
    await _force_workspace_status(factory, terminal_id, WorkspaceStatus.failed)
    worktree = tmp_path / "worktree"
    cmd = FakeCommandRunner()
    broken_ref = f"refs/heads/awf/{terminal_id}"
    broken_stderr = f"fatal: bad object {broken_ref}"
    cmd.queue_result(returncode=128, stderr=broken_stderr)  # first fetch: broken AWF ref
    cmd.queue_result(returncode=0)  # update-ref -d
    cmd.queue_result(returncode=0)  # worktree prune
    cmd.queue_result(returncode=0, stdout="")  # retry fetch: success
    cmd.queue_result(returncode=0, stdout="merge-base-sha\n")  # merge-base
    cmd.queue_result(returncode=0, stdout=_name_status_z("src/fix.py"))  # diff
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    local_base, changed_paths = await runner._remote_branch_diff_base_and_changed_paths(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch="awf/ws_remote_branch",
    )

    assert local_base == "merge-base-sha"
    assert changed_paths == ("src/fix.py",)
    call_args = [call.args for call in cmd.calls]
    assert call_args[0] == _git_worktree_command(
        worktree, "fetch", "origin", "refs/heads/awf/ws_remote_branch"
    )
    assert [
        "git",
        "-c",
        f"safe.directory={worktree}",
        "-C",
        str(worktree),
        "update-ref",
        "-d",
        broken_ref,
    ] in call_args
    assert _git_worktree_command(worktree, "worktree", "prune") in call_args
    assert (
        call_args.count(
            _git_worktree_command(worktree, "fetch", "origin", "refs/heads/awf/ws_remote_branch")
        )
        == 2
    )

    async with factory() as s:
        events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.git_mirror_repaired",
            limit=10,
        )
    assert len(events) == 1
    assert events[0].reason_code == "GIT_MIRROR_BROKEN_REF_REMOVED"
    assert events[0].payload is not None
    assert events[0].payload["broken_ref"] == broken_ref
    assert events[0].payload["broken_workspace_id"] == terminal_id


@pytest.mark.unit
async def test_remote_branch_diff_base_still_fails_closed_after_unrepairable_fetch(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Non-broken-ref fetch failures still fail closed with PROTECTED_SCOPE_DIFF_UNAVAILABLE."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=128, stderr="unknown remote ref")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    with pytest.raises(
        ProtectedScopeDiffError, match="fetch refs/heads/awf/ws_remote_missing"
    ) as exc_info:
        await runner._changed_paths_since_remote_branch(
            workspace_id=workspace_id,
            worktree_path=tmp_path / "worktree",
            remote_branch="awf/ws_remote_missing",
        )

    message = str(exc_info.value)
    assert "unknown remote ref" in message
    assert [call.args for call in cmd.calls] == [
        _git_worktree_command(
            tmp_path / "worktree",
            "fetch",
            "origin",
            "refs/heads/awf/ws_remote_missing",
        )
    ]


@pytest.mark.unit
async def test_remote_branch_diff_base_fails_closed_when_broken_ref_workspace_active(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken AWF ref belonging to an active workspace must not be deleted."""
    workspace_id = await seed_monitoring_workspace(factory)
    active_id = await seed_monitoring_workspace(factory)
    # Leave active_id in monitoring_pr (non-terminal) so repair refuses removal.
    worktree = tmp_path / "worktree"
    cmd = FakeCommandRunner()
    broken_ref = f"refs/heads/awf/{active_id}"
    cmd.queue_result(returncode=128, stderr=f"fatal: bad object {broken_ref}")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    with pytest.raises(ProtectedScopeDiffError) as exc_info:
        await runner._remote_branch_diff_base_and_changed_paths(
            workspace_id=workspace_id,
            worktree_path=worktree,
            remote_branch="awf/ws_remote_missing",
        )

    message = str(exc_info.value)
    assert "fetch refs/heads/awf/ws_remote_missing" in message
    assert [call.args for call in cmd.calls] == [
        _git_worktree_command(
            worktree,
            "fetch",
            "origin",
            "refs/heads/awf/ws_remote_missing",
        )
    ]


@pytest.mark.unit
async def test_remote_branch_diff_base_logs_repair_exception(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A broken AWF ref repair exception emits monitor.git_mirror_broken_ref_repair_failed."""
    from awf.common.config import Settings
    from awf.common.logging import configure_logging

    configure_logging(Settings(service_name="test", env="local", log_level="INFO", _env_file=None))

    workspace_id = await seed_monitoring_workspace(factory)
    terminal_id = await seed_monitoring_workspace(factory)
    await _force_workspace_status(factory, terminal_id, WorkspaceStatus.failed)
    worktree = tmp_path / "worktree"
    cmd = FakeCommandRunner()
    broken_ref = f"refs/heads/awf/{terminal_id}"
    cmd.queue_result(returncode=128, stderr=f"fatal: bad object {broken_ref}")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    # Force the repair to raise so the exception-log path is exercised.
    async def _exploding_repair(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("repair exploded")

    runner._repair_orphaned_broken_awf_ref = _exploding_repair  # type: ignore[method-assign]

    logger_name = "awf.runtime.pr_monitor_runner.logging"
    caplog.set_level(logging.ERROR, logger=logger_name)
    stdlib_logger = logging.getLogger(logger_name)
    stdlib_logger.addHandler(caplog.handler)
    stdlib_logger.propagate = False

    with pytest.raises(ProtectedScopeDiffError) as exc_info:
        await runner._remote_branch_diff_base_and_changed_paths(
            workspace_id=workspace_id,
            worktree_path=worktree,
            remote_branch="awf/ws_remote_missing",
        )

    assert "repair exploded" in str(exc_info.value)
    assert any(
        "monitor.git_mirror_broken_ref_repair_failed" in record.message
        and record.levelno == logging.ERROR
        and record.name == logger_name
        for record in caplog.records
    )


@pytest.mark.unit
def test_changed_paths_from_name_status_z_deduplicates_valid_nul_records() -> None:
    assert _changed_paths_from_name_status_z(
        "M\0src/fix.py\0M\0src/fix.py\0R100\0.github/workflows/ci.yml\0docs/ci.yml\0"
    ) == ("src/fix.py", ".github/workflows/ci.yml", "docs/ci.yml")


@pytest.mark.unit
def test_changed_paths_from_name_only_z_deduplicates_valid_nul_records() -> None:
    assert _changed_paths_from_name_only_z("src/fix.py\0src/fix.py\0tests/test_fix.py\0") == (
        "src/fix.py",
        "tests/test_fix.py",
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("diff_stdout", "expected_error", "message"),
    [
        ("M\tsrc/fix.py\n", ProtectedScopeDiffError, "expected NUL-delimited output"),
        ("M\0src/fix.py", ProtectedScopeDiffError, "missing terminating NUL"),
        (
            "M\0.github/workflows/ci.yml\0R100\0docs/old.yml\0",
            ProtectedScopeDiffError,
            "truncated",
        ),
    ],
)
def test_changed_paths_from_name_status_z_rejects_malformed_z_output(
    diff_stdout: str,
    expected_error: type[Exception],
    message: str,
) -> None:
    with pytest.raises(expected_error, match=message):
        _changed_paths_from_name_status_z(diff_stdout)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("diff_stdout", "message"),
    [
        ("src/fix.py\n", "expected NUL-delimited output"),
        ("src/fix.py\0tests/test_fix.py", "missing terminating NUL"),
        ("src/fix.py\0\0", "empty path"),
    ],
)
def test_changed_paths_from_name_only_z_rejects_malformed_z_output(
    diff_stdout: str,
    message: str,
) -> None:
    with pytest.raises(ProtectedScopeDiffError, match=message):
        _changed_paths_from_name_only_z(diff_stdout)


@pytest.mark.unit
async def test_changed_paths_since_remote_branch_fails_closed_for_malformed_z_output(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="merge-base-sha\n")
    cmd.queue_result(returncode=0, stdout="M\0.github/workflows/ci.yml\0R100\0docs/old.yml\0")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    workspace_id = await seed_monitoring_workspace(factory)
    with pytest.raises(ProtectedScopeDiffError, match="Could not parse committed diff"):
        await runner._changed_paths_since_remote_branch(
            workspace_id=workspace_id,
            worktree_path=tmp_path / "worktree",
            remote_branch="awf/ws_remote_missing",
        )
