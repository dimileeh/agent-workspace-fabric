"""Worktree cleanliness helpers for AWF-owned validation commands."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from awf.common.commands import CommandResult
from awf.runtime.git_porcelain import (
    changed_paths_from_porcelain as _changed_paths_from_porcelain,
)
from awf.runtime.git_porcelain import unquote_porcelain_path as _unquote_porcelain_path
from awf.runtime.git_porcelain import (
    untracked_paths_from_porcelain as _untracked_paths_from_porcelain,
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

_IgnoredPathSignature = tuple[str, str]
_IGNORED_DIRECTORY_SIGNATURE = "directory"
# Keep small ignored snapshots content-sensitive without letting dependency
# caches dominate validation startup.
_DEFAULT_IGNORED_SNAPSHOT_CONTENT_HASH_BYTES = 16 * 1024 * 1024


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


def _matching_ignored_root(path: str, ignored_paths: set[str]) -> str | None:
    """Return the most specific ignored root containing a path."""
    normalized_path = _normalize_porcelain_path(path)
    matches: list[str] = []
    for ignored_path in ignored_paths:
        normalized_ignored_path = _normalize_porcelain_path(ignored_path)
        if not normalized_ignored_path:
            continue
        if normalized_path == normalized_ignored_path or normalized_path.startswith(
            f"{normalized_ignored_path}/"
        ):
            matches.append(normalized_ignored_path)
    if not matches:
        return None
    return max(matches, key=len)


def _ignored_cleanup_parent_dirs(path: str, ignored_paths: set[str]) -> tuple[str, ...]:
    """Return parent dirs below the ignored root that may be empty after cleanup."""
    ignored_root = _matching_ignored_root(path, ignored_paths)
    if ignored_root is None:
        return ()

    parent = PurePosixPath(_normalize_porcelain_path(path)).parent
    cleanup_dirs: list[str] = []
    while True:
        parent_text = parent.as_posix()
        if parent_text in {"", "."} or parent_text == ignored_root:
            break
        cleanup_dirs.append(parent_text)
        parent = parent.parent
    return tuple(cleanup_dirs)


def _ignored_cleanup_dirs(path: str, ignored_paths: set[str]) -> tuple[str, ...]:
    """Return an ignored cleanup path plus parents that may be empty directories."""
    normalized_path = _normalize_porcelain_path(path)
    if _matching_ignored_root(normalized_path, ignored_paths) is None:
        return ()
    return (normalized_path, *_ignored_cleanup_parent_dirs(normalized_path, ignored_paths))


def _untracked_cleanup_parent_dirs(path: str, ignored_paths: set[str]) -> tuple[str, ...]:
    """Return non-ignored parent dirs that may be empty after cleanup."""
    normalized_path = _normalize_porcelain_path(path)
    if not normalized_path or _is_under_ignored_path(normalized_path, ignored_paths):
        return ()

    cleanup_dirs: list[str] = []
    parent = (
        PurePosixPath(normalized_path)
        if path.endswith("/")
        else PurePosixPath(normalized_path).parent
    )
    while True:
        parent_text = parent.as_posix()
        if parent_text in {"", "."}:
            break
        cleanup_dirs.append(parent_text)
        parent = parent.parent
    return tuple(cleanup_dirs)


def _is_directory(path: Path) -> bool:
    """Return whether a path is a real directory without following symlinks."""
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


def _snapshot_empty_ignored_dirs(
    *,
    worktree_path: Path,
    ignored_paths: tuple[str, ...],
) -> tuple[str, ...]:
    """Snapshot empty directories below ignored roots because git does not list them."""
    empty_dirs: list[str] = []
    normalized_ignored_paths = tuple(
        dict.fromkeys(
            normalized_path
            for path in ignored_paths
            if (normalized_path := _normalize_porcelain_path(path))
        )
    )
    for ignored_path in normalized_ignored_paths:
        ignored_root = worktree_path / ignored_path
        if not _is_directory(ignored_root):
            continue

        pending_dirs = [ignored_root]
        while pending_dirs:
            directory = pending_dirs.pop()
            try:
                children = tuple(directory.iterdir())
            except OSError:
                continue

            pending_dirs.extend(child for child in children if _is_directory(child))
            if directory == ignored_root or children:
                continue

            try:
                relative_dir = directory.relative_to(worktree_path).as_posix()
            except ValueError:
                continue
            empty_dirs.append(f"{relative_dir}/")

    return tuple(dict.fromkeys(empty_dirs))


def _snapshot_empty_untracked_dirs(
    *,
    worktree_path: Path,
    ignored_paths: tuple[str, ...],
) -> tuple[str, ...]:
    """Snapshot fileless non-ignored directory trees that git status omits."""
    empty_dirs: list[str] = []
    ignored_path_set = {_normalize_porcelain_path(path) for path in ignored_paths}

    def has_file_descendant(directory: Path) -> bool:
        try:
            children = tuple(sorted(directory.iterdir(), key=lambda child: child.name))
        except OSError:
            return True

        has_file = False
        for child in children:
            if child.name == ".git":
                has_file = True
                continue
            if not _is_directory(child):
                has_file = True
                continue
            try:
                relative_child = child.relative_to(worktree_path).as_posix()
            except ValueError:
                has_file = True
                continue
            child_path = f"{relative_child}/"
            if _is_under_ignored_path(child_path, ignored_path_set):
                has_file = True
                continue
            if has_file_descendant(child):
                has_file = True
            else:
                empty_dirs.append(child_path)
        return has_file

    has_file_descendant(worktree_path)
    return tuple(dict.fromkeys(empty_dirs))


def _cleanup_empty_ignored_dirs(
    *,
    worktree_path: Path,
    cleanup_paths: tuple[str, ...],
    ignored_paths: set[str],
    preserve_paths: set[str] | None = None,
) -> tuple[str, ...]:
    """Remove empty generated directories left below preserved ignored roots."""
    preserved_dirs = {
        _normalize_porcelain_path(path) for path in (preserve_paths or set()) if path.endswith("/")
    }
    candidate_dirs = {
        cleanup_dir
        for cleanup_path in cleanup_paths
        for cleanup_dir in _ignored_cleanup_dirs(cleanup_path, ignored_paths)
    }
    ordered_dirs = sorted(
        candidate_dirs,
        key=lambda cleanup_dir: len(PurePosixPath(cleanup_dir).parts),
        reverse=True,
    )
    failed_dirs: list[str] = []
    for cleanup_dir in ordered_dirs:
        if cleanup_dir in preserved_dirs:
            continue
        directory = worktree_path / cleanup_dir
        if not directory.exists() or not directory.is_dir():
            continue
        try:
            next(directory.iterdir())
        except StopIteration:
            pass
        except OSError:
            failed_dirs.append(cleanup_dir)
            continue
        else:
            continue

        try:
            directory.rmdir()
        except FileNotFoundError:
            continue
        except OSError:
            failed_dirs.append(cleanup_dir)
    return tuple(dict.fromkeys(failed_dirs))


def _cleanup_empty_untracked_parent_dirs(
    *,
    worktree_path: Path,
    cleanup_paths: tuple[str, ...],
    ignored_paths: set[str],
) -> tuple[str, ...]:
    """Remove empty generated parent directories left after file cleanup."""
    candidate_dirs = {
        cleanup_dir
        for cleanup_path in cleanup_paths
        for cleanup_dir in _untracked_cleanup_parent_dirs(cleanup_path, ignored_paths)
    }
    ordered_dirs = sorted(
        candidate_dirs,
        key=lambda cleanup_dir: len(PurePosixPath(cleanup_dir).parts),
        reverse=True,
    )
    failed_dirs: list[str] = []
    for cleanup_dir in ordered_dirs:
        directory = worktree_path / cleanup_dir
        if not directory.exists() or not _is_directory(directory):
            continue
        try:
            next(directory.iterdir())
        except StopIteration:
            pass
        except OSError:
            failed_dirs.append(cleanup_dir)
            continue
        else:
            continue

        try:
            directory.rmdir()
        except FileNotFoundError:
            continue
        except OSError:
            failed_dirs.append(cleanup_dir)
    return tuple(dict.fromkeys(failed_dirs))


def _ignored_path_still_reported(
    path: str,
    *,
    current_ignored_status_paths: tuple[str, ...],
    current_ignored_snapshot_paths: set[str],
) -> bool:
    """Return whether a baseline ignored path is still visible after validation."""
    normalized_path = _normalize_porcelain_path(path)
    if any(
        _normalize_porcelain_path(current_path) == normalized_path
        for current_path in current_ignored_status_paths
    ):
        return True
    return any(
        _is_under_ignored_path(snapshot_path, {normalized_path})
        for snapshot_path in current_ignored_snapshot_paths
    )


def _ignored_untracked_snapshot_from_ls_files(
    stdout: str | None,
) -> tuple[str, ...]:
    """Parse a null-delimited `git ls-files` output of ignored untracked paths."""
    if not stdout:
        return ()
    records = tuple(line for line in stdout.split("\0") if line)
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
        return (), (result.stderr or "git ls-files command failed.")[:1000]
    return _ignored_untracked_snapshot_from_ls_files(result.stdout), ""


def _regular_file_metadata_signature(file_stats: os.stat_result) -> str:
    """Return a bounded fallback signature for ignored regular files."""
    return (
        f"metadata:{file_stats.st_mode:o}:{file_stats.st_dev}:"
        f"{file_stats.st_ino}:{file_stats.st_size}:{file_stats.st_mtime_ns}"
    )


def _hash_regular_file_contents(path: Path) -> str:
    """Compute a stable content signature for a regular file."""
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _ignored_path_signature(
    path: Path,
    *,
    remaining_content_hash_bytes: int | None,
) -> tuple[str, int | None]:
    """Build an ignored path signature while honoring the content hash budget."""
    try:
        file_stats = path.lstat()
        if stat.S_ISLNK(file_stats.st_mode):
            return f"symlink:{path.readlink()}", remaining_content_hash_bytes
        if stat.S_ISDIR(file_stats.st_mode):
            return _IGNORED_DIRECTORY_SIGNATURE, remaining_content_hash_bytes
        if not stat.S_ISREG(file_stats.st_mode):
            return (
                f"special:{file_stats.st_mode:o}:{file_stats.st_dev}:"
                f"{file_stats.st_ino}:{file_stats.st_size}:{file_stats.st_mtime_ns}"
            ), remaining_content_hash_bytes
        if (
            remaining_content_hash_bytes is not None
            and file_stats.st_size > remaining_content_hash_bytes
        ):
            return (
                _regular_file_metadata_signature(file_stats),
                remaining_content_hash_bytes,
            )
        signature = _hash_regular_file_contents(path)
        if remaining_content_hash_bytes is None:
            return signature, None
        return signature, remaining_content_hash_bytes - file_stats.st_size
    except OSError:
        return "", remaining_content_hash_bytes


def _hash_file_contents(path: Path) -> str:
    """Compute a stable content signature for an ignored file snapshot entry."""
    signature, _ = _ignored_path_signature(path, remaining_content_hash_bytes=None)
    return signature


def _snapshot_ignored_path_signatures(
    worktree_path: Path,
    snapshot_paths: tuple[str, ...],
    *,
    max_content_hash_bytes: int | None = _DEFAULT_IGNORED_SNAPSHOT_CONTENT_HASH_BYTES,
) -> tuple[_IgnoredPathSignature, ...]:
    """Build per-path digests for ignored file snapshot entries."""
    if not snapshot_paths:
        return ()
    remaining_content_hash_bytes = (
        None if max_content_hash_bytes is None else max(0, max_content_hash_bytes)
    )
    signatures: list[_IgnoredPathSignature] = []
    for path in snapshot_paths:
        signature, remaining_content_hash_bytes = _ignored_path_signature(
            worktree_path / path,
            remaining_content_hash_bytes=remaining_content_hash_bytes,
        )
        signatures.append((path, signature))
    return tuple(signatures)


def _ignored_signature_lookup_by_normalized_path(
    signatures: tuple[_IgnoredPathSignature, ...],
) -> dict[str, _IgnoredPathSignature]:
    """Map ignored signatures by slash-tolerant path while preserving display paths."""
    lookup: dict[str, _IgnoredPathSignature] = {}
    for path, signature in signatures:
        lookup.setdefault(_normalize_porcelain_path(path), (path, signature))
    return lookup


@dataclass(frozen=True)
class ValidationWorktreeCheck:
    """Result payload describing whether the validation worktree is clean."""

    clean: bool
    skipped: bool = False
    paths: tuple[str, ...] = ()
    untracked_paths: tuple[str, ...] = ()
    ignored_paths: tuple[str, ...] = ()
    ignored_paths_snapshot: tuple[str, ...] = ()
    ignored_paths_snapshot_signatures: tuple[_IgnoredPathSignature, ...] = ()
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
        if self.verify_check is not None:
            if self.verify_check.reason_code is not None:
                details["verify_reason_code"] = self.verify_check.reason_code
            if self.verify_check.command_stderr:
                details["verify_command_stderr"] = self.verify_check.command_stderr
            if self.verify_check.paths:
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
    ignored_paths_snapshot_signatures: tuple[_IgnoredPathSignature, ...] = ()
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
        empty_ignored_dirs = _snapshot_empty_ignored_dirs(
            worktree_path=worktree_path,
            ignored_paths=tuple(ignore_ignored_paths or ignored_paths),
        )
        ignored_paths_snapshot = tuple(dict.fromkeys((*snapshot_paths, *empty_ignored_dirs)))
        ignored_paths_snapshot_signatures = _snapshot_ignored_path_signatures(
            worktree_path=worktree_path,
            snapshot_paths=ignored_paths_snapshot,
        )

    if ignore_all_ignored:
        ignored_paths_to_ignore = {_normalize_porcelain_path(path) for path in ignored_paths}
    elif ignore_ignored_paths is None:
        ignored_paths_to_ignore = set()
    else:
        ignored_paths_to_ignore = {_normalize_porcelain_path(path) for path in ignore_ignored_paths}
    changed_paths = _changed_paths_from_porcelain(status_stdout)
    untracked_paths_from_status = _untracked_paths_from_porcelain(
        status_stdout,
        include_ignored=True,
    )
    empty_untracked_dirs = _snapshot_empty_untracked_dirs(
        worktree_path=worktree_path,
        ignored_paths=ignored_paths,
    )
    # Ignored roots only suppress untracked or ignored artifacts; tracked files
    # below those roots must stay visible so cleanup can restore them.
    ignored_untracked_paths = {
        path
        for path in untracked_paths_from_status
        if _is_under_ignored_path(path, ignored_paths_to_ignore)
    }
    paths = tuple(
        dict.fromkeys(
            (
                *(path for path in changed_paths if path not in ignored_untracked_paths),
                *empty_untracked_dirs,
            )
        )
    )
    untracked_paths = tuple(
        dict.fromkeys(
            (
                *(
                    path
                    for path in untracked_paths_from_status
                    if path not in ignored_untracked_paths
                ),
                *empty_untracked_dirs,
            )
        )
    )
    if not paths and not untracked_paths:
        return ValidationWorktreeCheck(
            clean=True,
            ignored_paths=ignored_paths,
            ignored_paths_snapshot=ignored_paths_snapshot,
            ignored_paths_snapshot_signatures=ignored_paths_snapshot_signatures,
        )
    return ValidationWorktreeCheck(
        clean=False,
        paths=paths,
        untracked_paths=untracked_paths,
        ignored_paths=ignored_paths,
        ignored_paths_snapshot=ignored_paths_snapshot,
        ignored_paths_snapshot_signatures=ignored_paths_snapshot_signatures,
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
    ignore_ignored_paths_snapshot_signatures: tuple[_IgnoredPathSignature, ...] | None = None,
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

    async def _return_after_head_verification(
        failure: ValidationWorktreeCleanup,
    ) -> ValidationWorktreeCleanup:
        head_check = await _verify_head_unchanged(restore_ref=restore_ref)
        if head_check is not None:
            return head_check
        return failure

    if check.reason_code == VALIDATION_WORKTREE_STATUS_FAILED:
        return await _return_after_head_verification(
            ValidationWorktreeCleanup(
                cleaned=False,
                check=check,
                restore_ref=restore_ref,
                reason_code=VALIDATION_WORKTREE_STATUS_FAILED,
                message=check.message,
            )
        )

    tracked_paths = check.tracked_paths
    if restore_ref is None and tracked_paths:
        message = (
            "Could not restore validation worktree because "
            "`restore_ref` was not captured before validation."
        )
        return ValidationWorktreeCleanup(
            cleaned=False,
            check=check,
            restore_ref=restore_ref,
            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
            message=message,
        )

    if tracked_paths:
        assert restore_ref is not None
        restore = await run_git(
            [
                "--literal-pathspecs",
                "restore",
                "--source",
                restore_ref,
                "--staged",
                "--worktree",
                "--",
                *tracked_paths,
            ]
        )
        if not restore.ok:
            return await _return_after_head_verification(
                ValidationWorktreeCleanup(
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
            )

    ignored_paths = {_normalize_porcelain_path(path) for path in (ignore_ignored_paths or ())}
    ignored_pathspecs = tuple(ignore_ignored_paths or ())
    ignored_signature_lookup_by_normalized_path = _ignored_signature_lookup_by_normalized_path(
        ignore_ignored_paths_snapshot_signatures or ()
    )
    ignored_snapshot_set: set[str] = set()
    cleanup_untracked_paths = [
        path
        for path in check.untracked_paths
        if _normalize_porcelain_path(path) not in ignored_paths
    ]
    if ignore_ignored_paths_snapshot is not None and ignored_paths:
        current_ignored_file_paths, snapshot_stderr = await _snapshot_ignored_paths(
            run_git,
            pathspecs=tuple(ignored_pathspecs),
        )
        if snapshot_stderr:
            return await _return_after_head_verification(
                ValidationWorktreeCleanup(
                    cleaned=False,
                    check=check,
                    restore_ref=restore_ref,
                    reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
                    message=(
                        "Could not inspect ignored paths for validation cleanup with "
                        "`git ls-files`."
                    ),
                    cleanup_command=None,
                    cleanup_stderr=snapshot_stderr,
                )
            )
        current_empty_ignored_dirs = _snapshot_empty_ignored_dirs(
            worktree_path=worktree_path,
            ignored_paths=ignored_pathspecs,
        )
        current_ignored_paths = tuple(
            dict.fromkeys((*current_ignored_file_paths, *current_empty_ignored_dirs))
        )
        current_ignored_signatures = (
            _snapshot_ignored_path_signatures(
                worktree_path=worktree_path,
                snapshot_paths=current_ignored_paths,
            )
            if ignore_ignored_paths_snapshot_signatures is not None
            else ()
        )
        ignored_snapshot_set = set(ignore_ignored_paths_snapshot)
        ignored_snapshot_normalized_set = {
            _normalize_porcelain_path(path) for path in ignored_snapshot_set
        }
        current_ignored_signature_lookup_by_normalized_path = (
            _ignored_signature_lookup_by_normalized_path(current_ignored_signatures)
        )
        current_ignored_set = set(current_ignored_paths)
        current_ignored_normalized_set = {
            _normalize_porcelain_path(path) for path in current_ignored_set
        }
        deleted_ignored_paths = [
            path
            for path in ignore_ignored_paths_snapshot
            if _normalize_porcelain_path(path) not in current_ignored_normalized_set
            and not (
                path.endswith("/")
                and _is_directory(worktree_path / _normalize_porcelain_path(path))
            )
        ]
        if deleted_ignored_paths:
            return await _return_after_head_verification(
                ValidationWorktreeCleanup(
                    cleaned=False,
                    check=check,
                    restore_ref=restore_ref,
                    reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
                    message=(
                        "AWF validation removed pre-existing ignored files: "
                        f"{', '.join(deleted_ignored_paths)}"
                    ),
                ),
            )
        deleted_ignored_roots = [
            path
            for path in ignored_pathspecs
            if not _ignored_path_still_reported(
                path,
                current_ignored_status_paths=check.ignored_paths,
                current_ignored_snapshot_paths=current_ignored_set,
            )
        ]
        if deleted_ignored_roots:
            return await _return_after_head_verification(
                ValidationWorktreeCleanup(
                    cleaned=False,
                    check=check,
                    restore_ref=restore_ref,
                    reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
                    message=(
                        "AWF validation removed pre-existing ignored roots: "
                        f"{', '.join(deleted_ignored_roots)}"
                    ),
                ),
            )
        modified_snapshot_paths = []
        for path in current_ignored_paths:
            if not _is_under_ignored_path(path, ignored_paths):
                continue
            normalized_path = _normalize_porcelain_path(path)
            if normalized_path not in ignored_snapshot_normalized_set:
                continue
            if ignore_ignored_paths_snapshot_signatures is None:
                continue
            baseline_path, baseline_signature = ignored_signature_lookup_by_normalized_path.get(
                normalized_path,
                (path, ""),
            )
            _current_path, current_signature = (
                current_ignored_signature_lookup_by_normalized_path.get(
                    normalized_path,
                    (path, ""),
                )
            )
            if current_signature != baseline_signature:
                modified_snapshot_paths.append(baseline_path)
        if modified_snapshot_paths:
            return await _return_after_head_verification(
                ValidationWorktreeCleanup(
                    cleaned=False,
                    check=check,
                    restore_ref=restore_ref,
                    reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
                    message=(
                        "AWF validation modified pre-existing ignored files and they "
                        f"cannot be safely restored: {', '.join(modified_snapshot_paths)}"
                    ),
                ),
            )
        cleanup_untracked_paths.extend(
            _normalize_porcelain_path(path)
            for path in current_ignored_paths
            if _is_under_ignored_path(path, ignored_paths)
            and _normalize_porcelain_path(path) not in ignored_snapshot_normalized_set
        )

    cleanup_untracked_paths = list(dict.fromkeys(cleanup_untracked_paths))
    if restore_ref is None and cleanup_untracked_paths:
        return ValidationWorktreeCleanup(
            cleaned=False,
            check=check,
            restore_ref=restore_ref,
            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
            message=(
                "Could not restore validation worktree because "
                "`restore_ref` was not captured before validation."
            ),
        )
    if cleanup_untracked_paths:
        clean = await run_git(
            ["--literal-pathspecs", "clean", "-fdx", "--", *cleanup_untracked_paths]
        )
        if not clean.ok:
            return await _return_after_head_verification(
                ValidationWorktreeCleanup(
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
            )
        failed_empty_untracked_dirs = _cleanup_empty_untracked_parent_dirs(
            worktree_path=worktree_path,
            cleanup_paths=tuple(cleanup_untracked_paths),
            ignored_paths=ignored_paths,
        )
        if failed_empty_untracked_dirs:
            return await _return_after_head_verification(
                ValidationWorktreeCleanup(
                    cleaned=False,
                    check=check,
                    restore_ref=restore_ref,
                    reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
                    message=(
                        "AWF validation left empty untracked directories and cleanup could not "
                        f"remove them: {', '.join(failed_empty_untracked_dirs)}"
                    ),
                    cleanup_command="rmdir",
                )
            )
        failed_empty_ignored_dirs = _cleanup_empty_ignored_dirs(
            worktree_path=worktree_path,
            cleanup_paths=tuple(cleanup_untracked_paths),
            ignored_paths=ignored_paths,
            preserve_paths=ignored_snapshot_set,
        )
        if failed_empty_ignored_dirs:
            return await _return_after_head_verification(
                ValidationWorktreeCleanup(
                    cleaned=False,
                    check=check,
                    restore_ref=restore_ref,
                    reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
                    message=(
                        "AWF validation left empty ignored directories and cleanup could not "
                        f"remove them: {', '.join(failed_empty_ignored_dirs)}"
                    ),
                    cleanup_command="rmdir",
                )
            )

    if check.clean:
        head_check = await _verify_head_unchanged(restore_ref=restore_ref)
        if head_check is not None:
            return head_check
        return ValidationWorktreeCleanup(cleaned=True, check=check, restore_ref=restore_ref)

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
            return await _return_after_head_verification(
                ValidationWorktreeCleanup(
                    cleaned=False,
                    check=check,
                    restore_ref=restore_ref,
                    reason_code=verify.reason_code,
                    message=verify.message,
                    cleanup_command=None,
                    verify_check=verify,
                )
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
