"""Worktree cleanliness helpers for AWF-owned validation commands."""

from __future__ import annotations

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
    AWF_AGENT_RUNTIME_IGNORED_ROOTS as _AWF_AGENT_RUNTIME_IGNORED_ROOTS,
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
    VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED as _VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED,
)
from awf.runtime.validation_worktree_constants import (
    VALIDATION_WORKTREE_STATUS_FAILED as _VALIDATION_WORKTREE_STATUS_FAILED,
)

VALIDATION_WORKTREE_CLEANUP_FAILED: str = _VALIDATION_WORKTREE_CLEANUP_FAILED
VALIDATION_WORKTREE_PRE_EXISTING_DIRTY: str = _VALIDATION_WORKTREE_PRE_EXISTING_DIRTY
VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED: str = _VALIDATION_WORKTREE_SIDE_EFFECTS_CLEANED
VALIDATION_WORKTREE_STATUS_FAILED: str = _VALIDATION_WORKTREE_STATUS_FAILED
VALIDATION_INFRASTRUCTURE_ERROR: str = _VALIDATION_INFRASTRUCTURE_ERROR
AWF_AGENT_RUNTIME_IGNORED_ROOTS: tuple[str, ...] = _AWF_AGENT_RUNTIME_IGNORED_ROOTS

GitRunner = Callable[[list[str]], Awaitable[CommandResult]]

_GIT_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$", re.IGNORECASE)

# Removing (or restoring) a validation-authored ``.gitignore`` changes the ignore
# rules and can expose previously-ignored untracked files. After the first
# cleanup pass we re-clean those newly exposed paths until the worktree settles.
# A small cap keeps a pathological input (e.g. a `.gitignore` that re-ignores a
# `.gitignore`) from looping forever.
_MAX_CLEANUP_RECLEAN_PASSES = 5


def _first_output_line(stdout: str | None) -> str:
    """Return the first status-free output line from a git command."""
    if not stdout:
        return ""
    return stdout.splitlines()[0].strip()


def _normalize_porcelain_path(path: str) -> str:
    """Normalize porcelain-like paths for tolerant membership comparisons."""
    return path[:-1] if path.endswith("/") else path


def _collapse_descendant_cleanup_paths(paths: list[str]) -> list[str]:
    """Drop cleanup paths already covered by an ancestor cleanup path."""
    collapsed: list[tuple[str, str]] = []
    for path in paths:
        normalized_path = _normalize_porcelain_path(path)
        if not normalized_path:
            continue
        if any(
            normalized_path == existing_normalized_path
            or normalized_path.startswith(f"{existing_normalized_path}/")
            for _existing_path, existing_normalized_path in collapsed
        ):
            continue
        collapsed = [
            (existing_path, existing_normalized_path)
            for existing_path, existing_normalized_path in collapsed
            if not existing_normalized_path.startswith(f"{normalized_path}/")
        ]
        collapsed.append((path, normalized_path))
    return [path for path, _normalized_path in collapsed]


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
    path_is_dir_entry = path.endswith("/")
    for ignored_path in ignored_paths:
        # Normalize the ignored root too: roots may carry a trailing slash
        # (e.g. ``.claude/agent-memory/``), and git collapses a fully-untracked
        # directory to that exact root entry. Comparing normalized-to-normalized
        # matches the root itself as well as its descendants, while keeping the
        # sibling ``.claude/agent-memory-archive/`` excluded.
        normalized_ignored = _normalize_porcelain_path(ignored_path)
        # The exemption is scoped to the ignored *directory* and its descendants.
        # ``git status --untracked-files=all`` reports a regular file named exactly
        # ``.claude/agent-memory`` (no trailing slash) as ``?? .claude/agent-memory``
        # — a distinct path that must stay visible. Only suppress the equality case
        # for an actual directory entry: the incoming path carries a trailing slash
        # (git's collapsed root form / the empty-dir snapshot), or the ignored root
        # itself was stored without one (a genuinely-ignored entry, file or dir alike).
        if normalized_path == normalized_ignored and (
            path_is_dir_entry or not ignored_path.endswith("/")
        ):
            return True
        if normalized_path.startswith(f"{normalized_ignored}/"):
            return True
    return False


def is_under_agent_runtime_root(path: str) -> bool:
    """Return whether ``path`` is an AWF-agent-runtime artifact root/descendant."""
    return _is_under_ignored_path(path, set(AWF_AGENT_RUNTIME_IGNORED_ROOTS))


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


@dataclass(frozen=True)
class ValidationWorktreeCheck:
    """Result payload describing whether the validation worktree is clean."""

    clean: bool
    skipped: bool = False
    paths: tuple[str, ...] = ()
    untracked_paths: tuple[str, ...] = ()
    ignored_paths: tuple[str, ...] = ()
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
    cleaned_paths: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Return whether cleanup completed successfully."""
        return self.reason_code is None

    @property
    def side_effect_paths(self) -> tuple[str, ...]:
        """Return paths that prove validation left worktree side effects."""
        if self.cleaned_paths:
            return self.cleaned_paths
        if self.check.clean:
            return ()
        return tuple(dict.fromkeys((*self.check.paths, *self.check.untracked_paths)))

    def details(self) -> dict[str, object]:
        """Serialize cleanup metadata for failure reporting and evidence."""
        details = self.check.details()
        details["restore_ref"] = self.restore_ref
        if self.cleaned_paths:
            details["cleaned_paths"] = list(self.cleaned_paths)
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
) -> ValidationWorktreeCheck:
    """Return dirty paths before or after an AWF validation command.

    When ``ignore_all_ignored`` is set, everything git currently reports as
    ignored is treated as clean (ignored paths never enter the commit/PR).

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
    ignored_paths_to_ignore = (
        {_normalize_porcelain_path(path) for path in ignored_paths} if ignore_all_ignored else set()
    )
    # AWF-agent-runtime artifacts (reviewer subagent memory) never belong to the
    # PR, so suppress them as untracked/ignored UNCONDITIONALLY — independent of
    # the target repo's .gitignore and of the ``ignore_all_ignored`` flag. Only
    # untracked entries are suppressed below; tracked memory stays visible.
    ignored_paths_to_ignore |= set(AWF_AGENT_RUNTIME_IGNORED_ROOTS)
    changed_paths = _changed_paths_from_porcelain(status_stdout)
    untracked_paths_from_status = _untracked_paths_from_porcelain(
        status_stdout,
        include_ignored=True,
    )
    empty_untracked_dirs = _snapshot_empty_untracked_dirs(
        worktree_path=worktree_path,
        # The snapshot appends its results unfiltered below, so it must skip the
        # AWF-agent-runtime roots itself — an empty ``.claude/agent-memory/<agent>/``
        # (created before any file is written) would otherwise surface the root
        # and its parents as dirty, escaping the unconditional suppression above.
        ignored_paths=(*ignored_paths, *AWF_AGENT_RUNTIME_IGNORED_ROOTS),
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
        )
    return ValidationWorktreeCheck(
        clean=False,
        paths=paths,
        untracked_paths=untracked_paths,
        ignored_paths=ignored_paths,
        reason_code=VALIDATION_WORKTREE_PRE_EXISTING_DIRTY,
        message=(
            "Validation worktree has pre-existing uncommitted changes; "
            "refusing to run AWF-owned validation from a dirty tree."
        ),
    )


# Guard scope (what cleanup acts on):
#   tracked file changed       -> git restore from restore_ref
#   untracked, NOT ignored     -> git clean (delete the side effect)
#   anything git reports as IGNORED (created/modified/deleted) -> LEFT ALONE
# git's live `status --ignored` is the source of truth: ignored paths never
# enter the commit/PR, so AWF mutating them during validation is always safe.
async def cleanup_validation_worktree_side_effects(
    *,
    run_git: GitRunner,
    worktree_path: Path,
    restore_ref: str | None = None,
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
        ignore_all_ignored=True,
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

    # By default the pre-restore ``check`` (run with ``ignore_all_ignored=True``)
    # already gives exactly the untracked, non-ignored side effects. The one case
    # where it can be stale is when the tracked restore above just restored a
    # ``.gitignore`` that validation transiently edited: a path that was
    # un-ignored at check time may be ignored again now. Only then do we recompute
    # the cleanup set from a POST-restore status, so a re-ignored path is excluded
    # from both ``git clean`` and the empty-dir cleanup below (honoring the
    # "never police ignored paths" contract) without paying an extra status call
    # on every cleanup.
    restored_a_gitignore = any(
        path == ".gitignore" or path.endswith("/.gitignore") for path in tracked_paths
    )
    if restored_a_gitignore:
        post_restore_check = await check_validation_worktree_clean(
            run_git=run_git,
            worktree_path=worktree_path,
            ignore_all_ignored=True,
        )
        if post_restore_check.reason_code == VALIDATION_WORKTREE_STATUS_FAILED:
            return await _return_after_head_verification(
                ValidationWorktreeCleanup(
                    cleaned=False,
                    check=check,
                    restore_ref=restore_ref,
                    reason_code=VALIDATION_WORKTREE_STATUS_FAILED,
                    message=post_restore_check.message,
                    # Carry the recheck so its `git status` stderr survives in
                    # `details()` for diagnosis.
                    verify_check=post_restore_check,
                )
            )
        cleanup_source = post_restore_check
    else:
        cleanup_source = check
    cleanup_untracked_paths = _collapse_descendant_cleanup_paths(
        list(cleanup_source.untracked_paths)
    )
    cleaned_paths = tuple(dict.fromkeys((*tracked_paths, *cleanup_untracked_paths)))
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
        # `-ff` (second force) removes nested repositories created by validation.
        # Deliberately NOT `-x`: the cleanup must never delete gitignored files.
        # `git clean` re-evaluates `.gitignore` at clean time (after the tracked
        # restore above), so a path that validation transiently un-ignored by
        # editing a tracked `.gitignore` is left alone once the ignore rules are
        # restored, honoring the "never police ignored paths" contract.
        clean = await run_git(
            ["--literal-pathspecs", "clean", "-ffd", "--", *cleanup_untracked_paths]
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
        # ``cleanup_untracked_paths`` was recomputed from the POST-restore status,
        # so it already excludes anything git now reports as ignored. No ignored
        # parent dir can be implicated, so an empty ignored set is correct here.
        failed_empty_untracked_dirs = _cleanup_empty_untracked_parent_dirs(
            worktree_path=worktree_path,
            cleanup_paths=tuple(cleanup_untracked_paths),
            ignored_paths=set(),
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

        # Removing or restoring a validation-authored ``.gitignore`` can change
        # the ignore rules and expose files that the first pass saw as IGNORED
        # (so they were excluded from the cleanup set). Re-clean those newly
        # exposed untracked, non-ignored paths until the worktree settles. The
        # gate keeps the common case (no ``.gitignore`` touched) byte-for-byte
        # unchanged: no extra status call, no loop.
        ignore_rules_changed = restored_a_gitignore or any(
            path == ".gitignore" or path.endswith("/.gitignore") for path in cleanup_untracked_paths
        )
        if ignore_rules_changed and restore_ref is not None:
            reclean_paths: list[str] = []
            for _pass in range(_MAX_CLEANUP_RECLEAN_PASSES):
                recheck = await check_validation_worktree_clean(
                    run_git=run_git,
                    worktree_path=worktree_path,
                    ignore_all_ignored=True,
                )
                if recheck.reason_code == VALIDATION_WORKTREE_STATUS_FAILED:
                    return await _return_after_head_verification(
                        ValidationWorktreeCleanup(
                            cleaned=False,
                            check=check,
                            restore_ref=restore_ref,
                            reason_code=VALIDATION_WORKTREE_STATUS_FAILED,
                            message=recheck.message,
                            verify_check=recheck,
                        )
                    )
                exposed = _collapse_descendant_cleanup_paths(list(recheck.untracked_paths))
                if not exposed:
                    break
                reclean = await run_git(["--literal-pathspecs", "clean", "-ffd", "--", *exposed])
                if not reclean.ok:
                    return await _return_after_head_verification(
                        ValidationWorktreeCleanup(
                            cleaned=False,
                            check=check,
                            restore_ref=restore_ref,
                            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
                            message=(
                                "AWF validation left untracked files and `git clean` "
                                "could not remove them."
                            ),
                            cleanup_command="git clean",
                            cleanup_stderr=(reclean.stderr or "")[:1000],
                        )
                    )
                # ``exposed`` came from an ``ignore_all_ignored=True`` status, so
                # it never includes ignored paths; an empty ignored set is correct.
                failed_reclean_dirs = _cleanup_empty_untracked_parent_dirs(
                    worktree_path=worktree_path,
                    cleanup_paths=tuple(exposed),
                    ignored_paths=set(),
                )
                if failed_reclean_dirs:
                    return await _return_after_head_verification(
                        ValidationWorktreeCleanup(
                            cleaned=False,
                            check=check,
                            restore_ref=restore_ref,
                            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
                            message=(
                                "AWF validation left empty untracked directories and cleanup "
                                f"could not remove them: {', '.join(failed_reclean_dirs)}"
                            ),
                            cleanup_command="rmdir",
                        )
                    )
                reclean_paths.extend(exposed)
            if reclean_paths:
                cleaned_paths = tuple(
                    dict.fromkeys((*tracked_paths, *cleanup_untracked_paths, *reclean_paths))
                )

    if check.clean:
        head_check = await _verify_head_unchanged(restore_ref=restore_ref)
        if head_check is not None:
            return head_check
        return ValidationWorktreeCleanup(
            cleaned=True,
            check=check,
            restore_ref=restore_ref,
            cleaned_paths=cleaned_paths,
        )

    verify = await check_validation_worktree_clean(
        run_git=run_git,
        worktree_path=worktree_path,
        ignore_all_ignored=True,
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
        cleaned_paths=cleaned_paths,
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
