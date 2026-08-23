"""Tests for shared worktree writer lock coordination."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import FakeCommandRunner
from awf.runtime.pr_monitor_runner import remote_repair as pr_remote_repair
from awf.runtime.worktree_writer_lock import (
    _await_thread_join,
    exclusive_worktree_writer_lock,
    git_args_mutate_worktree,
    hold_exclusive_worktree_writer_lock,
    is_worktree_writer_lock_held,
    reap_stale_worktree_writer_locks,
    remove_worktree_writer_lock,
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
def test_remove_worktree_writer_lock_removes_unlocked_file(tmp_path: Path) -> None:
    worktree_path = tmp_path / "ws_reap"
    worktree_path.mkdir()
    with exclusive_worktree_writer_lock(worktree_path):
        lock_path = worktree_writer_lock_path(worktree_path)
        assert lock_path.exists()
    remove_worktree_writer_lock(worktree_path)
    assert not worktree_writer_lock_path(worktree_path).exists()


@pytest.mark.unit
def test_remove_worktree_writer_lock_skips_when_held(tmp_path: Path) -> None:
    worktree_path = tmp_path / "ws_held"
    worktree_path.mkdir()
    with exclusive_worktree_writer_lock(worktree_path):
        remove_worktree_writer_lock(worktree_path)
        assert worktree_writer_lock_path(worktree_path).exists()
        assert is_worktree_writer_lock_held(worktree_path)


@pytest.mark.unit
def test_reap_stale_worktree_writer_locks_skips_existing_worktree(tmp_path: Path) -> None:
    worktrees_dir = tmp_path / "worktrees"
    worktree_path = worktrees_dir / "ws_live"
    worktree_path.mkdir(parents=True)
    with exclusive_worktree_writer_lock(worktree_path):
        pass
    lock_path = worktree_writer_lock_path(worktree_path)
    assert lock_path.exists()
    reap_stale_worktree_writer_locks(worktrees_dir)
    assert lock_path.exists()


@pytest.mark.unit
def test_reap_stale_worktree_writer_locks_removes_orphan_lock(tmp_path: Path) -> None:
    worktrees_dir = tmp_path / "worktrees"
    worktrees_dir.mkdir()
    orphan_worktree = worktrees_dir / "ws_orphan"
    with exclusive_worktree_writer_lock(orphan_worktree):
        pass
    lock_path = worktree_writer_lock_path(orphan_worktree)
    assert lock_path.exists()
    reap_stale_worktree_writer_locks(worktrees_dir)
    assert not lock_path.exists()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (("status", "--porcelain"), False),
        (("rev-parse", "HEAD"), False),
        (("reset", "--hard", "HEAD"), True),
        (("stash", "push"), True),
        (("commit", "-m", "msg"), True),
        (("--literal-pathspecs", "reset", "--hard", "HEAD"), True),
        (("--literal-pathspecs", "clean", "-ffd", "--", "tmp"), True),
        (("--literal-pathspecs", "restore", "--source", "HEAD", "--", "file"), True),
        (("--literal-pathspecs", "status", "--porcelain"), False),
        (("-c", "core.abbrev=12", "status", "--porcelain"), False),
        (("-c", "core.abbrev=12", "reset", "--hard", "HEAD"), True),
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
async def test_hold_exclusive_worktree_writer_lock_cancel_during_acquire_raises(
    tmp_path: Path,
) -> None:
    """Cancellation during a blocked acquire must propagate without busy-spinning."""
    worktree_path = tmp_path / "ws_cancel_raises"
    worktree_path.mkdir()
    hold_event = threading.Event()
    release_holder = threading.Event()

    def hold_sync_lock() -> None:
        with exclusive_worktree_writer_lock(worktree_path):
            hold_event.set()
            release_holder.wait(timeout=5)

    holder = threading.Thread(target=hold_sync_lock)
    holder.start()
    assert hold_event.wait(timeout=5)

    async def blocked_acquire() -> None:
        async with hold_exclusive_worktree_writer_lock(worktree_path):
            pass

    task = asyncio.create_task(blocked_acquire())
    await asyncio.sleep(0.05)
    task.cancel()
    release_holder.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)

    holder.join(timeout=5)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_hold_exclusive_worktree_writer_lock_cancel_during_acquire_releases(
    tmp_path: Path,
) -> None:
    """Cancelled async acquire must not orphan a flock held by the worker thread."""
    worktree_path = tmp_path / "ws_cancel_acquire"
    worktree_path.mkdir()
    hold_event = threading.Event()
    release_holder = threading.Event()

    def hold_sync_lock() -> None:
        with exclusive_worktree_writer_lock(worktree_path):
            hold_event.set()
            release_holder.wait(timeout=5)

    holder = threading.Thread(target=hold_sync_lock)
    holder.start()
    assert hold_event.wait(timeout=5)

    async def blocked_acquire() -> None:
        async with hold_exclusive_worktree_writer_lock(worktree_path):
            pass

    task = asyncio.create_task(blocked_acquire())
    await asyncio.sleep(0.05)
    task.cancel()
    release_holder.set()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    holder.join(timeout=5)

    acquired = threading.Event()

    def verify_acquire() -> None:
        with exclusive_worktree_writer_lock(worktree_path):
            acquired.set()

    verifier = threading.Thread(target=verify_acquire)
    verifier.start()
    verifier.join(timeout=2)
    assert acquired.is_set()


@pytest.mark.asyncio
@pytest.mark.unit
async def test_await_thread_join_absorb_cancellation_uncancels_and_waits() -> None:
    """Absorbing cancellation must uncancel so the join loop can finish."""
    started = threading.Event()
    release = threading.Event()

    def _worker() -> None:
        started.set()
        release.wait(timeout=5)

    thread = threading.Thread(target=_worker)
    thread.start()
    assert started.wait(timeout=5)

    original_to_thread = asyncio.to_thread
    join_calls = 0
    cancelled_once = asyncio.Event()

    async def _to_thread_that_cancels_once(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal join_calls
        if getattr(fn, "__name__", None) == "join":
            join_calls += 1
            if join_calls == 1:
                await original_to_thread(fn, *args, **kwargs)
                cancelled_once.set()
                raise asyncio.CancelledError
        return await original_to_thread(fn, *args, **kwargs)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(asyncio, "to_thread", _to_thread_that_cancels_once)
        join_task = asyncio.create_task(_await_thread_join(thread, absorb_cancellation=True))
        await asyncio.wait_for(cancelled_once.wait(), timeout=1)
        release.set()
        await asyncio.wait_for(join_task, timeout=1)

    thread.join(timeout=5)
    assert not thread.is_alive()
    assert join_calls >= 2


@pytest.mark.asyncio
@pytest.mark.unit
async def test_await_thread_join_reraises_cancel_when_thread_finished() -> None:
    """CancelledError must propagate even if the worker thread already finished."""
    release = threading.Event()

    def _worker() -> None:
        release.wait(timeout=5)

    thread = threading.Thread(target=_worker)
    thread.start()

    original_to_thread = asyncio.to_thread

    async def _to_thread_that_cancels(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        if getattr(fn, "__name__", None) == "join":
            release.set()
            await original_to_thread(fn, *args, **kwargs)
            raise asyncio.CancelledError
        return await original_to_thread(fn, *args, **kwargs)

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(asyncio, "to_thread", _to_thread_that_cancels)
        with pytest.raises(asyncio.CancelledError):
            await _await_thread_join(thread, absorb_cancellation=False)

    thread.join(timeout=5)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_hold_exclusive_worktree_writer_lock_cancel_after_acquire_raises(
    tmp_path: Path,
) -> None:
    """Cancellation after acquire completes must still propagate to the caller."""
    worktree_path = tmp_path / "ws_cancel_after_acquire"
    worktree_path.mkdir()

    original_to_thread = asyncio.to_thread

    async def _to_thread_that_cancels_after_join(fn, *args, **kwargs):  # type: ignore[no-untyped-def]
        result = await original_to_thread(fn, *args, **kwargs)
        if getattr(fn, "__name__", None) == "join":
            raise asyncio.CancelledError
        return result

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(asyncio, "to_thread", _to_thread_that_cancels_after_join)
        with pytest.raises(asyncio.CancelledError):
            async with hold_exclusive_worktree_writer_lock(worktree_path):
                pass


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
