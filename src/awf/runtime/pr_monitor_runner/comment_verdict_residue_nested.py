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
    """Open a git metadata directory without following symlinks.

    Absolute and parent-escaping gitfile targets are accepted only when the
    resolved metadata directory stays under the outer AWF checkout or this
    worktree's linked bare mirror under ``mirrors/``; opens descend from that
    approved root rather than from ``/`` (PRRT_kwDOSJAM6s6ebFe3,
    PRRT_kwDOSJAM6s6ecze8).
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
    """Return the path from a nested ``.git`` ``commondir`` file, if present.

    Absent or empty ``commondir`` returns ``None`` (caller keeps marker-pin
    behavior). Unreadable or non-regular ``commondir`` raises ``OSError`` so
    callers can fail closed (review 5087582495 / PRRT_kwDOSJAM6s6ebprj).
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
    """Return ``(approved, common_fd)`` for a nested marker ``commondir``.

    ``common_fd`` is an opened approved common-directory descriptor when a
    non-empty ``commondir`` is present; the caller must retain and close it for
    the probe lifetime (PRRT_kwDOSJAM6s6ecAB2). Absent/empty ``commondir``
    returns ``(True, None)``.
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
    """Open a nested ``.git`` directory marker with ``O_NOFOLLOW`` for pinned git-dir probes.

    Yields ``(marker_fd, common_fd)`` when the marker is usable. ``common_fd`` is
    the retained approved common-directory descriptor when ``commondir`` is
    present, or ``None`` when absent/empty (review 5087582495 /
    PRRT_kwDOSJAM6s6ecAB2).
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
    """True when the effective nested worktree root stays inside the AWF checkout."""
    try:
        resolved_probe = probe_root.resolve()
        resolved_outer = worktree_path.resolve()
    except OSError:
        return False
    return resolved_probe.is_relative_to(resolved_outer)


def _module_git_dirs_under(
    git_dir: Path,
    *,
    roots: tuple[Path, ...],
) -> tuple[Path, ...] | None:
    """Return formal submodule git-dirs under ``git_dir/modules`` (fail closed).

    Symlinked ``modules/`` trees or targets that escape ``roots`` return ``None``
    so ``git-meta`` cannot omit nested configs (PRRT_kwDOSJAM6s6e4egX).

    Enumeration streams ``scandir`` entries and shares the residue directory-enum
    entry / depth / deadline budget with nested worktree scans so a wide or deep
    agent-controlled ``modules/`` tree cannot pin unbounded memory or wall time
    (PRRT_kwDOSJAM6s6e5zYG).
    """
    from awf.node.git_manager_ownership import _resolved_git_metadata_within_roots
    from awf.runtime.pr_monitor_runner.comment_verdict_residue_io import (
        _directory_enum_allows_descent,
        _directory_enum_consume_entries,
        _residue_directory_enum_budget,
    )

    found: list[Path] = []

    def _walk_modules(modules_path: Path, *, depth: int) -> bool:
        if not _directory_enum_allows_descent(depth):
            return False
        try:
            mode = modules_path.lstat().st_mode
        except FileNotFoundError:
            return True
        except OSError:
            return False
        if stat.S_ISLNK(mode):
            return False
        if not stat.S_ISDIR(mode):
            return True
        try:
            with os.scandir(modules_path) as entries:
                for entry in entries:
                    if entry.name in {".", ".."}:
                        continue
                    if not _directory_enum_consume_entries(1):
                        return False
                    try:
                        if entry.is_symlink():
                            return False
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        return False
                    if not is_dir:
                        continue
                    child = Path(entry.path)
                    contained = _resolved_git_metadata_within_roots(child, roots)
                    if contained is None:
                        return False
                    found.append(contained)
                    if not _walk_modules(child / "modules", depth=depth + 1):
                        return False
        except OSError:
            return False
        return True

    with _residue_directory_enum_budget():
        if not _walk_modules(git_dir / "modules", depth=0):
            return None
        return tuple(found)


def _nested_worktree_roots_with_git_markers(worktree_path: Path) -> tuple[Path, ...] | None:
    """Return nested checkout roots under ``worktree_path`` that have a ``.git`` marker.

    Bounded by the residue directory-enum budget. Symlink / unreadable walks fail
    closed (PRRT_kwDOSJAM6s6e4egX).
    """
    from awf.runtime.pr_monitor_runner.comment_verdict_residue_io import (
        _directory_enum_allows_descent,
        _has_nested_git_marker_at,
        _residue_directory_enum_budget,
        _sorted_worktree_directory_entry_names,
        _worktree_entry_kind_at,
    )

    found: list[Path] = []

    def _walk(*, dir_fd: int, rel: str, depth: int) -> bool:
        if not _directory_enum_allows_descent(depth):
            return False
        names = _sorted_worktree_directory_entry_names(dir_fd)
        if names is None:
            return False
        for name in names:
            if name == ".git":
                continue
            kind = _worktree_entry_kind_at(dir_fd, name)
            if kind is None:
                return False
            kind_name, _mode = kind
            if kind_name != "directory":
                continue
            try:
                child_fd = os.open(name, _WORKTREE_DIRECTORY_OPEN_FLAGS, dir_fd=dir_fd)
            except OSError:
                return False
            try:
                if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                    return False
                child_rel = f"{rel}/{name}" if rel else name
                if _has_nested_git_marker_at(child_fd):
                    found.append(worktree_path / child_rel)
                if not _walk(dir_fd=child_fd, rel=child_rel, depth=depth + 1):
                    return False
            finally:
                os.close(child_fd)
        return True

    with _residue_directory_enum_budget():
        try:
            root_fd = os.open(worktree_path, _WORKTREE_DIRECTORY_OPEN_FLAGS)
        except OSError:
            return None
        try:
            if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
                return None
            if not _walk(dir_fd=root_fd, rel="", depth=0):
                return None
        finally:
            os.close(root_fd)
    return tuple(found)
