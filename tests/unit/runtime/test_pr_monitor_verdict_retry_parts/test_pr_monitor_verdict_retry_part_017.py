"""An in-run service-recovery rerun never costs the timed-out run its work (#934).

A local watchdog timeout that coincides with an unhealthy Compose service is
intercepted *inside* ``_run_monitor_agent_with_service_recovery``: the loop
restarts the service and reruns the agent, so the #932 preserve handler in
``comment_verdict`` is never reached for that first run. If the rerun then ends
with a provider failure, the non-timeout path rolls the worktree back to the
attempt floor — deleting the commits the timed-out run made, which is exactly the
destruction #932 exists to prevent (PRRT_kwDOSJAM6s6fvdil).

The loop now publishes the HEAD it is about to rerun over, and every exit from the
item raises the rollback floor to it — accepting the rerun's verdict included
(PRRT_kwDOSJAM6s6fvw8m). The rerun's *own* unaccepted residue still rolls back,
but only as far as that floor, exactly as the cross-pass #932 re-attempt already
behaves.

When the loop cannot publish a floor at all it gives the rerun up instead and
re-raises the timeout, so this side must preserve the timed-out run's work rather
than roll back to the attempt start (PRRT_kwDOSJAM6s6fxp80).
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
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    item_start_head_state_key,
    peek_item_start_body_hash,
    peek_item_start_head,
)
from tests.unit.runtime._verdict_retry_fixtures import _agent_error, _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_TIMED_OUT_RUN_HEAD = "b" * 40
_RERUN_HEAD = "c" * 40
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
        heads_after_attempt=[_TIMED_OUT_RUN_HEAD, _TIMED_OUT_RUN_HEAD],
        dirty_after_attempt=[False, False],
        stranded_dirty_after_attempt=[False, False],
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
async def test_a_correction_timeout_after_an_earlier_rerun_reports_the_preserved_work(
    tmp_path: Path,
) -> None:
    """The raised floor must not become the *next* attempt's baseline either.

    Attempt 0 timed out, was rerun, and came back without a verdict, so the floor
    is now the timed-out run's commit and the item falls through to the
    correction attempt. That correction then times out without moving HEAD.
    Measuring it against the raised floor hides the commit attempt 0 kept, and
    the operator-hint retry gate reads that silence as "nothing survived".
    """
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[_TIMED_OUT_RUN_HEAD, _TIMED_OUT_RUN_HEAD],
        dirty_after_attempt=[False, False],
        stranded_dirty_after_attempt=[False, False],
    )
    runner.current_head = _ITEM_START_HEAD

    async def _run(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        attempt = runner.attempt
        runner.attempt += 1
        if attempt > 0:
            # The correction agent adds nothing before its own watchdog fires.
            raise _timeout_error()
        runner.current_head = _TIMED_OUT_RUN_HEAD
        sink = kwargs["timeout_rerun_floor_sink"]
        assert isinstance(sink, list)
        sink.append(_TIMED_OUT_RUN_HEAD)
        return AgentRunResult(returncode=0, stdout="I had a look at the thread.", stderr="")

    runner._run_monitor_agent_with_service_recovery = _run

    with pytest.raises(AgentVerdictExecutionError) as caught:
        await _invoke_item(runner, state=MonitorState())

    assert caught.value.reason_code == "AGENT_IDLE_TIMEOUT"
    assert caught.value.preserved_head_sha == _TIMED_OUT_RUN_HEAD
    assert runner.current_head == _TIMED_OUT_RUN_HEAD


@pytest.mark.unit
async def test_a_timeout_after_a_rerun_over_an_unknown_floor_still_reports_the_work(
    tmp_path: Path,
) -> None:
    """An unknown pre-raise floor must fail open, not fall back to the raised one.

    The item started with no readable HEAD, so the floor was ``None`` when the
    service-recovery rerun raised it. The baseline for "did this item leave work
    behind?" is therefore legitimately ``None`` — which the preserve handler is
    meant to read as "cannot show HEAD standing still", i.e. fail open. Letting
    that ``None`` mean "no baseline was threaded" hands the handler the raised
    floor instead, so the correction timeout reports nothing survived and the
    operator-hint retry gate parks work a human has to rescue.
    """
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[_TIMED_OUT_RUN_HEAD, _TIMED_OUT_RUN_HEAD],
        dirty_after_attempt=[False, False],
        stranded_dirty_after_attempt=[False, False],
        # Item-start and attempt-0-start HEAD reads both come back empty, so the
        # rollback floor is still None when the rerun raises it.
        rev_parse_sequence=[None, None],
    )
    runner.current_head = _ITEM_START_HEAD

    async def _run(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        attempt = runner.attempt
        runner.attempt += 1
        if attempt > 0:
            raise _timeout_error()
        runner.current_head = _TIMED_OUT_RUN_HEAD
        sink = kwargs["timeout_rerun_floor_sink"]
        assert isinstance(sink, list)
        sink.append(_TIMED_OUT_RUN_HEAD)
        return AgentRunResult(returncode=0, stdout="I had a look at the thread.", stderr="")

    runner._run_monitor_agent_with_service_recovery = _run

    with pytest.raises(AgentVerdictExecutionError) as caught:
        await comment_verdict._invoke_cli_for_verdict_result(
            runner,  # type: ignore[arg-type]
            workspace_id="ws_protocol",
            prompt="ORIGINAL REVIEW PROMPT",
            commit_message=f"fix: address PR review comment {_ITEM_ID}",
            compose_project="awf_ws_protocol",
            compose_file=Path("compose.yml"),
            state=MonitorState(),
            operation_start_head=None,
            evidence_item_id=_ITEM_ID,
        )

    assert caught.value.reason_code == "AGENT_IDLE_TIMEOUT"
    assert caught.value.preserved_head_sha == _TIMED_OUT_RUN_HEAD
    assert runner.current_head == _TIMED_OUT_RUN_HEAD


@pytest.mark.unit
async def test_a_non_fixed_verdict_after_a_recovery_rerun_keeps_the_timed_out_commits(
    tmp_path: Path,
) -> None:
    """Accepting the rerun's verdict is no licence to delete the timed-out work.

    A parsed FALSE POSITIVE / DEFER / NEEDS_HUMAN ends the item, so nothing will
    resume it — rewinding below the published floor would drop the commits the
    #932 preserve path exists to keep, and the cross-pass re-attempt (whose floor
    stays at the preserved HEAD) already refuses to do that.
    """
    runner = _recovery_rerun_runner(
        tmp_path,
        outcome="AWF-VERDICT: FALSE POSITIVE: the reviewer misread the diff",
    )

    result = await _invoke_item(runner, state=MonitorState())

    assert result.verdict == "false_positive"
    assert runner.reset_targets == []
    assert runner.current_head == _TIMED_OUT_RUN_HEAD


@pytest.mark.unit
async def test_a_non_fixed_verdict_still_rolls_the_reruns_own_residue_back(
    tmp_path: Path,
) -> None:
    """The floor is a limit, not an amnesty: the rerun's own commit still goes.

    The rerun self-commits on top of the timed-out run's HEAD and then answers
    FALSE POSITIVE, so its edits are unaccepted. They rewind to the published
    floor — and stop there.
    """
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[_RERUN_HEAD],
        dirty_after_attempt=[False],
        stranded_dirty_after_attempt=[False],
    )
    runner.current_head = _ITEM_START_HEAD

    async def _run(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        # The first (timed-out) run self-committed before the watchdog fired;
        # the rerun then added a commit of its own.
        sink = kwargs["timeout_rerun_floor_sink"]
        assert isinstance(sink, list)
        sink.append(_TIMED_OUT_RUN_HEAD)
        runner.current_head = _RERUN_HEAD
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: FALSE POSITIVE: the reviewer misread the diff",
            stderr="",
        )

    runner._run_monitor_agent_with_service_recovery = _run

    result = await _invoke_item(runner, state=MonitorState())

    assert result.verdict == "false_positive"
    assert runner.reset_targets == [_TIMED_OUT_RUN_HEAD]
    assert runner.current_head == _TIMED_OUT_RUN_HEAD


@pytest.mark.unit
async def test_a_protocol_violation_after_a_recovery_rerun_keeps_the_timed_out_commits(
    tmp_path: Path,
) -> None:
    """A returned run can still end the item without a verdict.

    Both attempts come back with output that carries no verdict record, so the
    item terminates through the protocol-violation rollback rather than through
    an exception handler. That exit is no more entitled to delete the timed-out
    run's commits than a provider failure is, so the floor must be raised on the
    returning path too.
    """
    runner = _recovery_rerun_runner(tmp_path, outcome="I had a look at the thread.")

    with pytest.raises(comment_verdict.AgentVerdictProtocolError) as caught:
        await _invoke_item(runner, state=MonitorState())

    assert caught.value.reason_code == comment_verdict.AGENT_VERDICT_PROTOCOL_VIOLATION
    assert runner.reset_targets == []
    assert runner.current_head == _TIMED_OUT_RUN_HEAD


@pytest.mark.unit
async def test_a_recovery_rerun_sinks_the_timed_out_runs_dirty_edits(
    tmp_path: Path,
) -> None:
    """Uncommitted work the timed-out run left is committed before the rerun.

    That run committed nothing, so the floor the loop can publish is the attempt
    floor itself and a provider failure on the rerun would ``reset --hard``
    straight through its dirty edits. The item therefore hands the loop its own
    dirty-worktree sink — the one the #932 preserve handler runs, anchored at the
    item start and labelled as preserved timeout work — so those edits become a
    commit the published floor covers (PRRT_kwDOSJAM6s6fvw8r).
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
    sink_calls: list[dict[str, object]] = []

    async def _commit_dirty(**kwargs: object) -> bool:
        sink_calls.append(kwargs)
        runner.current_head = _TIMED_OUT_RUN_HEAD
        return True

    runner._commit_dirty_worktree = _commit_dirty

    async def _run(**kwargs: object) -> AgentRunResult:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        dirty_sink = kwargs["timeout_rerun_dirty_sink"]
        assert callable(dirty_sink)
        assert await dirty_sink("AGENT_IDLE_TIMEOUT") is True
        floor = kwargs["timeout_rerun_floor_sink"]
        assert isinstance(floor, list)
        floor.append(runner.current_head)
        raise _agent_error()

    runner._run_monitor_agent_with_service_recovery = _run

    with pytest.raises(AgentVerdictExecutionError) as caught:
        await _invoke_item(runner, state=MonitorState())

    assert caught.value.reason_code == "AGENT_CLI_FAILED"
    assert [call["operation_start_head"] for call in sink_calls] == [_ITEM_START_HEAD]
    assert "preserved after agent timeout" in str(sink_calls[0]["message"])
    assert runner.reset_targets == []
    assert runner.current_head == _TIMED_OUT_RUN_HEAD


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


@pytest.mark.unit
async def test_a_rerun_given_up_for_want_of_a_floor_preserves_the_timed_out_commit(
    tmp_path: Path,
) -> None:
    """An unpublishable floor costs the rerun, not the work (PRRT_kwDOSJAM6s6fxp80).

    When no floor can be published the loop gives the rerun up and re-raises the
    watchdog timeout with the sink still empty — and that empty sink is exactly
    why the give-up is safe only if this side holds up its end: the caller leaves
    its rollback floor at the attempt start, so the timeout has to reach the #932
    preserve handler with the timed-out run's self-commit intact rather than be
    rewound to that floor like the provider failure above.
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
        # The run self-committed before its watchdog fired; the loop recovered the
        # service, found no HEAD it could publish, and so never reran the agent.
        runner.current_head = _TIMED_OUT_RUN_HEAD
        sink = kwargs["timeout_rerun_floor_sink"]
        assert isinstance(sink, list)
        assert sink == []
        raise _timeout_error()

    runner._run_monitor_agent_with_service_recovery = _run

    with pytest.raises(AgentVerdictExecutionError) as caught:
        await _invoke_item(runner, state=MonitorState())

    assert caught.value.reason_code == "AGENT_IDLE_TIMEOUT"
    assert caught.value.preserved_head_sha == _TIMED_OUT_RUN_HEAD
    assert runner.reset_targets == []
    assert runner.current_head == _TIMED_OUT_RUN_HEAD
    # One prompt: the agent was never rerun.
    assert runner.prompts == ["ORIGINAL REVIEW PROMPT"]


async def _dirty_sink_verdict_for(
    runner: _VerdictRunner,
    *,
    monkeypatch: pytest.MonkeyPatch | None = None,
    probe_error: Exception | None = None,
    expected_error: type[BaseException] = AgentVerdictExecutionError,
    expected_reason_code: str = "AGENT_IDLE_TIMEOUT",
) -> list[bool]:
    """Answer the loop's ``timeout_rerun_dirty_sink`` gets before it reruns.

    The re-raised timeout then reaches the #932 preserve handler, whose own
    outcome depends on whether the edits are still stranded once its sink has
    tried too (PRRT_kwDOSJAM6s6fwr71) — hence the caller-supplied expectation.
    """
    verdicts: list[bool] = []

    if probe_error is not None:
        assert monkeypatch is not None

        async def _raise(*_args: object, **_kwargs: object) -> str | None:
            raise probe_error

        monkeypatch.setattr(
            comment_verdict, "_read_correction_pr_worthy_residue_fingerprint", _raise
        )

    async def _run(**kwargs: object) -> None:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        dirty_sink = kwargs["timeout_rerun_dirty_sink"]
        assert callable(dirty_sink)
        verdicts.append(await dirty_sink("AGENT_IDLE_TIMEOUT"))
        # The loop re-raises the timeout it recovered when salvage is unconfirmed.
        raise _timeout_error()

    runner._run_monitor_agent_with_service_recovery = _run

    with pytest.raises(expected_error) as caught:
        await _invoke_item(runner, state=MonitorState())

    assert getattr(caught.value, "reason_code", None) == expected_reason_code
    return verdicts


@pytest.mark.unit
async def test_the_dirty_sink_refuses_the_rerun_when_edits_stay_stranded(
    tmp_path: Path,
) -> None:
    """A failed salvage answers "do not rerun", and the edits stay put.

    ``_commit_dirty_worktree`` returns False after a status/add/commit failure
    with the timed-out run's edits still dirty. No SHA floor can cover them, so
    rerunning would let the rollback a provider failure or non-FIXED verdict on
    the rerun performs delete work #932 promised to keep (PRRT_kwDOSJAM6s6fwTyO).

    The re-raised timeout then finds those same edits still stranded after the
    preserve handler's own sink retries, so the item ends on the commit-sink
    failure rather than an ordinary re-queued timeout (PRRT_kwDOSJAM6s6fwr71).
    The edits themselves still stay put either way.
    """
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[_TIMED_OUT_RUN_HEAD],
        dirty_after_attempt=[False],
        stranded_dirty_after_attempt=[True],
    )
    runner.current_head = _ITEM_START_HEAD

    assert await _dirty_sink_verdict_for(
        runner,
        expected_error=comment_verdict.AgentVerdictProtocolError,
        expected_reason_code="REPAIR_DIRTY_COMMIT_FAILED",
    ) == [False]
    assert runner.reset_targets == []


@pytest.mark.unit
async def test_the_dirty_sink_allows_the_rerun_when_there_was_nothing_to_salvage(
    tmp_path: Path,
) -> None:
    """A False sink over a clean worktree is the ordinary case, not a failure.

    ``_commit_dirty_worktree`` returns False whenever there is nothing to commit,
    which is what most timed-out runs leave behind. Treating that as unconfirmed
    salvage would abort every recovery rerun.
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

    assert await _dirty_sink_verdict_for(runner) == [True]


@pytest.mark.unit
async def test_the_dirty_sink_refuses_the_rerun_when_the_residue_probe_is_unreadable(
    tmp_path: Path,
) -> None:
    """An unreadable worktree cannot prove the edits were salvaged."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[_TIMED_OUT_RUN_HEAD],
        dirty_after_attempt=[False],
        stranded_dirty_after_attempt=[True],
        stranded_status_raises=True,
    )
    runner.current_head = _ITEM_START_HEAD

    assert await _dirty_sink_verdict_for(runner) == [False]


@pytest.mark.unit
async def test_the_dirty_sink_refuses_the_rerun_when_the_residue_probe_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising probe fails closed instead of escaping into the recovery loop."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[],
        heads_after_attempt=[_TIMED_OUT_RUN_HEAD],
        dirty_after_attempt=[False],
        stranded_dirty_after_attempt=[True],
    )
    runner.current_head = _ITEM_START_HEAD

    verdicts = await _dirty_sink_verdict_for(
        runner,
        monkeypatch=monkeypatch,
        probe_error=OSError("git status spawn failed"),
    )

    assert verdicts == [False]


@pytest.mark.unit
async def test_a_provider_failure_after_a_recovery_rerun_re_arms_the_item_anchor(
    tmp_path: Path,
) -> None:
    """The preserved commit keeps its evidence anchor when the rerun dies.

    The first timeout was intercepted inside the recovery loop, so the #932
    preserve handler — the one that remembers the item's original start HEAD —
    never ran for it. Raising the rollback floor keeps the commit but not its
    anchor: with no marker the next monitor pass starts the item at the preserved
    HEAD, so the timeout commit falls outside ``item_start_head``..HEAD and an
    honest no-change ``FIXED`` retry is rejected (PRRT_kwDOSJAM6s6fwTyP).
    """
    runner = _recovery_rerun_runner(tmp_path, outcome=_agent_error())
    state = MonitorState()

    with pytest.raises(AgentVerdictExecutionError):
        await _invoke_item(runner, state=state)

    assert peek_item_start_head(state, _ITEM_ID) == _ITEM_START_HEAD


@pytest.mark.unit
async def test_a_protocol_violation_after_a_recovery_rerun_re_arms_the_item_anchor(
    tmp_path: Path,
) -> None:
    """A verdict-less returned run owes the anchor just as a provider failure does."""
    runner = _recovery_rerun_runner(tmp_path, outcome="I had a look at the thread.")
    state = MonitorState()

    with pytest.raises(comment_verdict.AgentVerdictProtocolError):
        await _invoke_item(runner, state=state)

    assert peek_item_start_head(state, _ITEM_ID) == _ITEM_START_HEAD


@pytest.mark.unit
async def test_the_re_armed_recovery_rerun_anchor_carries_the_feedback_body_hash(
    tmp_path: Path,
) -> None:
    """The anchor stays bound to the feedback it was written for.

    An edited comment or a new thread reply keeps the item id but poses different
    feedback, and the entry guard drops an anchor whose body hash no longer
    matches. A re-armed anchor with no binding could never be dropped that way.
    """
    runner = _recovery_rerun_runner(tmp_path, outcome=_agent_error())
    state = MonitorState()

    with pytest.raises(AgentVerdictExecutionError):
        await comment_verdict._invoke_cli_for_verdict_result(
            runner,  # type: ignore[arg-type]
            workspace_id="ws_protocol",
            prompt="ORIGINAL REVIEW PROMPT",
            commit_message=f"fix: address PR review comment {_ITEM_ID}",
            compose_project="awf_ws_protocol",
            compose_file=Path("compose.yml"),
            state=state,
            operation_start_head=_ITEM_START_HEAD,
            evidence_item_id=_ITEM_ID,
            evidence_body_hash="body-hash-1",
        )

    assert peek_item_start_head(state, _ITEM_ID) == _ITEM_START_HEAD
    assert peek_item_start_body_hash(state, _ITEM_ID) == "body-hash-1"


@pytest.mark.unit
async def test_a_verdict_after_a_recovery_rerun_leaves_no_anchor_behind(
    tmp_path: Path,
) -> None:
    """Consume-on-verdict still holds: a finished item arms nothing.

    The rerun answered, so the item is done and no later pass over the same item
    id may inherit an anchor pointing at commits it never made.
    """
    runner = _recovery_rerun_runner(
        tmp_path,
        outcome="AWF-VERDICT: FALSE POSITIVE: the reviewer misread the diff",
    )
    state = MonitorState()

    result = await _invoke_item(runner, state=state)

    assert result.verdict == "false_positive"
    assert item_start_head_state_key(_ITEM_ID) not in state.threads_addressed_ids
