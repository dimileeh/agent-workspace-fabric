"""Pre-push validation fix-pass recovered-delta environment tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor_runner.constants import (
    _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
    _MIRROR_HOOKS_PATH_POISONED_REASON,
)
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


def _failed_validation_result(
    pre_push_validation: Any,
    tmp_path: Path,
    *,
    workspace_head_sha: str,
) -> object:
    return pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_failed",
        workspace_head_sha=workspace_head_sha,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="PR monitor pre-push validation failed: COMMAND_FAILED",
        validation_reason_code="COMMAND_FAILED",
        result=_validation_result(tmp_path, ok=False, reason_code="COMMAND_FAILED"),
    )


async def _make_fix_pass_runner(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> tuple[str, object, FakeCommandRunner, FakeAdapter]:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    return workspace_id, runner, cmd, adapter


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_recovered_delta_strips_git_object_lookup_env(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovered commit-range protected-scope diff must ignore inherited object dirs."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    import awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass as fix_pass

    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/private-objects")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/private-alternates")
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    fix_start_head = "1" * 40
    recovered_head = "2" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    cmd.queue_result(returncode=0, stdout="")
    adapter = FakeAdapter()
    adapter.queue(stdout="attempted fix\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        *_args: object,
        **_kwargs: object,
    ) -> str:
        return recovered_head

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        return False

    async def _rollback_failed_fix_pass(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(fix_pass, "verify_head_object_exists", _verify_head_object_exists)
    monkeypatch.setattr(
        fix_pass,
        "_recover_missing_head_object_from_filesystem",
        _recover_missing_head_object_from_filesystem,
    )
    monkeypatch.setattr(fix_pass, "mirror_path_for_worktree", lambda _worktree_path: None)
    monkeypatch.setattr(
        pre_push_validation,
        "_rollback_failed_pre_push_validation_fix_pass",
        _rollback_failed_fix_pass,
    )
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    validation_result = pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_failed",
        workspace_head_sha=fix_start_head,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="PR monitor pre-push validation failed: COMMAND_FAILED",
        validation_reason_code="COMMAND_FAILED",
        result=_validation_result(tmp_path, ok=False, reason_code="COMMAND_FAILED"),
    )

    committed, failure_reason = await pre_push_validation._run_pre_push_validation_fix_pass(
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
    assert failure_reason is None
    recovered_delta_call = next(
        call
        for call in cmd.calls
        if call.args[-4:] == ["diff", "--name-status", "-z", f"{fix_start_head}..{recovered_head}"]
    )
    assert recovered_delta_call.env is not None
    assert "GIT_OBJECT_DIRECTORY" not in recovered_delta_call.env
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in recovered_delta_call.env


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_returns_post_agent_mirror_repair_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    import awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass as fix_pass

    fix_start_head = "1" * 40
    workspace_id, runner, cmd, adapter = await _make_fix_pass_runner(factory, tmp_path)
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    adapter.queue(stdout="attempted fix\n")
    repair_calls = 0

    async def _repair_mirror_hooks(**_kwargs: object) -> str | None:
        nonlocal repair_calls
        repair_calls += 1
        return _MIRROR_HOOKS_PATH_POISONED_REASON if repair_calls == 2 else None

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        raise AssertionError("commit should not run after post-agent mirror repair failure")

    monkeypatch.setattr(fix_pass, "mirror_path_for_worktree", lambda _worktree_path: tmp_path)
    monkeypatch.setattr(
        fix_pass, "_repair_pre_push_validation_fix_mirror_hooks", _repair_mirror_hooks
    )
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)

    committed, failure_reason = await pre_push_validation._run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="codex/pr",
        remote_url=None,
        state=None,
        validation_result=_failed_validation_result(
            pre_push_validation,
            tmp_path,
            workspace_head_sha=fix_start_head,
        ),
        pass_number=1,
        total_passes=1,
        validation_commands=("pytest -q",),
    )

    assert (committed, failure_reason) == (False, _MIRROR_HOOKS_PATH_POISONED_REASON)
    assert repair_calls == 2


@pytest.mark.unit
@pytest.mark.parametrize(
    ("mirror_failure_reason", "rollback_failure_reason", "expected_failure_reason"),
    (
        (
            _MIRROR_HOOKS_PATH_POISONED_REASON,
            "PRE_PUSH_VALIDATION_ROLLBACK_FAILED",
            _MIRROR_HOOKS_PATH_POISONED_REASON,
        ),
        (None, "PRE_PUSH_VALIDATION_ROLLBACK_FAILED", "PRE_PUSH_VALIDATION_ROLLBACK_FAILED"),
    ),
)
async def test_pre_push_validation_fix_pass_cleanup_failure_preserves_specific_failure_reason(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mirror_failure_reason: str | None,
    rollback_failure_reason: str,
    expected_failure_reason: str,
) -> None:
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    import awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass as fix_pass

    fix_start_head = "2" * 40
    workspace_id, runner, cmd, adapter = await _make_fix_pass_runner(factory, tmp_path)
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    adapter.queue(
        exc=ComposeExecCleanupError(
            invocation_id="awf_cleanup",
            source="agent",
            label="monitor-pre-push-validation-fix",
            message="tagged process still running",
        )
    )
    repair_calls = 0
    rollback_calls: list[str] = []

    async def _repair_mirror_hooks(**_kwargs: object) -> str | None:
        nonlocal repair_calls
        repair_calls += 1
        return mirror_failure_reason if repair_calls == 2 else None

    async def _rollback_failed_fix_pass(*_args: object, **kwargs: object) -> str:
        rollback_calls.append(str(kwargs["reason"]))
        return rollback_failure_reason

    monkeypatch.setattr(fix_pass, "mirror_path_for_worktree", lambda _worktree_path: tmp_path)
    monkeypatch.setattr(
        fix_pass, "_repair_pre_push_validation_fix_mirror_hooks", _repair_mirror_hooks
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_rollback_failed_pre_push_validation_fix_pass",
        _rollback_failed_fix_pass,
    )

    committed, failure_reason = await pre_push_validation._run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="codex/pr",
        remote_url=None,
        state=None,
        validation_result=_failed_validation_result(
            pre_push_validation,
            tmp_path,
            workspace_head_sha=fix_start_head,
        ),
        pass_number=1,
        total_passes=1,
        validation_commands=("pytest -q",),
    )

    assert (committed, failure_reason) == (False, expected_failure_reason)
    assert rollback_calls == ["compose_cleanup_failed"]


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_agent_exception_repairs_mirror_before_rollback_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    import awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass as fix_pass

    fix_start_head = "7" * 40
    workspace_id, runner, cmd, adapter = await _make_fix_pass_runner(factory, tmp_path)
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    adapter.queue(exc=RuntimeError("unexpected fix-agent failure"))
    repair_calls = 0
    rollback_calls: list[str] = []

    async def _repair_mirror_hooks(**_kwargs: object) -> None:
        nonlocal repair_calls
        repair_calls += 1

    async def _rollback_failed_fix_pass(*_args: object, **kwargs: object) -> str:
        rollback_calls.append(str(kwargs["reason"]))
        return "PRE_PUSH_VALIDATION_ROLLBACK_FAILED"

    monkeypatch.setattr(fix_pass, "mirror_path_for_worktree", lambda _worktree_path: tmp_path)
    monkeypatch.setattr(
        fix_pass, "_repair_pre_push_validation_fix_mirror_hooks", _repair_mirror_hooks
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_rollback_failed_pre_push_validation_fix_pass",
        _rollback_failed_fix_pass,
    )

    committed, failure_reason = await pre_push_validation._run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="codex/pr",
        remote_url=None,
        state=None,
        validation_result=_failed_validation_result(
            pre_push_validation,
            tmp_path,
            workspace_head_sha=fix_start_head,
        ),
        pass_number=1,
        total_passes=1,
        validation_commands=("pytest -q",),
    )

    assert (committed, failure_reason) == (False, "PRE_PUSH_VALIDATION_ROLLBACK_FAILED")
    assert rollback_calls == ["agent_exception"]
    assert repair_calls == 2


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_returns_missing_head_when_recovery_anchor_missing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    import awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass as fix_pass

    fix_start_head = "3" * 40
    workspace_id, runner, cmd, adapter = await _make_fix_pass_runner(factory, tmp_path)
    cmd.queue_result(returncode=0, stdout=f"{fix_start_head}\n")
    adapter.queue(stdout="attempted fix\n")

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _mirror_commit_object_exists(*_args: object, **_kwargs: object) -> bool:
        return False

    async def _open_merge_candidate_head_sha(*_args: object, **_kwargs: object) -> None:
        return None

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        raise AssertionError("commit should not run when no recovery anchor is available")

    monkeypatch.setattr(fix_pass, "mirror_path_for_worktree", lambda _worktree_path: tmp_path)
    monkeypatch.setattr(fix_pass, "verify_head_object_exists", _verify_head_object_exists)
    monkeypatch.setattr(fix_pass, "_mirror_commit_object_exists", _mirror_commit_object_exists)
    monkeypatch.setattr(fix_pass, "_open_merge_candidate_head_sha", _open_merge_candidate_head_sha)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)

    committed, failure_reason = await pre_push_validation._run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="codex/pr",
        remote_url=None,
        state=None,
        validation_result=_failed_validation_result(
            pre_push_validation,
            tmp_path,
            workspace_head_sha=fix_start_head,
        ),
        pass_number=1,
        total_passes=1,
        validation_commands=("pytest -q",),
    )

    assert (committed, failure_reason) == (False, _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON)


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_returns_commit_exception_rollback_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    import awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass as fix_pass

    fix_start_head = "4" * 40
    post_agent_head = "5" * 40
    workspace_id, runner, cmd, adapter = await _make_fix_pass_runner(factory, tmp_path)
    adapter.queue(stdout="attempted fix\n")
    rev_parse_heads = [fix_start_head, post_agent_head]
    rollback_calls: list[dict[str, object]] = []

    async def _rev_parse_head(_worktree_path: Path) -> str:
        return rev_parse_heads.pop(0)

    async def _commit_dirty_worktree(**_kwargs: object) -> bool:
        raise RuntimeError("commit sink failed")

    async def _rollback_failed_fix_pass(*_args: object, **kwargs: object) -> str:
        rollback_calls.append(dict(kwargs))
        return "PRE_PUSH_VALIDATION_ROLLBACK_FAILED"

    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty_worktree)
    monkeypatch.setattr(fix_pass, "mirror_path_for_worktree", lambda _worktree_path: None)
    monkeypatch.setattr(
        pre_push_validation,
        "_rollback_failed_pre_push_validation_fix_pass",
        _rollback_failed_fix_pass,
    )

    committed, failure_reason = await pre_push_validation._run_pre_push_validation_fix_pass(
        runner,
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="codex/pr",
        remote_url=None,
        state=None,
        validation_result=_failed_validation_result(
            pre_push_validation,
            tmp_path,
            workspace_head_sha=fix_start_head,
        ),
        pass_number=1,
        total_passes=1,
        validation_commands=("pytest -q",),
    )

    assert (committed, failure_reason) == (False, "PRE_PUSH_VALIDATION_ROLLBACK_FAILED")
    assert rollback_calls[0]["reason"] == "commit_exception"
    assert rollback_calls[0]["restore_ref"] == post_agent_head
