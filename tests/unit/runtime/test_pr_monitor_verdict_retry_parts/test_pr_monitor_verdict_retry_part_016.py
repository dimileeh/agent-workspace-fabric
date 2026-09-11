"""A timed-out agent keeps its work even when compose cleanup also fails (#934).

The adapter tears the tracked exec stack down *before* raising its own
``AgentRunError``, so when ``cleanup_compose_exec_invocation`` fails — a tagged
process that will not die — the ``ComposeExecCleanupError`` replaces the timeout
and the #932 preserve handler is never reached. The ordinary cleanup branch then
rolls the worktree back to the attempt floor, deleting the timed-out agent's
commits and masking the timeout: exactly the destruction #932 exists to prevent.

The cleanup error now carries the watchdog classification, and the verdict
protocol takes the preserve path for it: sink, keep every commit, remember the
item's start HEAD — then still escalate the cleanup error, because a process AWF
could not prove dead is the outcome and must not be downgraded to a timeout.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from awf.common.compose_exec import ComposeExecCleanupError
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    item_start_head_state_key,
)
from awf.runtime.pr_monitor_runner.types import _MonitorMirrorHooksPathRepairFailedError
from tests.unit.runtime._verdict_retry_fixtures import _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_PRESERVED_HEAD = "b" * 40
_ITEM_ID = "issue:5558086911"
_TIMEOUT_REASON_CODES = ("AGENT_IDLE_TIMEOUT", "AGENT_TIMEOUT")
_MIRROR_PATH = Path("/mirrors/awf.git")


def _cleanup_error(agent_reason_code: str | None) -> ComposeExecCleanupError:
    """The cleanup failure the adapter raises in place of the agent's error."""
    exc = ComposeExecCleanupError(
        invocation_id="cleanup-failed",
        source="recovery",
        label="agent",
        message="tagged process still alive",
    )
    exc.agent_reason_code = agent_reason_code
    return exc


def _commit_then_fail_cleanup(runner: _VerdictRunner, exc: ComposeExecCleanupError) -> None:
    """Model an agent that self-commits and dirties the tree, then fails cleanup."""

    async def _run(**kwargs: object) -> None:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = _PRESERVED_HEAD
        runner._persistent_stranded_status_stdout = " M agent_edit.py\n"
        raise exc

    runner._run_monitor_agent_with_service_recovery = _run


def _timeout_cleanup_runner(
    tmp_path: Path,
    *,
    agent_reason_code: str | None,
) -> _VerdictRunner:
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[_PRESERVED_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _ITEM_START_HEAD
    _commit_then_fail_cleanup(runner, _cleanup_error(agent_reason_code))
    return runner


def _record_sink(runner: _VerdictRunner) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    real_sink = runner._commit_dirty_worktree

    async def _sink(**kwargs: object) -> bool:
        calls.append(dict(kwargs))
        return await real_sink(**kwargs)

    runner._commit_dirty_worktree = _sink
    return calls


async def _invoke_item(
    runner: _VerdictRunner,
    *,
    state: MonitorState | None = None,
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
async def test_timeout_with_failed_cleanup_preserves_work_and_escalates(
    tmp_path: Path,
    reason_code: str,
) -> None:
    """No rollback, the sink runs, the anchor is remembered, cleanup still escalates."""
    runner = _timeout_cleanup_runner(tmp_path, agent_reason_code=reason_code)
    sink_calls = _record_sink(runner)
    state = MonitorState()

    with (
        structlog.testing.capture_logs() as captured,
        pytest.raises(ComposeExecCleanupError) as caught,
    ):
        await _invoke_item(runner, state=state)

    # The cleanup failure is still the escalated outcome, not a timeout verdict.
    assert caught.value.reason_code == "EXEC_PROCESS_CLEANUP_FAILED"
    assert caught.value.agent_reason_code == reason_code
    # The whole point: the timed-out agent's commit survives.
    assert runner.reset_targets == []
    assert runner.current_head == _PRESERVED_HEAD
    assert len(sink_calls) == 1
    assert "preserved after agent timeout" in str(sink_calls[0]["message"])
    assert sink_calls[0]["operation_start_head"] == _ITEM_START_HEAD
    # The re-attempt still anchors its evidence range at the original item start.
    assert state.threads_addressed_ids[item_start_head_state_key(_ITEM_ID)] == _ITEM_START_HEAD
    preserved_events = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_timeout_cleanup_failure_work_preserved"
    ]
    assert len(preserved_events) == 1
    assert preserved_events[0]["reason_code"] == reason_code
    assert preserved_events[0]["cleanup_reason_code"] == "EXEC_PROCESS_CLEANUP_FAILED"
    assert preserved_events[0]["dirty_changes_committed"] is True


@pytest.mark.unit
@pytest.mark.parametrize("agent_reason_code", [None, "AGENT_CLI_FAILED"])
async def test_cleanup_failure_without_a_timeout_still_rolls_back(
    tmp_path: Path,
    agent_reason_code: str | None,
) -> None:
    """Only a watchdog timeout preserves; every other cleanup failure rolls back."""
    runner = _timeout_cleanup_runner(tmp_path, agent_reason_code=agent_reason_code)
    state = MonitorState()

    with pytest.raises(ComposeExecCleanupError):
        await _invoke_item(runner, state=state)

    # Rolled back both before the hook repair and after the commit sink.
    assert set(runner.reset_targets) == {_ITEM_START_HEAD}
    assert runner.current_head == _ITEM_START_HEAD
    assert item_start_head_state_key(_ITEM_ID) not in state.threads_addressed_ids


@pytest.mark.unit
async def test_preserved_timeout_cleanup_failure_repairs_mirror_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed teardown can leave a live agent, so hooks are still repaired."""
    runner = _timeout_cleanup_runner(tmp_path, agent_reason_code="AGENT_TIMEOUT")
    monkeypatch.setattr(comment_verdict, "mirror_path_for_worktree", lambda _path: _MIRROR_PATH)
    repairs: list[dict[str, object]] = []

    async def _repair(**kwargs: object) -> None:
        repairs.append(dict(kwargs))

    monkeypatch.setattr(comment_verdict, "_repair_mirror_hooks_or_raise", _repair)

    with pytest.raises(ComposeExecCleanupError):
        await _invoke_item(runner, state=MonitorState())

    assert repairs[-1]["stage"] == "after_comment_agent_timeout_cleanup_failure"
    assert repairs[-1]["mirror_path"] == _MIRROR_PATH
    assert runner.reset_targets == []


@pytest.mark.unit
async def test_mirror_repair_failure_after_preserved_timeout_keeps_the_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hook-repair failure propagates in place of the cleanup error — never a rollback.

    The repair runs before the sink, so this exit keeps the anchor and the
    commits without committing residue under a possibly poisoned hooks path.
    """
    runner = _timeout_cleanup_runner(tmp_path, agent_reason_code="AGENT_IDLE_TIMEOUT")
    monkeypatch.setattr(comment_verdict, "mirror_path_for_worktree", lambda _path: _MIRROR_PATH)

    async def _repair(**kwargs: object) -> None:
        if kwargs.get("stage") == "after_comment_agent_timeout_cleanup_failure":
            raise _MonitorMirrorHooksPathRepairFailedError("hooks poisoned")

    monkeypatch.setattr(comment_verdict, "_repair_mirror_hooks_or_raise", _repair)

    state = MonitorState()
    with pytest.raises(_MonitorMirrorHooksPathRepairFailedError):
        await _invoke_item(runner, state=state)

    assert runner.reset_targets == []
    assert runner.current_head == _PRESERVED_HEAD
    assert state.threads_addressed_ids[item_start_head_state_key(_ITEM_ID)] == _ITEM_START_HEAD
