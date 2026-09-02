"""Worktree path IO helpers for correction residue fingerprinting.

Leaf helpers for no-follow / nonblocking opens and entry classification. Kept
separate so ``comment_verdict_residue`` stays under the first-party line budget.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

_SPECIAL_ENTRY_KINDS = frozenset({"fifo", "socket", "char", "block", "other"})
_WORKTREE_REGULAR_OPEN_FLAGS = (
    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
)
_WORKTREE_DIRECTORY_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_WORKTREE_REGULAR_TEXT_READ_LIMIT_BYTES = 4096


@contextlib.contextmanager
def _open_worktree_regular_file(candidate: Path) -> Iterator[BinaryIO]:
    """Open a worktree regular file for byte reads without blocking on TOCTOU swaps.

    ``lstat`` may classify a path as regular moments before another worktree
    process replaces it with a FIFO; pathname-based ``open("rb")`` would then
    block until a writer connects. Open with ``O_NONBLOCK`` and re-validate the
    opened inode via ``fstat`` so swapped special files fail closed instead.
    """
    fd = os.open(candidate, _WORKTREE_REGULAR_OPEN_FLAGS)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EBADF, "worktree path is not a regular file after open")
    except OSError:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as fh:
        yield fh


@contextlib.contextmanager
def _open_worktree_regular_file_at(dir_fd: int, name: str) -> Iterator[BinaryIO]:
    """Open a directory-relative regular file without pathname re-entry."""
    fd = os.open(name, _WORKTREE_REGULAR_OPEN_FLAGS, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EBADF, "worktree entry is not a regular file after open")
    except OSError:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as fh:
        yield fh


def _worktree_proc_path_for_open_fd(dir_fd: int) -> Path | None:
    """Return the ``/proc/self/fd/<fd>`` path for an opened worktree directory fd."""
    try:
        os.fstat(dir_fd)
    except OSError:
        return None
    return Path(f"/proc/self/fd/{dir_fd}")


def _fresh_worktree_path_for_open_fd(dir_fd: int) -> Path | None:
    """Resolve the pinned directory pathname via ``/proc/self/fd/<fd>`` at call time."""
    proc_path = _worktree_proc_path_for_open_fd(dir_fd)
    if proc_path is None:
        return None
    try:
        return proc_path.readlink()
    except OSError:
        return None


def _read_worktree_regular_text(
    candidate: Path,
    *,
    max_bytes: int = _WORKTREE_REGULAR_TEXT_READ_LIMIT_BYTES,
) -> str | None:
    """Read bounded UTF-8 text from a worktree regular file without TOCTOU blocking."""
    try:
        with _open_worktree_regular_file(candidate) as fh:
            payload = fh.read(max_bytes + 1)
    except OSError:
        return None
    if len(payload) > max_bytes:
        return None
    return payload.decode("utf-8", errors="surrogateescape").strip()


def _read_worktree_regular_text_at(
    dir_fd: int,
    name: str,
    *,
    max_bytes: int = _WORKTREE_REGULAR_TEXT_READ_LIMIT_BYTES,
) -> str | None:
    """Read bounded UTF-8 text from a directory-relative regular file."""
    try:
        with _open_worktree_regular_file_at(dir_fd, name) as fh:
            payload = fh.read(max_bytes + 1)
    except OSError:
        return None
    if len(payload) > max_bytes:
        return None
    return payload.decode("utf-8", errors="surrogateescape").strip()


def _worktree_mode_from_kind(*, kind: str, st_mode: int) -> str | None:
    if kind == "symlink":
        return "120000"
    if kind == "regular":
        if stat.S_IMODE(st_mode) & stat.S_IXUSR:
            return "100755"
        return "100644"
    if kind == "directory":
        return "040000"
    if kind in _SPECIAL_ENTRY_KINDS:
        return kind
    return None


def _worktree_directory_entry_mode_token(*, kind: str, st_mode: int) -> str:
    """Return a Git-aligned mode token for directory-entry fingerprint prefixes."""
    if kind in {"regular", "symlink", "directory"}:
        return _worktree_mode_from_kind(kind=kind, st_mode=st_mode) or "<missing>"
    return oct(stat.S_IMODE(st_mode))


def _worktree_entry_kind_from_mode(file_mode: int) -> tuple[str, int]:
    if stat.S_ISLNK(file_mode):
        return "symlink", file_mode
    if stat.S_ISREG(file_mode):
        return "regular", file_mode
    if stat.S_ISDIR(file_mode):
        return "directory", file_mode
    if stat.S_ISFIFO(file_mode):
        return "fifo", file_mode
    if stat.S_ISSOCK(file_mode):
        return "socket", file_mode
    if stat.S_ISCHR(file_mode):
        return "char", file_mode
    if stat.S_ISBLK(file_mode):
        return "block", file_mode
    return "other", file_mode


def _worktree_entry_kind(candidate: Path) -> tuple[str, int] | None:
    """Classify a worktree path via ``lstat`` without opening or following it."""
    try:
        file_mode = candidate.lstat().st_mode
    except OSError:
        return None
    return _worktree_entry_kind_from_mode(file_mode)


def _worktree_entry_kind_at(dir_fd: int, name: str) -> tuple[str, int] | None:
    """Classify a directory entry via ``lstat`` relative to ``dir_fd`` without following it."""
    try:
        file_mode = os.lstat(name, dir_fd=dir_fd).st_mode
    except OSError:
        return None
    return _worktree_entry_kind_from_mode(file_mode)


def _has_nested_git_marker_at(dir_fd: int) -> bool:
    """True when a directory fd contains a real ``.git`` file or directory entry."""
    try:
        marker_mode = os.lstat(".git", dir_fd=dir_fd).st_mode
    except OSError:
        return False
    if stat.S_ISLNK(marker_mode):
        return False
    return stat.S_ISREG(marker_mode) or stat.S_ISDIR(marker_mode)


def _has_nested_git_marker(directory: Path) -> bool:
    """True when ``directory`` contains a real ``.git`` file or directory entry."""
    git_marker = directory / ".git"
    try:
        marker_mode = git_marker.lstat().st_mode
    except OSError:
        return False
    if stat.S_ISLNK(marker_mode):
        return False
    return stat.S_ISREG(marker_mode) or stat.S_ISDIR(marker_mode)


@contextlib.contextmanager
def _open_worktree_directory_path(
    directory: Path,
    *,
    outer_worktree_path: Path,
) -> Iterator[int | None]:
    """Open a contained worktree root without following any path-component symlink.

    Used to retain Git's effective ``core.worktree`` root across nested residue
    probes so pathname replacement after discovery cannot redirect reads
    (PRRT_kwDOSJAM6s6eY3eE). Pathname ``open(..., O_NOFOLLOW)`` only refuses a
    final-component symlink; after containment an agent can still replace an
    intermediate ancestor with a symlink into an external tree
    (PRRT_kwDOSJAM6s6ebFex). Descend from the outer AWF checkout so every
    ancestor is pinned with ``O_NOFOLLOW``. Yields ``None`` when the path
    cannot be opened as a directory inside the outer checkout.
    """
    try:
        relative = directory.resolve().relative_to(outer_worktree_path.resolve())
    except (OSError, ValueError):
        yield None
        return
    if not relative.parts:
        try:
            dir_fd = os.open(outer_worktree_path, _WORKTREE_DIRECTORY_OPEN_FLAGS)
        except OSError:
            yield None
            return
        try:
            if not stat.S_ISDIR(os.fstat(dir_fd).st_mode):
                yield None
                return
            yield dir_fd
        finally:
            os.close(dir_fd)
        return

    # Manual enter/exit so setup OSError yields None once (no double-yield).
    nested_cm = _open_worktree_directory(outer_worktree_path, relative.as_posix())
    try:
        dir_fd = nested_cm.__enter__()
    except OSError:
        yield None
        return
    try:
        yield dir_fd
    finally:
        nested_cm.__exit__(None, None, None)


@contextlib.contextmanager
def _open_worktree_directory(worktree_path: Path, path: str) -> Iterator[int]:
    """Open a worktree directory for no-follow enumeration without TOCTOU symlink swaps.

    ``lstat`` may classify a path as a directory moments before another worktree
    process replaces it with a symlink; pathname-based ``os.scandir(candidate)``
    would then follow the swapped target. Descend with ``O_NOFOLLOW`` and
    re-validate the opened inode via ``fstat`` so symlink swaps fail closed.
    """
    rel_parts = Path(path).parts
    if not rel_parts:
        raise OSError(errno.EINVAL, "worktree path is the directory root", path)
    dir_fd = os.open(worktree_path, _WORKTREE_DIRECTORY_OPEN_FLAGS)
    try:
        for part in rel_parts:
            if part in {".", ".."}:
                raise OSError(errno.EINVAL, "unsafe worktree directory path component", part)
            child_fd = os.open(part, _WORKTREE_DIRECTORY_OPEN_FLAGS, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = child_fd
        if not stat.S_ISDIR(os.fstat(dir_fd).st_mode):
            raise OSError(errno.ENOTDIR, "worktree path is not a directory after open")
    except OSError:
        os.close(dir_fd)
        raise
    try:
        yield dir_fd
    finally:
        os.close(dir_fd)


def _special_entry_blob_sha(*, kind: str, st_mode: int) -> str:
    hasher = hashlib.sha256()
    hasher.update(kind.encode("ascii"))
    hasher.update(b":")
    hasher.update(oct(stat.S_IMODE(st_mode)).encode("ascii"))
    return hasher.hexdigest()
