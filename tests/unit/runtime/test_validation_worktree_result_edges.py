"""Focused tests for validation worktree result edge branches."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

import awf.runtime.validation_worktree as validation_worktree
from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    VALIDATION_WORKTREE_STATUS_FAILED,
    ValidationWorktreeCheck,
    ValidationWorktreeCleanup,
    check_validation_worktree_clean,
    cleanup_validation_worktree_side_effects,
    validation_worktree_cleanup_failure_message,
    validation_worktree_preexisting_dirty_message,
)


@dataclass
class _CommandResultLike:
    """Minimal command-result stand-in for helper tests."""

    returncode: int
    stdout: str | None
    stderr: str | None

    @property
    def ok(self) -> bool:
        """Return whether the simulated command completed successfully."""
        return self.returncode == 0


@pytest.mark.unit
def test_result_details_include_stderr_and_verify_remaining_paths() -> None:
    """Structured cleanup details should retain nested failure evidence."""
    check = ValidationWorktreeCheck(
        clean=False,
        paths=("dirty.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
        command_stderr="status failed",
    )
    verify_check = ValidationWorktreeCheck(
        clean=False,
        paths=("still-dirty.py",),
        untracked_paths=("still-dirty.py",),
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
        command_stderr="verify failed",
    )
    cleanup = ValidationWorktreeCleanup(
        cleaned=False,
        check=check,
        restore_ref="HEAD",
        reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
        cleanup_stderr="clean failed",
        verify_check=verify_check,
    )

    assert cleanup.ok is False
    assert check.details()["command_stderr"] == "status failed"
    details = cleanup.details()
    assert details["cleanup_stderr"] == "clean failed"
    assert details["verify_reason_code"] == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert details["verify_command_stderr"] == "verify failed"
    assert details["remaining_paths"] == ["still-dirty.py"]
    assert details["remaining_untracked_paths"] == ["still-dirty.py"]


@pytest.mark.unit
def test_head_resolution_helpers_report_missing_output() -> None:
    """Empty rev-parse output should become explicit missing-HEAD evidence."""
    assert validation_worktree._first_output_line(None) == ""
    sha, message = validation_worktree._resolve_head_sha(
        _CommandResultLike(0, "\n", None),
        ref="HEAD",
    )

    assert sha is None
    assert message == "Could not resolve HEAD for `HEAD` from git rev-parse output."


@pytest.mark.unit
async def test_check_validation_worktree_clean_skips_non_git_test_double(
    tmp_path: Path,
) -> None:
    """Lightweight test doubles without a .git marker should skip the guard."""

    async def run_git(args: list[str]) -> _CommandResultLike:
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(
        run_git=run_git,
        worktree_path=tmp_path,
    )

    assert check.clean is True
    assert check.skipped is True


@pytest.mark.unit
async def test_cleanup_validation_worktree_side_effects_skips_non_git_test_double(
    tmp_path: Path,
) -> None:
    """Cleanup should no-op for lightweight test doubles without .git."""

    async def run_git(args: list[str]) -> _CommandResultLike:
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git,
        worktree_path=tmp_path,
        restore_ref="HEAD",
    )

    assert cleanup.cleaned is True
    assert cleanup.check.skipped is True
    assert cleanup.restore_ref == "HEAD"


@pytest.mark.unit
def test_validation_worktree_messages_handle_status_and_unknown_paths() -> None:
    """Message renderers should retain status failures and unknown dirty paths."""
    dirty_check = ValidationWorktreeCheck(
        clean=False,
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
        message="Validation worktree is dirty.",
    )
    status_cleanup = ValidationWorktreeCleanup(
        cleaned=False,
        check=dirty_check,
        reason_code=VALIDATION_WORKTREE_STATUS_FAILED,
        message="status failed",
    )
    cleanup = ValidationWorktreeCleanup(
        cleaned=False,
        check=dirty_check,
        reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
        message="cleanup failed",
    )

    assert validation_worktree_preexisting_dirty_message(dirty_check) == (
        "Validation worktree is dirty. Dirty paths: <unknown>"
    )
    assert validation_worktree_cleanup_failure_message(status_cleanup) == "status failed"
    assert validation_worktree_cleanup_failure_message(cleanup) == (
        "cleanup failed Dirty paths: <unknown>"
    )
