"""Persisted applied-operator-directive records for CI repair (#1256)."""

from __future__ import annotations

import pytest

from awf.runtime.monitor_state_keys import _applied_operator_directives_state_key
from awf.runtime.pr_monitor import MonitorState, OperatorHint
from awf.runtime.pr_monitor_runner.applied_operator_directives import (
    AppliedOperatorDirective,
    append_applied_operator_directive,
    load_applied_operator_directives,
    needs_human_verdict_reason,
    record_applied_operator_directive_from_hint,
    render_applied_operator_directives_section,
)


@pytest.mark.unit
def test_missing_state_key_loads_empty() -> None:
    state = MonitorState(threads_addressed_ids={"T1": "fix_committed"})
    assert load_applied_operator_directives(state) == ()
    assert _applied_operator_directives_state_key() not in state.threads_addressed_ids


@pytest.mark.unit
def test_append_caps_at_five_and_round_trips_through_persisted_store() -> None:
    state = MonitorState()
    for index in range(6):
        append_applied_operator_directive(
            state,
            AppliedOperatorDirective(
                directive=f"directive-{index}",
                operation_id=f"op-{index}",
                applied_at=f"2026-11-18T0{index}:00:00+00:00",
                head_sha=f"sha{index}",
            ),
        )

    loaded = load_applied_operator_directives(state)
    assert len(loaded) == 5
    assert [record.operation_id for record in loaded] == [
        "op-5",
        "op-4",
        "op-3",
        "op-2",
        "op-1",
    ]
    assert "directive-0" not in {record.directive for record in loaded}

    # Survive a resume-shaped reload from the persisted map alone.
    resumed = MonitorState(
        threads_addressed_ids=dict(state.threads_addressed_ids),
    )
    assert load_applied_operator_directives(resumed) == loaded


@pytest.mark.unit
def test_corrupt_or_non_list_payload_loads_empty() -> None:
    key = _applied_operator_directives_state_key()
    assert (
        load_applied_operator_directives(MonitorState(threads_addressed_ids={key: "not-json"}))
        == ()
    )
    assert (
        load_applied_operator_directives(
            MonitorState(threads_addressed_ids={key: '{"directive":"x"}'})
        )
        == ()
    )
    assert load_applied_operator_directives(
        MonitorState(
            threads_addressed_ids={
                key: (
                    '[{"directive":"","operation_id":"op","applied_at":"t","head_sha":"h"},'
                    '{"directive":"keep","operation_id":"op2","applied_at":"t","head_sha":"h"},'
                    '"skip-me"]'
                )
            }
        )
    ) == (
        AppliedOperatorDirective(
            directive="keep",
            operation_id="op2",
            applied_at="t",
            head_sha="h",
        ),
    )


@pytest.mark.unit
def test_whitespace_only_directive_is_not_recorded() -> None:
    state = MonitorState(
        pending_operator_hint=OperatorHint(
            reason="guide",
            directive="   ",
            operation_id="op-blank",
            reason_code="OPERATOR_GUIDE",
        )
    )
    record_applied_operator_directive_from_hint(
        state,
        hint=state.pending_operator_hint,
        head_sha="abc",
    )
    assert load_applied_operator_directives(state) == ()
    assert render_applied_operator_directives_section(()) == ""


@pytest.mark.unit
def test_needs_human_verdict_reason_uses_strict_then_last_valid() -> None:
    assert (
        needs_human_verdict_reason(
            "AWF-VERDICT: NEEDS_HUMAN: coverage-gate conflicts with operator decision op-1: blocked\n"
        )
        == "coverage-gate conflicts with operator decision op-1: blocked"
    )
    assert (
        needs_human_verdict_reason(
            "noise\n"
            "AWF-VERDICT: NEEDS_HUMAN: review-bot conflicts with operator decision op-2: undo\n"
            "trailing chatter\n"
        )
        == "review-bot conflicts with operator decision op-2: undo"
    )
    assert needs_human_verdict_reason("AWF-VERDICT: FIXED: all good\n") is None
    assert needs_human_verdict_reason("") is None
