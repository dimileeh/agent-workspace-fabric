"""Cross-process advisory lock serializing worktree writers with recovery."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar

WORKTREE_WRITER_LOCK_DIR = ".awf-worktree-writer-locks"

_MUTATING_GIT_SUBCOMMANDS = frozenset(
    {
        "add",
        "am",
        "checkout",
        "cherry-pick",
        "clean",
        "commit",
        "merge",
        "pull",
        "rebase",
        "reset",
        "restore",
        "revert",
        "stash",
    }
)

_T = TypeVar("_T")


def worktree_writer_lock_path(worktree_path: Path) -> Path:
    """Return the cross-process writer lock for one AWF-linked worktree."""
    return worktree_path.parent / WORKTREE_WRITER_LOCK_DIR / f"{worktree_path.name}.lock"


def git_args_mutate_worktree(args: tuple[str, ...] | list[str]) -> bool:
    """Return whether a git argv tail mutates tracked or untracked worktree state."""
    if not args:
        return False
    return args[0] in _MUTATING_GIT_SUBCOMMANDS


class _WorktreeWriterLockHandle:
    """Own one open lock file descriptor for a worktree writer lock."""

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._fd: int | None = None

    def acquire(self) -> None:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None


@contextlib.contextmanager
def exclusive_worktree_writer_lock(worktree_path: Path) -> Iterator[None]:
    """Hold the worktree writer lock for one synchronous critical section."""
    handle = _WorktreeWriterLockHandle(worktree_writer_lock_path(worktree_path))
    handle.acquire()
    try:
        yield
    finally:
        handle.release()


@contextlib.asynccontextmanager
async def hold_exclusive_worktree_writer_lock(worktree_path: Path) -> AsyncIterator[None]:
    """Hold the worktree writer lock across an async critical section."""
    handle = _WorktreeWriterLockHandle(worktree_writer_lock_path(worktree_path))
    await asyncio.to_thread(handle.acquire)
    try:
        yield
    finally:
        await asyncio.to_thread(handle.release)


def run_sync_under_worktree_writer_lock[T](
    worktree_path: Path,
    fn: Callable[..., T],
    /,
    *args: Any,
    **kwargs: Any,
) -> T:
    """Run ``fn`` while holding the worktree writer lock."""
    with exclusive_worktree_writer_lock(worktree_path):
        return fn(*args, **kwargs)
