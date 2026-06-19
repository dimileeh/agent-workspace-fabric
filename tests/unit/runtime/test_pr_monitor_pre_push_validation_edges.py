"""Focused edge coverage for PR monitor pre-push validation helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor_runner import pre_push_validation
from awf.runtime.validation_types import ValidationCommandResult, ValidationResult
from awf.runtime.validation_worktree import ValidationWorktreeCheck, ValidationWorktreeCleanup
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)
from tests.unit.runtime._pre_push_validation_helpers import (
    _FakeValidation,
    _mark_git_worktree,
    _set_resolved_profile,
    _validation_result,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a scoped async SQLAlchemy session factory for tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _failed_validation_result(tmp_path: Path) -> ValidationResult:
    stdout_path = tmp_path / "failed.stdout"
    stderr_path = tmp_path / "failed.stderr"
    stdout_path.write_text("failed\n", encoding="utf-8")
    stderr_path.write_text("", encoding="utf-8")
    return ValidationResult(
        commands=[
            ValidationCommandResult(
                command="pytest -q",
                returncode=1,
                duration_seconds=0.1,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                reason_code="PYTEST_TEST_FAILURE",
            )
        ]
    )


@pytest.mark.unit
def test_pre_push_side_effect_failure_result_preserves_result_when_artifact_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Synthetic side-effect failures should still be returned if artifacts cannot be written."""

    def _raise_write_text(_self: Path, _data: str, *_args: object, **_kwargs: object) -> int:
        raise OSError("artifact volume is read-only")

    monkeypatch.setattr(Path, "write_text", _raise_write_text)
    cleanup = ValidationWorktreeCleanup(
        cleaned=True,
        check=ValidationWorktreeCheck(clean=False, paths=("generated.txt",)),
        restore_ref="a" * 40,
        cleaned_paths=("generated.txt",),
    )

    result, details = pre_push_validation._pre_push_side_effect_failure_result(
        result=ValidationResult(commands=[]),
        cleanup=cleanup,
        workspace_id="ws_artifact_failure",
        validation_run_id="vr/side-effect",
        artifacts_root=tmp_path / "artifacts",
    )

    command = result.commands[-1]
    assert command.reason_code == pre_push_validation.VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED
    assert command.captured_stdout is not None
    assert "Cleaned paths: generated.txt" in command.captured_stdout
    assert command.stdout_path.name == "vr_side-effect.side_effects.stdout"
    assert details["cleaned_paths"] == ["generated.txt"]
    assert not command.stdout_path.exists()


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_returns_without_head_capture(
    tmp_path: Path,
) -> None:
    """A missing fix-start HEAD should stop before running the fix agent."""

    class _Runner:
        def __init__(self, worktrees_root: Path) -> None:
            self._worktrees_root = worktrees_root
            self.rev_parse_calls: list[Path] = []

        async def _rev_parse_head(self, worktree_path: Path) -> str | None:
            self.rev_parse_calls.append(worktree_path)
            return None

    runner = _Runner(tmp_path / "worktrees")
    validation_result = pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr_failed",
        workspace_head_sha=None,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="pre-push validation failed",
        validation_reason_code="PYTEST_TEST_FAILURE",
        result=_failed_validation_result(tmp_path),
    )

    committed, failure_reason = await pre_push_validation._run_pre_push_validation_fix_pass(
        runner,
        workspace_id="ws_missing_head",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch="awf/ws_missing_head",
        remote_url=None,
        state=None,
        validation_result=validation_result,
        pass_number=1,
        total_passes=1,
        validation_commands=("pytest -q",),
    )

    assert committed is False
    assert failure_reason is None
    assert runner.rev_parse_calls == [runner._worktrees_root / "ws_missing_head"]


@pytest.mark.unit
async def test_pre_push_validation_recovered_head_refreshes_supply_chain_policy(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovered pre-push HEAD commits must run supply-chain policy before validation."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    recovery_base = "1" * 40
    recovered_head = "2" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{recovery_base}\n")
    cmd.queue_result(returncode=0, stdout="M\0package-lock.json\0")
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    refresh_calls: list[dict[str, object]] = []

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        operation_start_head: str,
        task_tag: str | None = None,
    ) -> str | None:
        del self, workspace_id, worktree_path, task_tag
        assert operation_start_head == recovery_base
        return recovered_head

    async def _pre_push_validation_worktree_check(
        _self: object,
        *,
        worktree_path: Path,
    ) -> ValidationWorktreeCheck:
        del _self, worktree_path
        return ValidationWorktreeCheck(clean=True)

    async def _pre_push_validation_cleanup(
        _self: object,
        *,
        worktree_path: Path,
        restore_ref: str,
    ) -> ValidationWorktreeCleanup:
        del _self, worktree_path
        return ValidationWorktreeCleanup(
            cleaned=False,
            check=ValidationWorktreeCheck(clean=True),
            restore_ref=restore_ref,
        )

    async def _refresh_supply_chain_policy_before_push(**kwargs: object) -> str | None:
        refresh_calls.append(dict(kwargs))
        return "SUPPLY_CHAIN_REMOTE_SCRIPT_EXECUTION (package-lock.json)"

    monkeypatch.setattr(
        pre_push_validation,
        "verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_recover_missing_head_object_from_filesystem",
        _recover_missing_head_object_from_filesystem,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_worktree_check",
        _pre_push_validation_worktree_check,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_cleanup",
        _pre_push_validation_cleanup,
    )
    monkeypatch.setattr(
        runner,
        "_refresh_supply_chain_policy_before_push",
        _refresh_supply_chain_policy_before_push,
    )

    result = await pre_push_validation._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
    )

    assert result.passed is False
    assert result.reason_code == "MONITOR_POLICY_BLOCKED"
    assert "SUPPLY_CHAIN_REMOTE_SCRIPT_EXECUTION" in result.message
    assert refresh_calls == [
        {
            "workspace_id": workspace_id,
            "command_evidence": ("pytest -q",),
            "changed_paths": ("package-lock.json",),
        }
    ]
    assert validation.calls == []


@pytest.mark.unit
async def test_pre_push_validation_recovered_head_diff_failure_blocks_validation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovered pre-push HEAD commits fail closed when their changed paths are unavailable."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    recovery_base = "3" * 40
    recovered_head = "4" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{recovery_base}\n")
    cmd.queue_result(returncode=1, stdout="", stderr="bad object")
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    refresh_calls: list[dict[str, object]] = []

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        operation_start_head: str,
        task_tag: str | None = None,
    ) -> str | None:
        del self, workspace_id, worktree_path, task_tag
        assert operation_start_head == recovery_base
        return recovered_head

    async def _refresh_supply_chain_policy_before_push(**kwargs: object) -> str | None:
        refresh_calls.append(dict(kwargs))
        return None

    monkeypatch.setattr(
        pre_push_validation,
        "verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_recover_missing_head_object_from_filesystem",
        _recover_missing_head_object_from_filesystem,
    )
    monkeypatch.setattr(
        runner,
        "_refresh_supply_chain_policy_before_push",
        _refresh_supply_chain_policy_before_push,
    )

    result = await pre_push_validation._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
    )

    assert result.passed is False
    assert result.reason_code == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
    assert "recovered HEAD diff unavailable" in result.message
    assert refresh_calls == []
    assert validation.calls == []
