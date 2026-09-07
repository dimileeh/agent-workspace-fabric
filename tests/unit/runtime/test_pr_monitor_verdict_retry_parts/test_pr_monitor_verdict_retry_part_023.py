"""A timeout-tagged cancellation still owes the whole #932 sequence (#934).

Skipping the rollback keeps the timed-out agent's commits, but that is only one
of the three things the preserve path does. The adapter classified this timeout
inside the compose cleanup it runs *before* raising its ``AgentRunError``, so no
handler in the verdict protocol ever ran the other two: the uncommitted edits are
never sunk — the next pass's guard rejects them as
``PRE_EXISTING_DIRTY_WORKTREE`` — and the item's start HEAD is never recorded, so
a self-committed fix restarts anchored at its own preserved HEAD and is rejected
as ``AGENT_FIXED_WITHOUT_EVIDENCE`` (PRRT_kwDOSJAM6s6f2I94).

The sequence therefore runs from the cancellation branch, under a shield so a
re-delivered cancellation cannot leave the marker or the sink half-written, and
never raises: the tagged cancellation is re-raised immediately after and must not
be displaced.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import structlog

from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    item_start_head_state_key,
)
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
    """An agent that leaves dirty edits behind and is then cancelled in the adapter."""
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


def _record_sink_calls(runner: _VerdictRunner) -> list[dict[str, object]]:
    """Record every dirty-worktree sink call the preserve sequence makes."""
    sink_calls: list[dict[str, object]] = []
    original = runner._commit_dirty_worktree

    async def _commit(**kwargs: object) -> bool:
        sink_calls.append(kwargs)
        return await original(**kwargs)

    runner._commit_dirty_worktree = _commit
    return sink_calls


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
async def test_tagged_cancellation_sinks_dirt_and_records_the_anchor(
    tmp_path: Path,
    reason_code: str,
) -> None:
    """The tagged arm finishes the preserve sequence before propagating."""
    runner = _runner(tmp_path, agent_reason_code=reason_code)
    sink_calls = _record_sink_calls(runner)
    state = MonitorState()

    with structlog.testing.capture_logs() as captured, pytest.raises(asyncio.CancelledError):
        await _invoke_item(runner, state=state)

    assert runner.reset_targets == []
    assert len(sink_calls) == 1
    assert sink_calls[0]["operation_start_head"] == _ITEM_START_HEAD
    assert state.threads_addressed_ids[item_start_head_state_key(_ITEM_ID)] == _ITEM_START_HEAD
    preserved = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_cancelled_timeout_work_preserved"
    ]
    assert len(preserved) == 1
    assert preserved[0]["reason_code"] == reason_code
    assert preserved[0]["dirty_changes_committed"] is True
    assert preserved[0]["item_start_head_persisted"] is True


@pytest.mark.unit
@pytest.mark.parametrize("agent_reason_code", [None, "AGENT_CLI_FAILED"])
async def test_untagged_cancellation_preserves_nothing(
    tmp_path: Path,
    agent_reason_code: str | None,
) -> None:
    """Only a masked *timeout* is preserved; every other cancellation rolls back."""
    runner = _runner(tmp_path, agent_reason_code=agent_reason_code)
    sink_calls = _record_sink_calls(runner)
    state = MonitorState()

    with pytest.raises(asyncio.CancelledError):
        await _invoke_item(runner, state=state)

    assert runner.reset_targets == [_ITEM_START_HEAD]
    assert sink_calls == []
    assert item_start_head_state_key(_ITEM_ID) not in state.threads_addressed_ids


@pytest.mark.unit
async def test_redelivered_cancellation_cannot_truncate_the_sequence(
    tmp_path: Path,
) -> None:
    """A second cancellation mid-sink is absorbed; the sequence still finishes."""
    runner = _runner(tmp_path, agent_reason_code="AGENT_TIMEOUT")
    state = MonitorState()
    sink_started = asyncio.Event()
    release_sink = asyncio.Event()
    sink_calls: list[dict[str, object]] = []

    async def _slow_commit(**kwargs: object) -> bool:
        sink_calls.append(kwargs)
        sink_started.set()
        await release_sink.wait()
        runner.current_head = _TIMED_OUT_RUN_HEAD
        return True

    runner._commit_dirty_worktree = _slow_commit
    item = asyncio.ensure_future(_invoke_item(runner, state=state))
    await sink_started.wait()
    item.cancel()
    await asyncio.sleep(0)
    release_sink.set()

    with pytest.raises(asyncio.CancelledError):
        await item

    assert len(sink_calls) == 1
    assert runner.reset_targets == []
    assert state.threads_addressed_ids[item_start_head_state_key(_ITEM_ID)] == _ITEM_START_HEAD


@pytest.mark.unit
async def test_failed_preservation_does_not_displace_the_cancellation(
    tmp_path: Path,
) -> None:
    """A preserve step that raises is logged, never propagated over the timeout."""
    runner = _runner(tmp_path, agent_reason_code="AGENT_TIMEOUT")
    state = MonitorState()

    def _explode() -> object:
        raise RuntimeError("session factory is closed")

    runner._deps.session_factory = _explode

    with structlog.testing.capture_logs() as captured, pytest.raises(asyncio.CancelledError):
        await _invoke_item(runner, state=state)

    assert runner.reset_targets == []
    assert [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_cancelled_timeout_preserve_failed"
    ]


@pytest.mark.unit
async def test_durable_anchor_failure_still_sinks_the_dirt(
    tmp_path: Path,
) -> None:
    """A marker write that dies outside its own error set must not skip the sink.

    Losing the anchor costs the retry its evidence range; leaving the edits dirty
    wedges the next pass at ``PRE_EXISTING_DIRTY_WORKTREE`` — the state this whole
    path exists to avoid — so the anchor's failure may not take the sink with it.
    The one record this path emits must not read as an unqualified success either:
    the durable anchor is gone, and only the in-memory marker a dying worker may
    never persist is left (PRRT_kwDOSJAM6s6f2ckK).
    """
    runner = _runner(tmp_path, agent_reason_code="AGENT_TIMEOUT")
    sink_calls = _record_sink_calls(runner)
    state = MonitorState()

    def _explode() -> object:
        raise RuntimeError("session factory is closed")

    runner._deps.session_factory = _explode

    with structlog.testing.capture_logs() as captured, pytest.raises(asyncio.CancelledError):
        await _invoke_item(runner, state=state)

    assert runner.reset_targets == []
    assert len(sink_calls) == 1
    assert sink_calls[0]["operation_start_head"] == _ITEM_START_HEAD
    assert state.threads_addressed_ids[item_start_head_state_key(_ITEM_ID)] == _ITEM_START_HEAD
    preserved = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_cancelled_timeout_work_preserved"
    ]
    assert len(preserved) == 1
    assert preserved[0]["dirty_changes_committed"] is True
    assert preserved[0]["item_start_head_persisted"] is False
    assert "session factory is closed" in str(preserved[0]["item_start_head_error"])
