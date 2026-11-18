"""Persisted applied-operator-directive records for CI repair (#1256)."""

from __future__ import annotations

import pytest

from awf.runtime.monitor_state_keys import (
    _applied_operator_directives_state_key,
    _ci_repair_operator_decision_conflict_key_prefix,
)
from awf.runtime.pr_monitor import CheckFailure, MonitorState, OperatorHint
from awf.runtime.pr_monitor_runner.applied_operator_directives import (
    APPLIED_OPERATOR_FORGET_DECISIONS_MARKER,
    AppliedOperatorDirective,
    append_applied_operator_directive,
    ci_repair_operator_decision_conflict_reason,
    load_applied_operator_directives,
    mark_ci_repair_operator_decision_conflict,
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


@pytest.mark.unit
def test_applied_directives_lead_binds_code_decisions_not_one_offs() -> None:
    """Lead text: code decisions bind; one-offs do not become standing rules."""
    section = render_applied_operator_directives_section(
        (
            AppliedOperatorDirective(
                directive="Remove the experimental block entirely.",
                operation_id="op-1",
                applied_at="2026-11-18T00:00:00+00:00",
                head_sha="abc",
            ),
        )
    )
    lower = section.lower()
    assert "bind only what they decided about the code now on this branch" in lower
    assert "do not re-add what they removed" in lower
    assert "do not revert what they changed" in lower
    assert "one-off instructions" in lower
    assert "wait" in lower and "re-run" in lower
    assert "applied to that run only" in lower
    assert "do not restrict this repair" in lower
    assert "newer wins" in lower
    assert "undoing such a code decision" in lower
    assert "AWF-VERDICT: NEEDS_HUMAN:" in section
    assert "conflicts with operator decision" in section
    # Standing-rule / blanket-outrank wording must stay gone.
    assert "outrank a failing check" not in lower
    assert "do not undo them" not in lower
    assert "do not work around them" not in lower


@pytest.mark.unit
def test_forget_decisions_marker_clears_records_and_conflict_parks() -> None:
    """Exact AWF-FORGET-DECISIONS line clears history; remainder may be re-recorded."""
    state = MonitorState()
    append_applied_operator_directive(
        state,
        AppliedOperatorDirective(
            directive="Keep the experimental API.",
            operation_id="op-old",
            applied_at="2026-11-18T00:00:00+00:00",
            head_sha="sha-old",
        ),
    )
    failures = (
        CheckFailure(name="coverage-gate", conclusion="FAILURE", log_excerpt="x", run_id="1"),
    )
    mark_ci_repair_operator_decision_conflict(
        state,
        head_sha="sha-old",
        failures=failures,
        reason="coverage-gate conflicts with operator decision op-old: blocked",
    )
    assert load_applied_operator_directives(state)
    assert ci_repair_operator_decision_conflict_reason(state, head_sha="sha-old", failures=failures)
    assert any(
        key.startswith(_ci_repair_operator_decision_conflict_key_prefix())
        for key in state.threads_addressed_ids
    )

    hint = OperatorHint(
        reason="withdraw prior decisions",
        directive=(
            f"{APPLIED_OPERATOR_FORGET_DECISIONS_MARKER}\nPrefer the simpler public helper instead."
        ),
        operation_id="op-forget",
        reason_code="OPERATOR_GUIDE",
    )
    record_applied_operator_directive_from_hint(
        state,
        hint=hint,
        head_sha="sha-new",
        applied_text=hint.directive,
    )

    records = load_applied_operator_directives(state)
    assert len(records) == 1
    assert records[0].operation_id == "op-forget"
    assert records[0].directive == "Prefer the simpler public helper instead."
    assert APPLIED_OPERATOR_FORGET_DECISIONS_MARKER not in records[0].directive
    assert (
        ci_repair_operator_decision_conflict_reason(state, head_sha="sha-old", failures=failures)
        is None
    )
    assert not any(
        key.startswith(_ci_repair_operator_decision_conflict_key_prefix())
        for key in state.threads_addressed_ids
    )


@pytest.mark.unit
def test_forget_decisions_marker_alone_clears_without_recording() -> None:
    state = MonitorState()
    append_applied_operator_directive(
        state,
        AppliedOperatorDirective(
            directive="Prior decision.",
            operation_id="op-prior",
            applied_at="2026-11-18T00:00:00+00:00",
            head_sha="sha",
        ),
    )
    failures = (CheckFailure(name="lint", conclusion="FAILURE", log_excerpt="y", run_id="9"),)
    mark_ci_repair_operator_decision_conflict(
        state,
        head_sha="sha",
        failures=failures,
        reason="lint conflicts with operator decision op-prior: blocked",
    )

    hint = OperatorHint(
        reason="forget only",
        directive=APPLIED_OPERATOR_FORGET_DECISIONS_MARKER,
        operation_id="op-clear",
        reason_code="OPERATOR_GUIDE",
    )
    record_applied_operator_directive_from_hint(
        state,
        hint=hint,
        head_sha="sha2",
        applied_text=hint.directive,
    )

    assert load_applied_operator_directives(state) == ()
    assert (
        ci_repair_operator_decision_conflict_reason(state, head_sha="sha", failures=failures)
        is None
    )


@pytest.mark.unit
def test_forget_marker_requires_exact_line_not_substring() -> None:
    """Only a line that is exactly the marker triggers withdrawal."""
    state = MonitorState()
    append_applied_operator_directive(
        state,
        AppliedOperatorDirective(
            directive="Keep me.",
            operation_id="op-keep",
            applied_at="2026-11-18T00:00:00+00:00",
            head_sha="sha",
        ),
    )
    hint = OperatorHint(
        reason="mention marker without exact line",
        directive="Do not emit AWF-FORGET-DECISIONS unless withdrawing.",
        operation_id="op-mention",
        reason_code="OPERATOR_GUIDE",
    )
    record_applied_operator_directive_from_hint(
        state,
        hint=hint,
        head_sha="sha2",
        applied_text=hint.directive,
    )
    records = load_applied_operator_directives(state)
    assert [r.operation_id for r in records] == ["op-mention", "op-keep"]
    assert "Do not emit AWF-FORGET-DECISIONS unless withdrawing." in records[0].directive
