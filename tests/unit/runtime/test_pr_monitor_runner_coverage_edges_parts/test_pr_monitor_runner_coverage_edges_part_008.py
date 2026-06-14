"""Focused branch-coverage tests for PR monitor runner edge behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_mock
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.control.quality_gates_common import QualityGateViolation
from awf.db.repositories import (
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    MonitorState,
)
from awf.runtime.pr_monitor_runner import remote_repair as pr_remote_repair
from awf.runtime.pr_monitor_runner.types import (
    BaseFetchError,
    ProtectedScopeDiffError,
    ProviderRecoveryRetryError,
    _ProtectedScopeRollbackDeltaEvidence,
)
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


def _protected_workflow_violation() -> QualityGateViolation:
    return QualityGateViolation(
        path=".github/workflows/ci.yml",
        protected_pattern=".github/workflows/*.yml",
        section="jobs.tests.steps[0]",
        line=12,
        reason="required test step policy changed",
    )


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
async def test_commit_dirty_worktree_uses_refreshed_paths_for_protected_repair_autofix_retry(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    initial_path = ".github/workflows/ci.yml"
    repaired_path = "src/awf/example.py"
    hook_stderr = (
        "fix end of files................................................Failed\n"
        "- hook id: end-of-file-fixer\n"
        "- exit code: 1\n"
        "- files were modified by this hook\n\n"
        f"Fixing {repaired_path}\n"
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f" M {initial_path}\n")  # status --porcelain
    # status --untracked-files=all enumerates the post-repair worktree before staging.
    cmd.queue_result(returncode=0, stdout=f" M {repaired_path}\n")
    cmd.queue_result(returncode=0)  # initial git add -A after protected-scope repair
    cmd.queue_result(returncode=1)  # git diff --cached --quiet
    cmd.queue_result(returncode=1, stderr=hook_stderr)
    cmd.queue_result(returncode=0, stdout=f" M {repaired_path}\n")
    cmd.queue_result(returncode=0)  # bounded restage of the autofixed repaired path
    cmd.queue_result(returncode=0)  # retry git commit
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    repair_inputs: list[str] = []

    async def _repair_protected_scope_changes_before_commit(**kwargs: object) -> CommandResult:
        repair_inputs.append(str(kwargs["status_stdout"]))
        return CommandResult(returncode=0, stdout=f" M {repaired_path}\n", stderr="")

    async def _repair_agent_runtime_ownership(
        logger: object,
        workspace_id: str,
        worktree_path: Path,
        reason: str,
        event_name: str,
        reason_code: str,
    ) -> bool:
        del logger, workspace_id, worktree_path, reason, event_name, reason_code
        return True

    monkeypatch.setattr(
        runner,
        "_repair_protected_scope_changes_before_commit",
        _repair_protected_scope_changes_before_commit,
    )
    monkeypatch.setattr(
        pr_remote_repair,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )

    result = await runner._commit_dirty_worktree(
        workspace_id=workspace_id,
        message="fix: repair protected scope",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result is True
    assert repair_inputs == [f" M {initial_path}\n"]
    assert [call.args[-3:] for call in cmd.calls if call.args[-3:-2] == ["commit"]] == [
        ["commit", "-m", "fix: repair protected scope"],
        ["commit", "-m", "fix: repair protected scope"],
    ]
    assert any(call.args[-3:] == ["add", "--", repaired_path] for call in cmd.calls)


@pytest.mark.unit
async def test_commit_dirty_worktree_excludes_agent_memory_from_autofix_retry_scope(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-repair autofix retry scope must drop untracked agent-runtime memory.

    Regression for PR #577 review thread PRRT_kwDOSJAM6s6JXxXY: after a
    protected-scope repair the commit path must scope the pre-commit autofix
    retry (``operation_dirty_paths``) to exactly what it staged — the
    leaf-enumerated, agent-runtime-filtered ``stage_paths`` — so an untracked
    ``.claude/agent-memory/`` leftover never widens the retry's in-scope check.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    repaired_path = "src/awf/example.py"
    memory_path = ".claude/agent-memory/note.md"
    hook_stderr = (
        "fix end of files................................................Failed\n"
        "- hook id: end-of-file-fixer\n"
        "- exit code: 1\n"
        "- files were modified by this hook\n\n"
        f"Fixing {repaired_path}\n"
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=" M .github/workflows/ci.yml\n")  # initial dirty check
    # Post-repair stage status enumerates the repaired file plus untracked memory.
    cmd.queue_result(returncode=0, stdout=f" M {repaired_path}\n?? {memory_path}\n")
    cmd.queue_result(returncode=0)  # git add -A of the staged repaired file
    cmd.queue_result(returncode=1)  # git diff --cached --quiet
    cmd.queue_result(returncode=1, stderr=hook_stderr)  # git commit (hook autofix failure)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _repair_protected_scope_changes_before_commit(**kwargs: object) -> CommandResult:
        del kwargs
        return CommandResult(returncode=0, stdout=f" M {repaired_path}\n", stderr="")

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        del kwargs
        return True

    captured_scope: list[tuple[str, ...]] = []

    async def _capture_retry(
        *, operation_dirty_paths: object, **kwargs: object
    ) -> tuple[CommandResult, tuple[str, ...]]:
        del kwargs
        captured_scope.append(tuple(operation_dirty_paths))  # type: ignore[arg-type]
        return CommandResult(returncode=0, stdout="", stderr=""), (repaired_path,)

    monkeypatch.setattr(
        runner,
        "_repair_protected_scope_changes_before_commit",
        _repair_protected_scope_changes_before_commit,
    )
    monkeypatch.setattr(
        pr_remote_repair,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(
        pr_remote_repair,
        "_retry_monitor_precommit_autofix_commit_once",
        _capture_retry,
    )

    result = await runner._commit_dirty_worktree(
        workspace_id=workspace_id,
        message="fix: repair protected scope",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result is True
    # Only the staged repaired file is in scope; the untracked agent-memory
    # leftover (and any collapsed ``.claude/`` entry) is filtered out.
    assert captured_scope == [(repaired_path,)]


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
