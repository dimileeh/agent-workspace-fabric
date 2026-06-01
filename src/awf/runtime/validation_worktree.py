"""Worktree cleanliness helpers for AWF-owned validation commands."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from awf.common.commands import CommandResult
from awf.runtime.pr_monitor_runner.path_parsing import (
    _changed_paths_from_porcelain as _changed_paths_from_porcelain,
)
from awf.runtime.pr_monitor_runner.path_parsing import (
    _unquote_porcelain_path as _unquote_porcelain_path,
)
from awf.runtime.pr_monitor_runner.path_parsing import (
    _untracked_paths_from_porcelain as _untracked_paths_from_porcelain,
)
from awf.runtime.validation_worktree_constants import (
    VALIDATION_INFRASTRUCTURE_ERROR as _VALIDATION_INFRASTRUCTURE_ERROR,
)
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
VALIDATION_INFRASTRUCTURE_ERROR: str = _VALIDATION_INFRASTRUCTURE_ERROR

GitRunner = Callable[[list[str]], Awaitable[CommandResult]]

_GIT_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$", re.IGNORECASE)


def _first_output_line(stdout: str | None) -> str:
    """Return the first status-free output line from a git command."""
    if not stdout:
        return ""
    return stdout.splitlines()[0].strip()


def _normalize_porcelain_path(path: str) -> str:
    """Normalize porcelain-like paths for tolerant membership comparisons."""
    return path[:-1] if path.endswith("/") else path


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


def _is_under_ignored_path(path: str, ignored_paths: set[str]) -> bool:
    """Return whether `path` should be treated as part of an ignored root."""
    normalized_path = _normalize_porcelain_path(path)
    for ignored_path in ignored_paths:
        if normalized_path == ignored_path:
            return True
        if ignored_path.endswith("/") and normalized_path.startswith(ignored_path):
            return True
        if not ignored_path.endswith("/") and normalized_path.startswith(f"{ignored_path}/"):
            return True
    return False


def _ignored_untracked_snapshot_from_ls_files(
    stdout: str | None,
) -> tuple[str, ...]:
    """Parse a null-delimited `git ls-files` output of ignored untracked paths."""
    if not stdout:
        return ()
    records = tuple(line for line in stdout.split("\0") if line and line != "\x00")
    return tuple(dict.fromkeys(records))


async def _snapshot_ignored_paths(
    run_git: GitRunner,
    *,
    pathspecs: tuple[str, ...] = (),
) -> tuple[tuple[str, ...], str]:
    """Snapshot ignored untracked paths with a null-delimited command."""
    args = ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"]
    if pathspecs:
        args.extend(["--", *pathspecs])
    result = await run_git(args)
    if not result.ok:
        return (), (result.stderr or "")[:1000]
    return _ignored_untracked_snapshot_from_ls_files(result.stdout), ""


@dataclass(frozen=True)
class ValidationWorktreeCheck:
    """Result payload describing whether the validation worktree is clean."""

    clean: bool
    skipped: bool = False
    paths: tuple[str, ...] = ()
    untracked_paths: tuple[str, ...] = ()
    ignored_paths: tuple[str, ...] = ()
    ignored_paths_snapshot: tuple[str, ...] = ()
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
            "ignored_paths": list(self.ignored_paths),
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


def _ignored_paths_from_porcelain(status_stdout: str) -> tuple[str, ...]:
    """Extract ignored pathnames from a porcelain status output."""
    paths: list[str] = []
    for line in status_stdout.splitlines():
        if not line.startswith("!! "):
            continue
        path = line[3:]
        if not path:
            continue
        paths.append(_unquote_porcelain_path(path))
    return tuple(dict.fromkeys(paths))


async def check_validation_worktree_clean(
    *,
    run_git: GitRunner,
    worktree_path: Path,
    ignore_all_ignored: bool = False,
    ignore_ignored_paths: tuple[str, ...] | None = None,
    capture_ignored_paths_snapshot: bool = False,
) -> ValidationWorktreeCheck:
    """Return dirty paths before or after an AWF validation command.

    Unit tests often use plain directories instead of real git worktrees. Real
    AWF worktrees always contain a `.git` control file, so skip the guard only
    for those lightweight test doubles.
    """
    if not (worktree_path / ".git").exists():
        return ValidationWorktreeCheck(clean=True, skipped=True)

    status = await run_git(
        ["status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching"]
    )
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
    ignored_paths = _ignored_paths_from_porcelain(status_stdout)
    ignored_paths_snapshot: tuple[str, ...] = ()
    if capture_ignored_paths_snapshot and ignored_paths:
        if ignore_ignored_paths is None:
            ignore_ignored_paths = ()
        snapshot_paths, snapshot_stderr = await _snapshot_ignored_paths(
            run_git,
            pathspecs=tuple(ignore_ignored_paths or ()),
        )
        if not snapshot_paths and snapshot_stderr:
            return ValidationWorktreeCheck(
                clean=False,
                reason_code=VALIDATION_WORKTREE_STATUS_FAILED,
                message=(
                    "Could not inspect ignored paths for validation pre-check with `git ls-files`."
                ),
                command_stderr=snapshot_stderr,
            )
        ignored_paths_snapshot = snapshot_paths

    if ignore_all_ignored:
        ignored_paths_to_ignore = {_normalize_porcelain_path(path) for path in ignored_paths}
    elif ignore_ignored_paths is None:
        ignored_paths_to_ignore = set()
    else:
        ignored_paths_to_ignore = {_normalize_porcelain_path(path) for path in ignore_ignored_paths}
    paths = tuple(
        path
        for path in _changed_paths_from_porcelain(status_stdout)
        if not _is_under_ignored_path(path, ignored_paths_to_ignore)
    )
    untracked_paths = tuple(
        path
        for path in _untracked_paths_from_porcelain(status_stdout)
        if not _is_under_ignored_path(path, ignored_paths_to_ignore)
    )
    if not paths and not untracked_paths:
        return ValidationWorktreeCheck(
            clean=True,
            ignored_paths=ignored_paths,
            ignored_paths_snapshot=ignored_paths_snapshot,
        )
    return ValidationWorktreeCheck(
        clean=False,
        paths=paths,
        untracked_paths=untracked_paths,
        ignored_paths=ignored_paths,
        ignored_paths_snapshot=ignored_paths_snapshot,
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
    ignore_ignored_paths: tuple[str, ...] | None = None,
    ignore_ignored_paths_snapshot: tuple[str, ...] | None = None,
) -> ValidationWorktreeCleanup:
    """Restore dirty files created by AWF-owned validation commands."""

    async def _verify_head_unchanged(
        *, restore_ref: str | None
    ) -> ValidationWorktreeCleanup | None:
        """Verify HEAD still points at the pre-validation reference."""
        if restore_ref is None:
            return None

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
        ignore_ignored_paths=ignore_ignored_paths,
    )
    if check.skipped:
        return ValidationWorktreeCleanup(cleaned=True, check=check, restore_ref=restore_ref)

    if check.clean:
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

    ignored_paths = {_normalize_porcelain_path(path) for path in (ignore_ignored_paths or ())}
    cleanup_untracked_paths = [
        path
        for path in check.untracked_paths
        if _normalize_porcelain_path(path) not in ignored_paths
    ]
    if ignore_ignored_paths_snapshot is not None and ignored_paths:
        current_ignored_paths, snapshot_stderr = await _snapshot_ignored_paths(
            run_git,
            pathspecs=tuple(ignored_paths),
        )
        if snapshot_stderr:
            return ValidationWorktreeCleanup(
                cleaned=False,
                check=check,
                restore_ref=restore_ref,
                reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
                message=(
                    "Could not inspect ignored paths for validation cleanup with `git ls-files`."
                ),
                cleanup_command="git ls-files",
                cleanup_stderr=snapshot_stderr,
            )
        ignored_snapshot_set = set(ignore_ignored_paths_snapshot)
        cleanup_untracked_paths.extend(
            path
            for path in current_ignored_paths
            if path not in ignored_snapshot_set and _is_under_ignored_path(path, ignored_paths)
        )

    cleanup_untracked_paths = list(dict.fromkeys(cleanup_untracked_paths))
    if cleanup_untracked_paths:
        clean = await run_git(["clean", "-fdx", "--", *cleanup_untracked_paths])
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

    verify = await check_validation_worktree_clean(
        run_git=run_git,
        worktree_path=worktree_path,
        ignore_ignored_paths=ignore_ignored_paths,
    )
    if not verify.clean:
        if verify.reason_code != VALIDATION_WORKTREE_STATUS_FAILED:
            head_check = await _verify_head_unchanged(restore_ref=restore_ref)
            if head_check is not None:
                return head_check
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
