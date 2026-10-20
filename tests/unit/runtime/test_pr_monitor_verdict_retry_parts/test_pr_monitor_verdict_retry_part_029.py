"""Evidence decides, not stdout formatting — Scopes A and B of #1074 (part 029).

Two incidents drove these tests. In ws_6fc04496 (an operator hint), the agent
printed one valid ``AWF-VERDICT: FIXED:`` record and then a test-result follow-up
(F1). In ws_a0f7fcd8 (a comment-repair batch), attempt 0 printed ``FIXED`` before
its commit landed (F2), and the correction attempt then committed the real fix but
printed no record (F3). In both cases the protocol rolled back verified work over
formatting.

These tests drive the real protocol to pin both scopes. Scope A: a ``FIXED``
record followed by extra output is accepted when the attempt's own item-scoped
evidence gate passes. Scope B: a record-less correction inherits attempt 0's
evidence-refused ``FIXED``, which the correction's existing gates then judge. The
cases with no evidence keep today's path into the correction attempt.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict import (
    _FIXED_WITHOUT_EVIDENCE_CORRECTION_CONTEXT,
    _VERDICT_PROTOCOL_CORRECTION_SUFFIX,
    AGENT_FIXED_WITHOUT_EVIDENCE,
    AGENT_VERDICT_PROTOCOL_VIOLATION,
    VerdictResult,
)
from tests.unit.runtime._verdict_retry_fixtures import _invoke, _VerdictRunner
from tests.unit.runtime._verdict_trailing_output_fixtures import F1, F1_REASON, F2, F2_REASON, F3

pytest_plugins = ["tests.unit.runtime._verdict_retry_fixtures"]

_ITEM_START_HEAD = "a" * 40
_FIX_HEAD = "b" * 40
_REVIEWED_PATH = "src/app.py"


async def _invoke_item(runner: _VerdictRunner) -> VerdictResult:
    """A review-thread item: anchored, with the default ``require_fix_evidence``."""
    return await comment_verdict._invoke_cli_for_verdict_result(
        runner,  # type: ignore[arg-type]
        workspace_id="ws_protocol",
        prompt="ORIGINAL REVIEW PROMPT",
        commit_message="fix: review item",
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        operation_start_head=_ITEM_START_HEAD,
        evidence_item_path=_REVIEWED_PATH,
        evidence_item_line=10,
    )


def _runner(
    tmp_path: Path,
    *,
    outputs: list[str],
    heads: list[str],
    dirty: list[bool] | None = None,
    path_touched: bool = True,
    line_touched: bool = True,
) -> _VerdictRunner:
    (tmp_path / "ws_protocol").mkdir()
    runner = _VerdictRunner(
        worktrees_root=tmp_path,
        outputs=list(outputs),
        heads_after_attempt=heads,
        dirty_after_attempt=dirty,
        path_touched=path_touched,
        line_touched=line_touched,
    )
    runner.current_head = _ITEM_START_HEAD
    return runner


# --- Scope A: FIXED with extra output, gated on item-scoped evidence -----------


@pytest.mark.unit
async def test_f1_with_an_item_scoped_commit_is_fixed_on_attempt_zero(tmp_path: Path) -> None:
    """Scenario 1: no correction run, and the commit is kept."""
    runner = _runner(tmp_path, outputs=[F1], heads=[_FIX_HEAD], dirty=[True])

    result = await _invoke_item(runner)

    assert result == VerdictResult(verdict="fix_committed", reason=F1_REASON)
    assert runner.prompts == ["ORIGINAL REVIEW PROMPT"]
    assert runner.reset_targets == []
    assert runner.current_head == _FIX_HEAD


@pytest.mark.unit
async def test_f1_without_line_evidence_still_earns_its_correction(tmp_path: Path) -> None:
    """The tolerance uses each attempt's own gate. Attempt 0 is line-anchored.

    The commit changes the reviewed file away from the anchored line, so attempt 0
    has no evidence and keeps today's correction round. The correction's
    path-level gate (#925 D1) then accepts the same F1.
    """
    runner = _runner(
        tmp_path,
        outputs=[F1, F1],
        heads=[_FIX_HEAD, _FIX_HEAD],
        dirty=[True, False],
        line_touched=False,
    )

    result = await _invoke_item(runner)

    assert result == VerdictResult(verdict="fix_committed", reason=F1_REASON)
    assert len(runner.prompts) == 2
    assert runner.prompts[1].endswith(_VERDICT_PROTOCOL_CORRECTION_SUFFIX)
    assert _FIXED_WITHOUT_EVIDENCE_CORRECTION_CONTEXT not in runner.prompts[1]
    assert runner.reset_targets == []


@pytest.mark.unit
async def test_f1_whose_commit_misses_the_item_is_never_fixed(tmp_path: Path) -> None:
    """A commit with no item-scoped evidence never buys a tolerated FIXED.

    The commit is contentful, so Scope C keeps it and parks the item instead of
    failing the workspace.
    """
    runner = _runner(
        tmp_path,
        outputs=[F1, F1],
        heads=[_FIX_HEAD, _FIX_HEAD],
        dirty=[True, False],
        path_touched=False,
    )

    result = await _invoke_item(runner)

    assert result.verdict == "needs_human"
    assert result.reason is not None
    assert AGENT_VERDICT_PROTOCOL_VIOLATION in result.reason
    assert len(runner.prompts) == 2
    assert runner.reset_targets == []


@pytest.mark.unit
async def test_hint_f1_with_a_contentful_commit_is_accepted(tmp_path: Path) -> None:
    """Scenario 2 (the ws_6fc04496 case): the hint's own commit is the evidence."""
    runner = _runner(tmp_path, outputs=[F1], heads=[_FIX_HEAD], dirty=[True])

    result = await _invoke(runner, require_fix_evidence=False)

    assert result == VerdictResult(verdict="fix_committed", reason=F1_REASON)
    assert runner.prompts == ["ORIGINAL REVIEW PROMPT"]
    assert runner.reset_targets == []
    assert runner.current_head == _FIX_HEAD


@pytest.mark.unit
async def test_hint_f1_without_a_commit_takes_the_protocol_error_path(tmp_path: Path) -> None:
    """Scenario 2, negative: a commit-less hint FIXED must satisfy the strict contract."""
    runner = _runner(tmp_path, outputs=[F1, F1], heads=[_ITEM_START_HEAD, _ITEM_START_HEAD])

    result = await _invoke(runner, require_fix_evidence=False)

    assert len(runner.prompts) == 2
    assert runner.prompts[1].endswith(_VERDICT_PROTOCOL_CORRECTION_SUFFIX)
    assert result.verdict == "needs_human"
    assert result.reason is not None
    assert AGENT_VERDICT_PROTOCOL_VIOLATION in result.reason


@pytest.mark.unit
async def test_a_later_fixed_record_with_evidence_wins_over_an_earlier_defer(
    tmp_path: Path,
) -> None:
    """Scenario 8: multiple records are accepted when the last valid one is FIXED."""
    stdout = "AWF-VERDICT: DEFER: track it\nAWF-VERDICT: FIXED: fixed it after all\nchatter"
    runner = _runner(tmp_path, outputs=[stdout], heads=[_FIX_HEAD], dirty=[True])

    result = await _invoke_item(runner)

    assert result == VerdictResult(verdict="fix_committed", reason="fixed it after all")
    assert len(runner.prompts) == 1


@pytest.mark.unit
async def test_defer_then_false_positive_still_earns_the_correction(tmp_path: Path) -> None:
    """Scenario 8: non-FIXED records stay strict even when a commit exists."""
    runner = _runner(
        tmp_path,
        outputs=[
            "AWF-VERDICT: DEFER: first record\nAWF-VERDICT: FALSE POSITIVE: second record",
            "AWF-VERDICT: FALSE POSITIVE: the reviewer misread the diff",
        ],
        heads=[_ITEM_START_HEAD, _ITEM_START_HEAD],
    )

    result = await _invoke_item(runner)

    assert result == VerdictResult(
        verdict="false_positive",
        reason="the reviewer misread the diff",
    )
    assert len(runner.prompts) == 2
    assert runner.prompts[1].endswith(_VERDICT_PROTOCOL_CORRECTION_SUFFIX)


# --- Scope B: the correction inherits attempt 0's evidence-refused FIXED -------


@pytest.mark.unit
async def test_f2_then_f3_with_the_fix_committed_is_fixed(tmp_path: Path) -> None:
    """Scenario 4 (the ws_a0f7fcd8 case): the correction produced the missing evidence."""
    runner = _runner(
        tmp_path,
        outputs=[F2, F3],
        heads=[_ITEM_START_HEAD, _FIX_HEAD],
        dirty=[False, True],
    )

    result = await _invoke_item(runner)

    assert result == VerdictResult(verdict="fix_committed", reason=F2_REASON)
    assert len(runner.prompts) == 2
    assert _FIXED_WITHOUT_EVIDENCE_CORRECTION_CONTEXT in runner.prompts[1]
    assert runner.reset_targets == []
    assert runner.current_head == _FIX_HEAD


@pytest.mark.unit
async def test_inherited_fixed_outside_the_item_scope_keeps_the_commit_and_escalates(
    tmp_path: Path,
) -> None:
    """Same correction gates as today: contentful but unscoped → keep and escalate."""
    runner = _runner(
        tmp_path,
        outputs=[F2, F3],
        heads=[_ITEM_START_HEAD, _FIX_HEAD],
        dirty=[False, True],
        path_touched=False,
    )

    result = await _invoke_item(runner)

    assert result.verdict == "needs_human"
    assert result.reason is not None
    assert "carries no item-scoped evidence" in result.reason
    assert F2_REASON[:40] in result.reason
    assert runner.reset_targets == []
    assert runner.current_head == _FIX_HEAD


@pytest.mark.unit
async def test_inherited_fixed_with_no_change_at_all_parks_the_item(tmp_path: Path) -> None:
    """Still no evidence after the correction: refused, rolled back, parked (Scope C)."""
    runner = _runner(tmp_path, outputs=[F2, F3], heads=[_ITEM_START_HEAD, _ITEM_START_HEAD])

    result = await _invoke_item(runner)

    assert result.verdict == "needs_human"
    assert result.reason is not None
    assert result.reason.startswith(f"{AGENT_FIXED_WITHOUT_EVIDENCE}:")
    assert len(runner.prompts) == 2


@pytest.mark.unit
async def test_a_decorated_needs_human_on_the_correction_is_never_fixed(tmp_path: Path) -> None:
    """The correction asked for a human, decorated or not: no inherited FIXED.

    The commit carries item-scoped evidence, so inheriting attempt 0's FIXED would
    resolve the thread as fixed. The item parks instead, and the commit is kept.
    """
    runner = _runner(
        tmp_path,
        outputs=[
            F2,
            "**AWF-VERDICT: NEEDS_HUMAN: only partially addressed; a maintainer must decide**",
        ],
        heads=[_ITEM_START_HEAD, _FIX_HEAD],
        dirty=[False, True],
    )

    result = await _invoke_item(runner)

    assert result.verdict == "needs_human"
    assert result.reason is not None
    assert result.reason.startswith(f"{AGENT_VERDICT_PROTOCOL_VIOLATION}:")
    assert runner.reset_targets == []
    assert runner.current_head == _FIX_HEAD


@pytest.mark.unit
@pytest.mark.parametrize("attempt_zero", ["garbled prose with no record", F1])
async def test_only_a_strict_evidence_refused_fixed_is_inherited(
    tmp_path: Path,
    attempt_zero: str,
) -> None:
    """A protocol violation on attempt 0 has nothing to inherit, F1 included.

    The correction's commit is still kept, because Scope C parks a contentful item
    instead of rolling it back. It is never reported as ``fix_committed`` without a
    record.
    """
    runner = _runner(
        tmp_path,
        outputs=[attempt_zero, F3],
        heads=[_ITEM_START_HEAD, _FIX_HEAD],
        dirty=[False, True],
    )

    result = await _invoke_item(runner)

    assert result.verdict == "needs_human"
    assert result.reason is not None
    assert result.reason.startswith(f"{AGENT_VERDICT_PROTOCOL_VIOLATION}:")
    assert _FIXED_WITHOUT_EVIDENCE_CORRECTION_CONTEXT not in runner.prompts[1]
    assert runner.reset_targets == []
    assert runner.current_head == _FIX_HEAD
