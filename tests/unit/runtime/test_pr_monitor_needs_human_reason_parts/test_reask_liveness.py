"""Liveness-lock coverage for isolated NEEDS_HUMAN reason re-asks."""

from __future__ import annotations

import fcntl
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult
from awf.common.companions import isolated_reask_worktree_liveness_lock_path
from awf.runtime.pr_monitor_runner import comments
from awf.runtime.pr_monitor_runner.types import _MonitorPolicyBlockedError
from awf.service import gc_reconcile
from tests.unit.runtime.test_pr_monitor_needs_human_reason import (
    _git,
    _init_real_worktree,
    _LocalCommandRunner,
)


@pytest.mark.unit
async def test_isolated_reask_worktree_preserves_dirty_primary_worktree(tmp_path: Path) -> None:
    """A clarification checkout must not turn pre-existing primary-worktree edits into cleanup."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_dirty_primary")
    (worktree / "preexisting.txt").write_text("do not delete\n", encoding="utf-8")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_LocalCommandRunner()))

    with pytest.raises(_MonitorPolicyBlockedError, match="Could not prepare an isolated worktree"):
        await comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )

    assert (worktree / "preexisting.txt").read_text(encoding="utf-8") == "do not delete\n"


@pytest.mark.unit
async def test_isolated_reask_worktree_releases_liveness_lock_when_git_add_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thrown Git add error cannot leave a live-GC marker behind."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_add_raises")
    lock_paths: list[Path] = []
    acquire_lock = comments._acquire_isolated_reask_liveness_lock

    class _GitAddRaisesRunner(_LocalCommandRunner):
        """Test double used by the surrounding scenario."""

        async def run(
            self,
            args: list[str],
            *,
            timeout_seconds: float | None = None,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            """Run this test double and record the invocation."""
            if "worktree" in args and "add" in args:
                raise RuntimeError("worktree add failed")
            return await super().run(args, timeout_seconds=timeout_seconds, env=env)

    def _record_lock(path: Path) -> tuple[int, Path]:
        """Record lock for this test."""
        lock_fd, lock_path = acquire_lock(path)
        lock_paths.append(lock_path)
        return lock_fd, lock_path

    monkeypatch.setattr(comments, "_acquire_isolated_reask_liveness_lock", _record_lock)
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_GitAddRaisesRunner()))

    with pytest.raises(RuntimeError, match="worktree add failed"):
        await comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )

    assert lock_paths
    assert not lock_paths[0].exists()


@pytest.mark.unit
def test_reask_liveness_acquisition_rejects_marker_reaped_before_flock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GC cannot leave a monitor holding an unlinked pre-checkout marker."""
    path = (
        tmp_path
        / "git"
        / "worktrees"
        / "ws_reask_race__companion__isolated_reask_0123456789abcdef0123456789abcdef"
    )
    lock_path = isolated_reask_worktree_liveness_lock_path(path)
    real_flock = fcntl.flock
    reaped_before_monitor_lock = False

    def _race_flock(lock_fd: int, operation: int) -> None:
        nonlocal reaped_before_monitor_lock
        if not reaped_before_monitor_lock and operation == (fcntl.LOCK_EX | fcntl.LOCK_NB):
            reaped_before_monitor_lock = True
            gc_reconcile._reap_stale_pre_checkout_isolated_reask_liveness_locks(tmp_path)
        real_flock(lock_fd, operation)

    monkeypatch.setattr(comments.fcntl, "flock", _race_flock)

    with pytest.raises(FileNotFoundError):
        comments._acquire_isolated_reask_liveness_lock(path)

    assert reaped_before_monitor_lock
    assert not lock_path.exists()


@pytest.mark.unit
def test_reask_liveness_acquisition_preserves_replacement_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed monitor must not unlink a marker that replaced its own."""
    path = (
        tmp_path
        / "git"
        / "worktrees"
        / "ws_reask_race__companion__isolated_reask_0123456789abcdef0123456789abcdef"
    )
    lock_path = isolated_reask_worktree_liveness_lock_path(path)
    real_flock = fcntl.flock
    replacement_created = False

    def _replace_marker_after_lock(lock_fd: int, operation: int) -> None:
        nonlocal replacement_created
        real_flock(lock_fd, operation)
        if not replacement_created and operation == (fcntl.LOCK_EX | fcntl.LOCK_NB):
            replacement_created = True
            lock_path.unlink()
            lock_path.write_text("replacement", encoding="utf-8")

    monkeypatch.setattr(comments.fcntl, "flock", _replace_marker_after_lock)

    with pytest.raises(OSError, match="marker was replaced"):
        comments._acquire_isolated_reask_liveness_lock(path)

    assert replacement_created
    assert lock_path.read_text(encoding="utf-8") == "replacement"
