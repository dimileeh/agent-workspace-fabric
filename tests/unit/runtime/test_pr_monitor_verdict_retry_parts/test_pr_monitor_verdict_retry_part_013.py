"""The #932 retry anchor survives an attempt that dies before a verdict (#934 audit).

``consume_item_start_head`` clears the preserved-timeout marker at the *top* of
the item, before the fallible pre-launch ownership/mirror repair, the
provider-recovery gate, and the agent run itself. Every failure exit from there
aborts the fix cycle without marking the item addressed, so the item is attempted
again — but with the marker already gone that next attempt anchored at the
preserved HEAD, pushing the timed-out attempt's commits *out* of its own ``FIXED``
evidence range and turning an honest FIXED into ``AGENT_FIXED_WITHOUT_EVIDENCE``.

The anchor is now re-armed on every failure exit; a marker written since (a fresh
timeout on this very attempt) is newer and wins.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict import AgentVerdictExecutionError
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    item_start_head_state_key,
    remember_item_start_head,
)
from awf.runtime.pr_monitor_runner.types import (
    _MonitorAgentRuntimeOwnershipRepairFailedError,
)
from tests.unit.runtime._verdict_retry_fixtures import _agent_error, _VerdictRunner

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_PRESERVED_HEAD = "b" * 40
_REATTEMPT_HEAD = "c" * 40
_ITEM_ID = "issue:5558086911"


def _state_after_preserved_timeout() -> MonitorState:
    """The state #932 leaves behind: the original item start remembered."""
    state = MonitorState()
    remember_item_start_head(state, _ITEM_ID, _ITEM_START_HEAD)
    return state


async def _invoke_reattempt(
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
        # What the next monitor pass passes in: the live (preserved) HEAD.
        operation_start_head=_PRESERVED_HEAD,
        evidence_item_id=_ITEM_ID,
    )


@pytest.mark.unit
async def test_pre_launch_ownership_failure_re_arms_the_anchor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ownership repair fails before the agent launches: the anchor is put back."""
    (tmp_path / "ws_protocol").mkdir()

    async def _ownership_repair_fails(**_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(
        comment_verdict,
        "repair_agent_runtime_ownership",
        _ownership_repair_fails,
    )
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FIXED: never reached"],
        heads_after_attempt=[_REATTEMPT_HEAD],
    )
    runner.current_head = _PRESERVED_HEAD
    state = _state_after_preserved_timeout()

    with pytest.raises(_MonitorAgentRuntimeOwnershipRepairFailedError):
        await _invoke_reattempt(runner, state=state)

    assert state.threads_addressed_ids[item_start_head_state_key(_ITEM_ID)] == _ITEM_START_HEAD


@pytest.mark.unit
async def test_provider_failure_on_the_reattempt_re_arms_the_anchor(
    tmp_path: Path,
) -> None:
    """A non-timeout provider failure keeps the item retryable — and its anchor."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[_agent_error()],
        heads_after_attempt=[_REATTEMPT_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _PRESERVED_HEAD
    state = _state_after_preserved_timeout()

    with pytest.raises(AgentVerdictExecutionError) as caught:
        await _invoke_reattempt(runner, state=state)

    assert caught.value.reason_code == "AGENT_CLI_FAILED"
    # The preserved commits survive (the floor is the preserved HEAD), so the
    # next attempt must still measure FIXED from the original item start.
    assert runner.current_head == _PRESERVED_HEAD
    assert state.threads_addressed_ids[item_start_head_state_key(_ITEM_ID)] == _ITEM_START_HEAD


@pytest.mark.unit
async def test_protocol_violation_on_the_reattempt_re_arms_the_anchor(
    tmp_path: Path,
) -> None:
    """Two malformed answers end this attempt; the item's next pass keeps the anchor."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["still malformed", "malformed again"],
        heads_after_attempt=[_REATTEMPT_HEAD, _REATTEMPT_HEAD],
        dirty_after_attempt=[True, True],
    )
    runner.current_head = _PRESERVED_HEAD
    state = _state_after_preserved_timeout()

    with pytest.raises(comment_verdict.AgentVerdictProtocolError):
        await _invoke_reattempt(runner, state=state)

    assert state.threads_addressed_ids[item_start_head_state_key(_ITEM_ID)] == _ITEM_START_HEAD


@pytest.mark.unit
async def test_an_accepted_verdict_still_consumes_the_anchor(tmp_path: Path) -> None:
    """Only failures re-arm: a returned verdict finishes the item and clears it."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FALSE POSITIVE: the reviewer misread the diff"],
        heads_after_attempt=[_REATTEMPT_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _PRESERVED_HEAD
    state = _state_after_preserved_timeout()

    result = await _invoke_reattempt(runner, state=state)

    assert result.verdict == "false_positive"
    assert item_start_head_state_key(_ITEM_ID) not in state.threads_addressed_ids


@pytest.mark.unit
async def test_a_fresh_timeout_marker_is_not_overwritten_by_the_re_arm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The preserve path's own marker wins over the re-armed one.

    A second timeout re-writes the marker on its way out. The re-arm must not
    clobber that newer value with the one this attempt consumed on entry.
    """
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[_agent_error()],
        heads_after_attempt=[_REATTEMPT_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _PRESERVED_HEAD
    state = _state_after_preserved_timeout()

    async def _preserve_then_raise(_runner: object, **_kwargs: object) -> None:
        remember_item_start_head(state, _ITEM_ID, _REATTEMPT_HEAD)
        raise AgentVerdictExecutionError(reason_code="AGENT_IDLE_TIMEOUT")

    monkeypatch.setattr(comment_verdict, "handle_agent_run_error", _preserve_then_raise)

    with pytest.raises(AgentVerdictExecutionError):
        await _invoke_reattempt(runner, state=state)

    assert state.threads_addressed_ids[item_start_head_state_key(_ITEM_ID)] == _REATTEMPT_HEAD


@pytest.mark.unit
async def test_an_item_without_a_marker_gains_none_on_failure(tmp_path: Path) -> None:
    """Ordinary items are unchanged: nothing to re-arm, nothing written."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[_agent_error()],
        heads_after_attempt=[_REATTEMPT_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _PRESERVED_HEAD
    state = MonitorState()

    with pytest.raises(AgentVerdictExecutionError):
        await _invoke_reattempt(runner, state=state)

    assert item_start_head_state_key(_ITEM_ID) not in state.threads_addressed_ids
