"""Cancellation inside the adapter's *post-timeout cleanup* must keep the work (#934).

The adapter tears the tracked compose-exec stack down as soon as the watchdog
classifies a run as ``AGENT_TIMEOUT``/``AGENT_IDLE_TIMEOUT``, and it does that
*before* raising the ``AgentRunError`` the #932 preserve handler acts on. That
teardown awaits, so worker cancellation lands there with the timeout already
known and nothing published: neither protection channel is populated, and the
verdict protocol's cancellation branch rewinds to ``rollback_floor_head``,
deleting the timed-out agent's commits and edits.

The adapter therefore tags the escaping ``CancelledError`` with the watchdog
reason code, exactly as it tags a cleanup *failure*, and the cancellation branch
reads that tag as "the floor is protected — do not rewind"
(PRRT_kwDOSJAM6s6f0n6B).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import structlog

from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_verdict
from tests.unit.runtime._verdict_retry_fixtures import _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_TIMED_OUT_RUN_HEAD = "b" * 40
_ITEM_ID = "issue:5558086911"


def _cancelled_error(agent_reason_code: str | None) -> asyncio.CancelledError:
    """A worker cancellation as the adapter's timeout-cleanup await re-raises it."""
    exc = asyncio.CancelledError()
    if agent_reason_code is not None:
        exc.agent_reason_code = agent_reason_code  # type: ignore[attr-defined]
    return exc


def _runner(tmp_path: Path, *, agent_reason_code: str | None) -> _VerdictRunner:
    """An agent that self-commits and is then cancelled inside the adapter."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[_TIMED_OUT_RUN_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _ITEM_START_HEAD
    exc = _cancelled_error(agent_reason_code)

    async def _run(**kwargs: object) -> None:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = _TIMED_OUT_RUN_HEAD
        raise exc

    runner._run_monitor_agent_with_service_recovery = _run
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
@pytest.mark.parametrize("reason_code", ["AGENT_IDLE_TIMEOUT", "AGENT_TIMEOUT"])
async def test_cancellation_tagged_with_a_timeout_keeps_the_timed_out_work(
    tmp_path: Path,
    reason_code: str,
) -> None:
    """A timeout-tagged cancellation is protected: the run's commit survives."""
    runner = _runner(tmp_path, agent_reason_code=reason_code)
    state = MonitorState()

    with structlog.testing.capture_logs() as captured, pytest.raises(asyncio.CancelledError):
        await _invoke_item(runner, state=state)

    assert runner.reset_targets == []
    assert runner.current_head == _TIMED_OUT_RUN_HEAD
    preserved = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_cancellation_preserved_timeout_work"
    ]
    assert len(preserved) == 1
    assert preserved[0]["reason_code"] == reason_code


@pytest.mark.unit
@pytest.mark.parametrize("agent_reason_code", [None, "AGENT_CLI_FAILED"])
async def test_untagged_cancellation_still_rolls_back(
    tmp_path: Path,
    agent_reason_code: str | None,
) -> None:
    """Only a masked *timeout* is protected; every other cancellation rewinds."""
    runner = _runner(tmp_path, agent_reason_code=agent_reason_code)
    state = MonitorState()

    with pytest.raises(asyncio.CancelledError):
        await _invoke_item(runner, state=state)

    assert runner.reset_targets == [_ITEM_START_HEAD]
    assert runner.current_head == _ITEM_START_HEAD
