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
    return [(violation.code, violation.line) for violation in scan_test_quality([FIXTURES / name])]


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


@pytest.mark.unit
def test_scan_empty_path_list_returns_no_violations() -> None:
    assert scan_test_quality([]) == []


@pytest.mark.unit
def test_scan_handles_non_call_skip_decorator_and_pre_test_escape_hatch(
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "test_skip_edges.py"
    test_file.write_text(
        "\n".join(
            [
                "# awf-test-quality: ignore[SKIP_ONLY_TEST] because platform probe exercises import guard",
                "def test_suppressed_skip_only():",
                "    pytest.skip('platform unavailable')",
                "",
                "@pytest.mark.skip",
                "def test_plain_skip_marker():",
                "    assert value == value",
                "",
                "@pytest.mark.skipif(condition=True, reason='always disabled')",
                "def test_keyword_skipif_marker():",
                "    assert value == value",
                "",
            ]
        ),
        encoding="utf-8",
    )

    violations = scan_test_quality([test_file])

    assert [(violation.code, violation.line) for violation in violations] == [
        ("SKIP_ONLY_TEST", 5),
        ("SKIP_ONLY_TEST", 9),
    ]


@pytest.mark.unit
def test_scan_ignores_non_skip_only_unconditional_branch(tmp_path: Path) -> None:
    test_file = tmp_path / "test_skip_branch_edges.py"
    test_file.write_text(
        "\n".join(
            [
                "def test_branch_has_more_than_skip():",
                "    if True:",
                "        pytest.skip('platform unavailable')",
                "        print('diagnostic')",
                "",
            ]
        ),
        encoding="utf-8",
    )

    assert scan_test_quality([test_file]) == []


@pytest.mark.unit
def test_scan_handles_return_none_and_empty_ast_body(tmp_path: Path) -> None:
    test_file = tmp_path / "test_return_none.py"
    test_file.write_text(
        "\n".join(
            [
                "def test_return_none_is_empty():",
                "    return",
                "",
            ]
        ),
        encoding="utf-8",
    )

    violations = scan_test_quality([test_file])

    assert [(violation.code, violation.line) for violation in violations] == [
        ("EMPTY_TEST", 1),
    ]
    assert guardrails._body_without_docstring([]) == []  # noqa: SLF001


@pytest.mark.unit
def test_scan_allows_broad_monkeypatch_when_other_entrypoint_is_exercised(
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "test_monkeypatch_other_entrypoint.py"
    test_file.write_text(
        "\n".join(
            [
                "def test_subject_uses_wrapper(monkeypatch):",
                "    monkeypatch.setattr(module, 'subject', replacement)",
                "    wrapper()",
                "    subject()",
                "",
                "def test_empty_monkeypatch_call(monkeypatch):",
                "    monkeypatch.setattr()",
                "    wrapper()",
                "",
                "def test_one_arg_monkeypatch_call(monkeypatch):",
                "    monkeypatch.setattr(module)",
                "    wrapper()",
                "",
                "def test_non_string_attribute_name(monkeypatch):",
                "    monkeypatch.setattr(module, attribute_name)",
                "    wrapper()",
                "",
            ]
        ),
        encoding="utf-8",
    )

    assert scan_test_quality([test_file]) == []


@pytest.mark.unit
def test_escape_hatch_reports_malformed_unknown_and_prefixed_vague_reasons(
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "test_escape_hatch_edges.py"
    test_file.write_text(
        "\n".join(
            [
                "# awf-test-quality: ignore",
                "def test_malformed_escape():",
                "    pass",
                "",
                "# awf-test-quality: ignore[NOT_A_RULE] because generated fixture needs coverage",
                "def test_unknown_code_escape():",
                "    pass",
                "",
                "# awf-test-quality: ignore[EMPTY_TEST] because TODO after fixture migration",
                "def test_prefixed_vague_reason():",
                "    pass",
                "",
            ]
        ),
        encoding="utf-8",
    )

    violations = scan_test_quality([test_file])

    assert [(violation.code, violation.line) for violation in violations] == [
        ("INVALID_ESCAPE_HATCH", 1),
        ("EMPTY_TEST", 2),
        ("INVALID_ESCAPE_HATCH", 5),
        ("EMPTY_TEST", 6),
        ("INVALID_ESCAPE_HATCH", 9),
        ("EMPTY_TEST", 10),
    ]


@pytest.mark.unit
def test_exclusion_handles_paths_outside_scan_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"

    assert (
        guardrails._is_excluded(  # noqa: SLF001
            outside,
            exclude_globs=("*/outside.py",),
            scan_root=tmp_path / "nested",
        )
        is True
    )
