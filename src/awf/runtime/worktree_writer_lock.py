"""Cross-process advisory lock serializing worktree writers with recovery."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar, cast

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

# Tasks currently holding each async writer lock, keyed by lock-file path. Only
# ``hold_exclusive_worktree_writer_lock`` writes here, and only for the frame
# that actually took the flock: an entry lives exactly as long as the holding
# frame, so it can never outlive its task. See that helper for why a nested
# acquire must not reach ``flock``.
_ASYNC_WRITER_LOCK_OWNERS: dict[str, set[asyncio.Task[Any]]] = {}


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
    """Hold the worktree writer lock across an async critical section.

    Reentrant within a single asyncio task. ``flock`` ownership belongs to the
    open file description, so a nested acquire opens a *second* description and
    blocks against its own holder — a deadlock no timeout clears. The monitor's
    service-recovery loop holds this lock across the whole agent run and calls
    the dirty-worktree sink from inside it, and that sink stages and commits
    under the same lock (PRRT_kwDOSJAM6s6fvw8r). A nested acquire from the task
    that already holds the lock therefore yields without touching the flock: the
    outer frame still owns it, so the critical section stays exclusive against
    every other task, thread and process, and only the outer frame releases it.
    """
    lock_path = worktree_writer_lock_path(worktree_path)
    lock_key = str(lock_path)
    owner = asyncio.current_task()
    if owner is not None and owner in _ASYNC_WRITER_LOCK_OWNERS.get(lock_key, ()):
        yield
        return
    handle = _WorktreeWriterLockHandle(lock_path)
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
        if owner is not None:
            _ASYNC_WRITER_LOCK_OWNERS.setdefault(lock_key, set()).add(owner)
        yield
    finally:
        if acquired:
            if owner is not None:
                owners = _ASYNC_WRITER_LOCK_OWNERS.get(lock_key)
                if owners is not None:
                    owners.discard(owner)
                    if not owners:
                        del _ASYNC_WRITER_LOCK_OWNERS[lock_key]
            await _release_worktree_writer_lock_after_cancellation(handle)


@contextlib.contextmanager
def worktree_writer_locks_borrowed_from(owner: asyncio.Task[Any] | None) -> Iterator[None]:
    """Let this task reuse the writer locks ``owner`` already holds.

    ``hold_exclusive_worktree_writer_lock`` keys its reentrancy on the *task* that
    took the flock, so a helper task the holder spawns — a salvage sequence run to
    completion under ``asyncio.shield``, say — would open a second file description
    and deadlock against its own parent (PRRT_kwDOSJAM6s6f3oxD). Such a helper is
    awaited to completion inside the holding frame, so lending it the ownership for
    its duration keeps the section exclusive against every other task, thread and
    process while staying reentrant. Deleting an emptied entry stays the holding
    frame's job: the loan is always returned before that frame releases.
    """
    current = cast("asyncio.Task[Any]", asyncio.current_task())
    borrowed = tuple(key for key, owners in _ASYNC_WRITER_LOCK_OWNERS.items() if owner in owners)
    for key in borrowed:
        _ASYNC_WRITER_LOCK_OWNERS[key].add(current)
    try:
        yield
    finally:
        for key in borrowed:
            _ASYNC_WRITER_LOCK_OWNERS.get(key, set()).discard(current)


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
