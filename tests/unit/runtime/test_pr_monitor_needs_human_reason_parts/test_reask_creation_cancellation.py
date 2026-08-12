"""Creation-failure and cancellation coverage for isolated re-ask worktrees."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.runtime.pr_monitor_runner import comments
from awf.runtime.pr_monitor_runner.types import _MonitorPolicyBlockedError
from tests.unit.runtime.test_pr_monitor_needs_human_reason import (
    _git,
    _init_real_worktree,
    _LocalCommandRunner,
)


@pytest.mark.unit
async def test_isolated_reask_worktree_creation_failure_blocks_clarification(
    tmp_path: Path,
) -> None:
    """Do not start a re-ask when Git cannot create its isolated checkout."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_create_failure")
    command_runner = FakeCommandRunner()
    command_runner.queue_result(returncode=0)  # primary-worktree status
    command_runner.queue_result(returncode=1, stderr="worktree add failed")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=command_runner))

    with pytest.raises(_MonitorPolicyBlockedError, match="Could not create an isolated worktree"):
        await comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )

    assert "worktree" in command_runner.calls[1].args
    assert "add" in command_runner.calls[1].args
    assert "--no-checkout" in command_runner.calls[1].args


@pytest.mark.unit
async def test_isolated_reask_worktree_removes_checkout_when_creation_is_cancelled(
    tmp_path: Path,
) -> None:
    """Cancellation after Git creates the checkout cannot strand the re-ask worktree."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_create_cancelled")

    class _CancelAfterWorktreeAddRunner(_LocalCommandRunner):
        """Test double used by the surrounding scenario."""

        async def run(
            self,
            args: list[str],
            *,
            timeout_seconds: float | None = None,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            """Run this test double and record the invocation."""
            result = await super().run(args, timeout_seconds=timeout_seconds, env=env)
            if "worktree" in args and "add" in args:
                raise asyncio.CancelledError
            return result

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_CancelAfterWorktreeAddRunner()))

    with pytest.raises(asyncio.CancelledError):
        await comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )

    assert not list(worktree.parent.glob("*__companion__isolated_reask_*"))


@pytest.mark.unit
async def test_isolated_reask_worktree_removes_checkout_when_filter_metadata_lookup_is_cancelled(
    tmp_path: Path,
) -> None:
    """Cancellation during pinned filter discovery cannot strand the checkout."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_filter_metadata_lookup_cancelled")

    class _CancelDuringFilterMetadataLookupRunner(_LocalCommandRunner):
        """Cancel after the linked worktree is registered and its tree is read."""

        async def run(
            self,
            args: list[str],
            *,
            timeout_seconds: float | None = None,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            """Run setup until pinned filter metadata is queried."""
            result = await super().run(args, timeout_seconds=timeout_seconds, env=env)
            if "ls-tree" in args:
                raise asyncio.CancelledError
            return result

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_CancelDuringFilterMetadataLookupRunner())
    )

    with pytest.raises(asyncio.CancelledError):
        await comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )

    assert not list(worktree.parent.glob("*__companion__isolated_reask_*"))


@pytest.mark.unit
async def test_isolated_reask_worktree_removes_checkout_when_ownership_repair_is_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation during ownership repair cannot strand the checkout."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_ownership_repair_cancelled")
    repair_started = asyncio.Event()
    lock_fds: list[int] = []
    acquire_lock = comments._acquire_isolated_reask_liveness_lock

    async def _repair_agent_runtime_ownership(**_kwargs: object) -> bool:
        """Record the synthetic agent-worktree ownership repair."""
        repair_started.set()
        await asyncio.Event().wait()
        return True

    def _record_lock(path: Path) -> tuple[int, Path]:
        """Record lock for this test."""
        lock_fd, lock_path = acquire_lock(path)
        lock_fds.append(lock_fd)
        return lock_fd, lock_path

    monkeypatch.setattr(comments, "repair_agent_runtime_ownership", _repair_agent_runtime_ownership)
    monkeypatch.setattr(comments, "_acquire_isolated_reask_liveness_lock", _record_lock)
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_LocalCommandRunner()))
    task = asyncio.create_task(
        comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )
    )
    await asyncio.wait_for(repair_started.wait(), timeout=5.0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)

    assert not list(worktree.parent.glob("*__companion__isolated_reask_*"))
    assert lock_fds
    with pytest.raises(OSError):
        comments.os.fstat(lock_fds[0])


@pytest.mark.unit
async def test_isolated_reask_worktree_creation_cleanup_survives_second_cancellation(
    tmp_path: Path,
) -> None:
    """A second shutdown cancel cannot strand a checkout created before cancellation."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_create_second_cancelled")
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    class _CancelAfterWorktreeAddWithBlockingCleanupRunner(_LocalCommandRunner):
        """Test double used by the surrounding scenario."""

        async def run(
            self,
            args: list[str],
            *,
            timeout_seconds: float | None = None,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            """Run this test double and record the invocation."""
            if "worktree" in args and "remove" in args:
                cleanup_started.set()
                await release_cleanup.wait()
                result = await super().run(args, timeout_seconds=timeout_seconds, env=env)
                cleanup_finished.set()
                return result
            result = await super().run(args, timeout_seconds=timeout_seconds, env=env)
            if "worktree" in args and "add" in args:
                raise asyncio.CancelledError
            return result

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_CancelAfterWorktreeAddWithBlockingCleanupRunner())
    )
    task = asyncio.create_task(
        comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )
    )
    await asyncio.wait_for(cleanup_started.wait(), timeout=5.0)
    task.cancel()
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)

    assert cleanup_finished.is_set()
    assert not list(worktree.parent.glob("*__companion__isolated_reask_*"))


@pytest.mark.unit
async def test_isolated_reask_worktree_reports_cleanup_failure_when_creation_is_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed cancellation cleanup is observable without swallowing cancellation."""
    worktree = _init_real_worktree(tmp_path, "ws_reask_create_cancelled_cleanup_failure")
    warnings: list[tuple[str, dict[str, object]]] = []

    class _CancelAfterWorktreeAddWithFailedCleanupRunner(_LocalCommandRunner):
        """Test double used by the surrounding scenario."""

        async def run(
            self,
            args: list[str],
            *,
            timeout_seconds: float | None = None,
            env: dict[str, str] | None = None,
        ) -> CommandResult:
            """Run this test double and record the invocation."""
            if "worktree" in args and "remove" in args:
                return CommandResult(returncode=1, stdout="", stderr="worktree remove failed")
            result = await super().run(args, timeout_seconds=timeout_seconds, env=env)
            if "worktree" in args and "add" in args:
                raise asyncio.CancelledError
            return result

    class _RecordingLogger:
        """Test double used by the surrounding scenario."""

        def warning(self, event_name: str, **kwargs: object) -> None:
            """Capture a warning emitted by the test subject."""
            warnings.append((event_name, kwargs))

    runner = SimpleNamespace(
        _deps=SimpleNamespace(runner=_CancelAfterWorktreeAddWithFailedCleanupRunner())
    )
    monkeypatch.setattr(comments, "_log", _RecordingLogger())

    with pytest.raises(asyncio.CancelledError):
        await comments._create_isolated_reask_worktree(
            runner,
            worktree_path=worktree,
            restore_ref=_git(worktree, "rev-parse", "HEAD").stdout.strip(),
        )

    assert warnings == [
        (
            "monitor.needs_human_reason_reask_isolated_cleanup_failed_after_creation_cancellation",
            {
                "worktree_path": str(worktree),
                "reason_code": "VALIDATION_WORKTREE_CLEANUP_FAILED",
                "message": "`git worktree remove` could not remove the NEEDS_HUMAN reason re-ask checkout",
            },
        )
    ]
