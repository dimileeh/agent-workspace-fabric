"""Pre-push validation fix-pass missing-HEAD recovery tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor_pre_push_validation import (
    _mark_git_worktree,
    _validation_result,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a scoped async SQLAlchemy session factory for tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_missing_head_falls_back_from_stale_anchor(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale fix-pass opening HEAD must not block candidate-head recovery."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    import awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass as fix_pass_module

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    fix_start_head = "1" * 40
    candidate_head = "2" * 40
    mirror_path = tmp_path / "mirror.git"
    cmd = FakeCommandRunner()
    # Missing stale anchor in the mirror, then an empty recovered delta.
    cmd.queue_result(returncode=1, stderr="missing commit")
    cmd.queue_result(returncode=0, stdout="")
    adapter = FakeAdapter()
    adapter.queue(stdout="fixed validation and recovered HEAD\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    rev_parse_results = [fix_start_head, candidate_head]
    captured_recovery_heads: list[str] = []

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return rev_parse_results.pop(0)

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> None:
        return None

    async def _open_merge_candidate_head_sha(*_args: object) -> str:
        return candidate_head

    async def _recover_missing_head_object_from_filesystem(
        *_args: object,
        **kwargs: object,
    ) -> str:
        recovery_head = str(kwargs["operation_start_head"])
        captured_recovery_heads.append(recovery_head)
        return recovery_head

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        return True

    async def _head_descends_from(*_args: object, **_kwargs: object) -> bool:
        return True

    async def _cleanup_committed_pre_push_validation_fix_pass(
        *_args: object,
        **_kwargs: object,
    ) -> None:
        return None

    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    monkeypatch.setattr(fix_pass_module, "mirror_path_for_worktree", lambda _path: mirror_path)
    monkeypatch.setattr(
        fix_pass_module,
        "verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        fix_pass_module,
        "repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(
        fix_pass_module,
        "_open_merge_candidate_head_sha",
        _open_merge_candidate_head_sha,
    )
    monkeypatch.setattr(
        fix_pass_module,
        "_recover_missing_head_object_from_filesystem",
        _recover_missing_head_object_from_filesystem,
    )
    monkeypatch.setattr(pre_push_validation, "_head_descends_from", _head_descends_from)
    monkeypatch.setattr(
        pre_push_validation,
        "_cleanup_committed_pre_push_validation_fix_pass",
        _cleanup_committed_pre_push_validation_fix_pass,
    )
    validation_result = pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_failed",
        workspace_head_sha=fix_start_head,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="PR monitor pre-push validation failed: COMMAND_FAILED",
        validation_reason_code="COMMAND_FAILED",
        result=_validation_result(tmp_path, ok=False, reason_code="COMMAND_FAILED"),
    )

    committed, cleanup_failure_reason = await pre_push_validation._run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="codex/pr",
        remote_url=None,
        state=None,
        validation_result=validation_result,
        pass_number=1,
        total_passes=1,
        validation_commands=("pytest -q",),
    )

    assert committed is True
    assert cleanup_failure_reason is None
    assert captured_recovery_heads == [candidate_head]
    assert cmd.calls[0].args[-3:] == [
        "cat-file",
        "-e",
        f"{fix_start_head}^{{commit}}",
    ]
    assert cmd.calls[0].env is not None
    assert "GIT_OBJECT_DIRECTORY" not in cmd.calls[0].env
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in cmd.calls[0].env
    assert rev_parse_results == []


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_blocks_recovered_commit_protected_scope_violations(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovered commits must use committed protected diffs, not HEAD/worktree diffs.

    Regression for PRRT_kwDOSJAM6s6Ky-rn: after missing-HEAD recovery commits the
    filesystem tree, a dirty-status repair check compares the recovered
    ``HEAD:path`` with the recovered worktree and can miss protected workflow
    weakenings. The committed ``fix_start_head..recovered`` classifier must catch
    the violation and roll back before validation can continue to push.
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    import awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass as fix_pass_module

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    fix_start_head = "3" * 40
    recovered_head = "4" * 40
    old_workflow = """name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pytest
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: ruff check .
"""
    new_workflow = """name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pytest
"""
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="M\0.github/workflows/ci.yml\0")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout=old_workflow)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout=new_workflow)
    adapter = FakeAdapter()
    adapter.queue(stdout="fixed validation and recovered HEAD\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    rev_parse_results = [fix_start_head]
    rollback_calls: list[dict[str, object]] = []

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return rev_parse_results.pop(0)

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        *_args: object,
        **_kwargs: object,
    ) -> str:
        return recovered_head

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        raise AssertionError("protected recovered commit must block before dirty commit sink")

    async def _repair_protected_scope_changes_before_commit(
        **_kwargs: object,
    ) -> CommandResult:
        raise AssertionError("committed recovery must not use synthetic dirty status repair")

    async def _rollback_failed_pre_push_validation_fix_pass(
        *_args: object,
        **kwargs: object,
    ) -> str | None:
        rollback_calls.append(dict(kwargs))
        return None

    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    monkeypatch.setattr(
        runner,
        "_repair_protected_scope_changes_before_commit",
        _repair_protected_scope_changes_before_commit,
    )
    monkeypatch.setattr(fix_pass_module, "mirror_path_for_worktree", lambda _path: None)
    monkeypatch.setattr(
        fix_pass_module,
        "verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        fix_pass_module,
        "_recover_missing_head_object_from_filesystem",
        _recover_missing_head_object_from_filesystem,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_rollback_failed_pre_push_validation_fix_pass",
        _rollback_failed_pre_push_validation_fix_pass,
    )
    validation_result = pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_failed",
        workspace_head_sha=fix_start_head,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="PR monitor pre-push validation failed: COMMAND_FAILED",
        validation_reason_code="COMMAND_FAILED",
        result=_validation_result(tmp_path, ok=False, reason_code="COMMAND_FAILED"),
    )

    committed, cleanup_failure_reason = await pre_push_validation._run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="codex/pr",
        remote_url=None,
        state=None,
        validation_result=validation_result,
        pass_number=1,
        total_passes=1,
        validation_commands=("pytest -q",),
    )

    assert committed is False
    assert cleanup_failure_reason == "PROTECTED_SCOPE_REPAIR_FAILED"
    assert rollback_calls == [
        {
            "workspace_id": workspace_id,
            "worktree_path": worktree,
            "restore_ref": fix_start_head,
            "pass_number": 1,
            "reason": "recovered_protected_scope_repair_failed",
        }
    ]
    assert rev_parse_results == []
