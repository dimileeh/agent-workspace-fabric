"""CI failure evidence extraction edge tests."""

from __future__ import annotations

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
def test_ci_failure_evidence_skips_unparseable_commands_for_repro() -> None:
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
    assert evidence.suggested_repro_commands == ()
    assert any("AssertionError" in snippet for snippet in evidence.assertion_snippets)
    assert "fatal: repository not found" in evidence.error_summaries


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
