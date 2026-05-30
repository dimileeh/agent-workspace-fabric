"""Focused branch-coverage tests for PR monitor runner edge behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_mock
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.common.github_client import RepoRef
from awf.control.quality_gates_common import QualityGateViolation
from awf.db.enums import OperationStatus, OperationType, TaskClass, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    OperationRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckState,
    CheckTiming,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReviewComment,
    ReviewThread,
)
from awf.runtime.pr_monitor_runner import (
    PullRequestMonitorRunner,
)
from awf.runtime.pr_monitor_runner import remote_repair as pr_monitor_runner_remote_repair
from awf.runtime.pr_monitor_runner import remote_repair as pr_remote_repair
from awf.runtime.pr_monitor_runner.helpers import (
    _is_protected_manual_ready_handoff,
    _review_comment_body_state_key,
)
from awf.runtime.pr_monitor_runner.types import (
    BaseFetchError,
    ProtectedScopeDiffError,
    ProviderRecoveryRetryError,
    _ProtectedScopeRollbackDeltaEvidence,
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


def _protected_workflow_violation() -> QualityGateViolation:
    return QualityGateViolation(
        path=".github/workflows/ci.yml",
        protected_pattern=".github/workflows/*.yml",
        section="jobs.tests.steps[0]",
        line=12,
        reason="required test step policy changed",
    )


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
async def test_commit_dirty_worktree_stops_when_protected_scope_repair_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=" M .github/workflows/ci.yml\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _repair_fails(**_kwargs: object) -> object | None:
        return None

    monkeypatch.setattr(
        runner,
        "_repair_protected_scope_changes_before_commit",
        _repair_fails,
    )

    assert not await runner._commit_dirty_worktree(
        workspace_id=workspace_id,
        message="fix: repair protected scope",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )
    assert len(cmd.calls) == 1


@pytest.mark.unit
async def test_commit_dirty_worktree_fails_closed_when_protected_revert_check_errors(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.owned_paths = ["src/**"]
        await session.commit()

    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=" M .github/workflows/ci.yml\n")
    cmd.queue_result(returncode=0)  # cat-file HEAD:.github/workflows/ci.yml
    cmd.queue_result(returncode=0, stdout=_PROTECTED_WORKFLOW_BLOCKED)
    cmd.queue_result(returncode=128, stderr="bad revision")
    adapter = FakeAdapter()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    with pytest.raises(ProtectedScopeDiffError):
        await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message="fix: repair protected scope",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            protected_scope_revert_remote_branch=f"awf/{workspace_id}",
        )
    assert adapter.calls == []
    call_args = [call.args for call in cmd.calls]
    assert not any(args[:1] == ["git"] and "add" in args for args in call_args)
    assert not any(args[:1] == ["git"] and "commit" in args for args in call_args)


@pytest.mark.unit
async def test_protected_scope_repair_raises_provider_retry_before_cli(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.owned_paths = ["src/**"]
        await session.commit()

    adapter = FakeAdapter()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    mocker.patch.object(
        runner,
        "_provider_recovery_suppresses_cli",
        mocker.AsyncMock(return_value=True),
    )

    with pytest.raises(ProviderRecoveryRetryError):
        await runner._repair_protected_scope_changes_before_commit(
            workspace_id=workspace_id,
            status_stdout=" M .github/workflows/ci.yml\n",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
    assert adapter.calls == []


@pytest.mark.unit
async def test_protected_scope_violations_skip_empty_status(
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
            workspace_id="ws_without_changes",
            status_stdout="",
        )
        == []
    )


@pytest.mark.unit
async def test_protected_scope_repair_records_remaining_violations_after_agent_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.owned_paths = ["src/**"]
        await session.commit()

    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(returncode=1, stdout="tool crashed before cleanup")
    cmd.queue_result(returncode=0)  # cat-file HEAD:.github/workflows/ci.yml
    cmd.queue_result(returncode=0, stdout=_PROTECTED_WORKFLOW_BLOCKED)
    cmd.queue_result(returncode=0, stdout=" M .github/workflows/ci.yml\n")
    cmd.queue_result(returncode=0)  # cat-file HEAD:.github/workflows/ci.yml
    cmd.queue_result(returncode=0, stdout=_PROTECTED_WORKFLOW_BLOCKED)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert (
        await runner._repair_protected_scope_changes_before_commit(
            workspace_id=workspace_id,
            status_stdout=" M .github/workflows/ci.yml\n",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        is None
    )

    async with factory() as s:
        events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.monitor_protected_scope_repair_failed",
            limit=10,
        )
    assert len(events) == 1
    assert events[0].reason_code == "PROTECTED_SCOPE_REPAIR_FAILED"
    assert events[0].payload is not None
    assert events[0].payload["paths"] == [".github/workflows/ci.yml"]


@pytest.mark.unit
async def test_protected_scope_status_check_wraps_diff_read_failures(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.owned_paths = ["src/**"]
        await session.commit()

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _raise_diff_read_failure(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("could not read protected file")

    monkeypatch.setattr(
        runner,
        "_protected_file_diffs_for_status_paths",
        _raise_diff_read_failure,
    )

    with pytest.raises(ProtectedScopeDiffError, match="Could not read dirty protected-scope"):
        await runner._protected_scope_violations_for_status(
            workspace_id=workspace_id,
            status_stdout=" M .github/workflows/ci.yml\n",
        )


@pytest.mark.unit
async def test_sync_base_protected_scope_covers_missing_and_empty_diff_edges(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    worktree = tmp_path / "worktree"

    with pytest.raises(ProtectedScopeDiffError, match="Workspace row ws_missing"):
        await runner._protected_scope_violations_for_sync_base_push(
            workspace_id="ws_missing",
            worktree_path=worktree,
            remote_branch="awf/ws_missing",
            base_branch="development",
        )

    async def _no_remote_changes(**_kwargs: object) -> tuple[str, tuple[str, ...]]:
        return ("remote-base", ())

    monkeypatch.setattr(runner, "_remote_branch_diff_base_and_changed_paths", _no_remote_changes)
    assert (
        await runner._protected_scope_violations_for_sync_base_push(
            workspace_id=workspace_id,
            worktree_path=worktree,
            remote_branch=f"awf/{workspace_id}",
            base_branch="development",
        )
        == []
    )

    async def _remote_changes(**_kwargs: object) -> tuple[str, tuple[str, ...]]:
        return ("remote-base", ("src/remote.py",))

    async def _base_fetch_fails(**_kwargs: object) -> None:
        raise BaseFetchError("network reset")

    monkeypatch.setattr(runner, "_remote_branch_diff_base_and_changed_paths", _remote_changes)
    monkeypatch.setattr(runner, "_fetch_base", _base_fetch_fails)
    with pytest.raises(ProtectedScopeDiffError, match="Could not refresh the base branch"):
        await runner._protected_scope_violations_for_sync_base_push(
            workspace_id=workspace_id,
            worktree_path=worktree,
            remote_branch=f"awf/{workspace_id}",
            base_branch="development",
        )

    async def _fetch_base_ok(**_kwargs: object) -> None:
        return None

    async def _merged_base(**_kwargs: object) -> str:
        return "merged-base"

    async def _no_base_changes(**_kwargs: object) -> tuple[str, ...]:
        return ()

    monkeypatch.setattr(runner, "_fetch_base", _fetch_base_ok)
    monkeypatch.setattr(runner, "_merge_base_with_head", _merged_base)
    monkeypatch.setattr(runner, "_changed_paths_between_ref_and_head", _no_base_changes)
    assert (
        await runner._protected_scope_violations_for_sync_base_push(
            workspace_id=workspace_id,
            worktree_path=worktree,
            remote_branch=f"awf/{workspace_id}",
            base_branch="development",
        )
        == []
    )

    async def _different_base_changes(**_kwargs: object) -> tuple[str, ...]:
        return ("src/base.py",)

    monkeypatch.setattr(runner, "_changed_paths_between_ref_and_head", _different_base_changes)
    assert (
        await runner._protected_scope_violations_for_sync_base_push(
            workspace_id=workspace_id,
            worktree_path=worktree,
            remote_branch=f"awf/{workspace_id}",
            base_branch="development",
        )
        == []
    )


@pytest.mark.unit
async def test_sync_base_protected_scope_wraps_committed_diff_read_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _remote_changes(**_kwargs: object) -> tuple[str, tuple[str, ...]]:
        return ("remote-base", (".github/workflows/ci.yml",))

    async def _fetch_base_ok(**_kwargs: object) -> None:
        return None

    async def _merged_base(**_kwargs: object) -> str:
        return "merged-base"

    async def _base_changes(**_kwargs: object) -> tuple[str, ...]:
        return (".github/workflows/ci.yml",)

    async def _raise_committed_diff_read(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError("show failed")

    monkeypatch.setattr(runner, "_remote_branch_diff_base_and_changed_paths", _remote_changes)
    monkeypatch.setattr(runner, "_fetch_base", _fetch_base_ok)
    monkeypatch.setattr(runner, "_merge_base_with_head", _merged_base)
    monkeypatch.setattr(runner, "_changed_paths_between_ref_and_head", _base_changes)
    monkeypatch.setattr(
        pr_remote_repair,
        "protected_file_diffs_for_committed_paths",
        _raise_committed_diff_read,
    )

    with pytest.raises(ProtectedScopeDiffError, match="sync-base protected-scope"):
        await runner._protected_scope_violations_for_sync_base_push(
            workspace_id=workspace_id,
            worktree_path=tmp_path / "worktree",
            remote_branch=f"awf/{workspace_id}",
            base_branch="development",
        )


@pytest.mark.unit
async def test_repair_operation_start_head_uses_fallback_when_worktree_missing(
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

    head, result = await runner._repair_operation_start_head_result(
        workspace_id="ws_missing_worktree",
        worktree_path=tmp_path / "does-not-exist",
        operation_type="review_fix",
        fallback_head_sha="f" * 40,
    )

    assert head == "f" * 40
    assert result is None
    assert cmd.calls == []


@pytest.mark.unit
async def test_repair_delta_paths_records_malformed_committed_diff_fallback_errors(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="not-nul-delimited")
    cmd.queue_result(returncode=0, stdout="valid.py\0\0")
    cmd.queue_result(returncode=0, stdout="")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    delta = await runner._protected_scope_repair_delta_paths(
        workspace_id="ws_delta",
        worktree_path=tmp_path,
        operation_start_head="a" * 40,
    )

    assert delta.reverted_paths == ()
    assert [error["phase"] for error in delta.collection_errors] == [
        "committed_diff_parse",
        "committed_diff_name_only_fallback_parse",
    ]


@pytest.mark.unit
async def test_repair_delta_paths_records_committed_diff_command_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=2, stderr="diff failed")
    cmd.queue_result(returncode=0, stdout="")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    delta = await runner._protected_scope_repair_delta_paths(
        workspace_id="ws_delta",
        worktree_path=tmp_path,
        operation_start_head="a" * 40,
    )

    assert delta.collection_errors == (
        {"phase": "committed_diff_command", "returncode": 2, "stderr": "diff failed"},
    )


@pytest.mark.unit
async def test_rollback_protected_scope_repair_delta_records_cleanup_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=1, stderr="leftover untracked file")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    recorded: dict[str, object] = {}

    async def _delta(**_kwargs: object) -> _ProtectedScopeRollbackDeltaEvidence:
        return _ProtectedScopeRollbackDeltaEvidence(
            reverted_paths=(".github/workflows/ci.yml",),
            cleanup_paths=("generated.tmp",),
        )

    async def _record(**kwargs: object) -> None:
        recorded.update(kwargs)

    monkeypatch.setattr(runner, "_protected_scope_repair_delta_paths", _delta)
    monkeypatch.setattr(runner, "_record_protected_scope_rollback_result", _record)

    result = await runner._rollback_protected_scope_repair_delta_before_push(
        workspace_id="ws_delta",
        pr_number=42,
        worktree_path=tmp_path,
        protected_scope_block=pr_remote_repair._ProtectedScopePushBlock(  # noqa: SLF001
            message="blocked",
            reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
            violations=(_protected_workflow_violation(),),
        ),
        operation_start_head="a" * 40,
        attempted_head="b" * 40,
        remote_branch="awf/ws_delta",
    )

    assert result.failed is True
    assert result.returncode == 1
    assert result.details is not None
    assert result.details["rollback_status"] == "reset_succeeded_cleanup_failed"
    assert result.details["clean_stderr"] == "leftover untracked file"
    assert recorded["outcome"] == "failed"


@pytest.mark.unit
async def test_protected_scope_repair_filters_remote_restored_status_violation(
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
    violation = _protected_workflow_violation()

    async def _violations_for_status(**_kwargs: object) -> tuple[QualityGateViolation, ...]:
        return (violation,)

    async def _not_restored(**_kwargs: object) -> tuple[QualityGateViolation, ...]:
        return ()

    monkeypatch.setattr(runner, "_protected_scope_violations_for_status", _violations_for_status)
    monkeypatch.setattr(
        runner,
        "_protected_scope_violations_not_restored_to_remote_branch",
        _not_restored,
    )

    result = await runner._repair_protected_scope_changes_before_commit(
        workspace_id="ws_delta",
        status_stdout=" M .github/workflows/ci.yml\n",
        compose_project="awf_ws_delta",
        compose_file=tmp_path / "compose.yml",
        state=MonitorState(),
        protected_scope_revert_remote_branch="awf/ws_delta",
        remote_push_url="git@example.com/repo.git",
    )

    assert result is not None
    assert result.ok


@pytest.mark.unit
async def test_protected_scope_repair_filters_remote_restored_remaining_violation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=" M .github/workflows/ci.yml\n")
    adapter = FakeAdapter()
    adapter.queue()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    violation = _protected_workflow_violation()
    status_calls = 0
    filter_calls = 0

    async def _violations_for_status(**_kwargs: object) -> tuple[QualityGateViolation, ...]:
        nonlocal status_calls
        status_calls += 1
        return (violation,)

    async def _not_restored(**_kwargs: object) -> tuple[QualityGateViolation, ...]:
        nonlocal filter_calls
        filter_calls += 1
        return (violation,) if filter_calls == 1 else ()

    async def _prompt(**_kwargs: object) -> str:
        return "repair prompt"

    async def _suppresses_cli(_workspace_id: str) -> bool:
        return False

    monkeypatch.setattr(runner, "_protected_scope_violations_for_status", _violations_for_status)
    monkeypatch.setattr(
        runner,
        "_protected_scope_violations_not_restored_to_remote_branch",
        _not_restored,
    )
    monkeypatch.setattr(runner, "_protected_scope_repair_prompt", _prompt)
    monkeypatch.setattr(runner, "_provider_recovery_suppresses_cli", _suppresses_cli)

    result = await runner._repair_protected_scope_changes_before_commit(
        workspace_id="ws_delta",
        status_stdout=" M .github/workflows/ci.yml\n",
        compose_project="awf_ws_delta",
        compose_file=tmp_path / "compose.yml",
        state=MonitorState(),
        protected_scope_revert_remote_branch="awf/ws_delta",
        remote_push_url="git@example.com/repo.git",
    )

    assert result is not None
    assert result.ok
    assert status_calls == 2
    assert filter_calls == 2


@pytest.mark.unit
async def test_protected_scope_diff_unavailable_push_result_uses_block_details(
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

    async def _block(**_kwargs: object) -> pr_remote_repair._ProtectedScopePushBlock:  # noqa: SLF001
        return pr_remote_repair._ProtectedScopePushBlock(  # noqa: SLF001
            message="diff unavailable",
            reason_code="PROTECTED_SCOPE_DIFF_UNAVAILABLE",
        )

    monkeypatch.setattr(runner, "_protected_scope_diff_unavailable_block", _block)

    result = await runner._protected_scope_diff_unavailable_push_result(
        workspace_id="ws_delta",
        remote_branch="awf/ws_delta",
        exc=ProtectedScopeDiffError("no diff"),
    )

    assert result.failed is True
    assert result.protected_scope_diff_unavailable is True
    assert result.stderr == "diff unavailable"


@pytest.mark.unit
def test_read_worktree_text_reports_decode_and_os_errors(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.yml"
    invalid.write_bytes(b"\xff\xfe")
    with pytest.raises(ProtectedScopeDiffError, match="as UTF-8"):
        pr_monitor_runner_remote_repair._read_worktree_text(invalid, display_path="invalid.yml")  # noqa: SLF001

    directory = tmp_path / "config-dir"
    directory.mkdir()
    with pytest.raises(ProtectedScopeDiffError, match="Could not read protected worktree file"):
        pr_monitor_runner_remote_repair._read_worktree_text(directory, display_path="config-dir")  # noqa: SLF001


@pytest.mark.unit
async def test_feedback_refresh_drops_stale_review_comment_state(
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
    comment = ReviewComment(comment_id="review-1", body_excerpt="new feedback")
    state = MonitorState()
    state.threads_addressed_ids["review-1"] = "fix_committed"
    state.threads_addressed_ids[_review_comment_body_state_key("review-1")] = "old-body-hash"

    async def _no_remote_resolution_update(**_kwargs: object) -> bool:
        return False

    runner._apply_pr_feedback_resolution_state = _no_remote_resolution_update  # type: ignore[method-assign]

    changed = await runner._refresh_pr_feedback_resolution_state(
        workspace_id="ws_feedback",
        repo=RepoRef(owner="example", name="repo"),
        pr_number=42,
        status=_status_for_helpers(reviews=(comment,)),
        state=state,
    )

    assert changed is True
    assert "review-1" not in state.threads_addressed_ids
    assert _review_comment_body_state_key("review-1") not in state.threads_addressed_ids


@pytest.mark.unit
def test_deferred_issue_filed_marker_is_body_aware() -> None:
    # #305: the capture marker is keyed by thread id AND body hash so a same-body
    # resolve-retry stays idempotent (no duplicate issue), while new reviewer
    # replies (a changed body) re-capture into a fresh issue instead of being
    # silently resolved under the stale one.
    from awf.runtime.pr_monitor_runner.fix_cycle import _deferred_issue_filed_marker

    assert _deferred_issue_filed_marker("T1", "hashA") == _deferred_issue_filed_marker(
        "T1", "hashA"
    )
    assert _deferred_issue_filed_marker("T1", "hashA") != _deferred_issue_filed_marker(
        "T1", "hashB"
    )
    assert "T1" in _deferred_issue_filed_marker("T1", "hashA")


@pytest.mark.unit
def test_deferred_thread_conversation_includes_all_replies() -> None:
    # #305: a body-aware recapture fires because new reviewer replies changed the
    # thread, so the tracking issue must carry the whole conversation — not just
    # the truncated first-comment excerpt — or the new feedback is lost on resolve.
    from awf.runtime.pr_monitor import ReviewThread, ReviewThreadComment
    from awf.runtime.pr_monitor_runner.fix_cycle import _deferred_thread_conversation

    thread = ReviewThread(
        thread_id="T1",
        path="x",
        line=1,
        body_excerpt="first finding",
        comments=(
            ReviewThreadComment(comment_id="c1", body="first finding", author="cr"),
            ReviewThreadComment(
                comment_id="c2", body="follow-up reply\nsecond line", author="alice"
            ),
            ReviewThreadComment(comment_id="c3", body="", author="bot"),
        ),
    )
    body = _deferred_thread_conversation(thread)
    assert "first finding" in body
    assert "follow-up reply" in body and "second line" in body  # all replies, multi-line
    assert "alice" in body
    # Fallback to the excerpt when GitHub supplied no structured comments.
    bare = ReviewThread(thread_id="T2", path="x", line=1, body_excerpt="only excerpt", comments=())
    assert "only excerpt" in _deferred_thread_conversation(bare)


@pytest.mark.unit
def test_protected_manual_ready_handoff_rejects_blocking_bot_thread() -> None:
    # #305: a bot inline thread with needs_human/defer blocks in decide() gate 7
    # but is not "human deferred"; the protected-merge handoff must NOT report
    # ready-for-merge, mirroring the _notify_human_reason guard.
    base = _status_for_helpers()
    blocked = PRStatus(
        number=base.number,
        head_sha=base.head_sha,
        mergeable=base.mergeable,
        check_state=base.check_state,
        unresolved_inline_threads=(
            ReviewThread(
                thread_id="T_bot", path="x", line=1, body_excerpt="?", author="coderabbitai"
            ),
        ),
        unresolved_review_comments=(),
        base_behind_count=base.base_behind_count,
        merge_state_status=MergeStateStatus.BLOCKED,
    )
    state = MonitorState(threads_addressed_ids={"T_bot": "needs_human"})
    assert _is_protected_manual_ready_handoff(blocked, state) is False


@pytest.mark.unit
async def test_deferred_capture_transient_failure_requeues_instead_of_downgrading(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # #305: a transient `gh issue create` failure (502) must NOT permanently
    # downgrade a valid defer to needs_human. It clears the verdict so the next
    # poll re-addresses and re-attempts capture once GitHub recovers.
    from awf.runtime.pr_monitor import _mark_review_thread_addressed
    from awf.runtime.pr_monitor_runner.fix_cycle import _capture_deferred_review_thread

    ws_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1, stderr="HTTP 502 Bad Gateway")  # gh issue create (transient)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    thread = ReviewThread(
        thread_id="T_defer", path="src/x.py", line=1, body_excerpt="nit", author="rev"
    )
    state = MonitorState()
    _mark_review_thread_addressed(state, thread, "defer")

    result = await _capture_deferred_review_thread(
        runner,
        workspace_id=ws_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        thread=thread,
        state=state,
        base_branch="development",
        remote_branch=f"awf/{ws_id}",
        operation_id=None,
        operation_type=None,
        monitor_log=None,
    )

    # None (requeue), not False (which would downgrade to needs_human); and the
    # verdict is cleared so the thread is re-addressed next poll.
    assert result is None
    assert state.threads_addressed_ids.get("T_defer") is None


@pytest.mark.unit
async def test_fix_cycle_continues_to_next_thread_after_transient_capture(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When _capture_deferred_review_thread returns None (transient), the fix
    # cycle must not downgrade the thread and must move on to the next item.
    from awf.runtime.pr_monitor_runner import fix_cycle as fix_cycle_module
    from awf.runtime.pr_monitor_runner.types import _MonitorPolicyBlockedError

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    t1 = ReviewThread(
        thread_id="T_transient", path="src/a.py", line=1, body_excerpt="nit", author="rev"
    )
    t2 = ReviewThread(
        thread_id="T_block", path="src/b.py", line=2, body_excerpt="blocks", author="rev"
    )
    state = MonitorState()

    async def _address(**kwargs: object) -> str:
        thread = kwargs["thread"]
        assert isinstance(thread, ReviewThread)
        if thread.thread_id == t1.thread_id:
            return "defer"
        raise _MonitorPolicyBlockedError("policy blocked second thread")

    async def _capture_none(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(runner, "_address_thread", _address)
    monkeypatch.setattr(fix_cycle_module, "_capture_deferred_review_thread", _capture_none)

    result = await runner._run_fix_cycle(
        workspace_id="ws_transient_continue",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(t1, t2),
        initial_reviews=(),
        state=state,
        remote_branch="awf/ws_transient_continue",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    # The cycle moved past the transient thread to the second (which policy-blocks)
    # and never downgraded the transient thread to needs_human.
    assert result.failed is True
    assert state.threads_addressed_ids.get("T_transient") != "needs_human"
