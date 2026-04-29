"""Tests for AWF's static test-quality guardrails."""

from __future__ import annotations

import tokenize
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import awf.control.test_quality_guardrails as guardrails
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
def test_flags_unconditional_branch_pytest_skip_only_test() -> None:
    assert _scan_fixture("case_skip_only_unconditional_branch.py") == [
        ("SKIP_ONLY_TEST", 6),
    ]


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


@pytest.mark.unit
def test_escape_hatch_allows_concise_specific_rationale() -> None:
    assert _scan_fixture("case_escape_hatch_concise_specific.py") == []


@pytest.mark.unit
def test_escape_hatch_only_suppresses_adjacent_violation(tmp_path: Path) -> None:
    test_file = tmp_path / "test_escape_hatch_scope.py"
    test_file.write_text(
        "\n".join(
            [
                "def test_multiple_fake_asserts():",
                "    # awf-test-quality: ignore[FAKE_ASSERT] because legacy sentinel assertion",
                "    assert True",
                "    assert True",
                "",
            ]
        ),
        encoding="utf-8",
    )

    violations = scan_test_quality([test_file])

    assert [(violation.code, violation.line) for violation in violations] == [
        ("FAKE_ASSERT", 4),
    ]


@pytest.mark.unit
def test_exclude_globs_are_evaluated_from_repository_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(REPO_ROOT / "tests")

    violations = scan_test_quality(
        [FIXTURES],
        exclude_globs=("tests/fixtures/**",),
    )

    assert violations == []


@pytest.mark.unit
def test_scan_skips_unreadable_and_invalid_python_files(tmp_path: Path) -> None:
    invalid_encoding = tmp_path / "invalid_encoding.py"
    invalid_encoding.write_bytes(b"\xff")
    invalid_syntax = tmp_path / "invalid_syntax.py"
    invalid_syntax.write_text("def test_broken(:\n    pass\n", encoding="utf-8")
    valid_empty = tmp_path / "valid_empty.py"
    valid_empty.write_text("def test_placeholder():\n    pass\n", encoding="utf-8")

    violations = scan_test_quality([invalid_encoding, invalid_syntax, valid_empty])

    assert [(violation.path.name, violation.code, violation.line) for violation in violations] == [
        ("valid_empty.py", "EMPTY_TEST", 1),
    ]


@pytest.mark.unit
def test_scan_skips_os_errors_and_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    unreadable = tmp_path / "unreadable.py"
    unreadable.write_text("def test_unreadable():\n    pass\n", encoding="utf-8")
    valid_empty = tmp_path / "valid_empty.py"
    valid_empty.write_text("def test_placeholder():\n    pass\n", encoding="utf-8")
    original_read_text = Path.read_text

    def fail_for_unreadable(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == unreadable:
            raise OSError("cannot read test source")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_for_unreadable)

    violations = scan_test_quality([unreadable, valid_empty])

    assert [(violation.path.name, violation.code, violation.line) for violation in violations] == [
        ("valid_empty.py", "EMPTY_TEST", 1),
    ]


@pytest.mark.unit
def test_scan_treats_tokenize_errors_as_missing_escape_hatches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    valid_empty = tmp_path / "valid_empty.py"
    valid_empty.write_text("def test_placeholder():\n    pass\n", encoding="utf-8")

    def fail_generate_tokens(
        _readline: Callable[[], str],
    ) -> Iterator[tokenize.TokenInfo]:
        raise tokenize.TokenError("cannot tokenize test source", (1, 0))
        yield from ()

    monkeypatch.setattr(guardrails.tokenize, "generate_tokens", fail_generate_tokens)

    violations = scan_test_quality([valid_empty])

    assert [(violation.path.name, violation.code, violation.line) for violation in violations] == [
        ("valid_empty.py", "EMPTY_TEST", 1),
    ]
