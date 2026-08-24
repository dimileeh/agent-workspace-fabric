"""Commit-sink cleanup regressions for bounded verdict retries (part 3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.adapters.base import AgentRunResult
from awf.common.compose_exec import ComposeExecCleanupError
from awf.runtime.pr_monitor_runner.comment_verdict import AgentVerdictProtocolError
from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError, _MonitorPolicyBlockedError
from tests.unit.runtime._verdict_retry_fixtures import _invoke, _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]


@pytest.mark.unit
async def test_compose_cleanup_policy_blocked_during_commit_sink_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Compose cleanup commit-sink policy block must roll back before propagating."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    sink_commit_head = "b" * 40
    cleanup_error = ComposeExecCleanupError(
        invocation_id="cleanup-failed",
        source="recovery",
        label="agent",
        message="cleanup failed",
    )
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[sink_commit_head],
        dirty_after_attempt=[True],
    )
    runner.current_head = item_start_head

    async def _raise_cleanup(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        raise cleanup_error

    async def _raise_policy_blocked_during_sink(**_kwargs: object) -> bool:
        runner.current_head = sink_commit_head
        raise _MonitorPolicyBlockedError("Supply-chain policy blocked review fix.")

    runner._run_monitor_agent_with_service_recovery = _raise_cleanup
    runner._commit_dirty_worktree = _raise_policy_blocked_during_sink

    with pytest.raises(_MonitorPolicyBlockedError):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_compose_cleanup_protected_scope_diff_during_commit_sink_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Compose cleanup commit-sink protected-scope diff failure must roll back first."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    sink_commit_head = "b" * 40
    cleanup_error = ComposeExecCleanupError(
        invocation_id="cleanup-failed",
        source="recovery",
        label="agent",
        message="cleanup failed",
    )
    diff_exc = ProtectedScopeDiffError("protected-scope diff unavailable")
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[sink_commit_head],
        dirty_after_attempt=[True],
    )
    runner.current_head = item_start_head

    async def _raise_cleanup(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        raise cleanup_error

    async def _raise_protected_scope_diff_during_sink(**_kwargs: object) -> bool:
        runner.current_head = sink_commit_head
        raise diff_exc

    runner._run_monitor_agent_with_service_recovery = _raise_cleanup
    runner._commit_dirty_worktree = _raise_protected_scope_diff_during_sink

    with pytest.raises(ProtectedScopeDiffError) as caught:
        await _invoke(runner)

    assert caught.value is diff_exc
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_unexpected_failure_during_commit_sink_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Untyped commit-sink failures must roll back before propagating."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
    )

    async def _raise_unexpected_during_commit(**_kwargs: object) -> bool:
        runner.current_head = fixed_head
        raise RuntimeError("unexpected commit sink failure")

    runner._commit_dirty_worktree = _raise_unexpected_during_commit

    with pytest.raises(RuntimeError, match="unexpected commit sink failure"):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head


@pytest.mark.unit
async def test_unexpected_failure_during_commit_sink_rollback_failure_is_terminal(
    tmp_path: Path,
) -> None:
    """Failed rollback after an untyped commit-sink error must fail closed."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    fixed_head = "b" * 40
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: addressed review feedback"],
        heads_after_attempt=[fixed_head],
        dirty_after_attempt=[True],
        reset_fails=True,
    )

    async def _raise_unexpected_during_commit(**_kwargs: object) -> bool:
        runner.current_head = fixed_head
        raise RuntimeError("unexpected commit sink failure")

    runner._commit_dirty_worktree = _raise_unexpected_during_commit

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await _invoke(runner)

    assert caught.value.reason_code == "AGENT_VERDICT_PROTOCOL_VIOLATION"
    assert "roll back" in str(caught.value).lower()
    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == fixed_head


@pytest.mark.unit
async def test_compose_cleanup_unexpected_failure_during_commit_sink_rolls_back_before_reraise(
    tmp_path: Path,
) -> None:
    """Untyped compose-cleanup commit-sink failures must not leave unpushed commits."""
    (tmp_path / "ws_protocol").mkdir()
    item_start_head = "a" * 40
    sink_commit_head = "b" * 40
    cleanup_error = ComposeExecCleanupError(
        invocation_id="cleanup-failed",
        source="recovery",
        label="agent",
        message="cleanup failed",
    )
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[sink_commit_head],
        dirty_after_attempt=[True],
    )
    runner.current_head = item_start_head

    async def _raise_cleanup(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        raise cleanup_error

    async def _raise_unexpected_during_sink(**_kwargs: object) -> bool:
        runner.current_head = sink_commit_head
        raise RuntimeError("unexpected compose cleanup commit sink failure")

    runner._run_monitor_agent_with_service_recovery = _raise_cleanup
    runner._commit_dirty_worktree = _raise_unexpected_during_sink

    with pytest.raises(RuntimeError, match="unexpected compose cleanup commit sink failure"):
        await _invoke(runner)

    assert len(runner.prompts) == 1
    assert runner.reset_targets == [item_start_head]
    assert runner.current_head == item_start_head
