"""Focused edge coverage for PR monitor pre-push validation helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.runtime.pr_monitor_runner import pre_push_validation
from awf.runtime.validation_types import ValidationCommandResult, ValidationResult
from awf.runtime.validation_worktree import ValidationWorktreeCheck, ValidationWorktreeCleanup


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
