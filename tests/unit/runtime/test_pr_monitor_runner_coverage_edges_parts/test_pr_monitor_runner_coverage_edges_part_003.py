"""Focused branch-coverage tests for PR monitor runner edge behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import structlog
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.common.github_client import RepoRef
from awf.db.enums import OperationStatus, OperationType, TaskClass, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    OperationRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    AddressComments,
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
    SyncBase,
)
from awf.runtime.pr_monitor_runner import (
    PullRequestMonitorRunner,
)
from awf.runtime.pr_monitor_runner import remote_repair as pr_remote_repair
from awf.runtime.pr_monitor_runner.helpers import (
    _target_reconcile_failure_payload,
)
from awf.runtime.pr_monitor_runner.remote_ops import (
    _GitPushResult,
)
from awf.runtime.pr_monitor_runner.types import (
    ProviderRecoveryFallbackError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
)
from awf.service.alembic_resolver import AlembicResolveResult, AlembicResolveStatus
from awf.service.merge_queue import MergeQueueBlocker
from awf.service.target_branch_monitor import (
    TargetBranchMonitorError,
    TargetBranchMonitorResult,
    TargetBranchMonitorStatus,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
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
async def test_target_branch_reconcile_failure_reuses_exception_payload(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)

    class _CountingResultError(Exception):
        def __init__(self) -> None:
            super().__init__("target branch locked " + ("x" * 1200))
            self.result_accesses = 0
            self._result = CommandResult(
                returncode=128,
                stdout="stdout " + ("o" * 1200),
                stderr="stderr " + ("e" * 1200),
                reason_code="GIT_FAILED",
            )

        @property
        def result(self) -> CommandResult:
            self.result_accesses += 1
            return self._result

    failure = _CountingResultError()

    async def failing_reconciler(*, repo_url: str, branch: str, workspace_id: str) -> object:
        del repo_url, branch, workspace_id
        raise failure

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        post_merge_target_reconciler=failing_reconciler,
    )

    with structlog.testing.capture_logs() as captured:
        await runner._reconcile_target_branch_after_merge(
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            base_branch="development",
        )

    failure_log = next(
        event
        for event in captured
        if event.get("event") == "monitor.target_branch_reconcile_failed"
    )
    assert failure.result_accesses == 1
    assert len(failure_log["error"]) == 500
    assert len(failure_log["stderr"]) == 500
    assert len(failure_log["stdout"]) == 500

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        event_payload = ws.events[-1].payload
        assert len(event_payload["error"]) == 1000
        assert len(event_payload["stderr"]) == 1000
        assert len(event_payload["stdout"]) == 1000


@pytest.mark.unit
def test_target_reconcile_failure_payload_uses_command_error_contract() -> None:
    result = CommandResult(
        returncode=128,
        stdout="fatal stdout",
        stderr="fatal stderr",
        reason_code="GIT_FAILED",
    )
    exc = TargetBranchMonitorError(
        operation="target_branch.git_fetch",
        result=result,
    )
    exc.target_reconcile_payload = lambda: {  # type: ignore[attr-defined]
        "status": "committed",
        "resolver_results": [{"status": "resolved"}],
        "commit_sha": "abc123",
        "pushed": True,
    }

    payload = _target_reconcile_failure_payload(exc, error_limit=100)

    assert payload["status"] == "failed"
    assert "target_reconcile_status" not in payload
    assert payload["resolver_results"] == []
    assert payload["commit_sha"] is None
    assert payload["pushed"] is False
    assert payload["operation"] == "target_branch.git_fetch"
    assert payload["returncode"] == 128
    assert payload["command_reason_code"] == "GIT_FAILED"
    assert payload["stderr"] == "fatal stderr"
    assert payload["stdout"] == "fatal stdout"


@pytest.mark.unit
async def test_target_branch_reconcile_success_appends_payload_event(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)

    async def reconciler(*, repo_url: str, branch: str, workspace_id: str) -> object:
        return {
            "status": "TARGET_BRANCH_FAST_FORWARDED",
            "repo_url": repo_url,
            "branch": branch,
            "workspace_id": workspace_id,
        }

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        post_merge_target_reconciler=reconciler,
    )

    await runner._reconcile_target_branch_after_merge(
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        base_branch="development",
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.events[-1].event_type == "target_branch.reconciled"
        assert ws.events[-1].reason_code == "TARGET_BRANCH_FAST_FORWARDED"
        assert ws.events[-1].payload["branch"] == "development"


@pytest.mark.unit
async def test_target_branch_reconcile_event_preserves_resolver_operator_details(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)

    async def reconciler(*, repo_url: str, branch: str, workspace_id: str) -> object:
        checkout = tmp_path / "checkout"
        generated = checkout / "migrations" / "versions" / "merge001_merge_alembic_heads.py"
        return TargetBranchMonitorResult(
            repo_url=repo_url,
            branch=branch,
            checkout_path=checkout,
            status=TargetBranchMonitorStatus.committed,
            resolver_results=(
                AlembicResolveResult(
                    status=AlembicResolveStatus.resolved,
                    reason_code="ALEMBIC_HEADS_MERGED",
                    heads=("left001", "right001"),
                    generated_revision="merge001",
                    generated_path=generated,
                    generated_path_relative="migrations/versions/merge001_merge_alembic_heads.py",
                    message="Generated Alembic merge revision for 2 heads.",
                ),
            ),
            commit_sha="abc123",
            pushed=True,
            changed_paths=("migrations/versions/merge001_merge_alembic_heads.py",),
        )

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        post_merge_target_reconciler=reconciler,
    )

    with structlog.testing.capture_logs() as captured:
        await runner._reconcile_target_branch_after_merge(
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            base_branch="development",
        )

    reconciled_log = next(
        event for event in captured if event.get("event") == "monitor.target_branch_reconciled"
    )
    assert reconciled_log["status"] == "committed"
    assert reconciled_log["commit_sha"] == "abc123"
    assert reconciled_log["pushed"] is True
    assert reconciled_log["dry_run"] is False
    assert reconciled_log["commit_allowed"] is True
    assert reconciled_log["policy_reason_code"] is None
    assert reconciled_log["changed_paths"] == [
        "migrations/versions/merge001_merge_alembic_heads.py"
    ]
    logged_resolver = reconciled_log["resolver_results"][0]
    assert logged_resolver["reason_code"] == "ALEMBIC_HEADS_MERGED"
    assert logged_resolver["heads"] == ["left001", "right001"]
    assert logged_resolver["generated_revision"] == "merge001"
    assert logged_resolver["generated_path_relative"] == (
        "migrations/versions/merge001_merge_alembic_heads.py"
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        payload = ws.events[-1].payload
        assert payload["status"] == "committed"
        assert payload["commit_sha"] == "abc123"
        assert payload["pushed"] is True
        assert payload["branch"] == "development"
        assert payload["changed_paths"] == ["migrations/versions/merge001_merge_alembic_heads.py"]
        resolver = payload["resolver_results"][0]
        assert resolver["reason_code"] == "ALEMBIC_HEADS_MERGED"
        assert resolver["heads"] == ["left001", "right001"]
        assert resolver["generated_revision"] == "merge001"
        assert resolver["generated_path_relative"] == (
            "migrations/versions/merge001_merge_alembic_heads.py"
        )


@pytest.mark.unit
async def test_completed_filesystem_gc_exception_is_logged_and_swallowed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    class _ExplodingSessionFactory:
        def __call__(self) -> object:
            raise RuntimeError("database unavailable")

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.session_factory = _ExplodingSessionFactory()  # type: ignore[assignment]

    with structlog.testing.capture_logs() as captured:
        await runner._gc_completed_workspace_filesystem("ws_gc")

    assert any(
        event.get("event") == "monitor.filesystem_gc_raised"
        and event.get("workspace_id") == "ws_gc"
        for event in captured
    )


@pytest.mark.unit
async def test_dirty_worktree_helper_returns_false_for_non_commit_cases(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    case_number = 0

    async def run_case(
        *,
        workspace_exists: bool = True,
        queued: list[tuple[int, str, str]],
    ) -> tuple[bool, list[list[str]]]:
        nonlocal case_number
        case_number += 1
        cmd = FakeCommandRunner()
        for returncode, stdout, stderr in queued:
            cmd.queue_result(returncode=returncode, stdout=stdout, stderr=stderr)
        runner = make_runner(
            factory=factory,
            cmd=cmd,
            adapter=FakeAdapter(),
            sleep_fn=RecordedSleep(),
            worktrees_root=tmp_path / "worktrees",
        )
        workspace_id = f"ws_dirty_{case_number}"
        if workspace_exists:
            (tmp_path / "worktrees" / workspace_id).mkdir(parents=True)
        result = await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message="fix: dirty",
        )
        return result, [call.args for call in cmd.calls]

    missing_result, missing_calls = await run_case(workspace_exists=False, queued=[])
    status_result, status_calls = await run_case(queued=[(1, "", "status failed")])
    clean_result, clean_calls = await run_case(queued=[(0, "", "")])
    add_result, add_calls = await run_case(queued=[(0, " M a.py\n", ""), (1, "", "add failed")])
    cached_result, cached_calls = await run_case(
        queued=[(0, " M a.py\n", ""), (0, "", ""), (0, "", "")]
    )
    commit_result, commit_calls = await run_case(
        queued=[(0, " M a.py\n", ""), (0, "", ""), (1, "", ""), (1, "", "commit failed")]
    )

    assert missing_result is False
    assert missing_calls == []
    assert status_result is False
    assert [args[-2:] for args in status_calls] == [["status", "--porcelain"]]
    assert clean_result is False
    assert [args[-2:] for args in clean_calls] == [["status", "--porcelain"]]
    assert add_result is False
    assert [args[-2:] for args in add_calls] == [["status", "--porcelain"], ["add", "-A"]]
    assert cached_result is False
    assert [args[-2:] for args in cached_calls[:2]] == [["status", "--porcelain"], ["add", "-A"]]
    assert cached_calls[2][-3:] == ["diff", "--cached", "--quiet"]
    assert commit_result is False
    assert commit_calls[-1][-3:] == ["commit", "-m", "fix: dirty"]


@pytest.mark.unit
async def test_commit_dirty_worktree_repairs_runtime_ownership_around_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=" M pyproject.toml\n")
    cmd.queue_result(returncode=0)  # git add
    cmd.queue_result(returncode=1)  # git diff --cached --quiet
    cmd.queue_result(returncode=0)  # git commit
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    repair_call_lengths: list[int] = []

    async def _repair_agent_runtime_ownership(
        logger: object,
        workspace_id: str,
        worktree_path: Path,
        reason: str,
        event_name: str,
        reason_code: str,
    ) -> bool:
        del logger, workspace_id, event_name, reason_code
        assert worktree_path == worktree
        repair_call_lengths.append(len(cmd.calls))
        return True

    monkeypatch.setattr(
        pr_remote_repair,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    result = await runner._commit_dirty_worktree(
        workspace_id=workspace_id,
        message="fix: dirty",
    )

    assert result is True
    assert repair_call_lengths == [1, 4]


@pytest.mark.unit
async def test_commit_dirty_worktree_logs_commit_stderr_when_failed_commit_repair_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    commit_stderr = "fatal: unable to create commit\n" + ("detail " * 100)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=" M pyproject.toml\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=1, stderr=commit_stderr)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    repair_reasons: list[str] = []

    async def _repair_agent_runtime_ownership(
        logger: object,
        workspace_id: str,
        worktree_path: Path,
        reason: str,
        event_name: str,
        reason_code: str,
    ) -> bool:
        del logger, workspace_id, worktree_path, event_name, reason_code
        repair_reasons.append(reason)
        return reason != "dirty_worktree_post_commit_failed"

    monkeypatch.setattr(
        pr_remote_repair,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    with (
        structlog.testing.capture_logs() as captured,
        pytest.raises(_MonitorAgentRuntimeOwnershipRepairFailedError),
    ):
        await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message="fix: dirty",
        )

    assert repair_reasons == [
        "dirty_worktree_pre_commit",
        "dirty_worktree_post_commit_failed",
    ]
    assert any(
        event.get("event") == "monitor.dirty_worktree_post_commit_ownership_repair_failed"
        and event.get("workspace_id") == workspace_id
        and event.get("commit_stderr") == commit_stderr[:400]
        for event in captured
    )


@pytest.mark.unit
async def test_commit_dirty_worktree_logs_commit_when_post_commit_ownership_repair_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=" M pyproject.toml\n")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=1)
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    repair_reasons: list[str] = []

    async def _repair_agent_runtime_ownership(
        logger: object,
        workspace_id: str,
        worktree_path: Path,
        reason: str,
        event_name: str,
        reason_code: str,
    ) -> bool:
        del logger, workspace_id, worktree_path, event_name, reason_code
        repair_reasons.append(reason)
        return reason != "dirty_worktree_post_commit_succeeded"

    monkeypatch.setattr(
        pr_remote_repair,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    with (
        structlog.testing.capture_logs() as captured,
        pytest.raises(_MonitorAgentRuntimeOwnershipRepairFailedError),
    ):
        await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message="fix: dirty",
        )

    assert repair_reasons == [
        "dirty_worktree_pre_commit",
        "dirty_worktree_post_commit_succeeded",
    ]
    assert any(
        event.get("event") == "monitor.dirty_worktree_committed"
        and event.get("workspace_id") == workspace_id
        for event in captured
    )
    assert any(
        event.get("event") == "monitor.dirty_worktree_post_commit_succeeded_ownership_repair_failed"
        and event.get("workspace_id") == workspace_id
        for event in captured
    )


@pytest.mark.unit
async def test_commit_dirty_worktree_stops_before_add_when_runtime_repair_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=" M pyproject.toml\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _raise_repair(
        logger: object,
        workspace_id: str,
        worktree_path: Path,
        reason: str,
        event_name: str,
        reason_code: str,
    ) -> bool:
        del logger, workspace_id, event_name, reason_code
        assert worktree_path == worktree
        return False

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.remote_repair.repair_agent_runtime_ownership",
        _raise_repair,
    )

    with pytest.raises(_MonitorAgentRuntimeOwnershipRepairFailedError):
        await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message="fix: dirty",
        )
    assert len(cmd.calls) == 1


@pytest.mark.unit
async def test_execute_report_ci_failure_dispatches_fix_and_increments_iteration(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(returncode=1, stdout="partial CI fix")
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="abc1234567890def\n")  # operation start HEAD
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="")  # fetch remote branch for committed diff
    cmd.queue_result(returncode=0, stdout="merge-base-sha\n")
    cmd.queue_result(returncode=0, stdout="")  # committed diff has no protected paths
    cmd.queue_result(returncode=0, stderr="Everything up-to-date")
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
            failures=(CheckFailure(name="pytest", conclusion="FAILURE", log_excerpt="traceback"),)
        ),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert state.iter_count == 1
    assert "traceback" in adapter.calls[0]
    _assert_committed_diff_phase_ran(
        cmd,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
    )
    assert cmd.calls[-1].args[-2:] == ["origin", f"HEAD:refs/heads/awf/{workspace_id}"]
    async with factory() as s:
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id, limit=20)
        push_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )
    ci_operation = next(operation for operation in operations if operation.type == "ci_repair")
    assert len(push_events) == 1
    assert push_events[0].payload == {
        "schema": "control_audit.v1",
        "actor": "pr_monitor",
        "source": "pr_monitor",
        "action": "ci_repair_push",
        "outcome": "succeeded",
        "reason_code": "CI_REPAIR",
        "operation_id": ci_operation.id,
        "operation_type": "ci_repair",
        "pr_number": 42,
        "pr_url": "https://github.com/dimileeh/aira-web/pull/42",
        "source_head_sha": "abc1234567890def",
        "source_base_sha": "a" * 40,
        "target_branch": "development",
        "remote_branch": f"awf/{workspace_id}",
        "branch_name": f"awf/{workspace_id}",
    }


@pytest.mark.unit
async def test_execute_report_ci_failure_push_failure_records_failed_audit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(stdout="partial CI fix")
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="abc1234567890def\n")  # operation start HEAD
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="")  # fetch remote branch for committed diff
    cmd.queue_result(returncode=0, stdout="merge-base-sha\n")
    cmd.queue_result(returncode=0, stdout="")  # committed diff has no protected paths
    cmd.queue_result(
        returncode=128,
        stderr=("fatal: unable to access https://user:ghp_should_not_persist@github.com/org/repo"),
    )
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
            failures=(CheckFailure(name="pytest", conclusion="FAILURE", log_excerpt="traceback"),)
        ),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert state.iter_count == 1
    _assert_committed_diff_phase_ran(
        cmd,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
    )
    async with factory() as s:
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id, limit=20)
        push_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )
    ci_operation = next(operation for operation in operations if operation.type == "ci_repair")
    assert ci_operation.status == OperationStatus.failed.value
    assert len(push_events) == 1
    assert push_events[0].reason_code == "GIT_PUSH_FAILED"
    assert push_events[0].payload is not None
    assert push_events[0].payload["action"] == "ci_repair_push"
    assert push_events[0].payload["outcome"] == "failed"
    assert push_events[0].payload["operation_id"] == ci_operation.id
    assert push_events[0].payload["operation_type"] == "ci_repair"
    assert push_events[0].payload["evidence"]["operation"] == "git push"
    assert push_events[0].payload["evidence"]["returncode"] == 128
    assert "ghp_should_not_persist" not in repr(push_events[0].payload)
    assert "https://[redacted]@github.com/org/repo" in repr(push_events[0].payload)


@pytest.mark.unit
async def test_monitor_adapter_cleanup_failure_terminates_without_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=_CleanupFailingAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    terminal = await runner._execute(
        action=ReportCiFailure(
            failures=(CheckFailure(name="pytest", conclusion="FAILURE", log_excerpt="traceback"),)
        ),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True
    assert cmd.calls == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == "infrastructure_failure"
        assert "EXEC_PROCESS_CLEANUP_FAILED" in (ws.failure_message or "")
        assert ws.events[-1].reason_code == "EXEC_PROCESS_CLEANUP_FAILED"


@pytest.mark.unit
async def test_monitor_comment_cleanup_failure_terminates_without_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(factory)
    thread = ReviewThread(
        thread_id="T1",
        path="src/app.py",
        line=12,
        body_excerpt="please fix",
        author="reviewer",
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=_CleanupFailingAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    terminal = await runner._execute(
        action=AddressComments(threads=(thread,), review_comments=()),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(threads=(thread,)),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True
    assert cmd.calls == []
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.events[-1].reason_code == "EXEC_PROCESS_CLEANUP_FAILED"


@pytest.mark.unit
async def test_monitor_comment_provider_fallback_records_failed_operation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    thread = ReviewThread(
        thread_id="T_provider_fallback",
        path="src/app.py",
        line=12,
        body_excerpt="please fix",
        author="reviewer",
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _fallback_fix_cycle(**_kwargs: object) -> _GitPushResult:
        raise ProviderRecoveryFallbackError()

    monkeypatch.setattr(runner, "_run_fix_cycle", _fallback_fix_cycle)

    with pytest.raises(ProviderRecoveryFallbackError):
        await runner._execute(
            action=AddressComments(threads=(thread,), review_comments=()),
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            status=_status_for_helpers(threads=(thread,)),
            state=MonitorState(),
            base_branch="development",
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            monitor_log=None,
        )

    async with factory() as s:
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id, limit=10)

    comment_operation = next(
        operation for operation in operations if operation.type == "comment_repair"
    )
    assert comment_operation.status == OperationStatus.failed.value
    assert comment_operation.error_code == "PROVIDER_FALLBACK"
    assert comment_operation.result == {
        "status": "failed",
        "outcome": "provider_fallback",
        "reason_code": "PROVIDER_FALLBACK",
        "pushed": False,
    }


@pytest.mark.unit
async def test_monitor_comment_repair_push_failure_records_failed_audit_and_requeues(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(stdout="fixed locally")
    workspace_id = await seed_monitoring_workspace(factory)
    thread = ReviewThread(
        thread_id="T_push",
        path="src/app.py",
        line=12,
        body_excerpt="please fix",
        author="reviewer",
    )
    cmd.queue_result(returncode=0, stdout=pr_payload())
    cmd.queue_result(
        returncode=128,
        stderr=("fatal: unable to access https://user:ghp_should_not_persist@github.com/org/repo"),
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=AddressComments(threads=(thread,), review_comments=()),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(threads=(thread,)),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert state.iter_count == 1
    assert "T_push" not in state.threads_addressed_ids
    assert sum(call.args[:3] == ["gh", "api", "graphql"] for call in cmd.calls) == 1
    async with factory() as s:
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id, limit=20)
        push_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )
        resolution_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.comment_resolution",
            limit=10,
        )
    comment_operation = next(
        operation for operation in operations if operation.type == "comment_repair"
    )
    assert comment_operation.status == OperationStatus.failed.value
    assert len(push_events) == 1
    assert push_events[0].reason_code == "GIT_PUSH_FAILED"
    assert push_events[0].payload is not None
    assert push_events[0].payload["action"] == "comment_repair_push"
    assert push_events[0].payload["outcome"] == "failed"
    assert push_events[0].payload["operation_id"] == comment_operation.id
    assert push_events[0].payload["operation_type"] == "comment_repair"
    assert push_events[0].payload["evidence"]["operation"] == "git push"
    assert push_events[0].payload["evidence"]["returncode"] == 128
    assert "ghp_should_not_persist" not in repr(push_events[0].payload)
    assert "https://[redacted]@github.com/org/repo" in repr(push_events[0].payload)
    assert resolution_events == []


@pytest.mark.unit
async def test_monitor_comment_repair_workflow_scope_failure_requeues_without_terminating(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Verify workflow-scope comment repair failures requeue review items."""
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(stdout="fixed locally")
    workspace_id = await seed_monitoring_workspace(factory)
    thread = ReviewThread(
        thread_id="T_workflow_scope",
        path=".github/workflows/publish.yml",
        line=12,
        body_excerpt="publish workflow needs the reviewed fix",
        author="reviewer",
    )
    cmd.queue_result(returncode=0, stdout=pr_payload())
    cmd.queue_result(
        returncode=1,
        stderr=(
            "remote: refusing to allow a Personal Access Token to create or update workflow "
            "`.github/workflows/publish.yml` without `workflow` scope"
        ),
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=AddressComments(threads=(thread,), review_comments=()),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(threads=(thread,)),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert state.iter_count == 1
    assert "T_workflow_scope" not in state.threads_addressed_ids
    assert "__review_thread_body_hash__:T_workflow_scope" not in state.threads_addressed_ids
    assert "__needs_human_reason__:T_workflow_scope" not in state.threads_addressed_ids
    comment_calls = [call for call in cmd.calls if call.args[:3] == ["gh", "pr", "comment"]]
    assert len(comment_calls) == 1
    body = comment_calls[0].args[comment_calls[0].args.index("--body") + 1]
    assert "GitHub rejected the workflow-file push" in body
    assert "`workflow` scope for .github/workflows/publish.yml" in body
    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id, limit=20)
        push_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )
        resolution_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.comment_resolution",
            limit=10,
        )

    assert workspace is not None
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    comment_operation = next(
        operation for operation in operations if operation.type == "comment_repair"
    )
    assert comment_operation.status == OperationStatus.failed.value
    assert comment_operation.error_code == "GITHUB_WORKFLOW_SCOPE_REQUIRED"
    assert len(push_events) == 1
    assert push_events[0].reason_code == "GITHUB_WORKFLOW_SCOPE_REQUIRED"
    assert push_events[0].payload is not None
    assert push_events[0].payload["action"] == "comment_repair_push"
    assert push_events[0].payload["outcome"] == "failed"
    assert push_events[0].payload["operation_id"] == comment_operation.id
    assert push_events[0].payload["operation_type"] == "comment_repair"
    assert push_events[0].payload["evidence"]["reason_code"] == ("GITHUB_WORKFLOW_SCOPE_REQUIRED")
    assert resolution_events == []


@pytest.mark.unit
async def test_monitor_comment_repair_workflow_scope_notification_failure_requeues_without_masking_push_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Notification failures must not mask comment-repair workflow-scope pushes."""
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(stdout="fixed locally")
    workspace_id = await seed_monitoring_workspace(factory)
    thread = ReviewThread(
        thread_id="T_workflow_scope",
        path=".github/workflows/publish.yml",
        line=12,
        body_excerpt="publish workflow needs the reviewed fix",
        author="reviewer",
    )
    cmd.queue_result(returncode=0, stdout=pr_payload())
    cmd.queue_result(
        returncode=1,
        stderr=(
            "remote: refusing to allow a Personal Access Token to create or update workflow "
            "`.github/workflows/publish.yml` without `workflow` scope"
        ),
    )
    cmd.queue_result(returncode=1, stderr="bad credentials")  # gh pr comment notification
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=AddressComments(threads=(thread,), review_comments=()),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(threads=(thread,)),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert state.iter_count == 1
    assert "T_workflow_scope" not in state.threads_addressed_ids
    comment_calls = [call for call in cmd.calls if call.args[:3] == ["gh", "pr", "comment"]]
    assert len(comment_calls) == 1
    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id, limit=20)
        push_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )

    assert workspace is not None
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    comment_operation = next(
        operation for operation in operations if operation.type == "comment_repair"
    )
    assert comment_operation.status == OperationStatus.failed.value
    assert comment_operation.error_code == "GITHUB_WORKFLOW_SCOPE_REQUIRED"
    assert comment_operation.result is not None
    assert comment_operation.result["outcome"] == "github_workflow_scope_required"
    assert comment_operation.result["reason_code"] == "GITHUB_WORKFLOW_SCOPE_REQUIRED"
    assert len(push_events) == 1
    assert push_events[0].reason_code == "GITHUB_WORKFLOW_SCOPE_REQUIRED"
    assert push_events[0].payload is not None
    assert push_events[0].payload["action"] == "comment_repair_push"
    assert push_events[0].payload["outcome"] == "failed"
    assert push_events[0].payload["operation_id"] == comment_operation.id
    assert push_events[0].payload["operation_type"] == "comment_repair"
    assert push_events[0].payload["evidence"]["reason_code"] == "GITHUB_WORKFLOW_SCOPE_REQUIRED"


@pytest.mark.unit
async def test_monitor_comment_diff_baseline_unavailable_terminates_with_diff_reason(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(stdout="fixed locally")
    workspace_id = await seed_monitoring_workspace(factory)
    thread = ReviewThread(
        thread_id="T_diff_unavailable",
        path="src/app.py",
        line=12,
        body_excerpt="please fix",
        author="reviewer",
    )
    cmd.queue_result(returncode=0, stdout="")  # clean worktree before repair
    cmd.queue_result(returncode=0, stdout="abc1234567890def\n")  # operation start HEAD
    cmd.queue_result(returncode=0, stdout="")  # clean worktree after agent run
    cmd.queue_result(returncode=0, stdout=pr_payload())  # settle-window status poll
    cmd.queue_result(returncode=128, stderr="network reset")  # committed-diff baseline fetch
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
        action=AddressComments(threads=(thread,), review_comments=()),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(threads=(thread,)),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
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
    comment_operation = next(
        operation for operation in operations if operation.type == "comment_repair"
    )
    assert comment_operation.status == OperationStatus.failed.value
    assert comment_operation.error_code == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
    assert comment_operation.result is not None
    assert comment_operation.result["outcome"] == "protected_scope_diff_unavailable"
    assert comment_operation.result["reason_code"] == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
    assert len(push_events) == 1
    assert push_events[0].reason_code == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
    assert push_events[0].payload is not None
    assert push_events[0].payload["action"] == "comment_repair_push"
    assert push_events[0].payload["outcome"] == "failed"
    assert push_events[0].payload["evidence"]["reason_code"] == ("PROTECTED_SCOPE_DIFF_UNAVAILABLE")
    assert len(scope_events) == 1
    assert scope_events[0].reason_code == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
    assert scope_events[0].payload is not None
    assert scope_events[0].payload["reason"] == "diff_baseline_unavailable"


@pytest.mark.unit
async def test_monitor_sync_base_cleanup_failure_terminates_without_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(factory)
    cmd.queue_result(returncode=0)  # merge --abort
    cmd.queue_result(returncode=0)  # fetch
    cmd.queue_result(returncode=1, stderr="conflict")  # merge
    cmd.queue_result(returncode=0, stdout="UU src/app.py\n")  # status
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=_CleanupFailingAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    terminal = await runner._execute(
        action=SyncBase(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True
    assert [call.args[-2:] for call in cmd.calls] == [
        ["merge", "--abort"],
        ["origin", "+refs/heads/development:refs/remotes/origin/development"],
        ["--no-edit", "origin/development"],
        ["status", "--porcelain"],
    ]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.events[-1].reason_code == "EXEC_PROCESS_CLEANUP_FAILED"


@pytest.mark.unit
async def test_execute_sync_base_records_branch_push_audit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(factory)
    cmd.queue_result(returncode=0)  # merge --abort
    cmd.queue_result(returncode=0)  # fetch
    cmd.queue_result(returncode=0)  # merge
    cmd.queue_result(returncode=0)  # push
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=SyncBase(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert state.iter_count == 1
    async with factory() as s:
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id, limit=20)
        push_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )
    sync_operation = next(operation for operation in operations if operation.type == "sync_base")
    assert len(push_events) == 1
    assert push_events[0].payload == {
        "schema": "control_audit.v1",
        "actor": "pr_monitor",
        "source": "pr_monitor",
        "action": "sync_base_push",
        "outcome": "succeeded",
        "reason_code": "SYNC_BASE",
        "operation_id": sync_operation.id,
        "operation_type": "sync_base",
        "pr_number": 42,
        "pr_url": "https://github.com/dimileeh/aira-web/pull/42",
        "source_head_sha": "abc1234567890def",
        "source_base_sha": "a" * 40,
        "target_branch": "development",
        "remote_branch": f"awf/{workspace_id}",
        "branch_name": f"awf/{workspace_id}",
    }
