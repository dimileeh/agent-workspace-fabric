"""fix_ci_prompt applied-operator-directive section (#1256)."""

from __future__ import annotations

import pytest

from awf.common.prompt_evidence import EVIDENCE_QUOTE_PREFIX
from awf.runtime.monitor_prompts import fix_ci_prompt
from awf.runtime.pr_monitor import CheckFailure
from awf.runtime.pr_monitor_runner.applied_operator_directives import AppliedOperatorDirective

_FAILURES = (
    CheckFailure(
        name="coverage-gate",
        conclusion="FAILURE",
        log_excerpt="FAILED tests/test_x.py::test_y - AssertionError",
        run_id="42",
    ),
)


@pytest.mark.unit
def test_empty_applied_directives_keeps_prompt_byte_identical() -> None:
    baseline = fix_ci_prompt(pr_number=238, repo_slug="dimileeh/awf", failures=_FAILURES)
    with_default = fix_ci_prompt(
        pr_number=238,
        repo_slug="dimileeh/awf",
        failures=_FAILURES,
        applied_operator_directives=(),
    )
    assert with_default == baseline
    assert "already applied on this PR" not in baseline
    assert "AWF-VERDICT: NEEDS_HUMAN:" not in baseline


@pytest.mark.unit
def test_applied_directives_section_is_newest_first_and_enveloped() -> None:
    older = AppliedOperatorDirective(
        directive="Remove the experimental block entirely.",
        operation_id="op-older",
        applied_at="2026-11-17T17:56:00+00:00",
        head_sha="b34a78bf86",
    )
    newer = AppliedOperatorDirective(
        directive="Defer that review finding; add no code.",
        operation_id="op-newer",
        applied_at="2026-11-17T18:05:00+00:00",
        head_sha="b34a78bf86",
    )
    prompt = fix_ci_prompt(
        pr_number=1260,
        repo_slug="dimileeh/awf",
        failures=_FAILURES,
        # Callers (load_applied_operator_directives) pass newest first.
        applied_operator_directives=(newer, older),
    )

    section_marker = "these operator decisions were already applied on this PR"
    assert section_marker in prompt.lower() or (
        "operator decisions were already applied on this PR" in prompt
    )
    assert "outrank a failing check" in prompt
    assert "do not undo them" in prompt.lower() or "Do not undo them" in prompt
    assert "do not re-add" in prompt.lower() or "Do not re-add" in prompt
    assert "newer wins" in prompt.lower() or "newer one wins" in prompt.lower()
    assert "AWF-VERDICT: NEEDS_HUMAN:" in prompt
    assert "conflicts with operator decision" in prompt

    details_idx = prompt.index("Per-check failure details")
    section_idx = prompt.lower().index("operator decisions were already applied")
    assert section_idx < details_idx

    newer_idx = prompt.index("op-newer")
    older_idx = prompt.index("op-older")
    assert newer_idx < older_idx

    assert "source_kind: operator_decision" in prompt
    assert "source_id: op-newer" in prompt
    assert "applied_at: 2026-11-17T18:05:00+00:00" in prompt
    assert "head_sha: b34a78bf86" in prompt
    assert "### UNTRUSTED EXTERNAL EVIDENCE" in prompt

    assert [
        line for line in prompt.splitlines() if "Remove the experimental block entirely." in line
    ] == [f"{EVIDENCE_QUOTE_PREFIX}Remove the experimental block entirely."]
    assert [
        line for line in prompt.splitlines() if "Defer that review finding; add no code." in line
    ] == [f"{EVIDENCE_QUOTE_PREFIX}Defer that review finding; add no code."]
