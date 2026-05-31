"""Worktree cleanliness helpers for AWF-owned validation commands."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from awf.common.commands import CommandResult
from awf.runtime.pr_monitor_runner.path_parsing import (
    _changed_paths_from_porcelain,
    _untracked_paths_from_porcelain,
)

VALIDATION_WORKTREE_PRE_EXISTING_DIRTY = "VALIDATION_WORKTREE_PRE_EXISTING_DIRTY"
VALIDATION_WORKTREE_CLEANUP_FAILED = "VALIDATION_WORKTREE_CLEANUP_FAILED"
VALIDATION_WORKTREE_STATUS_FAILED = "VALIDATION_WORKTREE_STATUS_FAILED"

GitRunner = Callable[[list[str]], Awaitable[CommandResult]]


@dataclass(frozen=True)
class ValidationWorktreeCheck:
    clean: bool
    skipped: bool = False
    paths: tuple[str, ...] = ()
    untracked_paths: tuple[str, ...] = ()
    reason_code: str | None = None
    message: str = ""
    command_stderr: str = ""

    @property
    def tracked_paths(self) -> tuple[str, ...]:
        untracked = set(self.untracked_paths)
        return tuple(path for path in self.paths if path not in untracked)

    def details(self) -> dict[str, object]:
        details: dict[str, object] = {
            "paths": list(self.paths),
            "untracked_paths": list(self.untracked_paths),
        }
        if self.reason_code is not None:
            details["reason_code"] = self.reason_code
        if self.command_stderr:
            details["command_stderr"] = self.command_stderr
        return details


@dataclass(frozen=True)
class ValidationWorktreeCleanup:
    cleaned: bool
    check: ValidationWorktreeCheck
    restore_ref: str = "HEAD"
    reason_code: str | None = None
    message: str = ""
    cleanup_command: str | None = None
    cleanup_stderr: str = ""
    verify_check: ValidationWorktreeCheck | None = None

    @property
    def ok(self) -> bool:
        return self.reason_code is None

    def details(self) -> dict[str, object]:
        details = self.check.details()
        details["restore_ref"] = self.restore_ref
        if self.reason_code is not None:
            details["reason_code"] = self.reason_code
        if self.cleanup_command is not None:
            details["cleanup_command"] = self.cleanup_command
        if self.cleanup_stderr:
            details["cleanup_stderr"] = self.cleanup_stderr
        if self.verify_check is not None and self.verify_check.paths:
            details["remaining_paths"] = list(self.verify_check.paths)
            details["remaining_untracked_paths"] = list(self.verify_check.untracked_paths)
        return details


async def check_validation_worktree_clean(
    *,
    run_git: GitRunner,
    worktree_path: Path,
) -> ValidationWorktreeCheck:
    """Return dirty paths before or after an AWF validation command.

    Unit tests often use plain directories instead of real git worktrees. Real
    AWF worktrees always contain a `.git` control file, so skip the guard only
    for those lightweight test doubles.
    """
    if not (worktree_path / ".git").exists():
        return ValidationWorktreeCheck(clean=True, skipped=True)

    status = await run_git(["status", "--porcelain=v1", "--untracked-files=all"])
    if not status.ok:
        stderr = (status.stderr or "")[:1000]
        return ValidationWorktreeCheck(
            clean=False,
            reason_code=VALIDATION_WORKTREE_STATUS_FAILED,
            message=(
                "Could not inspect validation worktree cleanliness with `git status --porcelain`."
            ),
            command_stderr=stderr,
        )

    status_stdout = status.stdout or ""
    paths = tuple(_changed_paths_from_porcelain(status_stdout))
    untracked_paths = tuple(_untracked_paths_from_porcelain(status_stdout))
    if not paths and not untracked_paths:
        return ValidationWorktreeCheck(clean=True)
    return ValidationWorktreeCheck(
        clean=False,
        paths=paths,
        untracked_paths=untracked_paths,
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
        message=(
            "Validation worktree has pre-existing uncommitted changes; "
            "refusing to run AWF-owned validation from a dirty tree."
        ),
    )


async def cleanup_validation_worktree_side_effects(
    *,
    run_git: GitRunner,
    worktree_path: Path,
    restore_ref: str = "HEAD",
) -> ValidationWorktreeCleanup:
    """Restore dirty files created by AWF-owned validation commands."""
    check = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree_path)
    if check.clean:
        return ValidationWorktreeCleanup(cleaned=False, check=check, restore_ref=restore_ref)
    if check.reason_code == VALIDATION_WORKTREE_STATUS_FAILED:
        return ValidationWorktreeCleanup(
            cleaned=False,
            check=check,
            restore_ref=restore_ref,
            reason_code=VALIDATION_WORKTREE_STATUS_FAILED,
            message=check.message,
        )

    tracked_paths = check.tracked_paths
    if tracked_paths:
        restore = await run_git(
            ["restore", "--source", restore_ref, "--staged", "--worktree", "--", *tracked_paths]
        )
        if not restore.ok:
            return ValidationWorktreeCleanup(
                cleaned=False,
                check=check,
                restore_ref=restore_ref,
                reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
                message=(
                    "AWF validation left dirty tracked files and `git restore` "
                    "could not restore them."
                ),
                cleanup_command="git restore",
                cleanup_stderr=(restore.stderr or "")[:1000],
            )

    if check.untracked_paths:
        clean = await run_git(["clean", "-fd", "--", *check.untracked_paths])
        if not clean.ok:
            return ValidationWorktreeCleanup(
                cleaned=False,
                check=check,
                restore_ref=restore_ref,
                reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
                message=(
                    "AWF validation left untracked files and `git clean` could not remove them."
                ),
                cleanup_command="git clean",
                cleanup_stderr=(clean.stderr or "")[:1000],
            )

    verify = await check_validation_worktree_clean(run_git=run_git, worktree_path=worktree_path)
    if not verify.clean:
        return ValidationWorktreeCleanup(
            cleaned=False,
            check=check,
            restore_ref=restore_ref,
            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
            message="AWF validation worktree cleanup completed but the worktree is still dirty.",
            cleanup_command=None,
            verify_check=verify,
        )

    return ValidationWorktreeCleanup(
        cleaned=True,
        check=check,
        restore_ref=restore_ref,
        verify_check=verify,
    )


def validation_worktree_preexisting_dirty_message(check: ValidationWorktreeCheck) -> str:
    paths = ", ".join(check.paths) if check.paths else "<unknown>"
    return f"{check.message} Dirty paths: {paths}"


def validation_worktree_cleanup_failure_message(cleanup: ValidationWorktreeCleanup) -> str:
    paths = ", ".join(cleanup.check.paths) if cleanup.check.paths else "<unknown>"
    return f"{cleanup.message} Dirty paths: {paths}"
