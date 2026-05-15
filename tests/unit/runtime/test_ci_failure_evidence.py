"""Focused CI failure evidence extraction edge tests."""

from __future__ import annotations

import pytest

from awf.runtime import ci_failure_evidence


@pytest.mark.unit
def test_ci_failure_evidence_skips_run_step_without_known_command_marker() -> None:
    evidence = ci_failure_evidence.extract_ci_failure_evidence(
        "\t".join(["2026-05-15T00:00:00Z", "Run echo hello", "shell: bash"]),
        check_name="unit",
    )

    assert evidence.failing_commands == ()


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
