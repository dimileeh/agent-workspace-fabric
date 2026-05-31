"""Worktree cleanliness helpers for AWF-owned validation commands."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from awf.common.commands import CommandResult
from awf.runtime.validation_worktree_constants import (
    VALIDATION_WORKTREE_CLEANUP_FAILED as _VALIDATION_WORKTREE_CLEANUP_FAILED,
)
from awf.runtime.validation_worktree_constants import (
    VALIDATION_WORKTREE_PRE_EXISTING_DIRTY as _VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
)
from awf.runtime.validation_worktree_constants import (
    VALIDATION_WORKTREE_STATUS_FAILED as _VALIDATION_WORKTREE_STATUS_FAILED,
)

VALIDATION_WORKTREE_CLEANUP_FAILED: str = _VALIDATION_WORKTREE_CLEANUP_FAILED
VALIDATION_WORKTREE_PRE_EXISTING_DIRTY: str = _VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
VALIDATION_WORKTREE_STATUS_FAILED: str = _VALIDATION_WORKTREE_STATUS_FAILED

GitRunner = Callable[[list[str]], Awaitable[CommandResult]]

_PORCELAIN_C_ESCAPES = {
    "a": 0x07,
    "b": 0x08,
    "t": 0x09,
    "n": 0x0A,
    "v": 0x0B,
    "f": 0x0C,
    "r": 0x0D,
    '"': 0x22,
    "\\": 0x5C,
}
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$", re.IGNORECASE)


def _split_porcelain_rename_paths(path: str) -> tuple[str, str] | None:
    """Split porcelain rename paths without importing the PR monitor package."""
    in_quote = False
    escaped = False
    for index, char in enumerate(path):
        if in_quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_quote = False
            continue

        if char == '"':
            in_quote = True
            continue

        if path.startswith(" -> ", index):
            return path[:index], path[index + 4 :]

    return None


def _unquote_porcelain_path(path: str) -> str:
    """Decode Git's C-quoted porcelain path form when present."""
    if len(path) < 2 or path[0] != '"' or path[-1] != '"':
        return path

    raw = bytearray()
    end = len(path) - 1
    i = 1
    while i < end:
        char = path[i]
        if char != "\\":
            raw.extend(char.encode("utf-8", "surrogateescape"))
            i += 1
            continue

        i += 1
        if i >= end:
            raw.append(ord("\\"))
            break

        escaped = path[i]
        if escaped in _PORCELAIN_C_ESCAPES:
            raw.append(_PORCELAIN_C_ESCAPES[escaped])
            i += 1
            continue

        if "0" <= escaped <= "7":
            j = i + 1
            while j < end and j < i + 3 and "0" <= path[j] <= "7":
                j += 1
            raw.append(int(path[i:j], 8))
            i = j
            continue

        raw.extend(escaped.encode("utf-8", "surrogateescape"))
        i += 1

    return bytes(raw).decode("utf-8", "surrogateescape")


def _first_output_line(stdout: str | None) -> str:
    """Return the first status-free output line from a git command."""
    if not stdout:
        return ""
    return stdout.splitlines()[0].strip()


def _resolve_head_sha(result: CommandResult, *, ref: str) -> tuple[str | None, str]:
    """Extract a resolved revision SHA from a git rev-parse output."""
    sha = _first_output_line(result.stdout)
    if not sha:
        return (
            None,
            f"Could not resolve HEAD for `{ref}` from git rev-parse output.",
        )
    if "\x00" in sha or not _GIT_SHA_RE.fullmatch(sha):
        return (
            None,
            "Could not resolve HEAD from git rev-parse output: invalid object id.",
        )
    return sha, ""


def _changed_paths_from_porcelain(status_stdout: str) -> list[str]:
    """Extract changed paths from ``git status --porcelain`` output."""
    paths: list[str] = []
    for line in status_stdout.splitlines():
        if not line:
            continue
        if line.startswith("?? ") or (len(line) >= 4 and line[2] == " "):
            status = line[:2]
            path = line[3:]
        else:
            continue
        rename_paths = (
            _split_porcelain_rename_paths(path)
            if status[:1] in {"R", "C"} or status[1:2] in {"R", "C"}
            else None
        )
        if rename_paths:
            old_path, new_path = rename_paths
            paths.extend(
                [
                    _unquote_porcelain_path(old_path),
                    _unquote_porcelain_path(new_path),
                ]
            )
        else:
            paths.append(_unquote_porcelain_path(path))
    return list(dict.fromkeys(paths))


def _untracked_paths_from_porcelain(status_stdout: str) -> list[str]:
    """Extract untracked paths from ``git status --porcelain`` output."""
    paths: list[str] = []
    for line in status_stdout.splitlines():
        if not line.startswith("?? "):
            continue
        paths.append(_unquote_porcelain_path(line[3:]))
    return list(dict.fromkeys(paths))


@dataclass(frozen=True)
class ValidationWorktreeCheck:
    """Result payload describing whether the validation worktree is clean."""

    clean: bool
    skipped: bool = False
    paths: tuple[str, ...] = ()
    untracked_paths: tuple[str, ...] = ()
    reason_code: str | None = None
    message: str = ""
    command_stderr: str = ""

    @property
    def tracked_paths(self) -> tuple[str, ...]:
        """Return changed tracked paths, excluding any untracked entries."""
        untracked = set(self.untracked_paths)
        return tuple(path for path in self.paths if path not in untracked)

    def details(self) -> dict[str, object]:
        """Serialize check metadata for structured validation evidence."""
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
    """Result payload describing a validation-worktree cleanup attempt."""

    cleaned: bool
    check: ValidationWorktreeCheck
    restore_ref: str | None = None
    reason_code: str | None = None
    message: str = ""
    cleanup_command: str | None = None
    cleanup_stderr: str = ""
    verify_check: ValidationWorktreeCheck | None = None

    @property
    def ok(self) -> bool:
        """Return whether cleanup completed successfully."""
        return self.reason_code is None

    def details(self) -> dict[str, object]:
        """Serialize cleanup metadata for failure reporting and evidence."""
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
    restore_ref: str | None = None,
) -> ValidationWorktreeCleanup:
    """Restore dirty files created by AWF-owned validation commands."""

    async def _verify_head_unchanged(*, restore_ref: str) -> ValidationWorktreeCleanup | None:
        """Verify HEAD still points at the pre-validation reference."""
        restore_target = await run_git(["rev-parse", restore_ref])
        if not restore_target.ok:
            return ValidationWorktreeCleanup(
                cleaned=False,
                check=check,
                restore_ref=restore_ref,
                reason_code=VALIDATION_WORKTREE_STATUS_FAILED,
                message=("Could not verify validation worktree HEAD with `git rev-parse`."),
                cleanup_stderr=(restore_target.stderr or "")[:1000],
            )

        restore_ref_sha, target_message = _resolve_head_sha(
            restore_target,
            ref=restore_ref,
        )
        if restore_ref_sha is None:
            return ValidationWorktreeCleanup(
                cleaned=False,
                check=check,
                restore_ref=restore_ref,
                reason_code=VALIDATION_WORKTREE_STATUS_FAILED,
                message=f"Could not verify validation worktree HEAD: {target_message}",
            )

        current_head = await run_git(["rev-parse", "HEAD"])
        if not current_head.ok:
            return ValidationWorktreeCleanup(
                cleaned=False,
                check=check,
                restore_ref=restore_ref,
                reason_code=VALIDATION_WORKTREE_STATUS_FAILED,
                message=(
                    "Could not verify validation worktree HEAD after cleanup with `git rev-parse`."
                ),
                cleanup_stderr=(current_head.stderr or "")[:1000],
            )

        current_head_sha, current_message = _resolve_head_sha(
            current_head,
            ref="HEAD",
        )
        if current_head_sha is None:
            return ValidationWorktreeCleanup(
                cleaned=False,
                check=check,
                restore_ref=restore_ref,
                reason_code=VALIDATION_WORKTREE_STATUS_FAILED,
                message=(
                    f"Could not verify validation worktree HEAD after cleanup: {current_message}"
                ),
            )

        if current_head_sha != restore_ref_sha:
            rollback = await run_git(["reset", "--hard", restore_ref])
            if not rollback.ok:
                return ValidationWorktreeCleanup(
                    cleaned=False,
                    check=check,
                    restore_ref=restore_ref,
                    reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
                    message=(
                        "AWF validation changed HEAD during execution. "
                        f"Expected {restore_ref_sha[:8]}, found {current_head_sha[:8]}; "
                        "rollback to the validation start ref failed."
                    ),
                    cleanup_command="git reset --hard",
                    cleanup_stderr=(rollback.stderr or "")[:1000],
                )
            return ValidationWorktreeCleanup(
                cleaned=False,
                check=check,
                restore_ref=restore_ref,
                reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
                message=(
                    "AWF validation changed HEAD during execution. "
                    f"Expected {restore_ref_sha[:8]}, found {current_head_sha[:8]}."
                ),
                cleanup_command="git reset --hard",
            )

        return None

    check = await check_validation_worktree_clean(
        run_git=run_git,
        worktree_path=worktree_path,
    )
    if check.skipped:
        return ValidationWorktreeCleanup(cleaned=True, check=check, restore_ref=restore_ref)

    if check.clean:
        if restore_ref is not None:
            head_check = await _verify_head_unchanged(restore_ref=restore_ref)
            if head_check is not None:
                return head_check
        return ValidationWorktreeCleanup(cleaned=True, check=check, restore_ref=restore_ref)
    if check.reason_code == VALIDATION_WORKTREE_STATUS_FAILED:
        return ValidationWorktreeCleanup(
            cleaned=False,
            check=check,
            restore_ref=restore_ref,
            reason_code=VALIDATION_WORKTREE_STATUS_FAILED,
            message=check.message,
        )

    tracked_paths = check.tracked_paths
    if restore_ref is None and tracked_paths:
        return ValidationWorktreeCleanup(
            cleaned=False,
            check=check,
            restore_ref=restore_ref,
            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
            message=(
                "Could not verify validation worktree HEAD after cleanup because "
                "`restore_ref` was not captured before validation."
            ),
        )

    if tracked_paths:
        assert restore_ref is not None
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
        if verify.reason_code == VALIDATION_WORKTREE_STATUS_FAILED:
            return ValidationWorktreeCleanup(
                cleaned=False,
                check=check,
                restore_ref=restore_ref,
                reason_code=verify.reason_code,
                message=verify.message,
                cleanup_command=None,
                verify_check=verify,
            )
        return ValidationWorktreeCleanup(
            cleaned=False,
            check=check,
            restore_ref=restore_ref,
            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
            message="AWF validation worktree cleanup completed but the worktree is still dirty.",
            cleanup_command=None,
            verify_check=verify,
        )

    if restore_ref is not None:
        head_check = await _verify_head_unchanged(restore_ref=restore_ref)
        if head_check is not None:
            return head_check

    return ValidationWorktreeCleanup(
        cleaned=True,
        check=check,
        restore_ref=restore_ref,
        verify_check=verify,
    )


def validation_worktree_preexisting_dirty_message(check: ValidationWorktreeCheck) -> str:
    """Render a structured message for pre-existing dirty worktrees."""
    paths = ", ".join(check.paths) if check.paths else "<unknown>"
    return f"{check.message} Dirty paths: {paths}"


def validation_worktree_cleanup_failure_message(cleanup: ValidationWorktreeCleanup) -> str:
    """Render a structured message for cleanup failures with remaining paths."""
    if cleanup.reason_code == VALIDATION_WORKTREE_STATUS_FAILED:
        return cleanup.message
    if cleanup.verify_check is not None and cleanup.verify_check.paths:
        paths = ", ".join(cleanup.verify_check.paths)
    else:
        paths = ", ".join(cleanup.check.paths) if cleanup.check.paths else "<unknown>"
    return f"{cleanup.message} Dirty paths: {paths}"
