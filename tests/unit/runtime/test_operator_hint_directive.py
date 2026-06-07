"""Tests for the OperatorHint ``directive`` field round-trip (issue #447).

The ``guide`` operator control reuses the existing OperatorHint engine but
carries a first-class ``directive`` (the agent instruction) distinct from the
audit ``reason``. These tests pin the round-trip through persisted monitor
state and the status-transition helpers, plus the remonitor regression that a
hint without a directive serializes byte-identically to today.
"""

from __future__ import annotations

import json

import pytest

from awf.runtime.operator_hints import (
    OPERATOR_HINT_STATE_KEY,
    build_pending_operator_hint_payload,
    mark_operator_hint_agent_failed,
    mark_operator_hint_needs_human,
    operator_hint_from_threads,
    persist_operator_hint,
)
from awf.runtime.pr_monitor import MonitorState, OperatorHint


@pytest.mark.unit
def test_persist_round_trips_directive_distinct_from_reason() -> None:
    hint = OperatorHint(
        reason="operator guidance recorded",
        directive="implement the forge-neutral fix, do not defer",
        operation_id="op_guide",
        requested_at="2026-06-07T12:00:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )
    threads: dict[str, str] = {}
    persist_operator_hint(threads, hint)

    persisted = json.loads(threads[OPERATOR_HINT_STATE_KEY])
    assert persisted["directive"] == "implement the forge-neutral fix, do not defer"
    assert persisted["reason"] == "operator guidance recorded"

    restored = operator_hint_from_threads(threads)
    assert restored is not None
    assert restored.directive == "implement the forge-neutral fix, do not defer"
    assert restored.reason == "operator guidance recorded"
    assert restored.reason_code == "OPERATOR_GUIDE"


@pytest.mark.unit
def test_persist_without_directive_is_byte_identical_to_remonitor_today() -> None:
    hint = OperatorHint(
        reason="worker restarted",
        operation_id="op_remonitor",
        requested_at="2026-06-07T12:00:00+00:00",
    )
    threads: dict[str, str] = {}
    persist_operator_hint(threads, hint)

    persisted = json.loads(threads[OPERATOR_HINT_STATE_KEY])
    assert "directive" not in persisted
    assert persisted == {
        "operation_id": "op_remonitor",
        "reason": "worker restarted",
        "reason_code": "OPERATOR_REMONITOR",
        "requested_at": "2026-06-07T12:00:00+00:00",
        "status": "pending",
    }
    restored = operator_hint_from_threads(threads)
    assert restored is not None
    assert restored.directive is None


@pytest.mark.unit
def test_whitespace_only_directive_is_treated_as_absent() -> None:
    # A malformed/legacy payload could persist a blank directive. Since the
    # prompt path prefers directive over reason, a whitespace-only directive
    # must be read back as absent so the agent falls back to reason.
    threads = {
        OPERATOR_HINT_STATE_KEY: json.dumps(
            {
                "reason": "operator guidance recorded",
                "directive": "   ",
                "operation_id": "op_blank",
                "status": "pending",
            }
        )
    }

    restored = operator_hint_from_threads(threads)

    assert restored is not None
    assert restored.directive is None
    assert restored.reason == "operator guidance recorded"


@pytest.mark.unit
def test_build_pending_payload_includes_directive_only_when_set() -> None:
    with_directive = build_pending_operator_hint_payload(
        OperatorHint(reason="audit", directive="do the thing", operation_id="op1")
    )
    assert with_directive["directive"] == "do the thing"

    without_directive = build_pending_operator_hint_payload(
        OperatorHint(reason="audit", operation_id="op2")
    )
    assert "directive" not in without_directive


@pytest.mark.unit
def test_mark_needs_human_preserves_directive() -> None:
    hint = OperatorHint(
        reason="audit",
        directive="implement, do not defer",
        operation_id="op_guide",
    )
    state = MonitorState(pending_operator_hint=hint)

    mark_operator_hint_needs_human(state, "agent could not proceed")

    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "needs_human"
    assert state.pending_operator_hint.directive == "implement, do not defer"


@pytest.mark.unit
def test_mark_agent_failed_preserves_directive() -> None:
    hint = OperatorHint(
        reason="audit",
        directive="implement, do not defer",
        operation_id="op_guide",
    )
    state = MonitorState(pending_operator_hint=hint)

    mark_operator_hint_agent_failed(state, "agent crashed")

    assert state.pending_operator_hint is not None
    assert state.pending_operator_hint.status == "agent_failed"
    assert state.pending_operator_hint.directive == "implement, do not defer"
