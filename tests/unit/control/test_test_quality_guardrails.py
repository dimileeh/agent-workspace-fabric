"""Tests for AWF's static test-quality guardrails."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.control.test_quality_guardrails import scan_test_quality

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "test_quality_guardrails"


def _scan_fixture(name: str) -> list[tuple[str, int]]:
    return [
        (violation.code, violation.line)
        for violation in scan_test_quality([FIXTURES / name])
    ]


@pytest.mark.unit
def test_flags_empty_test_function() -> None:
    assert _scan_fixture("case_empty.py") == [("EMPTY_TEST", 1)]


@pytest.mark.unit
def test_flags_ellipsis_only_test_method() -> None:
    assert _scan_fixture("case_ellipsis_method.py") == [("EMPTY_TEST", 2)]


@pytest.mark.unit
def test_flags_assert_true_and_assert_false() -> None:
    assert _scan_fixture("case_fake_assert.py") == [
        ("FAKE_ASSERT", 2),
        ("FAKE_ASSERT", 6),
    ]


@pytest.mark.unit
def test_flags_unconditional_pytest_skip_only_test() -> None:
    assert _scan_fixture("case_skip_only.py") == [("SKIP_ONLY_TEST", 5)]


@pytest.mark.unit
def test_flags_unconditional_skip_decorator() -> None:
    assert _scan_fixture("case_skip_decorator.py") == [
        ("SKIP_ONLY_TEST", 4),
        ("SKIP_ONLY_TEST", 9),
    ]


@pytest.mark.unit
def test_allows_conditional_skipif_and_guarded_pytest_skip() -> None:
    assert _scan_fixture("case_conditional_skip.py") == []


@pytest.mark.unit
def test_flags_directly_exercised_monkeypatched_behavior() -> None:
    assert _scan_fixture("case_broad_monkeypatch.py") == [
        ("BROAD_MONKEYPATCH", 4),
    ]


@pytest.mark.unit
def test_flags_directly_exercised_monkeypatched_subject_leaf() -> None:
    assert _scan_fixture("case_broad_monkeypatch_leaf.py") == [
        ("BROAD_MONKEYPATCH", 4),
    ]


@pytest.mark.unit
def test_allows_monkeypatch_of_dependency_when_production_entrypoint_is_called() -> None:
    assert _scan_fixture("case_allowed_dependency_monkeypatch.py") == []


@pytest.mark.unit
def test_escape_hatch_requires_specific_rationale() -> None:
    violations = scan_test_quality([FIXTURES / "case_escape_hatch.py"])

    assert [(violation.code, violation.line) for violation in violations] == [
        ("INVALID_ESCAPE_HATCH", 6),
        ("FAKE_ASSERT", 8),
        ("INVALID_ESCAPE_HATCH", 11),
        ("EMPTY_TEST", 12),
    ]
