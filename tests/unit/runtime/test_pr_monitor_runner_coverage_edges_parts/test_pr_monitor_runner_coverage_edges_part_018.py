"""Focused branch-coverage tests for PR monitor runner edge behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_mock
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import AsyncioSubprocessRunner, CommandResult, FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.db.enums import OperationStatus, OperationType, TaskClass, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    OperationRepository,
    PolicyFindingRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckState,
    CheckTiming,
    MergeableState,
    MergeStateStatus,
    PRStatus,
    ReviewComment,
    ReviewThread,
)
from awf.runtime.pr_monitor_runner import (
    PullRequestMonitorRunner,
)
from awf.runtime.pr_monitor_runner.types import (
    BaseBehindCountError,
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


@pytest.mark.integration
async def test_changed_paths_since_remote_branch_reports_only_local_paths_when_remote_diverged(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    command_runner = AsyncioSubprocessRunner()

    async def run(*args: str, cwd: Path | None = None) -> None:
        result = await command_runner.run(list(args), cwd=str(cwd) if cwd else None)
        assert result.ok, result.stderr

    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    local = tmp_path / "local"
    remote_writer = tmp_path / "remote-writer"
    remote_branch = "awf/ws_remote_diverged"

    await run("git", "init", "--bare", str(remote))
    await run("git", "clone", str(remote), str(seed))
    await run("git", "config", "user.email", "awf@example.com", cwd=seed)
    await run("git", "config", "user.name", "AWF Test", cwd=seed)
    (seed / "README.md").write_text("base\n")
    await run("git", "add", "README.md", cwd=seed)
    await run("git", "commit", "-m", "base", cwd=seed)
    await run("git", "branch", "-M", "main", cwd=seed)
    await run("git", "push", "origin", "main", cwd=seed)
    await run("git", "checkout", "-b", remote_branch, cwd=seed)
    await run("git", "push", "origin", remote_branch, cwd=seed)

    await run("git", "clone", str(remote), str(local))
    await run("git", "checkout", remote_branch, cwd=local)
    await run("git", "config", "user.email", "awf@example.com", cwd=local)
    await run("git", "config", "user.name", "AWF Test", cwd=local)
    (local / "src").mkdir()
    (local / "src" / "fix.py").write_text("print('local fix')\n")
    await run("git", "add", "src/fix.py", cwd=local)
    await run("git", "commit", "-m", "local fix", cwd=local)

    await run("git", "clone", str(remote), str(remote_writer))
    await run("git", "checkout", remote_branch, cwd=remote_writer)
    await run("git", "config", "user.email", "awf@example.com", cwd=remote_writer)
    await run("git", "config", "user.name", "AWF Test", cwd=remote_writer)
    (remote_writer / ".github" / "workflows").mkdir(parents=True)
    (remote_writer / ".github" / "workflows" / "ci.yml").write_text("name: ci\n")
    await run("git", "add", ".github/workflows/ci.yml", cwd=remote_writer)
    await run("git", "commit", "-m", "remote ci", cwd=remote_writer)
    await run("git", "push", "origin", remote_branch, cwd=remote_writer)

    runner = make_runner(
        factory=factory,
        cmd=command_runner,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    paths = await runner._changed_paths_since_remote_branch(
        workspace_id="ws_remote_diverged",
        worktree_path=local,
        remote_branch=remote_branch,
    )

    assert paths == ("src/fix.py",)


@pytest.mark.unit
async def test_changed_paths_since_remote_branch_fails_closed_when_refs_are_unavailable(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=128, stderr="unknown remote ref")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    workspace_id = await seed_monitoring_workspace(factory)
    with pytest.raises(ProtectedScopeDiffError) as exc_info:
        await runner._changed_paths_since_remote_branch(
            workspace_id=workspace_id,
            worktree_path=tmp_path / "worktree",
            remote_branch="awf/ws_remote_missing",
        )

    message = str(exc_info.value)
    assert "fetch refs/heads/awf/ws_remote_missing" in message
    assert "unknown remote ref" in message


@pytest.mark.unit
async def test_changed_paths_since_remote_branch_fails_closed_when_merge_base_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=1, stderr="no merge base")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    with pytest.raises(ProtectedScopeDiffError) as exc_info:
        await runner._changed_paths_since_remote_branch(
            workspace_id=workspace_id,
            worktree_path=tmp_path / "worktree",
            remote_branch="awf/ws_remote_missing",
        )

    message = str(exc_info.value)
    assert "merge-base FETCH_HEAD HEAD" in message
    assert "no merge base" in message


@pytest.mark.unit
async def test_changed_paths_since_remote_branch_fails_closed_when_diff_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="merge-base-sha\n")
    cmd.queue_result(returncode=128, stderr="bad revision merge-base-sha")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    with pytest.raises(ProtectedScopeDiffError) as exc_info:
        await runner._changed_paths_since_remote_branch(
            workspace_id=workspace_id,
            worktree_path=tmp_path / "worktree",
            remote_branch="awf/ws_remote_missing",
        )

    message = str(exc_info.value)
    assert "diff merge-base-sha..HEAD" in message
    assert "bad revision merge-base-sha" in message


@pytest.mark.unit
async def test_sync_base_protected_scope_resolves_merged_base_before_base_diff(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # fetch remote branch for committed diff
    cmd.queue_result(returncode=0, stdout="merge-base-sha\n")
    cmd.queue_result(
        returncode=0, stdout=_name_status_z("src/fix.py")
    )  # diff against remote PR branch
    cmd.queue_result(returncode=0, stdout="")  # refresh base branch
    cmd.queue_result(returncode=0, stdout="merged-base-sha\n")
    cmd.queue_result(returncode=0, stdout=_name_status_z("src/fix.py"))  # diff against merged base
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    worktree = tmp_path / "worktree"

    await runner._protected_scope_violations_for_sync_base_push(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        base_branch="development",
    )

    assert [call.args for call in cmd.calls] == [
        _git_worktree_command(
            worktree,
            "fetch",
            "origin",
            f"refs/heads/awf/{workspace_id}",
        ),
        _git_worktree_command(
            worktree,
            "merge-base",
            "FETCH_HEAD",
            "HEAD",
        ),
        _git_worktree_command(
            worktree,
            "diff",
            "--name-status",
            "-z",
            "merge-base-sha..HEAD",
            "--",
        ),
        _git_worktree_command(
            worktree,
            "fetch",
            "origin",
            "+refs/heads/development:refs/remotes/origin/development",
        ),
        _git_worktree_command(
            worktree,
            "merge-base",
            "origin/development",
            "HEAD",
        ),
        _git_worktree_command(
            worktree,
            "diff",
            "--name-status",
            "-z",
            "merged-base-sha..HEAD",
            "--",
        ),
    ]


@pytest.mark.unit
async def test_sync_base_protected_scope_diffs_use_remote_branch_base(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    workflow_text = "name: CI\non: [pull_request]\njobs: {}\n"
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # fetch remote branch for committed diff
    cmd.queue_result(returncode=0, stdout="remote-branch-base-sha\n")
    cmd.queue_result(
        returncode=0,
        stdout=_name_status_z(".github/workflows/ci.yml"),
    )  # diff against remote PR branch
    cmd.queue_result(returncode=0, stdout="")  # refresh base branch
    cmd.queue_result(returncode=0, stdout="merged-base-sha\n")
    cmd.queue_result(
        returncode=0,
        stdout=_name_status_z(".github/workflows/ci.yml"),
    )  # diff against merged base
    _queue_protected_workflow_diff(cmd, old_text=workflow_text, new_text=workflow_text)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    worktree = tmp_path / "worktree"

    violations = await runner._protected_scope_violations_for_sync_base_push(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
        base_branch="development",
    )

    assert violations == []
    assert _git_worktree_command(
        worktree,
        "show",
        "remote-branch-base-sha:.github/workflows/ci.yml",
    ) in [call.args for call in cmd.calls]
    assert _git_worktree_command(
        worktree,
        "diff",
        "--unified=0",
        "remote-branch-base-sha..HEAD",
        "--",
        ".github/workflows/ci.yml",
    ) not in [call.args for call in cmd.calls]


@pytest.mark.unit
async def test_protected_scope_push_check_blocks_when_diff_baseline_cannot_be_resolved(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
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
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)

    block = await runner._protected_scope_push_block(
        workspace_id=workspace_id,
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
    )

    assert block is not None
    assert block.reason_code == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
    assert "could not verify protected-scope changes before push" in block.message
    assert "unknown remote ref" in block.message
    async with factory() as s:
        events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.monitor_protected_scope_push_blocked",
            limit=10,
        )
    assert len(events) == 1
    assert events[0].reason_code == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
    assert events[0].payload is not None
    assert events[0].payload["reason"] == "diff_baseline_unavailable"


@pytest.mark.unit
async def test_active_policy_block_message_propagates_session_factory_type_error(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    def _legacy_session_factory() -> object:
        raise TypeError("legacy test double")

    runner._deps.session_factory = _legacy_session_factory  # type: ignore[assignment]

    with pytest.raises(TypeError, match="legacy test double"):
        await runner._active_policy_block_message("ws_legacy")


@pytest.mark.unit
async def test_active_policy_block_message_propagates_repository_type_error(
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
        self: PolicyFindingRepository,
        workspace_id: str,
    ) -> object:
        del self, workspace_id
        raise TypeError("policy finding query passed the wrong argument type")

    monkeypatch.setattr(
        PolicyFindingRepository,
        "list_active_for_workspace",
        _raise_type_error,
    )

    with pytest.raises(TypeError, match="wrong argument type"):
        await runner._active_policy_block_message("ws_type_error")


@pytest.mark.unit
async def test_protected_scope_status_check_ignores_empty_or_missing_workspace(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert (
        await runner._protected_scope_violations_for_status(
            workspace_id="ws_missing",
            status_stdout="",
        )
        == []
    )
    assert (
        await runner._protected_scope_violations_for_status(
            workspace_id="ws_missing",
            status_stdout=" M .github/workflows/ci.yml\n",
        )
        == []
    )


@pytest.mark.unit
async def test_git_helpers_handle_bad_base_count_and_push_rejection_recovery(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1, stderr="rev-list failed")
    cmd.queue_result(returncode=0, stdout="not an int\n")
    cmd.queue_result(returncode=1, stderr="[rejected] non-fast-forward")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    worktree = tmp_path / "worktrees" / "ws_git"

    with pytest.raises(BaseBehindCountError):
        await runner._count_base_behind(worktree_path=worktree, base_branch="main")
    with pytest.raises(BaseBehindCountError):
        await runner._count_base_behind(worktree_path=worktree, base_branch="main")
    assert await runner._git_push(worktree_path=worktree, remote_branch="awf/ws_git") is False

    assert cmd.calls[-2].args[-2:] == ["origin", "awf/ws_git"]
    assert cmd.calls[-1].args[-2:] == ["--hard", "origin/awf/ws_git"]


@pytest.mark.unit
async def test_fork_push_rejection_does_not_reset_when_fetch_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1, stderr="[rejected] non-fast-forward")
    cmd.queue_result(returncode=128, stderr="fatal: could not read Username")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    worktree = tmp_path / "worktrees" / "ws_fork"

    result = await runner._git_push_result(
        worktree_path=worktree,
        remote_branch="fix/review",
        remote_url="https://github.com/contributor/aira-web.git",
    )

    assert result.failed is True
    assert result.recovered_by_resync is False
    assert "resync fetch failed" in result.stderr
    assert cmd.calls[0].args[-2:] == [
        "https://github.com/contributor/aira-web.git",
        "HEAD:refs/heads/fix/review",
    ]
    assert cmd.calls[1].args[-2:] == [
        "https://github.com/contributor/aira-web.git",
        "refs/heads/fix/review",
    ]
    assert not any("reset" in call.args for call in cmd.calls)


@pytest.mark.unit
async def test_fetch_base_repairs_multiple_broken_awf_refs_before_failing_workspace(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    fetch_base_once = mocker.patch.object(
        runner,
        "_fetch_base_once",
        mocker.AsyncMock(
            side_effect=[
                CommandResult(returncode=1, stdout="", stderr="bad ref ws_old_1"),
                CommandResult(returncode=1, stdout="", stderr="bad ref ws_old_2"),
                CommandResult(returncode=0, stdout="", stderr=""),
            ]
        ),
    )
    repair = mocker.patch.object(
        runner,
        "_repair_orphaned_broken_awf_ref",
        mocker.AsyncMock(side_effect=[True, True]),
    )

    await runner._fetch_base(
        workspace_id="ws_current",
        worktree_path=tmp_path / "worktrees" / "ws_current",
        base_branch="development",
    )

    assert fetch_base_once.await_count == 3
    assert repair.await_count == 2
    assert [call.kwargs["stderr"] for call in repair.await_args_list] == [
        "bad ref ws_old_1",
        "bad ref ws_old_2",
    ]
