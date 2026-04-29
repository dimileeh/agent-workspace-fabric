"""Self-check AWF's checked-in tests for false-green placeholders."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.control.test_quality_guardrails import scan_test_quality

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.unit
def test_awf_test_suite_has_no_test_quality_guardrail_violations() -> None:
    violations = scan_test_quality(
        [REPO_ROOT / "tests" / "unit", REPO_ROOT / "tests" / "integration"],
    )

    assert violations == [], "\n".join(
        f"{violation.path}:{violation.line}: {violation.code}: {violation.message}"
        for violation in violations
    )
