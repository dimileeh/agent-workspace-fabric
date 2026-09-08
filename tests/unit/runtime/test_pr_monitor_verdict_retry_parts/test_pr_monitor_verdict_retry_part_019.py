"""Cancellation must not undo the *cleanup-failure* timeout preservation (#934).

``preserve_timeout_work_and_raise_cleanup_error`` is the second entry into the
#932 preserve sequence: a watchdog timeout whose compose cleanup also failed. It
repairs mirror hooks and runs the dirty sink, and both of those await. Worker
cancellation there is a ``BaseException``, so it bypasses the preserve path and
lands on the verdict protocol's cancellation branch — which rewinds to the
attempt's ``rollback_floor_head`` and deletes the timed-out agent's commits
unless this path publishes its protected floor the way ``handle_agent_run_error``
does (PRRT_kwDOSJAM6s6fyvd1).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import structlog

from awf.common.compose_exec import ComposeExecCleanupError
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
_MIRROR_PATH = Path("/mirrors/awf.git")


def _cleanup_error(agent_reason_code: str | None) -> ComposeExecCleanupError:
    exc = ComposeExecCleanupError(
        invocation_id="cleanup-failed",
        source="recovery",
        label="agent",
        message="tagged process still alive",
    )
    exc.agent_reason_code = agent_reason_code
    return exc


def _runner(tmp_path: Path, *, agent_reason_code: str | None) -> _VerdictRunner:
    """An agent that self-commits and dirties the tree, then fails compose cleanup."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[_PRESERVED_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _ITEM_START_HEAD
    exc = _cleanup_error(agent_reason_code)

    async def _run(**kwargs: object) -> None:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = _PRESERVED_HEAD
        runner._persistent_stranded_status_stdout = " M agent_edit.py\n"
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
async def test_cancellation_during_cleanup_preserve_sink_keeps_the_work(
    tmp_path: Path,
    reason_code: str,
) -> None:
    """Cancel inside the preserve sink: the timed-out agent's commit survives."""
    runner = _runner(tmp_path, agent_reason_code=reason_code)

    async def _cancelled_sink(**_kwargs: object) -> bool:
        raise asyncio.CancelledError()

    runner._commit_dirty_worktree = _cancelled_sink
    state = MonitorState()

    with structlog.testing.capture_logs() as captured, pytest.raises(asyncio.CancelledError):
        await _invoke_item(runner, state=state)

    assert runner.reset_targets == []
    assert runner.current_head == _PRESERVED_HEAD
    assert state.threads_addressed_ids[item_start_head_state_key(_ITEM_ID)] == _ITEM_START_HEAD
    preserved = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_cancellation_preserved_timeout_work"
    ]
    assert len(preserved) == 1
    assert preserved[0]["reason_code"] == reason_code


@pytest.mark.unit
async def test_cancellation_during_cleanup_preserve_hook_repair_keeps_the_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hook repair runs before the sink and is cancellable too."""
    runner = _runner(tmp_path, agent_reason_code="AGENT_TIMEOUT")
    monkeypatch.setattr(comment_verdict, "mirror_path_for_worktree", lambda _path: _MIRROR_PATH)

    async def _repair(**kwargs: object) -> None:
        if kwargs.get("stage") == "after_comment_agent_timeout_cleanup_failure":
            raise asyncio.CancelledError()

    monkeypatch.setattr(comment_verdict, "_repair_mirror_hooks_or_raise", _repair)
    state = MonitorState()

    with pytest.raises(asyncio.CancelledError):
        await _invoke_item(runner, state=state)

    assert runner.reset_targets == []
    assert runner.current_head == _PRESERVED_HEAD
    assert state.threads_addressed_ids[item_start_head_state_key(_ITEM_ID)] == _ITEM_START_HEAD


@pytest.mark.unit
async def test_cancellation_during_non_timeout_cleanup_failure_still_rolls_back(
    tmp_path: Path,
) -> None:
    """Only a preserved timeout is protected; an ordinary cleanup failure rewinds."""
    runner = _runner(tmp_path, agent_reason_code="AGENT_CLI_FAILED")

    async def _cancelled_sink(**_kwargs: object) -> bool:
        raise asyncio.CancelledError()

    runner._commit_dirty_worktree = _cancelled_sink
    state = MonitorState()

    with pytest.raises(asyncio.CancelledError):
        await _invoke_item(runner, state=state)

    assert runner.reset_targets == [_ITEM_START_HEAD]
    assert runner.current_head == _ITEM_START_HEAD
    assert item_start_head_state_key(_ITEM_ID) not in state.threads_addressed_ids
