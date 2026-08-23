"""Cross-process advisory lock serializing worktree writers with recovery."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import threading
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


def _git_subcommand_from_args(args: tuple[str, ...] | list[str]) -> str | None:
    """Return the first git subcommand token, skipping leading global options."""
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            return args[index + 1] if index + 1 < len(args) else None
        if token.startswith("--"):
            name, _, inline_value = token.partition("=")
            if inline_value:
                index += 1
                continue
            if name in {
                "--exec-path",
                "--git-dir",
                "--work-tree",
                "--namespace",
                "--attr-source",
                "--config-env",
            }:
                index += 2
                continue
            index += 1
            continue
        if token.startswith("-") and len(token) > 1:
            if token in {"-C", "-c"}:
                index += 2
                continue
            index += 1
            continue
        return token
    return None


def git_args_mutate_worktree(args: tuple[str, ...] | list[str]) -> bool:
    """Return whether a git argv tail mutates tracked or untracked worktree state."""
    subcommand = _git_subcommand_from_args(args)
    if subcommand is None:
        return False
    return subcommand in _MUTATING_GIT_SUBCOMMANDS


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


async def _await_thread_join_after_cancellation(thread: threading.Thread) -> None:
    """Join a worker thread to completion even if the caller is cancelled."""
    while thread.is_alive():
        try:
            await asyncio.to_thread(thread.join, 0.05)
        except asyncio.CancelledError:
            if not thread.is_alive():
                return


async def _finish_worktree_writer_lock_acquire_after_cancellation(
    acquire_thread: threading.Thread,
    handle: _WorktreeWriterLockHandle,
) -> None:
    """Join a cancelled acquire thread and release if it acquired the flock."""
    await _await_thread_join_after_cancellation(acquire_thread)
    if handle._fd is None:
        return
    release_thread = threading.Thread(
        target=handle.release,
        name="awf-worktree-writer-lock-release-after-cancel",
    )
    release_thread.start()
    await _await_thread_join_after_cancellation(release_thread)


async def _release_worktree_writer_lock_after_cancellation(
    handle: _WorktreeWriterLockHandle,
) -> None:
    """Release a held writer lock even if the caller is cancelled."""
    release_thread = threading.Thread(
        target=handle.release,
        name="awf-worktree-writer-lock-release",
    )
    release_thread.start()
    await _await_thread_join_after_cancellation(release_thread)


@contextlib.asynccontextmanager
async def hold_exclusive_worktree_writer_lock(worktree_path: Path) -> AsyncIterator[None]:
    """Hold the worktree writer lock across an async critical section."""
    handle = _WorktreeWriterLockHandle(worktree_writer_lock_path(worktree_path))
    acquire_error: BaseException | None = None

    def _run_acquire() -> None:
        nonlocal acquire_error
        try:
            handle.acquire()
        except BaseException as exc:
            acquire_error = exc

    acquire_thread = threading.Thread(
        target=_run_acquire,
        name=f"awf-worktree-writer-lock-acquire-{worktree_path.name}",
    )
    acquire_thread.start()
    acquired = False
    try:
        try:
            await _await_thread_join_after_cancellation(acquire_thread)
            if acquire_error is not None:
                raise acquire_error
            acquired = True
        except asyncio.CancelledError:
            await _finish_worktree_writer_lock_acquire_after_cancellation(acquire_thread, handle)
            raise
        yield
    finally:
        if acquired:
            await _release_worktree_writer_lock_after_cancellation(handle)


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
