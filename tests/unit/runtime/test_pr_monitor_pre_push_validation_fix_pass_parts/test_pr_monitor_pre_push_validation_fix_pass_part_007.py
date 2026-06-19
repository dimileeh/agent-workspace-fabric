"""Recovered-head pre-push validation fix-pass edge tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.control.quality_gates_common import QualityGateViolation
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor_runner.constants import (
    _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
    _PROTECTED_SCOPE_REPAIR_FAILED_REASON,
)
from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError
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
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def _run_recovered_fix_pass(
    *,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cmd: FakeCommandRunner,
    protected_result: list[QualityGateViolation] | None = None,
    protected_error: ProtectedScopeDiffError | None = None,
    rollback_reason: str | None = None,
    recorded_rollback_reasons: list[str] | None = None,
) -> tuple[bool, str | None, list[str]]:
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    import awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass as fix_pass_module

    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    fix_start_head = "1" * 40
    recovered_head = "2" * 40
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
    rollback_reasons = recorded_rollback_reasons if recorded_rollback_reasons is not None else []

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return rev_parse_results.pop(0) if rev_parse_results else fix_start_head

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        *_args: object,
        **_kwargs: object,
    ) -> str:
        return recovered_head

    async def _protected_scope_violations_for_recovered_commit(
        *_args: object,
        **_kwargs: object,
    ) -> list[QualityGateViolation]:
        if protected_error is not None:
            raise protected_error
        return [] if protected_result is None else protected_result

    async def _rollback_failed_pre_push_validation_fix_pass(
        *_args: object,
        **kwargs: object,
    ) -> str | None:
        rollback_reasons.append(str(kwargs["reason"]))
        return rollback_reason

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(fix_pass_module, "mirror_path_for_worktree", lambda _path: None)
    monkeypatch.setattr(fix_pass_module, "verify_head_object_exists", _verify_head_object_exists)
    monkeypatch.setattr(
        fix_pass_module,
        "_recover_missing_head_object_from_filesystem",
        _recover_missing_head_object_from_filesystem,
    )
    monkeypatch.setattr(
        fix_pass_module,
        "_protected_scope_violations_for_recovered_commit",
        _protected_scope_violations_for_recovered_commit,
    )
    monkeypatch.setattr(
        fix_pass_module,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
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

    result = await pre_push_validation._run_pre_push_validation_fix_pass(
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
    return (*result, rollback_reasons)


@pytest.mark.unit
async def test_recovered_fix_pass_returns_unrecoverable_when_delta_diff_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1, stderr="bad diff")

    committed, failure_reason, rollback_reasons = await _run_recovered_fix_pass(
        factory=factory,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        cmd=cmd,
    )

    assert committed is False
    assert failure_reason == _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON
    assert rollback_reasons == ["recovered_delta_failed"]


@pytest.mark.unit
async def test_recovered_fix_pass_reraises_protected_diff_error_after_rollback(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="M\0.github/workflows/ci.yml\0")
    rollback_reasons: list[str] = []

    with pytest.raises(ProtectedScopeDiffError, match="diff unavailable"):
        await _run_recovered_fix_pass(
            factory=factory,
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            cmd=cmd,
            protected_error=ProtectedScopeDiffError("diff unavailable"),
            recorded_rollback_reasons=rollback_reasons,
        )
    assert rollback_reasons == ["recovered_protected_scope_diff_unavailable"]


@pytest.mark.unit
async def test_recovered_fix_pass_returns_protected_scope_reason_for_violations(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="M\0.github/workflows/ci.yml\0")
    violation = QualityGateViolation(
        path=".github/workflows/ci.yml",
        protected_pattern=".github/workflows/",
    )

    committed, failure_reason, rollback_reasons = await _run_recovered_fix_pass(
        factory=factory,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        cmd=cmd,
        protected_result=[violation],
    )

    assert committed is False
    assert failure_reason == _PROTECTED_SCOPE_REPAIR_FAILED_REASON
    assert rollback_reasons == ["recovered_protected_scope_repair_failed"]
