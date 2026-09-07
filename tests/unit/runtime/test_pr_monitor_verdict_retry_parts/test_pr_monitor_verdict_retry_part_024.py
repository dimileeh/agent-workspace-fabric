"""A cancellation mid-preservation must not truncate the #932 sequence (#934).

``handle_agent_run_error`` publishes ``timeout_preservation_sink`` before its
first await so the caller's cancellation branch stops rewinding the timed-out
agent's commits. Everything the preserve path still owes — the durable evidence
anchor and the dirty-worktree sink — runs *after* that publish, and both await.
A worker cancellation landing in either one used to escape with the sink already
claiming protection: the branch skipped the rollback (right) and its nested guard
also skipped ``preserve_cancelled_timeout_work``, because that helper only runs
for timeouts *no* handler ever saw. The next pass then found the timed-out edits
still dirty (``PRE_EXISTING_DIRTY_WORKTREE``) or the anchor only in memory, on a
worker that may never persist it (``AGENT_FIXED_WITHOUT_EVIDENCE``).

The already-started sequence therefore finishes under a shield before the
cancellation is delivered onward (PRRT_kwDOSJAM6s6f2gbw).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import structlog

from awf.adapters.base import AgentRunError
from awf.common.commands import CommandResult
from awf.db.enums import AgentRuntime
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner import comment_verdict_timeout_preserve as _preserve
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    item_start_head_state_key,
)
from tests.unit.runtime._verdict_retry_fixtures import _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_TIMED_OUT_RUN_HEAD = "b" * 40
_ITEM_ID = "issue:5558086911"


def _timeout_error() -> AgentRunError:
    return AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(returncode=124, stdout="", stderr="command idle timeout\n"),
        reason_code="AGENT_IDLE_TIMEOUT",
    )


def _runner(tmp_path: Path) -> _VerdictRunner:
    """An agent that times out with uncommitted edits still in the worktree."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[_timeout_error()],
        heads_after_attempt=[_TIMED_OUT_RUN_HEAD],
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
async def test_cancellation_mid_sink_still_commits_the_timed_out_edits(
    tmp_path: Path,
) -> None:
    """The dirty sink finishes; the next pass must not meet a dirty worktree."""
    runner = _runner(tmp_path)
    state = MonitorState()
    sink_started = asyncio.Event()
    release_sink = asyncio.Event()
    sink_finished: list[bool] = []

    async def _slow_commit(**kwargs: object) -> bool:
        del kwargs
        sink_started.set()
        await release_sink.wait()
        runner.current_head = _TIMED_OUT_RUN_HEAD
        sink_finished.append(True)
        return True

    runner._commit_dirty_worktree = _slow_commit
    item = asyncio.ensure_future(_invoke_item(runner, state=state))
    await sink_started.wait()
    item.cancel()
    await asyncio.sleep(0)
    release_sink.set()

    with pytest.raises(asyncio.CancelledError):
        await item

    assert sink_finished == [True]
    assert runner.reset_targets == []
    assert state.threads_addressed_ids[item_start_head_state_key(_ITEM_ID)] == _ITEM_START_HEAD


@pytest.mark.unit
async def test_cancellation_mid_anchor_write_still_reaches_the_sink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel during the durable anchor write may not skip the dirty sink."""
    runner = _runner(tmp_path)
    state = MonitorState()
    anchor_started = asyncio.Event()
    release_anchor = asyncio.Event()
    anchor_finished: list[bool] = []
    sink_calls: list[dict[str, object]] = []
    original_remember = _preserve.remember_item_start_head_durably
    original_commit = runner._commit_dirty_worktree

    async def _slow_remember(*args: Any, **kwargs: Any) -> None:
        anchor_started.set()
        await release_anchor.wait()
        await original_remember(*args, **kwargs)
        anchor_finished.append(True)

    async def _commit(**kwargs: object) -> bool:
        sink_calls.append(kwargs)
        return await original_commit(**kwargs)

    monkeypatch.setattr(_preserve, "remember_item_start_head_durably", _slow_remember)
    runner._commit_dirty_worktree = _commit
    item = asyncio.ensure_future(_invoke_item(runner, state=state))
    await anchor_started.wait()
    item.cancel()
    await asyncio.sleep(0)
    release_anchor.set()

    with pytest.raises(asyncio.CancelledError):
        await item

    assert anchor_finished == [True]
    assert len(sink_calls) == 1
    assert sink_calls[0]["operation_start_head"] == _ITEM_START_HEAD
    assert runner.reset_targets == []
    assert state.threads_addressed_ids[item_start_head_state_key(_ITEM_ID)] == _ITEM_START_HEAD


@pytest.mark.unit
async def test_preservation_failure_racing_the_cancellation_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A step that dies in the cancelling loop step is logged, never swallowed.

    ``asyncio.shield`` retrieves the inner exception itself once its outer future
    has been cancelled, so the awaiter is handed the ``CancelledError`` instead.
    The cancellation still wins — the worker is shutting down — but the failed
    preservation must not vanish with it.
    """
    runner = _runner(tmp_path)
    state = MonitorState()
    started = asyncio.Event()
    release = asyncio.Event()

    async def _steps(*args: Any, **kwargs: Any) -> _preserve.TimeoutSinkOutcome:
        started.set()
        await release.wait()
        raise RuntimeError("session factory is closed")

    monkeypatch.setattr(_preserve, "_timeout_anchor_and_sink_steps", _steps)
    with structlog.testing.capture_logs() as captured:
        item = asyncio.ensure_future(_invoke_item(runner, state=state))
        await started.wait()
        release.set()
        item.cancel()
        with pytest.raises(asyncio.CancelledError):
            await item

    failures = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_cancelled_timeout_preserve_failed"
    ]
    assert len(failures) == 1
    assert failures[0]["exc_type"] == "RuntimeError"
    assert runner.reset_targets == []
