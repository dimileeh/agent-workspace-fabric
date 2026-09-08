"""Worker cancellation must not undo a timeout preservation in flight (#934).

``handle_agent_run_error`` classifies a watchdog timeout and then spends several
awaits keeping the timed-out agent's work: the dirty sink, the stranded-residue
probe, the preserved-HEAD read, and the provider-recovery record. Every one of
those can be cancelled, and ``asyncio.CancelledError`` is a ``BaseException`` —
it bypasses the preserve handler's own broad handlers and lands on the verdict
protocol's cancellation branch, which rolls the worktree back to this attempt's
``rollback_floor_head``. That rollback deletes exactly the commits and the
salvaged sink commit #932 exists to keep (PRRT_kwDOSJAM6s6fylWD).

The preserve sequence therefore publishes its protected floor — and writes the
item-start marker — before its first await, so the cancellation branch preserves
instead of rewinding. Nothing changes for a provider failure: that rollback is
still correct and still runs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import structlog

from awf.adapters.base import AgentRunError
from awf.common.commands import CommandResult
from awf.db.enums import AgentRuntime
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    item_start_head_state_key,
)
from tests.unit.runtime._verdict_retry_fixtures import _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_PRESERVED_HEAD = "b" * 40
_ITEM_ID = "issue:5558086911"
_TIMEOUT_REASON_CODES = ("AGENT_IDLE_TIMEOUT", "AGENT_TIMEOUT")


def _run_error(reason_code: str) -> AgentRunError:
    return AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(returncode=1, stdout="", stderr="watchdog fired"),
        reason_code=reason_code,
    )


def _runner(tmp_path: Path, reason_code: str) -> _VerdictRunner:
    """An agent that self-commits and dirties the tree, then hits ``reason_code``."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[_run_error(reason_code)],
        heads_after_attempt=[_PRESERVED_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _ITEM_START_HEAD
    return runner


async def _invoke_item(
    runner: _VerdictRunner,
    *,
    state: MonitorState,
) -> comment_verdict.VerdictResult:
    return await comment_verdict._invoke_cli_for_verdict_result(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        prompt="ORIGINAL REVIEW PROMPT",
        commit_message=f"fix: address PR review comment {_ITEM_ID}",
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=_ITEM_START_HEAD,
        evidence_item_id=_ITEM_ID,
    )


@pytest.mark.unit
@pytest.mark.parametrize("reason_code", _TIMEOUT_REASON_CODES)
async def test_cancellation_during_timeout_sink_keeps_the_preserved_work(
    tmp_path: Path,
    reason_code: str,
) -> None:
    """Cancel inside the dirty sink: the timed-out agent's commit survives."""
    runner = _runner(tmp_path, reason_code)

    async def _cancelled_sink(**_kwargs: object) -> bool:
        raise asyncio.CancelledError()

    runner._commit_dirty_worktree = _cancelled_sink
    state = MonitorState()

    with structlog.testing.capture_logs() as captured, pytest.raises(asyncio.CancelledError):
        await _invoke_item(runner, state=state)

    assert runner.reset_targets == []
    assert runner.current_head == _PRESERVED_HEAD
    # The marker is established before the first await, so the re-attempt still
    # anchors its evidence range at the original item start.
    assert state.threads_addressed_ids[item_start_head_state_key(_ITEM_ID)] == _ITEM_START_HEAD
    preserved = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_cancellation_preserved_timeout_work"
    ]
    assert len(preserved) == 1
    assert preserved[0]["reason_code"] == reason_code


@pytest.mark.unit
async def test_cancellation_during_provider_recovery_record_keeps_preserved_work(
    tmp_path: Path,
) -> None:
    """The later preserve awaits are protected too, not just the sink."""
    runner = _runner(tmp_path, "AGENT_TIMEOUT")
    runner.provider_error_action = asyncio.CancelledError()
    state = MonitorState()

    with pytest.raises(asyncio.CancelledError):
        await _invoke_item(runner, state=state)

    assert runner.reset_targets == []
    assert runner.current_head == _PRESERVED_HEAD
    assert state.threads_addressed_ids[item_start_head_state_key(_ITEM_ID)] == _ITEM_START_HEAD


@pytest.mark.unit
async def test_cancellation_during_provider_failure_still_rolls_back(
    tmp_path: Path,
) -> None:
    """Only a preserved timeout is protected; a provider failure still rewinds."""
    runner = _runner(tmp_path, "AGENT_CLI_FAILED")
    runner.provider_error_action = asyncio.CancelledError()
    state = MonitorState()

    with pytest.raises(asyncio.CancelledError):
        await _invoke_item(runner, state=state)

    # Rolled back by the provider-failure branch before the cancellation, and the
    # cancellation branch is still free to roll back again (HEAD is already at the
    # floor, so it has nothing left to rewind).
    assert runner.reset_targets == [_ITEM_START_HEAD]
    assert runner.current_head == _ITEM_START_HEAD
    assert item_start_head_state_key(_ITEM_ID) not in state.threads_addressed_ids
