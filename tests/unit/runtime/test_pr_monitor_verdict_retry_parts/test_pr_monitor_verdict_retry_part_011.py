"""An agent timeout never deletes the agent's work (issue #932, part 2).

Observed on ws_84fddb4a98c94f7b8d6aa0d3 / PR #922: the agent committed
``259b258a9`` during an operator-hint run, the blind idle watchdog fired at 60
minutes, and the ``except AgentRunError`` handler rolled the commit away before
parking the monitor at ``NotifyHuman`` — an hour of work destroyed by a
*provider* failure, not by a bad verdict.

``AGENT_IDLE_TIMEOUT`` / ``AGENT_TIMEOUT`` now take a preserve path instead:
sink the uncommitted item-scoped edits through the existing dirty-worktree sink,
keep the item's commits, and record ``agent_failed`` (which re-queues the item)
with a reason naming the preserved HEAD. The original ``item_start_head`` is
persisted for the item so the re-attempt's FIXED evidence range still starts at
the original item start and the preserved commits count as this item's own work
under the #925/#928/#931 correction rules.

Rollback after a **bad verdict** (malformed, non-FIXED with mutation, no
evidence with no change) and after any non-timeout provider failure is
deliberately unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from awf.adapters.base import AgentRunError
from awf.common.commands import CommandResult
from awf.db.enums import AgentRuntime
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner import comment_verdict_timeout_preserve as timeout_preserve
from awf.runtime.pr_monitor_runner.comment_verdict import AgentVerdictExecutionError
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    item_start_head_state_key,
)
from awf.runtime.pr_monitor_runner.types import (
    ProviderRecoveryRetryError,
    _MonitorPolicyBlockedError,
)
from tests.unit.runtime._verdict_retry_fixtures import _agent_error, _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_PRESERVED_HEAD = "b" * 40
_ITEM_ID = "issue:5558086911"
_TIMEOUT_REASON_CODES = ("AGENT_IDLE_TIMEOUT", "AGENT_TIMEOUT")


def _timeout_error(reason_code: str) -> AgentRunError:
    return AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(
            returncode=124,
            stdout="",
            stderr="command idle timeout after 3600s without output or worktree activity\n",
        ),
        reason_code=reason_code,
    )


def _commit_then_fail(runner: _VerdictRunner, exc: AgentRunError) -> None:
    """Model an agent that self-commits and dirties the tree, then dies."""

    async def _run(**kwargs: object) -> None:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = _PRESERVED_HEAD
        runner._persistent_stranded_status_stdout = " M agent_edit.py\n"
        raise exc

    runner._run_monitor_agent_with_service_recovery = _run


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
    item_id: str | None = _ITEM_ID,
    operation_start_head: str = _ITEM_START_HEAD,
) -> comment_verdict.VerdictResult:
    return await comment_verdict._invoke_cli_for_verdict_result(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        prompt="ORIGINAL REVIEW PROMPT",
        commit_message=f"fix: address PR review comment {_ITEM_ID}",
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        state=state,
        operation_start_head=operation_start_head,
        evidence_item_id=item_id,
    )


@pytest.mark.unit
@pytest.mark.parametrize("reason_code", _TIMEOUT_REASON_CODES)
async def test_timeout_preserves_commits_and_sinks_dirty_edits(
    tmp_path: Path,
    reason_code: str,
) -> None:
    """No reset, the sink runs once, and the reason names the preserved HEAD."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[_timeout_error(reason_code)],
        heads_after_attempt=[_PRESERVED_HEAD],
        dirty_after_attempt=[True],
    )
    _commit_then_fail(runner, _timeout_error(reason_code))
    sink_calls = _record_sink(runner)
    state = MonitorState()

    with (
        structlog.testing.capture_logs() as captured,
        pytest.raises(AgentVerdictExecutionError) as caught,
    ):
        await _invoke_item(runner, state=state)

    assert caught.value.reason_code == reason_code
    assert caught.value.preserved_head_sha == _PRESERVED_HEAD
    assert caught.value.reason is not None
    assert _PRESERVED_HEAD in caught.value.reason
    assert reason_code in caught.value.reason
    # The whole point: HEAD is never reset back to the item start.
    assert runner.reset_targets == []
    assert runner.current_head == _PRESERVED_HEAD
    # Uncommitted item-scoped edits go through the existing dirty-worktree sink,
    # anchored at the item start so the sink stays item-scoped.
    assert len(sink_calls) == 1
    assert "preserved after agent timeout" in str(sink_calls[0]["message"])
    assert sink_calls[0]["operation_start_head"] == _ITEM_START_HEAD
    preserved_events = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_timeout_work_preserved"
    ]
    assert len(preserved_events) == 1
    assert preserved_events[0]["reason_code"] == reason_code
    assert preserved_events[0]["preserved_head"] == _PRESERVED_HEAD
    assert preserved_events[0]["item_start_head"] == _ITEM_START_HEAD
    assert preserved_events[0]["dirty_changes_committed"] is True


@pytest.mark.unit
async def test_timeout_persists_the_original_item_start_head(tmp_path: Path) -> None:
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[_timeout_error("AGENT_IDLE_TIMEOUT")],
        heads_after_attempt=[_PRESERVED_HEAD],
        dirty_after_attempt=[True],
    )
    _commit_then_fail(runner, _timeout_error("AGENT_IDLE_TIMEOUT"))
    state = MonitorState()

    with pytest.raises(AgentVerdictExecutionError):
        await _invoke_item(runner, state=state)

    assert state.threads_addressed_ids[item_start_head_state_key(_ITEM_ID)] == _ITEM_START_HEAD


@pytest.mark.unit
async def test_reattempt_anchors_evidence_at_the_original_item_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The re-queued item measures FIXED over the *original* range.

    The next monitor pass computes ``operation_start_head`` from the live HEAD,
    which is now the preserved commit — so without the marker the preserved work
    would sit *before* the evidence range and the honest FIXED would be rejected.
    """
    (tmp_path / "ws_protocol").mkdir()
    state = MonitorState()

    timed_out = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[_timeout_error("AGENT_IDLE_TIMEOUT")],
        heads_after_attempt=[_PRESERVED_HEAD],
        dirty_after_attempt=[True],
    )
    _commit_then_fail(timed_out, _timeout_error("AGENT_IDLE_TIMEOUT"))
    with pytest.raises(AgentVerdictExecutionError):
        await _invoke_item(timed_out, state=state)

    evidence_calls: list[dict[str, object]] = []
    real_evidence = comment_verdict._item_fix_evidence

    async def _probe(runner: object, **kwargs: object) -> bool:
        evidence_calls.append(dict(kwargs))
        return await real_evidence(runner, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(comment_verdict, "_item_fix_evidence", _probe)

    retried = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: kept the work the timed-out attempt committed"],
        heads_after_attempt=[_PRESERVED_HEAD],
        dirty_after_attempt=[False],
    )
    retried.current_head = _PRESERVED_HEAD

    result = await _invoke_item(
        retried,
        state=state,
        # What the next pass would naturally pass in: the live (preserved) HEAD.
        operation_start_head=_PRESERVED_HEAD,
    )

    assert result.verdict == "fix_committed"
    assert evidence_calls
    assert evidence_calls[0]["item_start_head"] == _ITEM_START_HEAD
    # Consumed on read: a later independent pass over the same item must not
    # anchor at this now-ancient HEAD.
    assert item_start_head_state_key(_ITEM_ID) not in state.threads_addressed_ids


@pytest.mark.unit
async def test_dirty_sink_failure_still_keeps_the_preserved_commits(tmp_path: Path) -> None:
    """A failing sink must not escalate into a rollback of the item's commits."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[_timeout_error("AGENT_TIMEOUT")],
        heads_after_attempt=[_PRESERVED_HEAD],
        dirty_after_attempt=[True],
    )
    _commit_then_fail(runner, _timeout_error("AGENT_TIMEOUT"))

    async def _blocked_sink(**_kwargs: object) -> bool:
        raise _MonitorPolicyBlockedError("supply-chain policy blocked the sink")

    runner._commit_dirty_worktree = _blocked_sink

    with (
        structlog.testing.capture_logs() as captured,
        pytest.raises(AgentVerdictExecutionError) as caught,
    ):
        await _invoke_item(runner, state=MonitorState())

    assert caught.value.reason_code == "AGENT_TIMEOUT"
    assert runner.reset_targets == []
    assert runner.current_head == _PRESERVED_HEAD
    sink_failures = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_timeout_dirty_sink_failed"
    ]
    assert len(sink_failures) == 1
    assert sink_failures[0]["exc_type"] == "_MonitorPolicyBlockedError"
    assert sink_failures[0]["reason_code"] == "AGENT_TIMEOUT"


@pytest.mark.unit
async def test_provider_recovery_escalation_still_escalates_without_rolling_back(
    tmp_path: Path,
) -> None:
    """A fallback escalation keeps short-circuiting the cycle — but keeps the work.

    ``_handle_provider_agent_run_error`` raises ``ProviderRecoveryRetryError`` to
    move the workspace onto the fallback provider. That must still propagate (the
    monitor's in-place fallback contract), and it costs nothing now: the work was
    already sunk and the marker written before the call, and nothing between here
    and the monitor loop rolls the worktree back.
    """
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[_timeout_error("AGENT_IDLE_TIMEOUT")],
        heads_after_attempt=[_PRESERVED_HEAD],
        dirty_after_attempt=[True],
        provider_error_action=ProviderRecoveryRetryError(),
    )
    _commit_then_fail(runner, _timeout_error("AGENT_IDLE_TIMEOUT"))
    state = MonitorState()

    with pytest.raises(ProviderRecoveryRetryError):
        await _invoke_item(runner, state=state)

    assert runner.reset_targets == []
    assert runner.current_head == _PRESERVED_HEAD
    # The evidence anchor survives the escalation, so the fallback provider's
    # attempt still measures FIXED from the original item start.
    assert state.threads_addressed_ids[item_start_head_state_key(_ITEM_ID)] == _ITEM_START_HEAD


@pytest.mark.unit
async def test_timeout_without_an_item_id_still_preserves_work(tmp_path: Path) -> None:
    """No item id (and no state) means no marker — but still no rollback."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[_timeout_error("AGENT_IDLE_TIMEOUT")],
        heads_after_attempt=[_PRESERVED_HEAD],
        dirty_after_attempt=[True],
    )
    _commit_then_fail(runner, _timeout_error("AGENT_IDLE_TIMEOUT"))

    with pytest.raises(AgentVerdictExecutionError) as caught:
        await _invoke_item(runner, state=None, item_id=None)

    assert caught.value.reason_code == "AGENT_IDLE_TIMEOUT"
    assert runner.reset_targets == []


@pytest.mark.unit
async def test_non_timeout_provider_failure_still_rolls_back(tmp_path: Path) -> None:
    """Regression guard: only timeouts take the preserve path."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[_agent_error()],
        heads_after_attempt=[_PRESERVED_HEAD],
        dirty_after_attempt=[True],
    )
    _commit_then_fail(runner, _agent_error())
    state = MonitorState()

    with pytest.raises(AgentVerdictExecutionError) as caught:
        await _invoke_item(runner, state=state)

    assert caught.value.reason_code == "AGENT_CLI_FAILED"
    assert caught.value.preserved_head_sha is None
    assert runner.reset_targets == [_ITEM_START_HEAD]
    assert runner.current_head == _ITEM_START_HEAD
    assert item_start_head_state_key(_ITEM_ID) not in state.threads_addressed_ids


@pytest.mark.unit
async def test_non_fixed_with_mutation_rollback_is_unchanged(tmp_path: Path) -> None:
    """Bad-verdict rollback is out of scope for #932 and must still fire."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "malformed after editing",
            "AWF-VERDICT: FALSE POSITIVE: nothing to do here",
        ],
        heads_after_attempt=[_PRESERVED_HEAD, "c" * 40],
        dirty_after_attempt=[True, True],
    )

    with pytest.raises(comment_verdict.AgentVerdictProtocolError) as caught:
        await _invoke_item(runner, state=MonitorState())

    assert caught.value.reason_code == comment_verdict.AGENT_NON_FIXED_WITH_MUTATION
    assert runner.reset_targets == [_ITEM_START_HEAD]


@pytest.mark.unit
async def test_timeout_with_the_sink_disabled_skips_it_but_still_preserves(
    tmp_path: Path,
) -> None:
    """``commit_dirty_changes=False`` callers keep their no-sink contract."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[_timeout_error("AGENT_IDLE_TIMEOUT")],
        heads_after_attempt=[_PRESERVED_HEAD],
        dirty_after_attempt=[True],
    )
    _commit_then_fail(runner, _timeout_error("AGENT_IDLE_TIMEOUT"))
    sink_calls = _record_sink(runner)

    with pytest.raises(AgentVerdictExecutionError) as caught:
        await comment_verdict._invoke_cli_for_verdict_result(
            runner,  # type: ignore[arg-type]
            workspace_id="ws_protocol",
            prompt="ORIGINAL REVIEW PROMPT",
            commit_message="fix: address operator hint",
            compose_project="awf_ws_protocol",
            compose_file=Path("compose.yml"),
            operation_start_head=_ITEM_START_HEAD,
            commit_dirty_changes=False,
        )

    assert caught.value.reason_code == "AGENT_IDLE_TIMEOUT"
    assert sink_calls == []
    assert runner.reset_targets == []


@pytest.mark.unit
async def test_preserved_head_falls_back_when_the_worktree_is_gone(tmp_path: Path) -> None:
    """A vanished worktree cannot be probed; the item start is the honest answer."""

    class _NoProbeRunner:
        async def _rev_parse_head(self, _worktree_path: Path) -> str:
            raise AssertionError("a missing worktree must not be probed")

    assert (
        await timeout_preserve._preserved_head_sha(
            _NoProbeRunner(),  # type: ignore[arg-type]
            worktree_path=tmp_path / "never_provisioned",
            rev_parse_head=None,
            fallback=_ITEM_START_HEAD,
        )
        == _ITEM_START_HEAD
    )


@pytest.mark.unit
@pytest.mark.parametrize("error", [OSError("git spawn failed"), TimeoutError("git stalled")])
async def test_preserved_head_probe_failure_falls_back_and_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()

    async def _raise(*_args: object, **_kwargs: object) -> str:
        raise error

    monkeypatch.setattr(timeout_preserve, "read_protocol_attempt_start_head", _raise)

    with structlog.testing.capture_logs() as captured:
        preserved = await timeout_preserve._preserved_head_sha(
            object(),  # type: ignore[arg-type]
            worktree_path=worktree,
            rev_parse_head=None,
            fallback=_ITEM_START_HEAD,
        )

    assert preserved == _ITEM_START_HEAD
    assert any(
        entry.get("event") == "monitor.agent_verdict_timeout_preserved_head_probe_failed"
        for entry in captured
    )


@pytest.mark.unit
async def test_preserved_head_falls_back_when_the_probe_returns_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "ws_protocol"
    worktree.mkdir()

    async def _unreadable(*_args: object, **_kwargs: object) -> str | None:
        return None

    monkeypatch.setattr(timeout_preserve, "read_protocol_attempt_start_head", _unreadable)

    assert (
        await timeout_preserve._preserved_head_sha(
            object(),  # type: ignore[arg-type]
            worktree_path=worktree,
            rev_parse_head=None,
            fallback=None,
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("preserved_head", "item_start_head", "expected_fragments"),
    [
        (_PRESERVED_HEAD, _ITEM_START_HEAD, (_PRESERVED_HEAD, _ITEM_START_HEAD, "original item")),
        (_PRESERVED_HEAD, None, (_PRESERVED_HEAD, "from that state")),
        (None, _ITEM_START_HEAD, ("no commit could be read",)),
    ],
)
def test_preserved_work_reason_wording(
    preserved_head: str | None,
    item_start_head: str | None,
    expected_fragments: tuple[str, ...],
) -> None:
    reason = timeout_preserve._preserved_work_reason(
        reason_code="AGENT_IDLE_TIMEOUT",
        preserved_head=preserved_head,
        item_start_head=item_start_head,
    )

    assert "AGENT_IDLE_TIMEOUT" in reason
    for fragment in expected_fragments:
        assert fragment in reason
