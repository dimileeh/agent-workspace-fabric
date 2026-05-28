"""Focused CI failure evidence extraction edge tests."""

from __future__ import annotations

import shlex

import pytest

from awf.runtime import ci_failure_evidence


@pytest.mark.unit
def test_ci_failure_evidence_handles_empty_logs_with_warning() -> None:
    evidence = ci_failure_evidence.extract_ci_failure_evidence(
        "",
        check_name="python-full-coverage",
    )

    assert evidence.evidence_warnings == (
        "GitHub Actions log unavailable for failed check python-full-coverage.",
    )
    assert evidence.failing_commands == ()
    assert ci_failure_evidence.redact_ci_log("") == ""  # noqa: SLF001


@pytest.mark.unit
def test_ci_failure_evidence_ignores_run_steps_without_supported_commands() -> None:
    assert (
        ci_failure_evidence._extract_command_from_line(  # noqa: SLF001
            "job\tRun echo hello\t2026-05-15T00:00:00Z"
        )
        is None
    )
    evidence = ci_failure_evidence.extract_ci_failure_evidence(
        "python-full-coverage\tFull coverage\tRun echo hello\n",
        check_name="python-full-coverage",
    )

    assert evidence.failing_commands == ()
    assert evidence.suggested_repro_commands == ()


@pytest.mark.unit
def test_ci_failure_evidence_suggests_generic_repro_when_pytest_command_is_unavailable() -> None:
    evidence = ci_failure_evidence.extract_ci_failure_evidence(
        "\n".join(
            [
                "FAILED tests/unit/test_example.py::test_failure - AssertionError",
                "E   AssertionError: token SECRET=super-secret",
                "Error: Process completed with exit code 1",
                "fatal: repository not found",
            ]
        ),
        check_name="unit",
    )

    assert (
        ci_failure_evidence._pytest_repro_command(  # noqa: SLF001
            ["pytest 'unterminated"]
        )
        is None
    )
    assert evidence.failing_commands == ()
    assert evidence.test_node_ids == ("tests/unit/test_example.py::test_failure",)
    assert evidence.suggested_repro_commands == (
        "uv run --python 3.12 --extra dev pytest tests/unit/test_example.py::test_failure -q",
    )
    assert any("AssertionError" in snippet for snippet in evidence.assertion_snippets)
    assert "fatal: repository not found" in evidence.error_summaries


@pytest.mark.unit
def test_ci_failure_evidence_preserves_github_error_annotations() -> None:
    timestamped_annotation = (
        "2026-05-15T00:00:00Z ::error title=Coverage below required threshold::"
        "Combined line+branch coverage 98.87% is below required 99.00%."
    )

    evidence = ci_failure_evidence.extract_ci_failure_evidence(
        "\n".join(
            [
                "Coverage totals: combined=98.87% line=99.40% branch=97.15%",
                "::error title=Coverage below required threshold::"
                "Combined line+branch coverage 98.87% is below required 99.00%.",
                timestamped_annotation,
                "Error: Process completed with exit code 1.",
            ]
        ),
        check_name="python-full-coverage",
    )

    assert (
        "::error title=Coverage below required threshold::"
        "Combined line+branch coverage 98.87% is below required 99.00%."
    ) in evidence.error_summaries
    assert timestamped_annotation in evidence.error_summaries
    assert "Error: Process completed with exit code 1." in evidence.error_summaries


@pytest.mark.unit
def test_ci_failure_evidence_preserves_prefixed_github_error_annotations() -> None:
    prefixed_annotation = (
        "2026-05-15T00:00:00Z python-full-coverage "
        "::error title=Coverage below required threshold::"
        "Combined line+branch coverage 98.87% is below required 99.00%."
    )

    evidence = ci_failure_evidence.extract_ci_failure_evidence(
        "\n".join(
            [
                "Coverage totals: combined=98.87% line=99.40% branch=97.15%",
                prefixed_annotation,
            ]
        ),
        check_name="python-full-coverage",
    )

    assert prefixed_annotation in evidence.error_summaries


@pytest.mark.unit
def test_ci_failure_evidence_rejects_glued_prefix_before_pytest_node() -> None:
    valid_node_id = "tests/unit/test_example.py::test_valid_failure"

    evidence = ci_failure_evidence.extract_ci_failure_evidence(
        "\n".join(
            [
                "FAILED:tests/unit/test_example.py::test_glued_prefix - AssertionError",
                f"FAILED {valid_node_id} - AssertionError",
            ]
        ),
        check_name="unit",
    )

    assert evidence.test_node_ids == (valid_node_id,)
    assert evidence.suggested_repro_commands == (
        f"uv run --python 3.12 --extra dev pytest {valid_node_id} -q",
    )


@pytest.mark.unit
def test_ci_failure_repro_command_skips_non_pytest_commands() -> None:
    assert (
        ci_failure_evidence._pytest_repro_command(  # noqa: SLF001
            ["npm test", "uv run pytest tests/unit/test_example.py"]
        )
        == "uv run pytest"
    )


@pytest.mark.unit
def test_ci_failure_evidence_dedupes_blank_and_duplicate_values() -> None:
    assert ci_failure_evidence._dedupe(["", " pytest  -q ", "pytest -q"]) == [  # noqa: SLF001
        "pytest -q"
    ]
    assert ci_failure_evidence._dedupe_preserving_values(  # noqa: SLF001
        ["", " node ", "node"]
    ) == ["node"]


@pytest.mark.unit
def test_ci_failure_evidence_skips_run_step_without_known_command_marker() -> None:
    evidence = ci_failure_evidence.extract_ci_failure_evidence(
        "\t".join(["2026-05-15T00:00:00Z", "Run echo hello", "shell: bash"]),
        check_name="unit",
    )

    assert evidence.failing_commands == ()


@pytest.mark.unit
def test_ci_failure_evidence_falls_back_to_configured_pytest_for_node_ids_without_command() -> None:
    node_id = "tests/unit/runtime/test_prompt.py::test_one"

    evidence = ci_failure_evidence.extract_ci_failure_evidence(
        f"FAILED {node_id} - AssertionError: boom\n",
        check_name="provider-neutral-check",
        pytest_fallback_commands=("uv run --python 3.12 --extra dev pytest --cov=awf",),
    )

    assert evidence.failing_commands == ()
    assert evidence.test_node_ids == (node_id,)
    assert evidence.suggested_repro_commands == (
        f"uv run --python 3.12 --extra dev pytest {node_id} -q",
    )


@pytest.mark.unit
def test_ci_failure_evidence_preserves_shell_setup_in_fallback_pytest_command() -> None:
    node_id = "tests/test_api.py::test_handles_request"

    evidence = ci_failure_evidence.extract_ci_failure_evidence(
        f"FAILED {node_id} - AssertionError: boom\n",
        check_name="api",
        pytest_fallback_commands=("cd services/api && pytest --maxfail=1",),
    )

    assert evidence.failing_commands == ()
    assert evidence.test_node_ids == (node_id,)
    assert evidence.suggested_repro_commands == (f"cd services/api && pytest {node_id} -q",)


@pytest.mark.unit
def test_ci_failure_evidence_fallback_bounds_and_quotes_multiple_node_ids() -> None:
    node_ids = [
        "tests/unit/a/test_one.py::test_alpha",
        "tests/unit/runtime/test_prompt.py::test_handles[bad value; echo owned]",
        "tests/unit/c/test_three.py::TestThree::test_gamma",
        "tests/unit/d/test_four.py::test_delta",
        "tests/unit/e/test_five.py::test_epsilon",
        "tests/unit/f/test_six.py::test_zeta",
    ]

    evidence = ci_failure_evidence.extract_ci_failure_evidence(
        "\n".join(f"FAILED {node_id} - AssertionError: boom" for node_id in node_ids),
        check_name="provider-neutral-check",
    )

    selected = node_ids[: ci_failure_evidence._MAX_REPRO_NODES]  # noqa: SLF001
    quoted = " ".join(shlex.quote(node_id) for node_id in selected)
    assert evidence.test_node_ids == tuple(node_ids)
    assert evidence.suggested_repro_commands == (
        f"uv run --python 3.12 --extra dev pytest {quoted} -q",
    )
    assert node_ids[-1] not in evidence.suggested_repro_commands[0]


@pytest.mark.unit
def test_ci_failure_evidence_bounds_and_quotes_multiple_node_ids_with_known_command() -> None:
    node_ids = [
        "tests/unit/a/test_one.py::test_alpha",
        "tests/unit/runtime/test_prompt.py::test_handles[bad value; echo owned]",
        "tests/unit/c/test_three.py::TestThree::test_gamma",
        "tests/unit/d/test_four.py::test_delta",
        "tests/unit/e/test_five.py::test_epsilon",
        "tests/unit/f/test_six.py::test_zeta",
    ]

    evidence = ci_failure_evidence.extract_ci_failure_evidence(
        "\n".join(
            [
                "tests\tRun tests\tpython -m pytest tests/unit",
                *(f"FAILED {node_id} - AssertionError: boom" for node_id in node_ids),
            ]
        ),
        check_name="provider-neutral-check",
    )

    selected = node_ids[: ci_failure_evidence._MAX_REPRO_NODES]  # noqa: SLF001
    quoted = " ".join(shlex.quote(node_id) for node_id in selected)
    assert evidence.test_node_ids == tuple(node_ids)
    assert evidence.suggested_repro_commands == (f"python -m pytest {quoted} -q",)
    assert node_ids[-1] not in evidence.suggested_repro_commands[0]


@pytest.mark.unit
def test_ci_failure_evidence_scans_noisy_malformed_pytest_lines_in_bounded_time() -> None:
    valid_node_id = "tests/unit/runtime/test_prompt.py::test_keeps_working"
    malformed_node = "tests/unit/runtime/test_prompt.py::" + ("case" * 2000)
    noisy_line = f"Backend CI\tRun tests\tFAILED {malformed_node} " + " ".join(
        f"package_{index}-1.0.0" for index in range(400)
    )

    evidence = ci_failure_evidence.extract_ci_failure_evidence(
        "\n".join(
            [
                noisy_line,
                f"FAILED {valid_node_id} - AssertionError: boom",
            ]
        ),
        check_name="Backend CI",
    )

    assert evidence.test_node_ids == (valid_node_id,)
    assert evidence.suggested_repro_commands == (
        f"uv run --python 3.12 --extra dev pytest {valid_node_id} -q",
    )


@pytest.mark.unit
def test_ci_failure_evidence_linear_scanner_preserves_bracketed_parameters() -> None:
    node_ids = [
        "tests/unit/runtime/test_prompt.py::test_handles[bad value; echo owned]",
        "pkg/tests/test_api.py::TestApi::test_nested[a - b]",
        "tests/unit/runtime/test_prompt.py::test_handles[(a)]",
        "pkg/tests/test_api.py::TestApi::test_with_angle<id><x>",
    ]
    line = " and ".join(f"`{node_id}`" for node_id in node_ids)

    assert ci_failure_evidence._pytest_node_candidates(line) == node_ids  # noqa: SLF001


@pytest.mark.unit
def test_ci_failure_evidence_rejects_unterminated_bracketed_node_ids() -> None:
    evidence = ci_failure_evidence.extract_ci_failure_evidence(
        "\n".join(
            [
                "FAILED tests/unit/runtime/test_prompt.py::test_handles[param - truncated",
                "FAILED tests/unit/runtime/test_prompt.py::test_alpha - AssertionError: boom",
            ]
        ),
        check_name="unit",
    )

    assert evidence.test_node_ids == ("tests/unit/runtime/test_prompt.py::test_alpha",)


@pytest.mark.unit
def test_ci_failure_evidence_rejects_wrapped_pytest_node_ids() -> None:
    line = "Failed: (tests/unit/runtime/test_prompt.py::test_x), and {tests/unit/runtime/test_prompt.py::test_y}"

    assert ci_failure_evidence._pytest_node_candidates(line) == []  # noqa: SLF001


@pytest.mark.unit
def test_pytest_node_shape_rejects_missing_parts_prefix_space_and_urls() -> None:
    assert not ci_failure_evidence._looks_like_pytest_node("tests/unit/test_example.py::")  # noqa: SLF001
    assert not ci_failure_evidence._looks_like_pytest_node("(tests/unit/test_example.py::test_x")  # noqa: SLF001
    assert not ci_failure_evidence._looks_like_pytest_node("tests/unit bad/test_example.py::test_x")  # noqa: SLF001
    assert not ci_failure_evidence._looks_like_pytest_node(
        "https://example.test/test_example.py::test_x"
    )  # noqa: SLF001


@pytest.mark.unit
def test_pytest_node_boundary_covers_suffix_and_whitespace_edges() -> None:
    node = "tests/unit/test_example.py::test_case"

    assert ci_failure_evidence._has_pytest_node_boundary(node, 0, len(node))  # noqa: SLF001
    assert not ci_failure_evidence._has_pytest_node_boundary(f"{node})", 0, len(node))  # noqa: SLF001
    assert ci_failure_evidence._has_pytest_node_boundary(f"{node}, next", 0, len(node))  # noqa: SLF001
    assert not ci_failure_evidence._has_pytest_node_boundary(  # noqa: SLF001
        f"{node} extra text",
        0,
        len(node),
    )


@pytest.mark.unit
def test_pytest_node_boundary_rejects_unsupported_scanner_stop() -> None:
    with pytest.raises(AssertionError, match="unsupported pytest node boundary"):
        ci_failure_evidence._has_pytest_node_boundary(  # noqa: SLF001
            "tests/unit/runtime/test_prompt.py::test_x/",
            0,
            len("tests/unit/runtime/test_prompt.py::test_x"),
        )


@pytest.mark.unit
def test_pytest_repro_command_skips_unparseable_command_before_valid_pytest() -> None:
    command = ci_failure_evidence._pytest_repro_command(  # noqa: SLF001
        [
            "pytest 'unterminated",
            "uv run --python 3.12 --extra dev pytest tests/unit -q",
        ]
    )

    assert command == "uv run --python 3.12 --extra dev pytest"


@pytest.mark.unit
def test_ci_failure_shell_tokens_handle_quotes_and_escapes() -> None:
    tokens = ci_failure_evidence._shell_tokens(  # noqa: SLF001
        r"""uv run pytest "tests/unit/test space.py::test_name" escaped\ value"""
    )

    assert [token.value for token in tokens] == [
        "uv",
        "run",
        "pytest",
        "tests/unit/test space.py::test_name",
        "escaped value",
    ]
    assert tokens[2].end_index == len("uv run pytest")

    quoted_escape_tokens = ci_failure_evidence._shell_tokens(  # noqa: SLF001
        r'''pytest "tests/unit/test_example.py::test_handles[bad \"value\"]"'''
    )
    assert quoted_escape_tokens[-1].value == (
        'tests/unit/test_example.py::test_handles[bad "value"]'
    )


@pytest.mark.unit
def test_ci_failure_shell_tokens_reject_unterminated_escape_and_quote() -> None:
    with pytest.raises(ValueError):
        ci_failure_evidence._shell_tokens("pytest tests/unit/test_example.py\\")  # noqa: SLF001
    with pytest.raises(ValueError):
        ci_failure_evidence._shell_tokens("pytest 'tests/unit/test_example.py")  # noqa: SLF001


@pytest.mark.unit
def test_ci_failure_shell_tokens_accept_empty_or_whitespace_commands() -> None:
    assert ci_failure_evidence._shell_tokens("") == []  # noqa: SLF001
    assert ci_failure_evidence._shell_tokens("   ") == []  # noqa: SLF001


@pytest.mark.unit
def test_pytest_repro_command_returns_none_without_pytest_command() -> None:
    command = ci_failure_evidence._pytest_repro_command(  # noqa: SLF001
        ["ruff check src/awf tests"]
    )

    assert command is None


@pytest.mark.unit
def test_ci_failure_dedupe_helpers_skip_empty_and_duplicate_values() -> None:
    assert ci_failure_evidence._dedupe(["  Error: boom  ", "", "Error:   boom"]) == [  # noqa: SLF001
        "Error: boom"
    ]
    assert ci_failure_evidence._dedupe_preserving_values(  # noqa: SLF001
        [" node-a ", " ", "node-a", "node-b"]
    ) == ["node-a", "node-b"]
