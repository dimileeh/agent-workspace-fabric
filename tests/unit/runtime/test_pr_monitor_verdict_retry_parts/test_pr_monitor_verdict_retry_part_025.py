"""The failed-cleanup preserve entry owes the same mid-sequence shield (#934).

``preserve_timeout_work_and_raise_cleanup_error`` publishes
``timeout_preservation_sink`` before its first await, exactly as
``handle_agent_run_error`` does, so the caller's cancellation branch stops
rewinding the timed-out agent's commits. Everything the claim still owes — the
durable evidence anchor, the mirror-hook repair and the dirty-worktree sink —
runs after that publish and awaits. A worker cancellation landing in any of them
escaped with the claim already made but the work only half kept: the branch
skipped the rollback (right) and its nested guard also skipped
``preserve_cancelled_timeout_work``, which only runs for timeouts *no* handler
ever saw. The next pass then met the timed-out edits still dirty
(``PRE_EXISTING_DIRTY_WORKTREE``) or an anchor only in memory on a worker that
may never persist it (``AGENT_FIXED_WITHOUT_EVIDENCE``).

The already-started sequence therefore finishes under a shield before the
cancellation is delivered onward, on this entry too (PRRT_kwDOSJAM6s6f2gbw).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import structlog

from awf.common.compose_exec import ComposeExecCleanupError
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner import comment_verdict_timeout_preserve as _preserve
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    item_start_head_state_key,
)
from tests.unit.runtime._verdict_retry_fixtures import _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_PRESERVED_HEAD = "b" * 40
_ITEM_ID = "issue:5558086911"


def _cleanup_error() -> ComposeExecCleanupError:
    exc = ComposeExecCleanupError(
        invocation_id="cleanup-failed",
        source="recovery",
        label="agent",
        message="tagged process still alive",
    )
    exc.agent_reason_code = "AGENT_TIMEOUT"
    return exc


def _runner(tmp_path: Path) -> _VerdictRunner:
    """A timed-out agent whose exec-stack teardown also failed."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["unused"],
        heads_after_attempt=[_PRESERVED_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _ITEM_START_HEAD

    async def _run(**kwargs: object) -> None:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = _PRESERVED_HEAD
        runner._persistent_stranded_status_stdout = " M agent_edit.py\n"
        raise _cleanup_error()

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
async def test_cleanup_failure_cancelled_mid_sink_still_commits_the_edits(
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
        runner._persistent_stranded_status_stdout = ""
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
    assert runner.current_head == _PRESERVED_HEAD
    assert state.threads_addressed_ids[item_start_head_state_key(_ITEM_ID)] == _ITEM_START_HEAD


@pytest.mark.unit
async def test_cleanup_failure_cancelled_mid_anchor_write_still_reaches_the_sink(
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
async def test_cleanup_failure_durable_anchor_failure_still_sinks_the_edits(
    tmp_path: Path,
) -> None:
    """An anchor write dying outside its own error set may not skip the sink.

    The claim is already published here too, so aborting on the anchor left the
    timed-out edits dirty for the next pass's ``PRE_EXISTING_DIRTY_WORKTREE`` guard
    and replaced the cleanup failure — the outcome that must survive this path —
    with an unrelated exception (PRRT_kwDOSJAM6s6f2_KT).
    """
    runner = _runner(tmp_path)
    state = MonitorState()
    sink_calls: list[dict[str, object]] = []
    original_commit = runner._commit_dirty_worktree

    async def _commit(**kwargs: object) -> bool:
        sink_calls.append(kwargs)
        return await original_commit(**kwargs)

    def _explode() -> object:
        raise RuntimeError("session factory is closed")

    runner._commit_dirty_worktree = _commit
    runner._deps.session_factory = _explode

    with (
        structlog.testing.capture_logs() as captured,
        pytest.raises(ComposeExecCleanupError),
    ):
        await _invoke_item(runner, state=state)

    assert len(sink_calls) == 1
    assert sink_calls[0]["operation_start_head"] == _ITEM_START_HEAD
    assert runner.reset_targets == []
    assert runner.current_head == _PRESERVED_HEAD
    assert state.threads_addressed_ids[item_start_head_state_key(_ITEM_ID)] == _ITEM_START_HEAD
    anchor_failures = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_timeout_preserve_anchor_failed"
    ]
    assert len(anchor_failures) == 1
    assert anchor_failures[0]["exc_type"] == "RuntimeError"
    preserved = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_timeout_cleanup_failure_work_preserved"
    ]
    assert len(preserved) == 1
    assert preserved[0]["dirty_changes_committed"] is True
