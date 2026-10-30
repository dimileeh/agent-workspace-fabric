"""Explicit decisions are distinct from success, incomplete work and bad output."""

import json
from pathlib import Path

import pytest

from awf.runtime.planning import (
    build_conformance_prompt,
    conformance_requires_awf_validation,
    parse_conformance_report,
    satisfied_report_for_ci_delegated_validation,
)


def decision_payload(**overrides: object) -> str:
    return json.dumps(
        {
            "status": "needs_human",
            "summary": "Pinned external dependency needs approval.",
            "decision_request": "Approve updating the pinned dependency, or retain the old contract?",
            "gaps": [{"kind": "implementation", "detail": "Dependency contract is incompatible."}],
            **overrides,
        }
    )


@pytest.mark.unit
def test_explicit_decision_is_neither_iteration_nor_success() -> None:
    report = parse_conformance_report(decision_payload())
    assert report.status.value == "needs_human"
    assert not report.satisfied
    assert report.decision_request.startswith("Approve")
    assert report.reason_code == "PLAN_CONFORMANCE_NEEDS_HUMAN"


@pytest.mark.unit
@pytest.mark.parametrize("field", ["summary", "decision_request"])
@pytest.mark.parametrize("bad", [None, "", "  ", 42, True, [], {}, "x" * 1001])
def test_invalid_decision_is_report_error_not_attention(field: str, bad: object) -> None:
    report = parse_conformance_report(decision_payload(**{field: bad}))
    assert report.status.value == "needs_iteration"
    assert report.reason_code == "PLAN_CONFORMANCE_REPORT_INVALID"


@pytest.mark.unit
@pytest.mark.parametrize("status", ["NEEDS_HUMAN", "blocked", "needs-human", "needs_iteration"])
def test_no_new_aliases_or_prose_inference(status: str) -> None:
    report = parse_conformance_report(decision_payload(status=status))
    assert report.status.value == "needs_iteration"


@pytest.mark.unit
def test_decision_cannot_be_coerced_to_validation_handoff_or_ci_success() -> None:
    report = parse_conformance_report(
        decision_payload(
            gaps=[{"kind": "awf_validation_evidence", "detail": "Operator decision required."}],
            reason_code="CONFORMANCE_REQUIRES_AWF_VALIDATION",
        )
    )
    assert not conformance_requires_awf_validation(report, [Path("implementation.py")])
    assert satisfied_report_for_ci_delegated_validation(report) is None


@pytest.mark.unit
def test_prompt_distinguishes_authority_from_agent_invented_constraints() -> None:
    prompt = build_conformance_prompt(
        task_prompt="Implement within the authorized scope",
        plan_path=Path("plan.md"),
        report_path=Path("report.json"),
        iteration=0,
    )
    assert "needs_human" in prompt
    assert "decision_request" in prompt
    assert "self-imposed" in prompt
    assert "in-scope alternatives" in prompt


@pytest.mark.unit
def test_decision_reason_catalog_explains_directive_not_grant() -> None:
    from awf.service.doctor.reasons import reason_text_for_code

    reason = reason_text_for_code("PLAN_CONFORMANCE_NEEDS_HUMAN")
    assert reason is not None
    assert "--directive" in reason.related_command
    assert "grant alone" in reason.action
