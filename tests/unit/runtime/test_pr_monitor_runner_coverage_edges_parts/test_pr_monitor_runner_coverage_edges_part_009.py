"""Additional branch-coverage tests for PR monitor runner edge behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.common.github_client import RepoRef
from awf.db.enums import OperationStatus, WorkspaceStatus
from awf.db.repositories import (
    OperationRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    AddressComments,
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReviewThread,
    SyncBase,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _changed_paths_from_name_only_z,
    _changed_paths_from_porcelain,
    _changed_paths_from_porcelain_z,
    _porcelain_z_records,
    _split_porcelain_rename_paths,
    _unquote_porcelain_path,
)
from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError
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


class _CleanupFailingAdapter(FakeAdapter):
    async def run(self, **_kwargs: object) -> object:  # type: ignore[override]
        raise ComposeExecCleanupError(
            invocation_id="awf_monitor_cleanup_failed",
            source="agent",
            label="monitor",
            message="tagged process still alive",
        )


def _status_for_helpers(
    *,
    head_sha: str = "abc1234567890def",
    threads: tuple[ReviewThread, ...] = (),
) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=threads,
        unresolved_review_comments=(),
        blocking_reviews=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )


@pytest.mark.unit
def test_porcelain_path_parsing_covers_c_escapes_and_malformed_z_records() -> None:
    assert _split_porcelain_rename_paths('"old \\" -> name.py" -> "new name.py"') == (
        '"old \\" -> name.py"',
        '"new name.py"',
    )
    assert _split_porcelain_rename_paths('"still quoted -> name.py') is None
    assert _unquote_porcelain_path(r'"src/a\040b\011c\nline\q.txt"') == ("src/a b\tc\nlineq.txt")
    assert _unquote_porcelain_path('"src/trailing' + "\\" + '"') == "src/trailing\\"
    assert _changed_paths_from_porcelain('R  "old\\040name.py" -> "new\\040name.py"\n') == [
        "old name.py",
        "new name.py",
    ]
    assert _porcelain_z_records("bad\0 M kept.py\0R  new.py\0") == [
        (" M", "kept.py", None),
        ("R ", "new.py", None),
    ]
    assert _changed_paths_from_porcelain_z("R  new.py\0") == ["new.py"]


@pytest.mark.unit
def test_name_only_z_path_parser_deduplicates_and_rejects_empty_paths() -> None:
    assert _changed_paths_from_name_only_z("src/a.py\0src/b.py\0src/a.py\0") == (
        "src/a.py",
        "src/b.py",
    )
    with pytest.raises(ProtectedScopeDiffError, match="empty path"):
        _changed_paths_from_name_only_z("src/a.py\0\0")


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
