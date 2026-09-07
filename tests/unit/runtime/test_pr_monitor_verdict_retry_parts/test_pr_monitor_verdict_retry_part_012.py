"""A bad verdict on the re-attempt never deletes preserved work (#934 audit).

#932 keeps a timed-out attempt's commits and remembers the *original*
``item_start_head`` so the re-attempt's FIXED evidence range still starts where
the item started. But that restored anchor was also the rollback target: every
``_rollback_or_classify_failure`` / ``_rollback_unaccepted_protocol_retry_changes``
site reset to it, so any later bad verdict on the re-attempt (malformed twice,
non-FIXED with mutation, no-change FIXED) rewound past the preserved HEAD and
destroyed exactly the commits #932 was written to save.

The anchor and the rollback floor are now separate: the anchor is the original
item start (evidence only), the floor is the re-attempt's own start — the
preserved HEAD. Rollback stops there.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.adapters.base import AgentRunError
from awf.common.commands import CommandResult
from awf.db.enums import AgentRuntime
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict import AgentVerdictExecutionError
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    remember_item_start_head,
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
async def test_reattempt_malformed_twice_rolls_back_only_to_the_preserved_head(
    tmp_path: Path,
) -> None:
    """Two protocol violations terminate the item — without losing #932's commits."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["still malformed", "malformed again"],
        heads_after_attempt=[_REATTEMPT_HEAD, _REATTEMPT_HEAD],
        dirty_after_attempt=[True, True],
    )
    runner.current_head = _PRESERVED_HEAD

    with pytest.raises(comment_verdict.AgentVerdictProtocolError) as caught:
        await _invoke_reattempt(runner, state=_state_after_preserved_timeout())

    assert caught.value.reason_code == comment_verdict.AGENT_VERDICT_PROTOCOL_VIOLATION
    assert runner.reset_targets == [_PRESERVED_HEAD]
    assert runner.current_head == _PRESERVED_HEAD


@pytest.mark.unit
async def test_reattempt_non_fixed_with_mutation_rolls_back_only_to_the_preserved_head(
    tmp_path: Path,
) -> None:
    """The correction mutated then answered non-FIXED: rewind its own work only."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[
            "malformed after editing",
            "AWF-VERDICT: FALSE POSITIVE: nothing to do here",
        ],
        heads_after_attempt=[_REATTEMPT_HEAD, "d" * 40],
        dirty_after_attempt=[True, True],
    )
    runner.current_head = _PRESERVED_HEAD

    with pytest.raises(comment_verdict.AgentVerdictProtocolError) as caught:
        await _invoke_reattempt(runner, state=_state_after_preserved_timeout())

    assert caught.value.reason_code == comment_verdict.AGENT_NON_FIXED_WITH_MUTATION
    assert runner.reset_targets == [_PRESERVED_HEAD]
    assert runner.current_head == _PRESERVED_HEAD


@pytest.mark.unit
async def test_reattempt_first_attempt_non_fixed_rolls_back_only_to_the_preserved_head(
    tmp_path: Path,
) -> None:
    """A straight FALSE POSITIVE is accepted after rolling back to the floor."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FALSE POSITIVE: the reviewer misread the diff"],
        heads_after_attempt=[_REATTEMPT_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _PRESERVED_HEAD

    result = await _invoke_reattempt(runner, state=_state_after_preserved_timeout())

    assert result.verdict == "false_positive"
    assert runner.reset_targets == [_PRESERVED_HEAD]
    assert runner.current_head == _PRESERVED_HEAD


@pytest.mark.unit
async def test_reattempt_provider_failure_rolls_back_only_to_the_preserved_head(
    tmp_path: Path,
) -> None:
    """A non-timeout provider failure still rolls back — but not past the floor."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[_agent_error()],
        heads_after_attempt=[_REATTEMPT_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _PRESERVED_HEAD

    with pytest.raises(AgentVerdictExecutionError) as caught:
        await _invoke_reattempt(runner, state=_state_after_preserved_timeout())

    assert caught.value.reason_code == "AGENT_CLI_FAILED"
    assert runner.reset_targets == [_PRESERVED_HEAD]
    assert runner.current_head == _PRESERVED_HEAD


@pytest.mark.unit
async def test_second_timeout_on_the_reattempt_still_preserves_and_re_anchors(
    tmp_path: Path,
) -> None:
    """Timing out again keeps re-arming the original anchor, never a rollback."""
    (tmp_path / "ws_protocol").mkdir()
    timeout = AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(returncode=124, stdout="", stderr="idle timeout\n"),
        reason_code="AGENT_IDLE_TIMEOUT",
    )
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=[timeout],
        heads_after_attempt=[_REATTEMPT_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _PRESERVED_HEAD

    async def _run(**kwargs: object) -> None:
        runner.prompts.append(str(kwargs["prompt"]))
        runner.attempt += 1
        runner.current_head = _REATTEMPT_HEAD
        runner._persistent_stranded_status_stdout = " M agent_edit.py\n"
        raise timeout

    runner._run_monitor_agent_with_service_recovery = _run
    state = _state_after_preserved_timeout()

    with pytest.raises(AgentVerdictExecutionError) as caught:
        await _invoke_reattempt(runner, state=state)

    assert caught.value.reason_code == "AGENT_IDLE_TIMEOUT"
    assert runner.reset_targets == []
    assert runner.current_head == _REATTEMPT_HEAD


@pytest.mark.unit
async def test_floor_falls_back_to_the_live_head_when_the_caller_passes_none(
    tmp_path: Path,
) -> None:
    """No ``operation_start_head`` + a restored anchor: the live HEAD is the floor.

    The restored anchor must never become the floor by default — the live HEAD
    already contains the preserved commits, so it is the honest rollback target.
    """
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: FALSE POSITIVE: the reviewer misread the diff"],
        heads_after_attempt=[_REATTEMPT_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _PRESERVED_HEAD

    result = await comment_verdict._invoke_cli_for_verdict_result(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        prompt="ORIGINAL REVIEW PROMPT",
        commit_message=f"fix: address PR review comment {_ITEM_ID}",
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        state=_state_after_preserved_timeout(),
        operation_start_head=None,
        evidence_item_id=_ITEM_ID,
    )

    assert result.verdict == "false_positive"
    assert runner.reset_targets == [_PRESERVED_HEAD]


@pytest.mark.unit
async def test_floor_recovers_from_the_attempt_start_probe_when_the_pre_loop_read_fails(
    tmp_path: Path,
) -> None:
    """A transient pre-loop HEAD failure still leaves a usable rollback floor."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: DEFER: needs a product decision"],
        heads_after_attempt=[_REATTEMPT_HEAD],
        dirty_after_attempt=[True],
        # The pre-loop read fails; the in-loop attempt-start probe succeeds.
        rev_parse_sequence=[None],
    )
    runner.current_head = _PRESERVED_HEAD

    result = await comment_verdict._invoke_cli_for_verdict_result(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        prompt="ORIGINAL REVIEW PROMPT",
        commit_message=f"fix: address PR review comment {_ITEM_ID}",
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        state=_state_after_preserved_timeout(),
        operation_start_head=None,
        evidence_item_id=_ITEM_ID,
    )

    assert result.verdict == "defer"
    assert runner.reset_targets == [_PRESERVED_HEAD]


@pytest.mark.unit
async def test_without_a_preserved_marker_the_floor_is_the_item_start(
    tmp_path: Path,
) -> None:
    """Ordinary items are unchanged: anchor and rollback floor are the same commit."""
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=["AWF-VERDICT: DEFER: needs a product decision"],
        heads_after_attempt=[_REATTEMPT_HEAD],
        dirty_after_attempt=[True],
    )
    runner.current_head = _PRESERVED_HEAD

    result = await _invoke_reattempt(runner, state=MonitorState())

    assert result.verdict == "defer"
    # No marker to restore, so ``operation_start_head`` is both anchor and floor.
    assert runner.reset_targets == [_PRESERVED_HEAD]
