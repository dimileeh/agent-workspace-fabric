"""Pre-push validation fix-pass recovered-delta environment tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
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
