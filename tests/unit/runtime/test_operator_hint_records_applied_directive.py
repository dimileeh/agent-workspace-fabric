"""AddressOperatorHint finalize records applied directives (#1256)."""

from __future__ import annotations

import pytest

from awf.runtime.operator_hints import (
    mark_operator_hint_agent_failed,
    mark_operator_hint_needs_human,
)
from awf.runtime.pr_monitor import MonitorState, OperatorHint
from awf.runtime.pr_monitor_runner.applied_operator_directives import (
    load_applied_operator_directives,
)
from awf.runtime.pr_monitor_runner.operator_hint_retirement import (
    _finalize_processed_operator_hint,
)


@pytest.mark.unit
def test_finalize_fixed_with_directive_records_applied_directive() -> None:
    hint = OperatorHint(
        reason="operator guide",
        directive="Remove the experimental block entirely.",
        operation_id="op-guide-1",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)

    _finalize_processed_operator_hint(
        state,
        hint=hint,
        acted_feedback_text=hint.directive,
        head_sha="b34a78bf86abcdef",
    )

    records = load_applied_operator_directives(state)
    assert len(records) == 1
    assert records[0].directive == "Remove the experimental block entirely."
    assert records[0].operation_id == "op-guide-1"
    assert records[0].head_sha == "b34a78bf86abcdef"
    assert records[0].applied_at


@pytest.mark.unit
def test_remonitor_without_directive_does_not_record() -> None:
    hint = OperatorHint(
        reason="please remonitor",
        directive=None,
        operation_id="op-remonitor",
        reason_code="OPERATOR_REMONITOR",
    )
    state = MonitorState(pending_operator_hint=hint)

    _finalize_processed_operator_hint(
        state,
        hint=hint,
        acted_feedback_text=None,
        head_sha="abc123",
    )

    assert load_applied_operator_directives(state) == ()


@pytest.mark.unit
def test_ruling_only_empty_acted_text_does_not_record_unapplied_instructions() -> None:
    """Ruling-only paths pass empty acted text; do not store the rest of the guide.

    A guide can mix an ``AWF-RULING`` line with other instructions. Park /
    preserved-attempt disposition skips the agent and passes
    ``acted_feedback_text=""`` because only the ruling ran. Recording the full
    ``hint.directive`` would tell later CI repair that unexecuted instructions
    were already applied and outrank its checks (PRRT_kwDOSJAM6s64xWll).
    """
    ruling = "AWF-RULING PRRT_kwDOSJAM6s64xWll FALSE_POSITIVE"
    extra = "Also delete the coverage workflow and skip the lint gate."
    hint = OperatorHint(
        reason="operator guide",
        directive=f"{ruling}\n{extra}",
        operation_id="op-ruling-only",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)

    _finalize_processed_operator_hint(
        state,
        hint=hint,
        acted_feedback_text="",
        head_sha="b34a78bf86abcdef",
    )

    assert load_applied_operator_directives(state) == ()


@pytest.mark.unit
def test_ruling_only_records_applied_ruling_excerpt_when_provided() -> None:
    """When finalize is given only the applied ruling text, store that — not the rest."""
    ruling = "AWF-RULING PRRT_kwDOSJAM6s64xWll FALSE_POSITIVE"
    extra = "Also delete the coverage workflow and skip the lint gate."
    hint = OperatorHint(
        reason="operator guide",
        directive=f"{ruling}\n{extra}",
        operation_id="op-ruling-excerpt",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)

    _finalize_processed_operator_hint(
        state,
        hint=hint,
        acted_feedback_text=ruling,
        head_sha="b34a78bf86abcdef",
    )

    records = load_applied_operator_directives(state)
    assert len(records) == 1
    assert records[0].directive == ruling
    assert extra not in records[0].directive


@pytest.mark.unit
def test_needs_human_hint_does_not_record() -> None:
    hint = OperatorHint(
        reason="operator guide",
        directive="Do not touch coverage workflows.",
        operation_id="op-needs-human",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)

    mark_operator_hint_needs_human(state, "agent asked for a human")

    assert load_applied_operator_directives(state) == ()
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"


@pytest.mark.unit
def test_agent_failed_hint_does_not_record() -> None:
    hint = OperatorHint(
        reason="operator guide",
        directive="Defer the review finding.",
        operation_id="op-agent-failed",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=hint)

    mark_operator_hint_agent_failed(state, "adapter crashed")

    assert load_applied_operator_directives(state) == ()
    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "agent_failed"


@pytest.mark.unit
def test_replaced_before_run_does_not_record() -> None:
    """A pending hint that never finalized leaves no applied-directive record."""
    first = OperatorHint(
        reason="first guide",
        directive="First directive that was replaced.",
        operation_id="op-first",
        reason_code="OPERATOR_GUIDE",
    )
    replacement = OperatorHint(
        reason="replacement guide",
        directive="Second directive that wins the queue.",
        operation_id="op-second",
        reason_code="OPERATOR_GUIDE",
    )
    state = MonitorState(pending_operator_hint=first)
    # Operator replacement: pending hint swapped before AddressOperatorHint ran.
    state.pending_operator_hint = replacement

    assert load_applied_operator_directives(state) == ()
