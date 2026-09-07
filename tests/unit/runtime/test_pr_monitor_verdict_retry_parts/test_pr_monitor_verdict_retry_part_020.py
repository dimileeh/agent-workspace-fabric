"""Cancellation must not undo the *service-recovery* timeout bookkeeping (#934).

A watchdog timeout that coincides with an unhealthy Compose service is handled
*inside* ``_run_monitor_agent_with_service_recovery``: the loop restarts the
service, salvages the timed-out run's uncommitted edits through the caller's
dirty sink, publishes the HEAD it is about to rerun over, and reruns the agent.
Until that HEAD is published neither protection channel is populated — the
#932 preserve handler never saw this timeout, so it wrote nothing, and the floor
sink is still empty — while the sink and the HEAD probe both await.

Worker cancellation there is a ``BaseException``: it bypasses the loop's own
handlers and lands on the verdict protocol's cancellation branch, which rewinds
to ``rollback_floor_head`` and deletes the timed-out agent's commits plus any
salvage commit the sink just made (PRRT_kwDOSJAM6s6fy7ju). The loop therefore
claims the caller's preservation sink for the duration of that bookkeeping, and
hands the protection back to the floor raise once the floor is published.

These drive the *real* recovery loop, not a stub, so the hand-off between the two
protection channels is exercised end to end.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import structlog

from awf.adapters.base import AgentRunError, AgentRunResult
from awf.common.commands import CommandResult
from awf.db.enums import AgentRuntime
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import agent_service_recovery, comment_verdict
from tests.unit.runtime._verdict_retry_fixtures import _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_TIMED_OUT_RUN_HEAD = "b" * 40
_SALVAGE_HEAD = "c" * 40
_RERUN_HEAD = "d" * 40
_ITEM_ID = "issue:5558086911"


def _timeout_error() -> AgentRunError:
    return AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(returncode=124, stdout="", stderr="idle timeout\n"),
        reason_code="AGENT_IDLE_TIMEOUT",
    )


class _TimeoutThenCancelAdapter:
    """Times out on the first run; the rerun (if reached) does ``rerun_action``."""

    is_hosted = False
    name = AgentRuntime.codex

    def __init__(self, runner: _VerdictRunner, *, rerun_action: BaseException | str) -> None:
        self._runner = runner
        self._rerun_action = rerun_action
        self.runs = 0

    async def run(self, **_kwargs: Any) -> AgentRunResult:
        self.runs += 1
        self._runner.attempt += 1
        if self.runs == 1:
            # The timed-out run self-committed and left further edits dirty.
            self._runner.current_head = _TIMED_OUT_RUN_HEAD
            self._runner._persistent_stranded_status_stdout = " M agent_edit.py\n"
            raise _timeout_error()
        if isinstance(self._rerun_action, BaseException):
            # The rerun leaves residue of its own above the published floor.
            self._runner.current_head = _RERUN_HEAD
            raise self._rerun_action
        return AgentRunResult(returncode=0, stdout=self._rerun_action, stderr="")


def _recovery_loop_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    rerun_action: BaseException | str,
    salvage_commits: bool,
) -> _VerdictRunner:
    """A runner whose agent run goes through the real service-recovery loop."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[_SALVAGE_HEAD, _SALVAGE_HEAD],
        dirty_after_attempt=[salvage_commits, salvage_commits],
    )
    runner.current_head = _ITEM_START_HEAD
    runner._deps.adapter = _TimeoutThenCancelAdapter(runner, rerun_action=rerun_action)

    async def _loop(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        return await agent_service_recovery._run_monitor_agent_with_service_recovery(
            runner,  # type: ignore[arg-type]
            **kwargs,  # type: ignore[arg-type]
        )

    runner._run_monitor_agent_with_service_recovery = _loop

    async def _recover(*_args: object, **_kwargs: object) -> int | None:
        return 1

    async def _guards(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        agent_service_recovery,
        "_recover_monitor_agent_service_after_error",
        _recover,
    )
    monkeypatch.setattr(
        agent_service_recovery,
        "_rerun_monitor_agent_pre_launch_guards",
        _guards,
    )
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
async def test_cancellation_during_the_recovery_salvage_keeps_the_timed_out_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel inside the loop's dirty sink: nothing rewinds the timed-out commit."""
    runner = _recovery_loop_runner(
        tmp_path,
        monkeypatch,
        rerun_action="AWF-VERDICT: FIXED: done",
        salvage_commits=True,
    )

    async def _cancelled_sink(**_kwargs: object) -> bool:
        raise asyncio.CancelledError()

    runner._commit_dirty_worktree = _cancelled_sink
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
    assert preserved[0]["reason_code"] == "AGENT_IDLE_TIMEOUT"


@pytest.mark.unit
async def test_cancellation_during_the_recovery_floor_probe_keeps_the_timed_out_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The HEAD probe runs after the sink and is just as unprotected."""
    runner = _recovery_loop_runner(
        tmp_path,
        monkeypatch,
        rerun_action="AWF-VERDICT: FIXED: done",
        salvage_commits=True,
    )

    cancelled_probes = 0

    async def _cancelled_head(_worktree_path: Path) -> str | None:
        # Cancel only the loop's own floor probe: the item's attempt-start probe
        # runs before the timeout, and the rollback the cancellation branch would
        # otherwise perform reads HEAD through this same seam.
        nonlocal cancelled_probes
        if runner._deps.adapter.runs >= 1 and not cancelled_probes:
            cancelled_probes += 1
            raise asyncio.CancelledError()
        return str(runner.current_head)

    runner._rev_parse_head = _cancelled_head
    state = MonitorState()

    with pytest.raises(asyncio.CancelledError):
        await _invoke_item(runner, state=state)

    assert runner.reset_targets == []
    assert runner.current_head == _SALVAGE_HEAD


@pytest.mark.unit
async def test_cancellation_after_the_floor_is_published_rewinds_only_to_that_floor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the floor is published the protection is the floor raise, not the mark.

    The rerun's own unaccepted residue must still roll back — to the published
    floor, which keeps the timed-out run's commits and the salvage commit.
    """
    runner = _recovery_loop_runner(
        tmp_path,
        monkeypatch,
        rerun_action=asyncio.CancelledError(),
        salvage_commits=True,
    )
    state = MonitorState()

    with structlog.testing.capture_logs() as captured, pytest.raises(asyncio.CancelledError):
        await _invoke_item(runner, state=state)

    assert runner.reset_targets == [_SALVAGE_HEAD]
    assert runner.current_head == _SALVAGE_HEAD
    assert not [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_cancellation_preserved_timeout_work"
    ]
