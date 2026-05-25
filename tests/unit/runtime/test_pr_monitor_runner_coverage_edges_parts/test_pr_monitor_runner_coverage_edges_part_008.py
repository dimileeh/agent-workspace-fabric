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
    _review_comment_body_state_key,
)
from awf.runtime.pr_monitor_runner.types import (
    BaseFetchError,
    ProtectedScopeDiffError,
    ProviderRecoveryRetryError,
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
