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


def _worktree_writer_lock_gate_path(lock_path: Path) -> Path:
    """Return the stable gate that coordinates writer acquisition and cleanup."""
    return lock_path.with_name(f"{lock_path.name}.gate")


def is_worktree_writer_lock_held(worktree_path: Path) -> bool:
    """Return whether another process holds the worktree writer lock."""
    lock_path = worktree_writer_lock_path(worktree_path)
    try:
        lock_fd = os.open(lock_path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(lock_fd)
    return False


def remove_worktree_writer_lock(worktree_path: Path) -> None:
    """Best-effort cleanup of an unlocked writer lock left after worktree teardown."""
    lock_path = worktree_writer_lock_path(worktree_path)
    gate_path = _worktree_writer_lock_gate_path(lock_path)
    try:
        gate_fd = os.open(str(gate_path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return
    try:
        try:
            fcntl.flock(gate_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        try:
            lock_fd = os.open(lock_path, os.O_RDONLY)
        except OSError:
            return
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return
            with contextlib.suppress(OSError):
                lock_path.unlink()
        finally:
            os.close(lock_fd)
    finally:
        os.close(gate_fd)


def reap_stale_worktree_writer_locks(worktrees_dir: Path) -> None:
    """Remove writer lock files whose worktree checkout no longer exists."""
    lock_dir = worktrees_dir / WORKTREE_WRITER_LOCK_DIR
    try:
        lock_paths = tuple(lock_dir.glob("*.lock"))
    except OSError:
        return
    for lock_path in lock_paths:
        worktree_path = worktrees_dir / lock_path.name.removesuffix(".lock")
        if (
            not lock_path.is_file()
            or lock_path.is_symlink()
            or worktree_path.exists()
            or is_worktree_writer_lock_held(worktree_path)
        ):
            continue
        remove_worktree_writer_lock(worktree_path)


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
        self._gate_fd: int | None = None

    def acquire(self) -> None:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        gate_fd = os.open(
            str(_worktree_writer_lock_gate_path(self._lock_path)),
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        try:
            fcntl.flock(gate_fd, fcntl.LOCK_SH)
        except OSError:
            os.close(gate_fd)
            raise
        try:
            fd = os.open(str(self._lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                os.close(fd)
                raise
        except BaseException:
            os.close(gate_fd)
            raise
        self._fd = fd
        self._gate_fd = gate_fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
        finally:
            if self._gate_fd is not None:
                try:
                    fcntl.flock(self._gate_fd, fcntl.LOCK_UN)
                finally:
                    os.close(self._gate_fd)
                    self._gate_fd = None


@contextlib.contextmanager
def exclusive_worktree_writer_lock(worktree_path: Path) -> Iterator[None]:
    """Hold the worktree writer lock for one synchronous critical section."""
    handle = _WorktreeWriterLockHandle(worktree_writer_lock_path(worktree_path))
    handle.acquire()
    try:
        yield
    finally:
        handle.release()


async def _await_thread_join(
    thread: threading.Thread,
    *,
    absorb_cancellation: bool = False,
) -> None:
    """Wait for a worker thread, optionally absorbing caller cancellation."""
    while thread.is_alive():
        try:
            await asyncio.shield(asyncio.to_thread(thread.join, 0.05))
        except asyncio.CancelledError:
            if not absorb_cancellation:
                raise
            if not thread.is_alive():
                return
            current_task = asyncio.current_task()
            if current_task is not None:
                current_task.uncancel()


async def _await_thread_join_after_cancellation(thread: threading.Thread) -> None:
    """Join a worker thread to completion even if the caller is cancelled."""
    await _await_thread_join(thread, absorb_cancellation=True)


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
            await _await_thread_join(acquire_thread)
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
