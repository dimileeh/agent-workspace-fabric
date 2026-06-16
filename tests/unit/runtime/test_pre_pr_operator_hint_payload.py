"""Focused coverage for ``pre_pr_operator_hint_from_payload``.

The pre-PR ``blocked`` resume path reconstructs an ``OperatorHint`` from the
standalone ``Workspace.pending_operator_hint`` JSON column (rather than the
monitor-state map). These tests pin the malformed-payload guards and the
status-coercion fallback that protect that reconstruction.
"""

from __future__ import annotations

import pytest

from awf.runtime.operator_hints import pre_pr_operator_hint_from_payload


@pytest.mark.unit
@pytest.mark.parametrize("payload", [None, "not-a-dict", 42, ["reason"]])
def test_returns_none_for_non_dict_payload(payload: object) -> None:
    assert pre_pr_operator_hint_from_payload(payload) is None


@pytest.mark.unit
@pytest.mark.parametrize("reason", [None, "", "   ", 123])
def test_returns_none_when_reason_missing_or_blank(reason: object) -> None:
    # A hint without a usable directive reason cannot be reconstructed.
    assert pre_pr_operator_hint_from_payload({"reason": reason}) is None


@pytest.mark.unit
def test_unknown_status_is_coerced_to_pending() -> None:
    # A malformed/absent status must not propagate verbatim — it falls back to
    # ``pending`` so the resume path treats the hint as actionable.
    hint = pre_pr_operator_hint_from_payload({"reason": "revert the change", "status": "bogus"})
    assert hint is not None
    assert hint.status == "pending"
    assert hint.reason == "revert the change"


@pytest.mark.unit
@pytest.mark.parametrize("status", ["pending", "needs_human", "agent_failed"])
def test_known_status_is_preserved(status: str) -> None:
    hint = pre_pr_operator_hint_from_payload({"reason": "r", "status": status})
    assert hint is not None
    assert hint.status == status
