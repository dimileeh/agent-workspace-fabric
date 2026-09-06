"""An in-run service-recovery rerun never costs the timed-out run its work (#934).

A local watchdog timeout that coincides with an unhealthy Compose service is
intercepted *inside* ``_run_monitor_agent_with_service_recovery``: the loop
restarts the service and reruns the agent, so the #932 preserve handler in
``comment_verdict`` is never reached for that first run. If the rerun then ends
with a provider failure, the non-timeout path rolls the worktree back to the
attempt floor — deleting the commits the timed-out run made, which is exactly the
destruction #932 exists to prevent (PRRT_kwDOSJAM6s6fvdil).

The loop now publishes the HEAD it is about to rerun over, and every failure exit
from the agent run raises the rollback floor to it. A run that came back with a
verdict keeps the ordinary floor: its rerun was a complete run, so unaccepted
residue still rolls back.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.adapters.base import AgentRunError, AgentRunResult
from awf.common.commands import CommandResult
from awf.db.enums import AgentRuntime
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict import AgentVerdictExecutionError
from tests.unit.runtime._verdict_retry_fixtures import _agent_error, _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_TIMED_OUT_RUN_HEAD = "b" * 40
_ITEM_ID = "issue:5558086911"


def _timeout_error() -> AgentRunError:
    return AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(returncode=124, stdout="", stderr="idle timeout\n"),
        reason_code="AGENT_IDLE_TIMEOUT",
    )


def _recovery_rerun_runner(
    tmp_path: Path,
    *,
    outcome: str | BaseException,
) -> _VerdictRunner:
    """A run whose first attempt timed out, was rerun, and ended in ``outcome``.

    The rerun itself adds nothing: HEAD stays at the timed-out run's commit, so
    only the published floor can keep that commit alive.
    """
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[_TIMED_OUT_RUN_HEAD],
        dirty_after_attempt=[False],
        stranded_dirty_after_attempt=[False],
    )
    runner.current_head = _ITEM_START_HEAD

    async def _run(**kwargs: object) -> None:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        # The first (timed-out) run self-committed before the watchdog fired.
        runner.current_head = _TIMED_OUT_RUN_HEAD
        sink = kwargs["timeout_rerun_floor_sink"]
        assert isinstance(sink, list)
        sink.append(_TIMED_OUT_RUN_HEAD)
        if isinstance(outcome, BaseException):
            raise outcome
        return AgentRunResult(returncode=0, stdout=outcome, stderr="")

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
async def test_provider_failure_after_a_recovery_rerun_keeps_the_timed_out_commits(
    tmp_path: Path,
) -> None:
    """The rollback stops at the published floor instead of the attempt start.

    HEAD already *is* that floor, so the rewind becomes a no-op — without the
    raise it would have reset to ``_ITEM_START_HEAD`` and dropped the commit.
    """
    runner = _recovery_rerun_runner(tmp_path, outcome=_agent_error())

    with pytest.raises(AgentVerdictExecutionError) as caught:
        await _invoke_item(runner, state=MonitorState())

    assert caught.value.reason_code == "AGENT_CLI_FAILED"
    assert runner.reset_targets == []
    assert runner.current_head == _TIMED_OUT_RUN_HEAD


@pytest.mark.unit
async def test_a_second_timeout_after_a_recovery_rerun_still_reports_preserved_work(
    tmp_path: Path,
) -> None:
    """The raised floor is a rollback limit, not the "did this attempt work?" baseline.

    The rerun added nothing, but the attempt as a whole did: the first run's
    commit is still there for the next pass to resume from, so the preserved HEAD
    must be reported rather than suppressed as "no new work".
    """
    runner = _recovery_rerun_runner(tmp_path, outcome=_timeout_error())

    with pytest.raises(AgentVerdictExecutionError) as caught:
        await _invoke_item(runner, state=MonitorState())

    assert caught.value.reason_code == "AGENT_IDLE_TIMEOUT"
    assert caught.value.preserved_head_sha == _TIMED_OUT_RUN_HEAD
    assert runner.reset_targets == []


@pytest.mark.unit
async def test_a_verdict_after_a_recovery_rerun_still_rolls_back_unaccepted_residue(
    tmp_path: Path,
) -> None:
    """A completed rerun is a complete run: non-FIXED residue goes back to the start."""
    runner = _recovery_rerun_runner(
        tmp_path,
        outcome="AWF-VERDICT: FALSE POSITIVE: the reviewer misread the diff",
    )

    result = await _invoke_item(runner, state=MonitorState())

    assert result.verdict == "false_positive"
    assert runner.reset_targets == [_ITEM_START_HEAD]
    assert runner.current_head == _ITEM_START_HEAD


@pytest.mark.unit
async def test_a_provider_failure_without_a_rerun_still_rolls_back_to_the_item_start(
    tmp_path: Path,
) -> None:
    """Nothing published means nothing timed out: unaccepted edits still go away."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[_agent_error()],
        heads_after_attempt=[_TIMED_OUT_RUN_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _ITEM_START_HEAD

    with pytest.raises(AgentVerdictExecutionError):
        await _invoke_item(runner, state=MonitorState())

    assert runner.reset_targets == [_ITEM_START_HEAD]
    assert runner.current_head == _ITEM_START_HEAD
