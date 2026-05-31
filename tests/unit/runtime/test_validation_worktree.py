"""Unit tests for validation worktree cleanup helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from awf.runtime.validation_worktree import (
    VALIDATION_WORKTREE_CLEANUP_FAILED,
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
    VALIDATION_WORKTREE_STATUS_FAILED,
    ValidationWorktreeCheck,
    ValidationWorktreeCleanup,
    check_validation_worktree_clean,
    cleanup_validation_worktree_side_effects,
    validation_worktree_cleanup_failure_message,
)


@dataclass
class _CommandResultLike:
    """Minimal command-result stand-in for status/revert command assertions."""

    returncode: int
    stdout: str | None
    stderr: str | None
    reason_code: str | None = None

    @property
    def ok(self) -> bool:
        """Return whether the simulated command completed successfully."""
        return self.returncode == 0


@pytest.mark.unit
async def test_check_validation_worktree_clean_handles_none_stdout_as_clean(tmp_path: Path) -> None:
    """A git status result with ``None`` stdout should behave as a clean worktree."""
    worktree = _init_fake_worktree(tmp_path)

    async def run_git(args: list[str]) -> _CommandResultLike:
        if args == ["status", "--porcelain=v1", "--untracked-files=all"]:
            return _CommandResultLike(0, None, "")
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree)

    assert check.clean is True
    assert check.reason_code is None


@pytest.mark.unit
async def test_check_validation_worktree_clean_treats_untracked_paths_as_dirty(
    tmp_path: Path,
) -> None:
    """Untracked files are pre-existing dirt and should be rejected by the guard."""
    worktree = _init_fake_worktree(tmp_path)

    async def run_git(args: list[str]) -> _CommandResultLike:
        if args == ["status", "--porcelain=v1", "--untracked-files=all"]:
            return _CommandResultLike(0, "?? untracked.py\n", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    check = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree)

    assert check.clean is False
    assert check.reason_code == VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
    assert check.paths == ("untracked.py",)
    assert check.untracked_paths == ("untracked.py",)


def _init_fake_worktree(tmp_path: Path) -> Path:
    """Create a fake worktree path with a minimal `.git` marker."""
    worktree = tmp_path / "worktree"
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / ".git").write_text("gitdir: /tmp/fake.git\n", encoding="utf-8")
    return worktree


@pytest.mark.unit
async def test_cleanup_validation_worktree_restores_tracked_files_with_none_stderr(
    tmp_path: Path,
) -> None:
    """A failed git restore should not crash if stderr is None."""
    worktree = _init_fake_worktree(tmp_path)

    async def run_git(args: list[str]) -> _CommandResultLike:
        if args == ["status", "--porcelain=v1", "--untracked-files=all"]:
            return _CommandResultLike(0, " M tracked.py\n", "")
        if args[:1] == ["restore"]:
            return _CommandResultLike(1, "", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git, worktree_path=worktree
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert cleanup.cleanup_command == "git restore"
    assert cleanup.cleanup_stderr == ""


@pytest.mark.unit
async def test_cleanup_validation_worktree_cleans_untracked_files_with_none_stderr(
    tmp_path: Path,
) -> None:
    """A failed git clean should not crash if stderr is None."""
    worktree = _init_fake_worktree(tmp_path)

    async def run_git(args: list[str]) -> _CommandResultLike:
        if args == ["status", "--porcelain=v1", "--untracked-files=all"]:
            return _CommandResultLike(0, "?? untracked.py\n", "")
        if args[:1] == ["clean"]:
            return _CommandResultLike(1, "", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git, worktree_path=worktree
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert cleanup.cleanup_command == "git clean"
    assert cleanup.cleanup_stderr == ""


@pytest.mark.unit
async def test_cleanup_validation_worktree_verify_check_does_not_report_status_as_cleanup_command(
    tmp_path: Path,
) -> None:
    """If cleanup succeeds but worktree remains dirty, do not label status as the cleanup command."""
    worktree = _init_fake_worktree(tmp_path)

    calls: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        calls.append(tuple(args))
        if args == ["status", "--porcelain=v1", "--untracked-files=all"]:
            if len(calls) == 1:
                return _CommandResultLike(0, " M tracked.py\n", "")
            return _CommandResultLike(0, " M tracked.py\n", "")
        if args[:1] == ["restore"]:
            return _CommandResultLike(0, "", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git, worktree_path=worktree
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert cleanup.cleanup_command is None
    assert cleanup.verify_check is not None and not cleanup.verify_check.clean


@pytest.mark.unit
async def test_cleanup_validation_worktree_verify_status_failure_is_preserved(
    tmp_path: Path,
) -> None:
    """Status inspection failures during post-clean verification."""
    worktree = _init_fake_worktree(tmp_path)
    calls: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        calls.append(tuple(args))
        if args == ["status", "--porcelain=v1", "--untracked-files=all"]:
            if len(calls) == 1:
                return _CommandResultLike(0, " M tracked.py\n", "")
            return _CommandResultLike(1, "", "status command failed")
        if args[:1] == ["restore"]:
            return _CommandResultLike(0, "", None)
        if args[:1] == ["clean"]:
            return _CommandResultLike(0, "", None)
        if args == ["rev-parse", "HEAD"]:
            return _CommandResultLike(0, "abc1234\n", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git, worktree_path=worktree
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_STATUS_FAILED
    assert cleanup.message == (
        "Could not inspect validation worktree cleanliness with `git status --porcelain`."
    )
    assert cleanup.verify_check is not None
    assert cleanup.verify_check.reason_code == VALIDATION_WORKTREE_STATUS_FAILED
    assert cleanup.verify_check.command_stderr == "status command failed"


@pytest.mark.unit
async def test_cleanup_validation_worktree_rejects_invalid_head_output(
    tmp_path: Path,
) -> None:
    """Malformed ``git rev-parse`` output must fail as status-check validation."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "deadbeef01"

    async def run_git(args: list[str]) -> _CommandResultLike:
        if args == ["status", "--porcelain=v1", "--untracked-files=all"]:
            return _CommandResultLike(0, "", None)
        if args == ["rev-parse", restore_ref]:
            return _CommandResultLike(0, "M\x00src/fix.py\0", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git,
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_STATUS_FAILED
    assert cleanup.message == (
        "Could not verify validation worktree HEAD: "
        "Could not resolve HEAD from git rev-parse output: invalid object id."
    )


@pytest.mark.unit
async def test_cleanup_validation_worktree_fails_when_head_changes(
    tmp_path: Path,
) -> None:
    """A clean worktree whose HEAD advanced during validation should fail cleanup."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    current_head = "b" * 40

    async def run_git(args: list[str]) -> _CommandResultLike:
        if args == ["status", "--porcelain=v1", "--untracked-files=all"]:
            return _CommandResultLike(0, "", None)
        if args == ["rev-parse", restore_ref]:
            return _CommandResultLike(0, f"{restore_ref}\n", None)
        if args == ["rev-parse", "HEAD"]:
            return _CommandResultLike(0, f"{current_head}\n", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git, worktree_path=worktree, restore_ref=restore_ref
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert cleanup.cleanup_command == "git rev-parse"
    assert "Expected aaaaaaaa, found bbbbbbbb." in cleanup.message


@pytest.mark.unit
async def test_cleanup_validation_worktree_rejects_clean_state_with_default_head_reference(
    tmp_path: Path,
) -> None:
    """A clean worktree cannot be validated without a captured pre-validation HEAD."""
    worktree = _init_fake_worktree(tmp_path)
    calls: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        calls.append(tuple(args))
        if args == ["status", "--porcelain=v1", "--untracked-files=all"]:
            return _CommandResultLike(0, "", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git, worktree_path=worktree, restore_ref="HEAD"
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert cleanup.message == (
        "Could not verify validation worktree HEAD after cleanup because "
        "`restore_ref` was not captured before validation."
    )
    assert calls == [("status", "--porcelain=v1", "--untracked-files=all")]


@pytest.mark.unit
async def test_cleanup_validation_worktree_detects_head_change_after_dirty_cleanup(
    tmp_path: Path,
) -> None:
    """A clean tree after dirty cleanup should still fail if validation changed HEAD."""
    worktree = _init_fake_worktree(tmp_path)
    restore_ref = "a" * 40
    current_head = "b" * 40
    calls: list[tuple[str, ...]] = []

    async def run_git(args: list[str]) -> _CommandResultLike:
        calls.append(tuple(args))
        if args == ["status", "--porcelain=v1", "--untracked-files=all"]:
            if len(calls) == 1:
                return _CommandResultLike(0, "?? untracked.py\n", "")
            return _CommandResultLike(0, "", None)
        if args == ["clean", "-fd", "--", "untracked.py"]:
            return _CommandResultLike(0, "", None)
        if args == ["rev-parse", restore_ref]:
            return _CommandResultLike(0, f"{restore_ref}\n", None)
        if args == ["rev-parse", "HEAD"]:
            return _CommandResultLike(0, f"{current_head}\n", None)
        raise AssertionError(f"unexpected git command: {args!r}")

    cleanup = await cleanup_validation_worktree_side_effects(
        run_git=run_git,
        worktree_path=worktree,
        restore_ref=restore_ref,
    )

    assert cleanup.reason_code == VALIDATION_WORKTREE_CLEANUP_FAILED
    assert cleanup.cleanup_command == "git rev-parse"
    assert "Expected aaaaaaaa, found bbbbbbbb." in cleanup.message


@pytest.mark.unit
def test_validation_worktree_cleanup_failure_message_prefers_verify_paths() -> None:
    """Human-readable cleanup failures should report remaining dirty paths when verification runs."""
    cleanup = ValidationWorktreeCleanup(
        cleaned=False,
        check=ValidationWorktreeCheck(
            clean=False,
            paths=("initial.py",),
            untracked_paths=(),
            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
            message="AWF validation worktree cleanup completed but the worktree is still dirty.",
        ),
        restore_ref="HEAD",
        reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
        message="AWF validation worktree cleanup completed but the worktree is still dirty.",
        cleanup_command=None,
        verify_check=ValidationWorktreeCheck(
            clean=False,
            paths=("remaining.py",),
            untracked_paths=("remaining_untracked.py",),
            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
            message="AWF validation worktree cleanup completed but the worktree is still dirty.",
        ),
    )

    message = validation_worktree_cleanup_failure_message(cleanup)

    assert "remaining.py" in message
    assert "initial.py" not in message
    assert "remaining_untracked.py" not in message
