"""Tests for executor coverage gap metadata and failure messages."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.control.executor import (
    _apply_baseline_coverage_ratchet,
    _validation_failure_message,
    _validation_run_coverage_metadata,
)
from awf.runtime.validation import (
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
)


def _command_result(tmp_path: Path, *, returncode: int = 1) -> ValidationCommandResult:
    stdout = tmp_path / "cmd.stdout"
    stderr = tmp_path / "cmd.stderr"
    stdout.write_text("stdout", encoding="utf-8")
    stderr.write_text("stderr", encoding="utf-8")
    return ValidationCommandResult(
        command="pytest --cov",
        returncode=returncode,
        duration_seconds=0.1,
        stdout_path=stdout,
        stderr_path=stderr,
        phase="coverage",
        reason_code="COVERAGE_BELOW_THRESHOLD",
        policy_failed=returncode != 0,
    )


def _coverage(
    tmp_path: Path,
    *,
    percent: float | None,
    minimum: float = 99,
    reason_code: str = "COVERAGE_BELOW_THRESHOLD",
    status: str = "failed",
    command_result: ValidationCommandResult | None = None,
    gaps: list[dict[str, object]] | None = None,
) -> ValidationCoverageResult:
    return ValidationCoverageResult(
        provider="python",
        percent=percent,
        minimum_percent=minimum,
        enforce=True,
        status=status,
        reason_code=reason_code,
        command_result=command_result if command_result is not None else _command_result(tmp_path),
        gaps=gaps if gaps is not None else [],
    )


@pytest.mark.unit
def test_failure_message_includes_top_gaps(tmp_path: Path) -> None:
    gaps: list[dict[str, object]] = [
        {"file": "src/awf/control/executor.py", "missing_lines": ["10-20", "50", "75-80"]},
        {"file": "src/awf/runtime/validation.py", "missing_lines": ["30-45"]},
    ]
    result = ValidationResult(
        coverage=_coverage(
            tmp_path,
            percent=88,
            minimum=99,
            reason_code="COVERAGE_BELOW_THRESHOLD",
            gaps=gaps,
        )
    )

    message = _validation_failure_message(result)

    assert "88.0% is below required 99.0%" in message
    assert "top uncovered" in message.lower() or "missing" in message.lower()
    assert "src/awf/control/executor.py" in message
    assert "10-20" in message
    assert "src/awf/runtime/validation.py" in message
    assert "30-45" in message


@pytest.mark.unit
def test_failure_message_preserves_behavior_without_gaps(tmp_path: Path) -> None:
    result = ValidationResult(
        coverage=_coverage(
            tmp_path,
            percent=88,
            minimum=99,
            reason_code="COVERAGE_BELOW_THRESHOLD",
            gaps=[],
        )
    )

    message = _validation_failure_message(result)

    assert message == (
        "validation failed: coverage 88.0% is below required 99.0%"
        "; add meaningful tests and do not lower coverage thresholds"
    )


@pytest.mark.unit
def test_failure_message_preserves_behavior_with_none_gaps(tmp_path: Path) -> None:
    coverage = _coverage(
        tmp_path,
        percent=88,
        minimum=99,
        reason_code="COVERAGE_BELOW_THRESHOLD",
    )
    coverage = ValidationCoverageResult(
        provider=coverage.provider,
        percent=coverage.percent,
        minimum_percent=coverage.minimum_percent,
        enforce=coverage.enforce,
        status=coverage.status,
        reason_code=coverage.reason_code,
        command_result=coverage.command_result,
        gaps=None,
    )
    result = ValidationResult(coverage=coverage)

    message = _validation_failure_message(result)

    assert message == (
        "validation failed: coverage 88.0% is below required 99.0%"
        "; add meaningful tests and do not lower coverage thresholds"
    )


@pytest.mark.unit
def test_validation_run_coverage_metadata_includes_gaps(tmp_path: Path) -> None:
    gaps: list[dict[str, object]] = [
        {"file": "src/a.py", "missing_lines": ["10-20"]},
    ]
    result = ValidationResult(
        coverage=_coverage(
            tmp_path,
            percent=91,
            reason_code="COVERAGE_BELOW_THRESHOLD",
            gaps=gaps,
        )
    )

    metadata = _validation_run_coverage_metadata(result)

    assert metadata is not None
    assert metadata["gaps"] == gaps
    assert metadata["percent"] == 91.0


@pytest.mark.unit
def test_baseline_debt_ratchet_preserves_gaps_in_metadata(tmp_path: Path) -> None:
    gaps: list[dict[str, object]] = [
        {"file": "src/awf/old_debt.py", "missing_lines": ["1-500"]},
    ]
    command = _command_result(tmp_path, returncode=1)
    result = ValidationResult(
        commands=[command],
        coverage=_coverage(
            tmp_path,
            percent=90,
            command_result=command,
            gaps=gaps,
        ),
    )
    baseline = _coverage(tmp_path, percent=90, status="failed", gaps=[])

    adjusted = _apply_baseline_coverage_ratchet(result, baseline_coverage=baseline)

    assert adjusted.all_passed
    assert adjusted.coverage is not None
    assert adjusted.coverage.status == "baseline_debt"
    assert adjusted.coverage.reason_code == "COVERAGE_BASELINE_DEBT_NO_REGRESSION"
    assert adjusted.coverage.gaps == gaps


@pytest.mark.unit
def test_failure_message_with_gaps_includes_baseline_context(tmp_path: Path) -> None:
    gaps: list[dict[str, object]] = [
        {"file": "src/mod.py", "missing_lines": ["1-100"]},
    ]
    result = ValidationResult(
        coverage=_coverage(
            tmp_path,
            percent=88,
            minimum=99,
            reason_code="COVERAGE_BELOW_THRESHOLD",
            gaps=gaps,
        )
    )
    baseline = _coverage(tmp_path, percent=90, minimum=99)

    message = _validation_failure_message(result, baseline_coverage=baseline)

    assert "88.0% is below required 99.0%" in message
    assert "pre-agent base coverage was 90.0%" in message
    assert "src/mod.py" in message


@pytest.mark.unit
def test_failure_message_coverage_not_found_ignores_gaps(tmp_path: Path) -> None:
    gaps: list[dict[str, object]] = [
        {"file": "src/x.py", "missing_lines": ["1"]},
    ]
    result = ValidationResult(
        coverage=_coverage(
            tmp_path,
            percent=None,
            reason_code="COVERAGE_NOT_FOUND",
            gaps=gaps,
        )
    )

    message = _validation_failure_message(result)

    assert message == "validation failed: coverage output was not found"
    assert "src/x.py" not in message


@pytest.mark.unit
def test_failure_message_caps_gap_list_at_five(tmp_path: Path) -> None:
    gaps: list[dict[str, object]] = [
        {"file": f"src/file_{i:02d}.py", "missing_lines": [f"{i}0-{i}9"]}
        for i in range(10)
    ]
    result = ValidationResult(
        coverage=_coverage(
            tmp_path,
            percent=80,
            minimum=99,
            reason_code="COVERAGE_BELOW_THRESHOLD",
            gaps=gaps,
        )
    )

    message = _validation_failure_message(result)

    assert "80.0% is below required 99.0%" in message
    assert "src/file_00.py" in message
    assert "src/file_04.py" in message
    assert "src/file_05.py" not in message
