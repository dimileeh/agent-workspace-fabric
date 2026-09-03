"""Nested git metadata helpers for correction residue fingerprinting.

Approved-root discovery and O_NOFOLLOW opens for nested gitfile / marker targets.
Kept separate so ``comment_verdict_residue`` stays under the first-party line budget.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
from collections.abc import Iterator
from pathlib import Path

from awf.runtime.pr_monitor_runner.comment_verdict_residue_io import (
    _WORKTREE_DIRECTORY_OPEN_FLAGS,
    _fresh_worktree_path_for_open_fd,
    _read_worktree_regular_text,
    _read_worktree_regular_text_at,
)


def _nested_git_probe_git_dir(nested_root: Path) -> Path | None:
    """Return the Git metadata directory for a nested embedded repository gitfile."""
    git_marker = nested_root / ".git"
    try:
        marker_mode = git_marker.lstat().st_mode
    except OSError:
        return None
    if stat.S_ISDIR(marker_mode):
        return None
    if not stat.S_ISREG(marker_mode):
        return None
    git_file = _read_worktree_regular_text(git_marker)
    if git_file is None:
        return None
    prefix = "gitdir:"
    if not git_file.startswith(prefix):
        return None
    git_dir = Path(git_file[len(prefix) :].strip())
    if not git_dir.is_absolute():
        git_dir = nested_root / git_dir
    try:
        return git_dir.resolve()
    except OSError:
        return None


def _parse_nested_git_dir_gitfile_at(dir_fd: int) -> Path | None:
    """Return the git-dir path from a nested ``.git`` gitfile without resolving it."""
    try:
        marker_mode = os.lstat(".git", dir_fd=dir_fd).st_mode
    except OSError:
        return None
    if not stat.S_ISREG(marker_mode):
        return None
    git_file = _read_worktree_regular_text_at(dir_fd, ".git")
    if git_file is None:
        return None
    prefix = "gitdir:"
    if not git_file.startswith(prefix):
        return None
    git_dir = Path(git_file[len(prefix) :].strip())
    if not git_dir.parts:
        return None
    return git_dir


def _linked_mirror_name_matches_workspace(name: str, workspace_id: str) -> bool:
    """Return whether linked-worktree metadata name belongs to ``workspace_id``."""
    if name == workspace_id:
        return True
    if not name.startswith(workspace_id):
        return False
    suffix = name.removeprefix(workspace_id)
    return bool(suffix) and suffix.isdigit()


def _linked_mirror_root_for_worktree(outer_worktree_path: Path) -> Path | None:
    """Return this worktree's bare mirror under the expected ``mirrors/`` root.

    Discovers the outer checkout's linked-worktree gitdir and common directory,
    then admits only that repository's mirror — not sibling repos under the
    shared ``mirrors/`` parent (PRRT_kwDOSJAM6s6ecze8).
    """
    try:
        outer = outer_worktree_path.resolve()
    except OSError:
        return None
    expected_mirrors = outer.parent.parent / "mirrors"
    try:
        expected_mirrors_resolved = expected_mirrors.resolve()
    except OSError:
        return None

    git_marker = outer / ".git"
    # Prefer the no-follow / nonblocking helper: agent-writable markers can be
    # swapped to a FIFO after ``lstat``, and ``Path.read_text`` would hang while
    # ``UnicodeDecodeError`` would escape the OSError fail-closed path
    # (review 5088264438).
    git_file = _read_worktree_regular_text(git_marker)
    if git_file is None:
        return None
    prefix = "gitdir:"
    if not git_file.startswith(prefix):
        return None
    linked_git_dir = Path(git_file[len(prefix) :].strip())
    if not linked_git_dir.parts:
        return None
    if not linked_git_dir.is_absolute():
        linked_git_dir = outer / linked_git_dir
    try:
        linked_resolved = linked_git_dir.resolve()
    except OSError:
        return None
    if not linked_resolved.is_relative_to(expected_mirrors_resolved):
        return None
    if linked_resolved.parent.name != "worktrees":
        return None
    if not _linked_mirror_name_matches_workspace(linked_resolved.name, outer.name):
        return None

    bare_from_layout = linked_resolved.parent.parent
    if not bare_from_layout.is_relative_to(
        expected_mirrors_resolved
    ):  # pragma: no cover - layout invariant
        return None
    if bare_from_layout == expected_mirrors_resolved:
        return None

    common_path = bare_from_layout
    commondir_marker = linked_resolved / "commondir"
    raw = _read_worktree_regular_text(commondir_marker)
    if raw:
        common = Path(raw)
        if not common.is_absolute():
            common = linked_resolved / common
        try:
            common_resolved = common.resolve()
        except OSError:
            return None
        if not common_resolved.is_relative_to(expected_mirrors_resolved):
            return None
        if common_resolved == expected_mirrors_resolved:
            return None
        if linked_resolved.parent.resolve() != (common_resolved / "worktrees").resolve():
            return None
        common_path = common_resolved

    return common_path


def _approved_git_metadata_roots(outer_worktree_path: Path) -> tuple[Path, ...]:
    """Return roots that may host nested gitfile metadata for residue probes.

    Nested gitfiles may point at a separate git-dir inside the AWF checkout or at
    this worktree's linked bare mirror under the sibling ``mirrors/`` tree
    (``<worktrees_root>/../mirrors/<repo>.git``). Sibling-repo mirrors,
    cross-workspace checkouts, and host paths are not approved
    (PRRT_kwDOSJAM6s6ebFe3, PRRT_kwDOSJAM6s6ecze8).
    """
    try:
        outer = outer_worktree_path.resolve()
    except OSError:
        return ()
    roots: list[Path] = [outer]
    mirror = _linked_mirror_root_for_worktree(outer)
    if mirror is not None:
        roots.append(mirror)
    return tuple(roots)


def _approved_root_for_git_dir(
    candidate: Path,
    *,
    outer_worktree_path: Path,
) -> Path | None:
    """Return the approved root containing ``candidate``, or ``None``."""
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    for root in _approved_git_metadata_roots(outer_worktree_path):
        try:
            if resolved == root or resolved.is_relative_to(root):
                return root
        except (OSError, ValueError):
            continue
    return None


def _open_git_dir_path_at(
    dir_fd: int,
    git_dir: Path,
    *,
    outer_worktree_path: Path,
) -> int | None:
    """
    Open a Git metadata directory beneath an approved repository root without following symlinks.
    
    Parameters:
        dir_fd (int): File descriptor for the directory containing a relative target.
        git_dir (Path): Absolute or relative path to the Git metadata directory.
        outer_worktree_path (Path): Path to the outer worktree used to determine approved roots.
    
    Returns:
        int | None: An open directory file descriptor, or `None` if the target is outside an approved root or cannot be opened.
    """
    if git_dir.is_absolute():
        candidate = git_dir
    else:
        nested_root = _fresh_worktree_path_for_open_fd(dir_fd)
        if nested_root is None:
            return None
        candidate = nested_root / git_dir

    approved_root = _approved_root_for_git_dir(
        candidate,
        outer_worktree_path=outer_worktree_path,
    )
    if approved_root is None:
        return None
    try:
        relative = candidate.resolve().relative_to(approved_root.resolve())
    except (OSError, ValueError):
        return None

    try:
        current_fd = os.open(approved_root, _WORKTREE_DIRECTORY_OPEN_FLAGS)
    except OSError:
        return None
    try:
        for part in relative.parts:
            if part in {".", ""}:
                continue
            if part == "..":
                os.close(current_fd)
                return None
            next_fd = os.open(part, _WORKTREE_DIRECTORY_OPEN_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        if not stat.S_ISDIR(os.fstat(current_fd).st_mode):
            os.close(current_fd)
            return None
        return current_fd
    except OSError:
        os.close(current_fd)
        return None


@contextlib.contextmanager
def _open_nested_git_dir_gitfile_target_at(
    dir_fd: int,
    *,
    outer_worktree_path: Path,
) -> Iterator[tuple[int, int | None] | None]:
    """Open a nested ``.git`` gitfile target with ``O_NOFOLLOW`` for pinned git-dir probes.

    Yields ``(target_fd, common_fd)`` when the target is usable. ``common_fd`` is
    the retained approved common-directory descriptor when ``commondir`` is
    present on the target, or ``None`` when absent/empty
    (PRRT_kwDOSJAM6s6ecabC).
    """
    git_dir = _parse_nested_git_dir_gitfile_at(dir_fd)
    if git_dir is None:
        yield None
        return
    target_fd = _open_git_dir_path_at(
        dir_fd,
        git_dir,
        outer_worktree_path=outer_worktree_path,
    )
    if target_fd is None:
        yield None
        return
    common_fd: int | None = None
    try:
        if not stat.S_ISDIR(os.fstat(target_fd).st_mode):
            yield None
            return
        approved, common_fd = _try_open_nested_git_marker_commondir_at(
            target_fd,
            outer_worktree_path=outer_worktree_path,
        )
        if not approved:
            yield None
            return
        yield target_fd, common_fd
    finally:
        if common_fd is not None:
            os.close(common_fd)
        os.close(target_fd)


def _parse_nested_git_commondir_at(marker_fd: int) -> Path | None:
    """
    Parse the optional ``commondir`` file for a nested Git metadata directory.
    
    Returns:
        Path | None: The parsed path, or ``None`` when the file is absent or empty.
    
    Raises:
        OSError: If the file is unreadable, cannot be read, or is not a regular file.
    """
    try:
        mode = os.lstat("commondir", dir_fd=marker_fd).st_mode
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OSError(exc.errno, "nested git commondir is unreadable") from exc
    if not stat.S_ISREG(mode):
        raise OSError(errno.EINVAL, "nested git commondir is not a regular file")
    text = _read_worktree_regular_text_at(marker_fd, "commondir")
    if text is None:
        raise OSError(errno.EIO, "nested git commondir could not be read")
    if not text:
        return None
    common = Path(text)
    if not common.parts:  # pragma: no cover - non-empty stripped text always has parts
        return None
    return common


def _try_open_nested_git_marker_commondir_at(
    marker_fd: int,
    *,
    outer_worktree_path: Path,
) -> tuple[bool, int | None]:
    """
    Validate an optional nested Git `commondir` reference and open its approved directory.
    
    Parameters:
        marker_fd (int): File descriptor for the nested Git marker directory.
        outer_worktree_path (Path): Path to the outer worktree used to approve the
            referenced common directory.
    
    Returns:
        tuple[bool, int | None]: A tuple containing the approval status and the
            opened common-directory descriptor, or `None` when no `commondir` is
            present. Invalid or inaccessible metadata returns `(False, None)`.
    """
    try:
        common = _parse_nested_git_commondir_at(marker_fd)
    except OSError:
        return False, None
    if common is None:
        return True, None
    common_fd = _open_git_dir_path_at(
        marker_fd,
        common,
        outer_worktree_path=outer_worktree_path,
    )
    if common_fd is None:
        return False, None
    return True, common_fd


@contextlib.contextmanager
def _open_nested_git_dir_marker_at(
    dir_fd: int,
    *,
    outer_worktree_path: Path,
) -> Iterator[tuple[int, int | None] | None]:
    """
    Open a nested `.git` directory marker and validate its optional `commondir` metadata.
    
    Parameters:
        outer_worktree_path (Path): Path to the outer worktree used to approve the
            optional common Git directory.
    
    Yields:
        tuple[int, int | None] | None: The marker descriptor and optional approved
            common-directory descriptor when usable; `None` when the marker or its
            metadata is invalid.
    """
    try:
        marker_mode = os.lstat(".git", dir_fd=dir_fd).st_mode
    except OSError:
        yield None
        return
    if not stat.S_ISDIR(marker_mode):
        yield None
        return
    # Agent-writable .git can vanish or become a symlink between lstat and open
    # (ENOENT / ELOOP under O_NOFOLLOW). Match _open_git_dir_path_at: fail closed
    # rather than letting OSError escape the generator (PRRT_kwDOSJAM6s6etk6c).
    try:
        marker_fd = os.open(".git", _WORKTREE_DIRECTORY_OPEN_FLAGS, dir_fd=dir_fd)
    except OSError:
        yield None
        return
    common_fd: int | None = None
    try:
        if not stat.S_ISDIR(os.fstat(marker_fd).st_mode):
            yield None
            return
        approved, common_fd = _try_open_nested_git_marker_commondir_at(
            marker_fd,
            outer_worktree_path=outer_worktree_path,
        )
        if not approved:
            yield None
            return
        yield marker_fd, common_fd
    finally:
        if common_fd is not None:
            os.close(common_fd)
        os.close(marker_fd)


def _nested_probe_root_within_outer_worktree(
    *,
    probe_root: Path,
    worktree_path: Path,
) -> bool:
    """
    Determines whether the nested probe root is contained within the outer worktree.
    
    Parameters:
    	probe_root (Path): Root directory used for the nested probe.
    	worktree_path (Path): Outer worktree directory.
    
    Returns:
    	`True` if the resolved probe root is within the resolved outer worktree, `False` otherwise.
    """
    try:
        resolved_probe = probe_root.resolve()
        resolved_outer = worktree_path.resolve()
    except OSError:
        return False
    return resolved_probe.is_relative_to(resolved_outer)
