"""Focused branch-coverage tests for PR monitor runner edge behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
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
from awf.runtime.ownership import AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
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
)
from awf.runtime.pr_monitor_runner import (
    PullRequestMonitorRunner,
)
from awf.runtime.pr_monitor_runner.remote_ops import (
    _GitPushResult,
    _ProtectedScopePushBlock,
)
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
)
from awf.service.merge_queue import MergeQueueBlocker
from awf.service.supply_chain_policy import SupplyChainPolicyRefreshService
from tests.postgres import postgres_test_engine
from tests.shared.monitor_runner import DefaultMergeMethodGitHubClient
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
@pytest.mark.parametrize(
    ("status_returncode", "status_stdout", "status_stderr", "expected_reason"),
    [
        (0, " M leftover.txt\n?? scratch.log\n", "", "PRE_EXISTING_DIRTY_WORKTREE"),
        (128, "", "fatal: not a git repository\n", "REPAIR_WORKTREE_STATUS_FAILED"),
    ],
)
async def test_execute_ci_repair_start_failures_are_terminal(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    status_returncode: int,
    status_stdout: str,
    status_stderr: str,
    expected_reason: str,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(
        returncode=status_returncode,
        stdout=status_stdout,
        stderr=status_stderr,
    )
    adapter = FakeAdapter()
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
            failures=(CheckFailure(name="test", conclusion="FAILURE", log_excerpt="pytest failed"),)
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
    assert adapter.calls == []
    assert [call.args for call in cmd.calls] == [
        _git_worktree_command(worktree, "status", "--porcelain", "--untracked-files=all")
    ]
    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id, limit=10)

    assert workspace is not None
    assert workspace.status == WorkspaceStatus.failed.value
    assert workspace.events[-1].reason_code == expected_reason
    ci_operation = next(operation for operation in operations if operation.type == "ci_repair")
    assert ci_operation.status == OperationStatus.failed.value
    assert ci_operation.error_code == expected_reason
    assert ci_operation.result["outcome"] == "repair_start_blocked"
    assert ci_operation.result["reason_code"] == expected_reason


@pytest.mark.unit
async def test_execute_comment_repair_pre_existing_dirty_worktree_is_terminal(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=" M leftover.txt\n")
    adapter = FakeAdapter()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    thread = ReviewThread(
        thread_id="T_dirty_start",
        path="src/app.py",
        line=12,
        body_excerpt="please fix",
        author="reviewer",
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
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True
    assert state.iter_count == 0
    assert adapter.calls == []
    assert "T_dirty_start" not in state.threads_addressed_ids
    assert [call.args for call in cmd.calls] == [
        _git_worktree_command(worktree, "status", "--porcelain", "--untracked-files=all")
    ]
    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id, limit=10)

    assert workspace is not None
    assert workspace.status == WorkspaceStatus.failed.value
    assert workspace.events[-1].reason_code == "PRE_EXISTING_DIRTY_WORKTREE"
    comment_operation = next(
        operation for operation in operations if operation.type == "comment_repair"
    )
    assert comment_operation.status == OperationStatus.failed.value
    assert comment_operation.error_code == "PRE_EXISTING_DIRTY_WORKTREE"
    assert comment_operation.result["outcome"] == "repair_start_blocked"


@pytest.mark.unit
async def test_execute_ci_repair_missing_operation_start_head_is_terminal(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # clean worktree before repair
    cmd.queue_result(returncode=128, stderr="fatal: cannot resolve HEAD\n")
    adapter = FakeAdapter()
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
            failures=(CheckFailure(name="test", conclusion="FAILURE", log_excerpt="pytest failed"),)
        ),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(head_sha=""),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True
    assert state.iter_count == 0
    assert adapter.calls == []
    assert [call.args for call in cmd.calls] == [
        _git_worktree_command(worktree, "status", "--porcelain", "--untracked-files=all"),
        _git_worktree_command(worktree, "rev-parse", "HEAD"),
    ]
    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id, limit=10)

    assert workspace is not None
    assert workspace.status == WorkspaceStatus.failed.value
    assert workspace.events[-1].reason_code == "REPAIR_START_HEAD_UNAVAILABLE"
    ci_operation = next(operation for operation in operations if operation.type == "ci_repair")
    assert ci_operation.status == OperationStatus.failed.value
    assert ci_operation.error_code == "REPAIR_START_HEAD_UNAVAILABLE"
    assert ci_operation.result["outcome"] == "repair_start_blocked"
    assert ci_operation.result["failure_evidence"]["phase"] == "repair_start"


@pytest.mark.unit
async def test_execute_comment_repair_missing_operation_start_head_is_terminal(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # clean worktree before repair
    cmd.queue_result(returncode=0, stdout="")
    adapter = FakeAdapter()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    thread = ReviewThread(
        thread_id="T_missing_start",
        path="src/app.py",
        line=12,
        body_excerpt="please fix",
        author="reviewer",
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=AddressComments(threads=(thread,), review_comments=()),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(head_sha="", threads=(thread,)),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is True
    assert state.iter_count == 0
    assert adapter.calls == []
    assert "T_missing_start" not in state.threads_addressed_ids
    assert [call.args for call in cmd.calls] == [
        _git_worktree_command(worktree, "status", "--porcelain", "--untracked-files=all"),
        _git_worktree_command(worktree, "rev-parse", "HEAD"),
    ]
    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id, limit=10)

    assert workspace is not None
    assert workspace.status == WorkspaceStatus.failed.value
    assert workspace.events[-1].reason_code == "REPAIR_START_HEAD_UNAVAILABLE"
    comment_operation = next(
        operation for operation in operations if operation.type == "comment_repair"
    )
    assert comment_operation.status == OperationStatus.failed.value
    assert comment_operation.error_code == "REPAIR_START_HEAD_UNAVAILABLE"
    assert comment_operation.result["outcome"] == "repair_start_blocked"
    assert comment_operation.result["failure_evidence"]["phase"] == "repair_start"


@pytest.mark.unit
async def test_ci_fix_protected_scope_repair_ownership_repair_failure_returns_failed_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(stdout="attempted ci fix")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _protected_scope_push_block(**_kwargs: object) -> _ProtectedScopePushBlock:
        # A diff-unavailable block (NO violations) still routes through
        # ``_repair_protected_scope_commits_before_push`` — only a block WITH
        # violations takes the WS-2 protected-pause path — so this exercises an
        # ownership-repair failure surfacing from the diff-unavailable repair branch.
        return _ProtectedScopePushBlock(
            message="protected scope diff unavailable",
            reason_code="PROTECTED_SCOPE_DIFF_UNAVAILABLE",
            violations=(),
        )

    async def _repair_ownership_failed(**_kwargs: object) -> _GitPushResult:
        raise _MonitorAgentRuntimeOwnershipRepairFailedError(
            AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
        )

    monkeypatch.setattr(runner, "_protected_scope_push_block", _protected_scope_push_block)
    monkeypatch.setattr(
        runner,
        "_repair_protected_scope_commits_before_push",
        _repair_ownership_failed,
    )

    with pytest.raises(_MonitorAgentRuntimeOwnershipRepairFailedError) as exc_info:
        await runner._run_ci_fix(
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            failures=(
                CheckFailure(name="pytest", conclusion="FAILURE", log_excerpt="assert 1 == 2"),
            ),
            compose_project=f"awf_{workspace_id}",
            compose_file=tmp_path / "compose.yml",
            workspace_id=workspace_id,
            remote_branch=f"awf/{workspace_id}",
        )

    assert exc_info.value.reason_code == AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE


@pytest.mark.unit
async def test_ci_fix_ownership_repair_failure_blocks_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(stdout="attempted ci fix")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _ownership_repair_failed(**_kwargs: object) -> bool:
        raise _MonitorAgentRuntimeOwnershipRepairFailedError(
            AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
        )

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _ownership_repair_failed)

    push_result = await runner._run_ci_fix(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        failures=(CheckFailure(name="pytest", conclusion="FAILURE", log_excerpt="assert 1 == 2"),),
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        workspace_id=workspace_id,
        remote_branch=f"awf/{workspace_id}",
    )

    assert push_result.failed is True
    assert push_result.pushed is False
    assert push_result.returncode == 1
    assert push_result.reason_code == AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
    assert push_result.stderr == AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE


@pytest.mark.unit
async def test_refresh_supply_chain_policy_before_push_propagates_type_error(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _raise_type_error(
        self: SupplyChainPolicyRefreshService,
        workspace_id: str,
        *,
        command_evidence: Sequence[str],
        changed_paths: Sequence[str],
    ) -> object:
        del self, workspace_id, command_evidence, changed_paths
        raise TypeError("policy refresh passed the wrong argument type")

    monkeypatch.setattr(
        SupplyChainPolicyRefreshService,
        "refresh_workspace_open_candidate",
        _raise_type_error,
    )

    with pytest.raises(TypeError, match="wrong argument type"):
        await runner._refresh_supply_chain_policy_before_push(
            workspace_id="ws_type_error",
            command_evidence=(),
            changed_paths=(),
        )


@pytest.mark.unit
async def test_git_push_result_blocks_existing_supply_chain_finding_before_git_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        ws.resolved_profile = {
            "security": {
                "supply_chain": {
                    "remote_script_execution": {"mode": "block"},
                }
            }
        }
        await SupplyChainPolicyRefreshService(s).refresh_workspace_open_candidate(
            workspace_id,
            command_evidence="$ curl https://install.example/setup.sh | sh",
            changed_paths=(),
        )
        await s.commit()

    cmd = FakeCommandRunner()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)

    push_result = await runner._git_push_result(
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
    )

    assert push_result.failed is True
    assert push_result.pushed is False
    assert "SUPPLY_CHAIN_REMOTE_SCRIPT_EXECUTION" in push_result.stderr
    assert cmd.calls == []


@pytest.mark.unit
async def test_ci_fix_pauses_into_blocked_when_committed_protected_quality_gate_edits(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """WS-2: a protected-scope violation in an unpushed CI-repair commit PAUSES the
    workspace into ``blocked`` for an operator decision (preserving the offending
    commit) instead of the old silent ``git reset --hard`` rollback that failed the
    workspace. Wires the CI-repair push site into the protected-pause flow the
    comment-addressing path already uses (PRRT_kwDOSJAM6s6KFDHT)."""
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
    cmd.queue_result(returncode=0, stdout="blocked-head-sha\n")  # preserved HEAD (no reset)
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
    assert push_result.paused_into_blocked is True
    assert push_result.reason_code == "PROTECTED_SCOPE_PAUSED_BLOCKED"
    assert ".github/workflows/ci.yml" in push_result.stderr
    assert len(adapter.calls) == 1
    assert push_result.details is not None
    assert push_result.details["preserved_head_sha"] == "blocked-head-sha"
    assert push_result.details["paused_into_blocked"] is True
    call_args = [call.args for call in cmd.calls]
    # The offending commit is PRESERVED — no reset/clean before the operator decides.
    assert not any(
        args[:1] == ["git"] and "reset" in args and "--hard" in args for args in call_args
    )
    assert not any(args[:1] == ["git"] and "push" in args for args in call_args)
    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)
        assert workspace is not None
        assert workspace.status == "blocked"
        assert workspace.block_epoch == 1
        assert workspace.block_resume_phase == "monitor_protected_scope_push"
        events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.monitor_protected_scope_paused",
            limit=10,
        )
    assert len(events) == 1
    assert events[0].reason_code == "PROTECTED_SCOPE_PAUSED_BLOCKED"
    assert events[0].payload is not None
    assert events[0].payload["paths"] == [".github/workflows/ci.yml"]
    assert events[0].payload["preserved_head_sha"] == "blocked-head-sha"


@pytest.mark.unit
async def test_unpushed_commit_protected_scope_detects_rename_source(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)
        assert workspace is not None
        workspace.owned_paths = ["src/**"]
        await s.commit()

    workflow_text = "name: CI\non: [pull_request]\njobs: {}\n"
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # fetch remote branch for committed diff
    cmd.queue_result(returncode=0, stdout="merge-base-sha\n")
    cmd.queue_result(
        returncode=0,
        stdout="R100\0.github/workflows/ci.yml\0docs/ci.yml\0",
    )
    cmd.queue_result(returncode=0)  # cat-file merge-base:.github/workflows/ci.yml
    cmd.queue_result(returncode=0, stdout=workflow_text)
    cmd.queue_result(returncode=128, stderr="path does not exist in HEAD")
    cmd.queue_result(returncode=0)  # ls-tree confirms renamed source is absent from HEAD
    cmd.queue_result(returncode=0, stdout="diff --git a/.github/workflows/ci.yml b/docs/ci.yml\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    violations = await runner._protected_scope_violations_for_unpushed_commits(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
    )

    assert len(violations) == 1
    assert violations[0].path == ".github/workflows/ci.yml"
    assert "workflow file deleted outside declared owned_paths" in violations[0].reason
    assert _git_worktree_command(
        worktree,
        "show",
        "merge-base-sha:.github/workflows/ci.yml",
    ) in [call.args for call in cmd.calls]


class _CiPauseRecordingGh(DefaultMergeMethodGitHubClient):
    """gh double that records protected-pause notification ``post_comment`` calls."""

    def __init__(self, cmd: FakeCommandRunner) -> None:
        super().__init__(cmd)
        self.posts: list[dict[str, object]] = []

    async def post_comment(self, *, repo: object, pr_number: int, body: str) -> None:
        self.posts.append({"repo": repo, "pr_number": pr_number, "body": body})


@pytest.mark.unit
async def test_execute_ci_fix_pauses_into_blocked_when_local_commit_touches_protected_scope(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """WS-2: the loop's ``ReportCiFailure`` path pauses the workspace into ``blocked``
    for an operator decision (preserving the offending commit) when a CI-repair commit
    touches an unowned protected file, ending the monitor cycle cleanly with a
    succeeded ``protected_scope_paused`` operation instead of the old rollback that
    failed the workspace (PRRT_kwDOSJAM6s6KFDHT)."""
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
    cmd.queue_result(returncode=0, stdout="blocked-head-sha\n")  # preserved HEAD (no reset)
    adapter = FakeAdapter()
    adapter.queue(stdout="Committed locally.")
    gh = _CiPauseRecordingGh(cmd)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
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
    assert len(adapter.calls) == 1
    push_calls = [
        call.args for call in cmd.calls if call.args[:1] == ["git"] and "push" in call.args
    ]
    assert push_calls == []
    # The offending commit is PRESERVED — no reset/clean before the operator decides.
    assert not any(
        call.args[:1] == ["git"] and "reset" in call.args and "--hard" in call.args
        for call in cmd.calls
    )
    # An operator notification comment was posted to the PR.
    assert len(gh.posts) == 1
    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id, limit=20)
        paused_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.monitor_protected_scope_paused",
            limit=10,
        )

    assert workspace is not None
    assert workspace.status == WorkspaceStatus.blocked.value
    assert workspace.block_epoch == 1
    assert workspace.block_resume_phase == "monitor_protected_scope_push"
    ci_operation = next(operation for operation in operations if operation.type == "ci_repair")
    assert ci_operation.status == OperationStatus.succeeded.value
    assert ci_operation.result["outcome"] == "protected_scope_paused"
    assert ci_operation.result["reason_code"] == "PROTECTED_SCOPE_PAUSED_BLOCKED"
    assert ci_operation.result["pushed"] is False
    assert len(paused_events) == 1
    assert paused_events[0].payload is not None
    assert paused_events[0].payload["paths"] == [".github/workflows/ci.yml"]
    assert paused_events[0].payload["preserved_head_sha"] == "blocked-head-sha"


@pytest.mark.unit
async def test_protected_scope_commit_repair_rolls_back_delta_without_agent_or_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="blocked-head-sha\n")  # attempted HEAD
    cmd.queue_result(
        returncode=0,
        stdout=_name_status_z(
            ".github/workflows/ci.yml",
            "plans/PR282_CI_SETUP_UV_PLAN.md",
            "plans/strange\nname.md",
            "tests/unit/control/test_ci_workflow_toolchain.py",
        ),
    )
    cmd.queue_result(returncode=0, stdout="?? plans/orphan.md\0")
    cmd.queue_result(returncode=0, stdout="HEAD is now at start-sha\n")
    cmd.queue_result(returncode=0, stdout="")
    adapter = FakeAdapter()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    remote_branch = f"awf/{workspace_id}"

    push_result = await runner._repair_protected_scope_commits_before_push(
        workspace_id=workspace_id,
        pr_number=42,
        protected_scope_block=_ProtectedScopePushBlock(
            message="protected scope blocked",
            reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
            violations=(
                QualityGateViolation(
                    path=".github/workflows/ci.yml",
                    protected_pattern=".github/**",
                ),
            ),
        ),
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        remote_branch=remote_branch,
        operation_start_head="start-sha",
    )

    assert push_result.failed is True
    assert push_result.pushed is False
    assert push_result.returncode == 1
    assert push_result.reason_code == "PROTECTED_SCOPE_PUSH_BLOCKED"
    assert "rolled back the local repair delta" in push_result.stderr
    assert push_result.details is not None
    assert push_result.details["branch_restored"] is True
    assert push_result.details["reverted_paths"] == [
        ".github/workflows/ci.yml",
        "plans/PR282_CI_SETUP_UV_PLAN.md",
        "plans/orphan.md",
        "plans/strange\nname.md",
        "tests/unit/control/test_ci_workflow_toolchain.py",
    ]
    assert adapter.calls == []
    assert _git_worktree_command(worktree, "reset", "--hard", "start-sha") in [
        call.args for call in cmd.calls
    ]
    assert _git_worktree_command(
        worktree,
        "--literal-pathspecs",
        "clean",
        "-fd",
        "--",
        "plans/orphan.md",
    ) in [call.args for call in cmd.calls]
    assert not any(
        call.args == _git_worktree_command(worktree, "clean", "-fd") for call in cmd.calls
    )
    assert not any(call.args[:1] == ["git"] and "push" in call.args for call in cmd.calls)

    async with factory() as session:
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )

    assert [event.payload["outcome"] for event in events if event.payload] == [
        "succeeded",
        "requested",
    ]
    assert events[0].payload is not None
    assert events[0].payload["evidence"]["branch_restored"] is True


@pytest.mark.unit
async def test_protected_scope_rollback_failed_reset_omits_unattempted_clean_result(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="blocked-head-sha\n")  # attempted HEAD
    cmd.queue_result(returncode=0, stdout=_name_status_z(".github/workflows/ci.yml"))
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=128, stderr="fatal: could not reset\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    push_result = await runner._repair_protected_scope_commits_before_push(
        workspace_id=workspace_id,
        pr_number=42,
        protected_scope_block=_ProtectedScopePushBlock(
            message="protected scope blocked",
            reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
            violations=(
                QualityGateViolation(
                    path=".github/workflows/ci.yml",
                    protected_pattern=".github/**",
                ),
            ),
        ),
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
        operation_start_head="start-sha",
    )

    assert push_result.failed is True
    assert push_result.returncode == 128
    assert push_result.details is not None
    assert push_result.details["branch_restored"] is False
    assert "clean_attempted" not in push_result.details
    assert "clean_returncode" not in push_result.details
    assert not any("clean" in call.args for call in cmd.calls)


@pytest.mark.unit
async def test_protected_scope_rollback_distinguishes_reset_from_incomplete_cleanup_evidence(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="blocked-head-sha\n")  # attempted HEAD
    cmd.queue_result(returncode=0, stdout=_name_status_z(".github/workflows/ci.yml"))
    cmd.queue_result(returncode=128, stderr="fatal: status unavailable\n")
    cmd.queue_result(returncode=0, stdout="HEAD is now at start-sha\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    push_result = await runner._repair_protected_scope_commits_before_push(
        workspace_id=workspace_id,
        pr_number=42,
        protected_scope_block=_ProtectedScopePushBlock(
            message="protected scope blocked",
            reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
            violations=(
                QualityGateViolation(
                    path=".github/workflows/ci.yml",
                    protected_pattern=".github/**",
                ),
            ),
        ),
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
        operation_start_head="start-sha",
    )

    assert push_result.failed is True
    assert push_result.returncode == 1
    assert "reset the local repair delta" in push_result.stderr
    assert "untracked repair leftovers may remain" in push_result.stderr
    assert "local rollback failed" not in push_result.stderr
    assert push_result.details is not None
    assert push_result.details["branch_reset"] is True
    assert push_result.details["branch_restored"] is False
    assert push_result.details["untracked_evidence_complete"] is False
    assert push_result.details["rollback_status"] == "reset_succeeded_cleanup_uncertain"
    assert push_result.details["reverted_path_collection_errors"] == [
        {
            "phase": "worktree_status_command",
            "returncode": 128,
            "stderr": "fatal: status unavailable\n",
        }
    ]
    assert not any("clean" in call.args for call in cmd.calls)


@pytest.mark.unit
async def test_protected_scope_commit_repair_missing_start_head_does_not_push_or_repair(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="blocked-head-sha\n")
    adapter = FakeAdapter()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    push_result = await runner._repair_protected_scope_commits_before_push(
        workspace_id=workspace_id,
        pr_number=42,
        protected_scope_block=_ProtectedScopePushBlock(
            message="protected scope blocked",
            reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
            violations=(
                QualityGateViolation(
                    path=".github/workflows/ci.yml",
                    protected_pattern=".github/**",
                ),
            ),
        ),
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
    )

    assert push_result.failed is True
    assert push_result.pushed is False
    assert push_result.reason_code == "PROTECTED_SCOPE_PUSH_BLOCKED"
    assert "operation start commit was unavailable" in push_result.stderr
    assert push_result.details is not None
    assert push_result.details["rollback_status"] == "skipped_missing_operation_start_head"
    assert push_result.details["branch_restored"] is False
    assert adapter.calls == []
    assert not any(call.args[:1] == ["git"] and "push" in call.args for call in cmd.calls)
    assert _git_worktree_command(worktree, "reset", "--hard", "start-sha") not in [
        call.args for call in cmd.calls
    ]


@pytest.mark.unit
async def test_protected_scope_revert_verifies_tracked_restore_against_fetch_head(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # fetch remote branch
    cmd.queue_result(returncode=0)  # tracked path matches FETCH_HEAD
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    remaining = await runner._protected_scope_violations_not_restored_to_remote_branch(
        workspace_id=workspace_id,
        status_stdout=" M .github/workflows/ci.yml\n",
        violations=[
            QualityGateViolation(
                path=".github/workflows/ci.yml",
                protected_pattern=".github/**",
            )
        ],
        remote_branch=f"awf/{workspace_id}",
    )

    assert remaining == []
    assert [call.args for call in cmd.calls] == [
        _git_worktree_command(
            worktree,
            "fetch",
            "origin",
            f"refs/heads/awf/{workspace_id}",
        ),
        _git_worktree_command(
            worktree,
            "diff",
            "--quiet",
            "FETCH_HEAD",
            "--",
            ".github/workflows/ci.yml",
        ),
    ]


@pytest.mark.unit
async def test_protected_scope_revert_skips_empty_violation_list(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    remaining = await runner._protected_scope_violations_not_restored_to_remote_branch(
        workspace_id=workspace_id,
        status_stdout="",
        violations=[],
        remote_branch=f"awf/{workspace_id}",
    )

    assert remaining == []
    assert cmd.calls == []


@pytest.mark.unit
async def test_protected_scope_revert_raises_when_remote_fetch_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=128, stdout="", stderr="no such ref")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    with pytest.raises(ProtectedScopeDiffError, match="fetch refs/heads"):
        await runner._protected_scope_violations_not_restored_to_remote_branch(
            workspace_id=workspace_id,
            status_stdout=" M .github/workflows/ci.yml\n",
            violations=[
                QualityGateViolation(
                    path=".github/workflows/ci.yml",
                    protected_pattern=".github/**",
                )
            ],
            remote_branch=f"awf/{workspace_id}",
        )


@pytest.mark.unit
async def test_protected_scope_revert_verifies_untracked_restore_against_fetch_head(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # fetch remote branch
    cmd.queue_result(returncode=0, stdout="remote-blob\n")
    cmd.queue_result(returncode=0, stdout="remote-blob\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    remaining = await runner._protected_scope_violations_not_restored_to_remote_branch(
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

    assert remaining == []
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
