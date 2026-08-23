"""Tests for shared worktree writer lock coordination."""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import FakeCommandRunner
from awf.runtime.pr_monitor_runner import remote_repair as pr_remote_repair
from awf.runtime.worktree_writer_lock import (
    exclusive_worktree_writer_lock,
    git_args_mutate_worktree,
    hold_exclusive_worktree_writer_lock,
    worktree_writer_lock_path,
)

_WORKSPACE_ID = "ws_writer_lock_commit_sink"


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


@pytest.mark.asyncio
@pytest.mark.unit
async def test_commit_dirty_worktree_holds_writer_lock_during_staging(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Commit-sink git mutations participate in the recovery writer lock."""
    cmd = FakeCommandRunner()
    dirty = " M src/app.py\n"
    for result in (
        {"returncode": 0, "stdout": dirty},
        {"returncode": 0, "stdout": dirty},
        {"returncode": 0},
        {"returncode": 1},
        {"returncode": 0},
    ):
        cmd.queue_result(**result)

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=cmd),
        _worktrees_root=tmp_path / "worktrees",
    )
    worktree = runner._worktrees_root / _WORKSPACE_ID
    worktree.mkdir(parents=True)

    async def _refresh_policy(**_kwargs: object) -> None:
        return None

    runner._refresh_supply_chain_policy_before_push = _refresh_policy

    monkeypatch.setattr(pr_remote_repair, "mirror_path_for_worktree", lambda _path: None)

    async def _head_exists(_worktree_path: Path) -> bool:
        return True

    monkeypatch.setattr(pr_remote_repair, "verify_head_object_exists", _head_exists)

    async def _repair_ownership(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(pr_remote_repair, "repair_agent_runtime_ownership", _repair_ownership)

    lock_entered = False
    original_lock = hold_exclusive_worktree_writer_lock

    @contextlib.asynccontextmanager
    async def _spy_writer_lock(worktree_path: Path):
        nonlocal lock_entered
        lock_entered = True
        async with original_lock(worktree_path):
            yield

    monkeypatch.setattr(pr_remote_repair, "hold_exclusive_worktree_writer_lock", _spy_writer_lock)

    committed = await pr_remote_repair._commit_dirty_worktree(
        runner,
        workspace_id=_WORKSPACE_ID,
        message="fix: repair",
        task_tag=None,
    )

    assert committed is True
    assert lock_entered is True
    assert any("add" in call.args for call in cmd.calls)
