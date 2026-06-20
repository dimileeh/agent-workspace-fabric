"""Focused edge coverage for PR monitor pre-push validation helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.control.quality_gates import QualityGateViolation
from awf.db.session import make_session_factory
from awf.node.git_manager import GitOperationError
from awf.runtime.pr_monitor_runner import pre_push_validation
from awf.runtime.pr_monitor_runner.constants import (
    _MIRROR_HOOKS_PATH_POISONED_REASON,
    _PROTECTED_SCOPE_REPAIR_FAILED_REASON,
)
from awf.runtime.pr_monitor_runner.types import (
    _MonitorHeadObjectMissingError,
    _MonitorPolicyBlockedError,
)
from awf.runtime.validation_types import ValidationCommandResult, ValidationResult
from awf.runtime.validation_worktree import ValidationWorktreeCheck, ValidationWorktreeCleanup
from awf.runtime.validation_worktree_constants import VALIDATION_WORKTREE_CLEANUP_FAILED
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


def _write_worktree_with_mirror(tmp_path: Path, workspace_id: str) -> Path:
    """Create a linked worktree shape backed by a bare mirror path."""
    worktree = tmp_path / "worktrees" / workspace_id
    mirror = tmp_path / "mirrors" / "test.git"
    linked_git_dir = mirror / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    linked_git_dir.mkdir(parents=True)
    (worktree / ".git").write_text(
        f"gitdir: {linked_git_dir}\n",
        encoding="utf-8",
    )
    (linked_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    return worktree


async def _existing_mirror_commit(
    self: object,
    mirror_path: Path,
    commit_sha: str,
) -> bool:
    """Treat scripted recovery anchors as present in the fake mirror."""
    del self, mirror_path, commit_sha
    return True


async def _clean_pre_push_validation_worktree_check(
    self: object,
    *,
    worktree_path: Path,
) -> ValidationWorktreeCheck:
    del self, worktree_path
    return ValidationWorktreeCheck(clean=True)


async def _clean_pre_push_validation_cleanup(
    self: object,
    *,
    worktree_path: Path,
    restore_ref: str,
) -> ValidationWorktreeCleanup:
    del self, worktree_path
    return ValidationWorktreeCleanup(
        cleaned=False,
        check=ValidationWorktreeCheck(clean=True),
        restore_ref=restore_ref,
    )


def _patch_clean_pre_push_validation_worktree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_worktree_check",
        _clean_pre_push_validation_worktree_check,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_cleanup",
        _clean_pre_push_validation_cleanup,
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
async def test_pre_push_validation_fix_pass_policy_block_preserves_exception_reason_code(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reason-coded policy blocks from the fix-pass commit sink must not be flattened."""
    workspace_id = "workspace_fix_policy_reason"
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True, exist_ok=True)
    validation_calls = 0
    validation_result = pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr1",
        workspace_head_sha="a" * 40,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="attempt 1 failed",
        validation_reason_code="PYTEST_TEST_FAILURE",
        result=_validation_result(tmp_path, ok=False, reason_code="PYTEST_TEST_FAILURE"),
    )

    async def _run_pre_push_validation(
        _self: object,
        **_kwargs: object,
    ) -> pre_push_validation._PrePushValidationResult:
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls > 1:
            raise AssertionError("policy block should stop before retry validation")
        return validation_result

    async def _pre_push_validation_commands(
        _self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
    ) -> tuple[str, ...]:
        del workspace_id, worktree_path
        return ("pytest -q",)

    async def _run_fix_pass(_runner: object, **_kwargs: object) -> tuple[bool, str | None]:
        raise _MonitorPolicyBlockedError(
            "protected-scope repair failed",
            reason_code=_PROTECTED_SCOPE_REPAIR_FAILED_REASON,
        )

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_run_pre_push_validation",
        _run_pre_push_validation,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_commands",
        _pre_push_validation_commands,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_run_pre_push_validation_fix_pass",
        _run_fix_pass,
    )

    result = await pre_push_validation._run_pre_push_validation_with_fix_passes(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
        remote_url=None,
        state=None,
    )

    assert validation_calls == 1
    assert result.reason_code == _PROTECTED_SCOPE_REPAIR_FAILED_REASON
    assert "protected-scope repair failed" in result.message


@pytest.mark.unit
async def test_pre_push_validation_fix_pass_head_object_missing_preserves_exception_reason_code(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing-HEAD failures from the fix pass must keep their specific reason."""
    workspace_id = "workspace_fix_head_reason"
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True, exist_ok=True)
    validation_calls = 0
    validation_result = pre_push_validation._PrePushValidationResult(
        passed=False,
        validation_run_id="vr1",
        workspace_head_sha="a" * 40,
        reason_code="PRE_PUSH_VALIDATION_FAILED",
        message="attempt 1 failed",
        validation_reason_code="PYTEST_TEST_FAILURE",
        result=_validation_result(tmp_path, ok=False, reason_code="PYTEST_TEST_FAILURE"),
    )

    async def _run_pre_push_validation(
        _self: object,
        **_kwargs: object,
    ) -> pre_push_validation._PrePushValidationResult:
        nonlocal validation_calls
        validation_calls += 1
        if validation_calls > 1:
            raise AssertionError("missing HEAD should stop before retry validation")
        return validation_result

    async def _pre_push_validation_commands(
        _self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
    ) -> tuple[str, ...]:
        del workspace_id, worktree_path
        return ("pytest -q",)

    async def _run_fix_pass(_runner: object, **_kwargs: object) -> tuple[bool, str | None]:
        raise _MonitorHeadObjectMissingError(
            "HEAD_OBJECT_MISSING_FIX_PASS_CUSTOM",
            "HEAD object missing for workspace_fix_head_reason",
        )

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        pre_push_validation_fix_passes=1,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_run_pre_push_validation",
        _run_pre_push_validation,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_commands",
        _pre_push_validation_commands,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_run_pre_push_validation_fix_pass",
        _run_fix_pass,
    )

    result = await pre_push_validation._run_pre_push_validation_with_fix_passes(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
        remote_url=None,
        state=None,
    )

    assert validation_calls == 1
    assert result.reason_code == "HEAD_OBJECT_MISSING_FIX_PASS_CUSTOM"
    assert "HEAD object missing" in result.message


@pytest.mark.unit
async def test_pre_push_validation_fails_closed_on_git_mirror_hooks_repair_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expected mirror repair failures should produce the poisoned-path reason."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = _write_worktree_with_mirror(tmp_path, workspace_id)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'1' * 40}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        raise GitOperationError(
            operation="mirror.hooks_path_repair",
            returncode=1,
            stdout="",
            stderr="failed",
            reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
        )

    monkeypatch.setattr(
        pre_push_validation,
        "repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
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
    assert result.reason_code == "MIRROR_HOOKS_PATH_POISONED"


@pytest.mark.unit
async def test_pre_push_validation_does_not_mislabel_unexpected_mirror_repair_error(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected mirror repair bugs must not be reported as poisoned hooks paths."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = _write_worktree_with_mirror(tmp_path, workspace_id)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{'2' * 40}\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        raise RuntimeError("repair exploded")

    monkeypatch.setattr(
        pre_push_validation,
        "repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )

    with pytest.raises(RuntimeError, match="repair exploded"):
        await pre_push_validation._run_pre_push_validation(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            remote_branch=f"awf/{workspace_id}",
        )


@pytest.mark.unit
async def test_pre_push_validation_repairs_mirror_hooks_after_validation_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation failures must repair mirror hooks before returning."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = _write_worktree_with_mirror(tmp_path, workspace_id)
    cmd = FakeCommandRunner()
    local_head = "3" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    validation = _FakeValidation(_failed_validation_result(tmp_path))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    repair_calls: list[Path] = []

    async def _repair_mirror_hooks_path(mirror_path: Path) -> bool:
        repair_calls.append(mirror_path)
        return False

    monkeypatch.setattr(pre_push_validation, "repair_mirror_hooks_path", _repair_mirror_hooks_path)
    _patch_clean_pre_push_validation_worktree(monkeypatch)

    result = await pre_push_validation._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
    )

    assert result.passed is False
    assert result.reason_code == pre_push_validation.PRE_PUSH_VALIDATION_FAILED_REASON
    assert repair_calls == [tmp_path / "mirrors" / "test.git"] * 2


@pytest.mark.unit
async def test_pre_push_validation_fails_closed_when_post_validation_mirror_repair_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poisoned mirror after validation must block the validated push result."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = _write_worktree_with_mirror(tmp_path, workspace_id)
    cmd = FakeCommandRunner()
    local_head = "4" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    validation = _FakeValidation(_failed_validation_result(tmp_path))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    repair_calls = 0

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        nonlocal repair_calls
        repair_calls += 1
        if repair_calls == 2:
            raise GitOperationError(
                operation="mirror.hooks_path_repair",
                returncode=1,
                stdout="",
                stderr="failed",
                reason_code="MIRROR_HOOKS_PATH_REPAIR_FAILED",
            )
        return False

    monkeypatch.setattr(pre_push_validation, "repair_mirror_hooks_path", _repair_mirror_hooks_path)
    _patch_clean_pre_push_validation_worktree(monkeypatch)

    result = await pre_push_validation._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
    )

    assert result.passed is False
    assert result.reason_code == _MIRROR_HOOKS_PATH_POISONED_REASON


@pytest.mark.unit
async def test_pre_push_validation_repairs_mirror_hooks_after_cleanup_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation cleanup failures must still repair mirror hooks before returning."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = _write_worktree_with_mirror(tmp_path, workspace_id)
    cmd = FakeCommandRunner()
    local_head = "5" * 40
    cmd.queue_result(returncode=0, stdout=f"{local_head}\n")
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    repair_calls: list[Path] = []

    async def _repair_mirror_hooks_path(mirror_path: Path) -> bool:
        repair_calls.append(mirror_path)
        return False

    async def _pre_push_validation_cleanup(
        self: object,
        *,
        worktree_path: Path,
        restore_ref: str,
    ) -> ValidationWorktreeCleanup:
        del self, worktree_path
        return ValidationWorktreeCleanup(
            cleaned=False,
            check=ValidationWorktreeCheck(clean=False, paths=("generated.txt",)),
            restore_ref=restore_ref,
            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
            message="cleanup failed",
            cleanup_command="git restore",
            cleanup_stderr="restore failed",
        )

    monkeypatch.setattr(pre_push_validation, "repair_mirror_hooks_path", _repair_mirror_hooks_path)
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_worktree_check",
        _clean_pre_push_validation_worktree_check,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_cleanup",
        _pre_push_validation_cleanup,
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
    assert result.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert repair_calls == [tmp_path / "mirrors" / "test.git"] * 2


@pytest.mark.unit
async def test_pre_push_validation_missing_head_uses_candidate_recovery_anchor(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing-HEAD pre-push recovery must not reuse the broken current HEAD SHA."""
    broken_head = "b" * 40
    candidate_head = "c" * 40
    workspace_id = await seed_monitoring_workspace(factory, head_sha=candidate_head)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{broken_head}\n")
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    recovery_anchors: list[str] = []

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        operation_start_head: str,
        task_tag: str | None = None,
        command_evidence: object = (),
    ) -> str | None:
        del self, workspace_id, worktree_path, task_tag, command_evidence
        recovery_anchors.append(operation_start_head)
        return operation_start_head

    async def _pre_push_validation_worktree_check(
        self: object,
        *,
        worktree_path: Path,
    ) -> ValidationWorktreeCheck:
        del self, worktree_path
        return ValidationWorktreeCheck(clean=True)

    async def _pre_push_validation_cleanup(
        self: object,
        *,
        worktree_path: Path,
        restore_ref: str,
    ) -> ValidationWorktreeCleanup:
        del self, worktree_path
        return ValidationWorktreeCleanup(
            cleaned=False,
            check=ValidationWorktreeCheck(clean=True),
            restore_ref=restore_ref,
        )

    monkeypatch.setattr(
        pre_push_validation,
        "repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
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

    result = await pre_push_validation._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
    )

    assert result.passed is True
    assert result.workspace_head_sha == candidate_head
    assert recovery_anchors == [candidate_head]
    assert candidate_head != broken_head


@pytest.mark.unit
async def test_pre_push_validation_missing_head_skips_dangling_operation_start_anchor(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing-HEAD pre-push recovery falls back when the operation anchor is dangling."""
    broken_head = "b" * 40
    dangling_operation_head = "d" * 40
    candidate_head = "c" * 40
    workspace_id = await seed_monitoring_workspace(factory, head_sha=candidate_head)
    await _set_resolved_profile(factory, workspace_id)
    worktree = _write_worktree_with_mirror(tmp_path, workspace_id)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{broken_head}\n")
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    recovery_anchors: list[str] = []
    checked_anchors: list[str] = []

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return False

    async def _mirror_commit_object_exists(
        self: object,
        mirror_path: Path,
        commit_sha: str,
    ) -> bool:
        del self, mirror_path
        checked_anchors.append(commit_sha)
        return commit_sha == candidate_head

    async def _recover_missing_head_object_from_filesystem(
        self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        operation_start_head: str,
        task_tag: str | None = None,
        command_evidence: object = (),
    ) -> str | None:
        del self, workspace_id, worktree_path, task_tag, command_evidence
        recovery_anchors.append(operation_start_head)
        return operation_start_head

    async def _pre_push_validation_worktree_check(
        self: object,
        *,
        worktree_path: Path,
    ) -> ValidationWorktreeCheck:
        del self, worktree_path
        return ValidationWorktreeCheck(clean=True)

    async def _pre_push_validation_cleanup(
        self: object,
        *,
        worktree_path: Path,
        restore_ref: str,
    ) -> ValidationWorktreeCleanup:
        del self, worktree_path
        return ValidationWorktreeCleanup(
            cleaned=False,
            check=ValidationWorktreeCheck(clean=True),
            restore_ref=restore_ref,
        )

    monkeypatch.setattr(
        pre_push_validation,
        "repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "verify_head_object_exists",
        _verify_head_object_exists,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_mirror_commit_object_exists",
        _mirror_commit_object_exists,
        raising=False,
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

    result = await pre_push_validation._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
        operation_start_head=dangling_operation_head,
    )

    assert result.passed is True
    assert result.workspace_head_sha == candidate_head
    assert checked_anchors == [dangling_operation_head]
    assert recovery_anchors == [candidate_head]


@pytest.mark.unit
async def test_pre_push_validation_missing_head_recovery_policy_block_cleans_residue(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Policy-blocked missing-HEAD recovery cleans staged residue before retry."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    recovery_base = "1" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{recovery_base}\n")
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    recovery_calls: list[dict[str, object]] = []
    cleanup_calls: list[dict[str, object]] = []

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        operation_start_head: str,
        task_tag: str | None = None,
        command_evidence: object = (),
    ) -> str | None:
        del self, worktree_path, task_tag
        assert operation_start_head == recovery_base
        recovery_calls.append(
            {
                "workspace_id": workspace_id,
                "command_evidence": command_evidence,
            }
        )
        raise _MonitorPolicyBlockedError(
            "PROTECTED_SCOPE_REPAIR_FAILED (.github/workflows/ci.yml)",
            reason_code=_PROTECTED_SCOPE_REPAIR_FAILED_REASON,
        )

    async def _pre_push_validation_cleanup(
        self: object,
        *,
        worktree_path: Path,
        restore_ref: str,
    ) -> ValidationWorktreeCleanup:
        del self
        cleanup_calls.append(
            {
                "worktree_path": worktree_path,
                "restore_ref": restore_ref,
            }
        )
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=ValidationWorktreeCheck(clean=False, paths=("package-lock.json",)),
            restore_ref=restore_ref,
            cleaned_paths=("package-lock.json",),
        )

    monkeypatch.setattr(
        pre_push_validation,
        "repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
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
        "_pre_push_validation_cleanup",
        _pre_push_validation_cleanup,
    )
    result = await pre_push_validation._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
        operation_start_head=recovery_base,
    )

    assert result.passed is False
    assert result.reason_code == _PROTECTED_SCOPE_REPAIR_FAILED_REASON
    assert "PROTECTED_SCOPE_REPAIR_FAILED" in result.message
    assert result.workspace_head_sha == recovery_base
    assert recovery_calls == [
        {
            "workspace_id": workspace_id,
            "command_evidence": ("pytest -q",),
        }
    ]
    assert cleanup_calls == [
        {
            "worktree_path": worktree,
            "restore_ref": recovery_base,
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

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        operation_start_head: str,
        task_tag: str | None = None,
        command_evidence: object = (),
    ) -> str | None:
        del self, workspace_id, worktree_path, task_tag
        assert operation_start_head == recovery_base
        return recovered_head

    async def _refresh_supply_chain_policy_before_push(**kwargs: object) -> str | None:
        refresh_calls.append(dict(kwargs))
        return None

    monkeypatch.setattr(
        pre_push_validation,
        "repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
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
        "_mirror_commit_object_exists",
        _existing_mirror_commit,
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
        operation_start_head=recovery_base,
    )

    assert result.passed is False
    assert result.reason_code == "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
    assert "recovered HEAD diff unavailable" in result.message
    assert refresh_calls == []
    assert validation.calls == []


@pytest.mark.unit
async def test_pre_push_validation_recovered_head_blocks_committed_protected_scope_violation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovered pre-push HEAD commits must validate protected files as committed diffs."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    recovery_base = "5" * 40
    recovered_head = "6" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{recovery_base}\n")
    cmd.queue_result(returncode=0, stdout="M\0.github/workflows/ci.yml\0")
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    committed_diff_calls: list[dict[str, object]] = []
    ownership_calls: list[dict[str, object]] = []
    cleanup_calls: list[dict[str, object]] = []

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        operation_start_head: str,
        task_tag: str | None = None,
        command_evidence: object = (),
    ) -> str | None:
        del self, workspace_id, worktree_path, task_tag, command_evidence
        assert operation_start_head == recovery_base
        return recovered_head

    async def _repair_agent_runtime_ownership(**kwargs: object) -> bool:
        ownership_calls.append(dict(kwargs))
        return True

    async def _protected_scope_violations_for_recovered_commit(
        *args: object,
        **kwargs: object,
    ) -> list[QualityGateViolation]:
        committed_diff_calls.append({"args": args, **kwargs})
        return [
            QualityGateViolation(
                path=".github/workflows/ci.yml",
                protected_pattern=".github/workflows/",
            )
        ]

    async def _repair_protected_scope_changes_before_commit(**_kwargs: object) -> CommandResult:
        raise AssertionError("recovered committed diffs must not use dirty-status repair")

    async def _pre_push_validation_cleanup(
        self: object,
        *,
        worktree_path: Path,
        restore_ref: str,
    ) -> ValidationWorktreeCleanup:
        del self
        cleanup_calls.append(
            {
                "worktree_path": worktree_path,
                "restore_ref": restore_ref,
            }
        )
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=ValidationWorktreeCheck(clean=True),
            restore_ref=restore_ref,
        )

    monkeypatch.setattr(
        pre_push_validation,
        "repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
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
        "_mirror_commit_object_exists",
        _existing_mirror_commit,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(
        runner,
        "_repair_protected_scope_changes_before_commit",
        _repair_protected_scope_changes_before_commit,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_protected_scope_violations_for_recovered_commit",
        _protected_scope_violations_for_recovered_commit,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_pre_push_validation_cleanup",
        _pre_push_validation_cleanup,
    )

    result = await pre_push_validation._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
        operation_start_head=recovery_base,
    )

    assert result.passed is False
    assert result.workspace_head_sha == recovery_base
    assert result.reason_code == "PROTECTED_SCOPE_REPAIR_FAILED"
    assert "recovered HEAD protected-scope repair failed" in result.message
    assert ownership_calls == [
        {
            "logger": pre_push_validation._log,
            "workspace_id": workspace_id,
            "worktree_path": worktree,
            "reason": "dirty_worktree_pre_commit",
            "event_name": "monitor.agent_runtime_ownership_repair_failed",
        }
    ]
    assert committed_diff_calls == [
        {
            "args": (runner,),
            "workspace_id": workspace_id,
            "worktree_path": worktree,
            "base_ref": recovery_base,
            "changed_paths": (".github/workflows/ci.yml",),
        }
    ]
    assert cleanup_calls == [
        {
            "worktree_path": worktree,
            "restore_ref": recovery_base,
        }
    ]
    assert validation.calls == []


@pytest.mark.unit
async def test_pre_push_validation_recovered_head_rename_includes_source_path(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovered pre-push HEAD rename diffs must include protected source paths."""
    monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/tmp/private-objects")
    monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/tmp/private-alternates")
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    recovery_base = "5" * 40
    recovered_head = "7" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{recovery_base}\n")
    cmd.queue_result(
        returncode=0,
        stdout="R100\0.github/workflows/ci.yml\0docs/ci.yml\0",
    )
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    committed_diff_calls: list[dict[str, object]] = []

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        operation_start_head: str,
        task_tag: str | None = None,
        command_evidence: object = (),
    ) -> str | None:
        del self, workspace_id, worktree_path, task_tag, command_evidence
        assert operation_start_head == recovery_base
        return recovered_head

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        return True

    async def _protected_scope_violations_for_recovered_commit(
        *args: object,
        **kwargs: object,
    ) -> list[QualityGateViolation]:
        committed_diff_calls.append({"args": args, **kwargs})
        return [
            QualityGateViolation(
                path=".github/workflows/ci.yml",
                protected_pattern=".github/workflows/",
            )
        ]

    monkeypatch.setattr(
        pre_push_validation,
        "repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
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
        "_mirror_commit_object_exists",
        _existing_mirror_commit,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_protected_scope_violations_for_recovered_commit",
        _protected_scope_violations_for_recovered_commit,
    )

    result = await pre_push_validation._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
        operation_start_head=recovery_base,
    )

    recovered_diff_command = next(call for call in cmd.calls if "--name-status" in call.args)
    recovered_diff_call = recovered_diff_command.args
    assert "--name-only" not in recovered_diff_call
    assert recovered_diff_command.env is not None
    assert "GIT_OBJECT_DIRECTORY" not in recovered_diff_command.env
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in recovered_diff_command.env
    assert result.passed is False
    assert result.workspace_head_sha == recovered_head
    assert result.reason_code == "PROTECTED_SCOPE_REPAIR_FAILED"
    assert committed_diff_calls == [
        {
            "args": (runner,),
            "workspace_id": workspace_id,
            "worktree_path": worktree,
            "base_ref": recovery_base,
            "changed_paths": (".github/workflows/ci.yml", "docs/ci.yml"),
        }
    ]
    assert validation.calls == []


@pytest.mark.unit
async def test_pre_push_validation_recovered_head_ownership_repair_failure_blocks_validation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovered pre-push HEAD ownership repair failures must stop before validation starts."""
    workspace_id = await seed_monitoring_workspace(factory)
    await _set_resolved_profile(factory, workspace_id)
    worktree = tmp_path / "worktrees" / workspace_id
    _mark_git_worktree(worktree)
    recovery_base = "7" * 40
    recovered_head = "8" * 40
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{recovery_base}\n")
    cmd.queue_result(returncode=0, stdout="M\0.github/workflows/ci.yml\0")
    validation = _FakeValidation(_validation_result(tmp_path, ok=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.validation = validation  # type: ignore[assignment]
    committed_diff_calls: list[dict[str, object]] = []

    async def _verify_head_object_exists(_worktree_path: Path) -> bool:
        return False

    async def _repair_mirror_hooks_path(_mirror_path: Path) -> bool:
        return False

    async def _recover_missing_head_object_from_filesystem(
        self: object,
        *,
        workspace_id: str,
        worktree_path: Path,
        operation_start_head: str,
        task_tag: str | None = None,
        command_evidence: object = (),
    ) -> str | None:
        del self, workspace_id, worktree_path, task_tag, command_evidence
        assert operation_start_head == recovery_base
        return recovered_head

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        return False

    async def _protected_scope_violations_for_recovered_commit(
        *args: object,
        **kwargs: object,
    ) -> list[QualityGateViolation]:
        committed_diff_calls.append({"args": args, **kwargs})
        return []

    monkeypatch.setattr(
        pre_push_validation,
        "repair_mirror_hooks_path",
        _repair_mirror_hooks_path,
    )
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
        "_mirror_commit_object_exists",
        _existing_mirror_commit,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "repair_agent_runtime_ownership",
        _repair_agent_runtime_ownership,
    )
    monkeypatch.setattr(
        pre_push_validation,
        "_protected_scope_violations_for_recovered_commit",
        _protected_scope_violations_for_recovered_commit,
    )

    result = await pre_push_validation._run_pre_push_validation(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
        operation_start_head=recovery_base,
    )

    assert result.passed is False
    assert result.workspace_head_sha == recovered_head
    assert result.reason_code == "AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED"
    assert validation.calls == []
    assert committed_diff_calls == []
