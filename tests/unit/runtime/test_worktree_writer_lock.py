"""Tests for shared worktree writer lock coordination."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from awf.runtime.worktree_writer_lock import (
    exclusive_worktree_writer_lock,
    git_args_mutate_worktree,
    hold_exclusive_worktree_writer_lock,
    worktree_writer_lock_path,
)


@pytest.mark.unit
def test_worktree_writer_lock_path_is_per_worktree(tmp_path: Path) -> None:
    first = tmp_path / "ws_one"
    second = tmp_path / "ws_two"
    assert worktree_writer_lock_path(first) != worktree_writer_lock_path(second)
    assert worktree_writer_lock_path(first).name == "ws_one.lock"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("status", "--porcelain"), False),
        (("rev-parse", "HEAD"), False),
        (("reset", "--hard", "HEAD"), True),
        (("stash", "push"), True),
        (("commit", "-m", "msg"), True),
    ],
)
def test_git_args_mutate_worktree(args: tuple[str, ...], expected: bool) -> None:
    assert git_args_mutate_worktree(args) is expected


@pytest.mark.unit
def test_exclusive_worktree_writer_lock_serializes_threads(tmp_path: Path) -> None:
    worktree_path = tmp_path / "ws_lock"
    worktree_path.mkdir()
    active = 0
    max_active = 0
    lock = threading.Lock()

    def worker() -> None:
        nonlocal active, max_active
        with exclusive_worktree_writer_lock(worktree_path):
            with lock:
                active += 1
                max_active = max(max_active, active)
            threading.Event().wait(0.05)
            with lock:
                active -= 1

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert max_active == 1


@pytest.mark.asyncio
@pytest.mark.unit
async def test_hold_exclusive_worktree_writer_lock_async(tmp_path: Path) -> None:
    worktree_path = tmp_path / "ws_async_lock"
    worktree_path.mkdir()
    async with hold_exclusive_worktree_writer_lock(worktree_path):
        assert worktree_writer_lock_path(worktree_path).exists()
