"""Tests for executor coverage gap metadata and failure messages."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.control.executor.helpers import (
    _apply_baseline_coverage_ratchet,
    _coverage_preserves_below_threshold_baseline,
    _coverage_result_from_metadata,
    _extract_string_tokens,
    _post_validation_conformance_fix_result,
    _validation_failure_message,
    _validation_run_coverage_metadata,
    _validation_run_reason_code,
)
from awf.control.executor.types import _PlanningRunFailure
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
    failing_test_node_ids: list[str] | None = None,
    failing_test_evidence: list[str] | None = None,
    provider_failure_evidence: list[str] | None = None,
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
        failing_test_node_ids=failing_test_node_ids if failing_test_node_ids is not None else [],
        failing_test_evidence=failing_test_evidence if failing_test_evidence is not None else [],
        provider_failure_evidence=(
            provider_failure_evidence if provider_failure_evidence is not None else []
        ),
    )


@pytest.mark.unit
def test_extract_string_tokens_filters_non_string_list_items() -> None:
    assert _extract_string_tokens(["tests/test_app.py::test_ok", 1, None, "FAILED test"]) == [
        "tests/test_app.py::test_ok",
        "FAILED test",
    ]
    assert _extract_string_tokens("FAILED test") == []
    assert _extract_string_tokens(None) == []


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
def test_validation_run_coverage_metadata_includes_failing_test_evidence(
    tmp_path: Path,
) -> None:
    node_ids = ["tests/unit/test_widget.py::test_handles_edges"]
    evidence = ["FAILED tests/unit/test_widget.py::test_handles_edges - AssertionError"]
    result = ValidationResult(
        coverage=_coverage(
            tmp_path,
            percent=99.2,
            reason_code="COVERAGE_OK",
            status="passed",
            failing_test_node_ids=node_ids,
            failing_test_evidence=evidence,
        )
    )

    metadata = _validation_run_coverage_metadata(result)

    assert metadata is not None
    assert metadata["failing_test_node_ids"] == node_ids
    assert metadata["failing_test_evidence"] == evidence


@pytest.mark.unit
def test_validation_run_reason_prefers_pytest_failure_when_coverage_met(
    tmp_path: Path,
) -> None:
    command = _command_result(tmp_path, returncode=1)
    command = ValidationCommandResult(
        command=command.command,
        returncode=command.returncode,
        duration_seconds=command.duration_seconds,
        stdout_path=command.stdout_path,
        stderr_path=command.stderr_path,
        phase=command.phase,
        reason_code="PYTEST_TEST_FAILURE",
        stream_ids=command.stream_ids,
        metadata={"failing_test_node_ids": ["tests/unit/test_widget.py::test_handles_edges"]},
    )
    result = ValidationResult(
        commands=[command],
        coverage=_coverage(
            tmp_path,
            percent=99.2,
            reason_code="COVERAGE_OK",
            status="passed",
            command_result=command,
            failing_test_node_ids=["tests/unit/test_widget.py::test_handles_edges"],
            failing_test_evidence=[
                "FAILED tests/unit/test_widget.py::test_handles_edges - AssertionError"
            ],
        ),
    )

    assert _validation_run_reason_code(result) == "PYTEST_TEST_FAILURE"


@pytest.mark.unit
def test_validation_run_reason_rejects_rehydrated_coverage_with_failing_tests(
    tmp_path: Path,
) -> None:
    result = ValidationResult(
        coverage=_coverage(
            tmp_path,
            percent=99.2,
            reason_code="COVERAGE_OK",
            status="passed",
            command_result=None,
            failing_test_node_ids=["tests/unit/test_widget.py::test_handles_edges"],
            failing_test_evidence=[
                "FAILED tests/unit/test_widget.py::test_handles_edges - AssertionError"
            ],
        )
    )

    assert not result.all_passed
    assert _validation_run_reason_code(result) == "PYTEST_TEST_FAILURE"
    message = _validation_failure_message(result)
    assert "tests/unit/test_widget.py::test_handles_edges" in message
    assert "coverage met the 99.0% requirement at 99.2%" in message


@pytest.mark.unit
def test_coverage_wrapped_pytest_failure_and_coverage_below_threshold_surfaces_both(
    tmp_path: Path,
) -> None:
    node_ids = ["tests/unit/test_widget.py::test_handles_edges"]
    evidence = ["FAILED tests/unit/test_widget.py::test_handles_edges - AssertionError"]
    command = _command_result(tmp_path, returncode=1)
    command = ValidationCommandResult(
        command=command.command,
        returncode=command.returncode,
        duration_seconds=command.duration_seconds,
        stdout_path=command.stdout_path,
        stderr_path=command.stderr_path,
        phase=command.phase,
        reason_code="PYTEST_TEST_FAILURE",
        stream_ids=command.stream_ids,
        metadata={
            "failing_test_node_ids": node_ids,
            "failing_test_evidence": evidence,
        },
    )
    result = ValidationResult(
        commands=[command],
        coverage=_coverage(
            tmp_path,
            percent=98,
            minimum=99,
            reason_code="COVERAGE_BELOW_THRESHOLD",
            status="failed",
            command_result=command,
            failing_test_node_ids=node_ids,
            failing_test_evidence=evidence,
        ),
    )

    assert _validation_run_reason_code(result) == "PYTEST_TEST_FAILURE"
    assert result.coverage is not None
    assert result.coverage.reason_code == "COVERAGE_BELOW_THRESHOLD"
    message = _validation_failure_message(result)
    assert "tests/unit/test_widget.py::test_handles_edges" in message
    assert "AssertionError" in message
    assert "coverage 98.0% is also below required 99.0%" in message
    assert "fix the failing test first" in message


@pytest.mark.unit
def test_coverage_wrapped_pytest_failure_and_provider_fail_under_surfaces_both(
    tmp_path: Path,
) -> None:
    node_ids = ["tests/unit/test_widget.py::test_handles_edges"]
    evidence = ["FAILED tests/unit/test_widget.py::test_handles_edges - AssertionError"]
    command = _command_result(tmp_path, returncode=1)
    result = ValidationResult(
        commands=[
            ValidationCommandResult(
                command=command.command,
                returncode=command.returncode,
                duration_seconds=command.duration_seconds,
                stdout_path=command.stdout_path,
                stderr_path=command.stderr_path,
                phase=command.phase,
                reason_code="PYTEST_TEST_FAILURE",
                stream_ids=command.stream_ids,
                metadata={
                    "failing_test_node_ids": node_ids,
                    "failing_test_evidence": evidence,
                },
            )
        ],
        coverage=_coverage(
            tmp_path,
            percent=99.0,
            minimum=99.0,
            reason_code="COVERAGE_FAIL_UNDER_NOT_REACHED",
            command_result=command,
            failing_test_node_ids=node_ids,
            failing_test_evidence=evidence,
            provider_failure_evidence=[
                "FAIL Required test coverage of 99.0% not reached. Total coverage: 99.00%"
            ],
        ),
    )

    assert _validation_run_reason_code(result) == "PYTEST_TEST_FAILURE"
    message = _validation_failure_message(result)
    assert "tests/unit/test_widget.py::test_handles_edges" in message
    assert "coverage provider also reported that fail-under was not reached" in message
    assert "coverage met the 99.0% requirement" not in message


@pytest.mark.unit
def test_validation_failure_message_names_failing_tests_when_coverage_met(
    tmp_path: Path,
) -> None:
    node_ids = ["tests/unit/test_widget.py::test_handles_edges"]
    evidence = ["FAILED tests/unit/test_widget.py::test_handles_edges - AssertionError"]
    command = _command_result(tmp_path, returncode=1)
    command = ValidationCommandResult(
        command=command.command,
        returncode=command.returncode,
        duration_seconds=command.duration_seconds,
        stdout_path=command.stdout_path,
        stderr_path=command.stderr_path,
        phase=command.phase,
        reason_code="PYTEST_TEST_FAILURE",
        stream_ids=command.stream_ids,
        metadata={
            "failing_test_node_ids": node_ids,
            "failing_test_evidence": evidence,
        },
    )
    result = ValidationResult(
        commands=[command],
        coverage=_coverage(
            tmp_path,
            percent=99.2,
            minimum=99,
            reason_code="COVERAGE_OK",
            status="passed",
            command_result=command,
            failing_test_node_ids=node_ids,
            failing_test_evidence=evidence,
        ),
    )

    message = _validation_failure_message(result)

    assert "tests/unit/test_widget.py::test_handles_edges" in message
    assert "coverage met the 99.0% requirement at 99.2%" in message
    assert "raise coverage" not in message.lower()
    assert "add meaningful tests" not in message.lower()


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
def test_baseline_debt_ratchet_handles_coverage_without_command_result(
    tmp_path: Path,
) -> None:
    result = ValidationResult(
        coverage=ValidationCoverageResult(
            provider="python",
            percent=90,
            minimum_percent=99,
            enforce=True,
            status="failed",
            reason_code="COVERAGE_BELOW_THRESHOLD",
            command_result=None,
        )
    )
    baseline = _coverage(tmp_path, percent=90, status="failed")

    adjusted = _apply_baseline_coverage_ratchet(result, baseline_coverage=baseline)

    assert adjusted.commands == []
    assert adjusted.coverage is not None
    assert adjusted.coverage.command_result is None
    assert adjusted.coverage.status == "baseline_debt"


@pytest.mark.unit
def test_baseline_preservation_rejects_test_failures_and_complete_baselines(
    tmp_path: Path,
) -> None:
    baseline = _coverage(tmp_path, percent=90, status="failed")

    assert not _coverage_preserves_below_threshold_baseline(
        _coverage(
            tmp_path,
            percent=90,
            failing_test_node_ids=["tests/unit/test_app.py::test_fails"],
        ),
        baseline_coverage=baseline,
    )
    assert not _coverage_preserves_below_threshold_baseline(
        _coverage(tmp_path, percent=None),
        baseline_coverage=baseline,
    )
    assert not _coverage_preserves_below_threshold_baseline(
        _coverage(tmp_path, percent=90),
        baseline_coverage=_coverage(tmp_path, percent=None),
    )
    assert not _coverage_preserves_below_threshold_baseline(
        _coverage(tmp_path, percent=99),
        baseline_coverage=_coverage(tmp_path, percent=99, status="passed"),
    )


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
def test_failure_message_reports_provider_fail_under_without_trusting_rounded_percent(
    tmp_path: Path,
) -> None:
    evidence = ["FAIL Required test coverage of 99.0% not reached. Total coverage: 99.00%"]
    result = ValidationResult(
        coverage=_coverage(
            tmp_path,
            percent=99.0,
            minimum=99.0,
            reason_code="COVERAGE_FAIL_UNDER_NOT_REACHED",
            provider_failure_evidence=evidence,
        )
    )

    metadata = _validation_run_coverage_metadata(result)
    assert metadata is not None
    assert metadata["provider_failure_evidence"] == evidence
    assert _coverage_result_from_metadata(metadata).provider_failure_evidence == evidence

    message = _validation_failure_message(result)

    assert "coverage provider reported that fail-under was not reached" in message
    assert "displayed rounded coverage was 99.00%" in message
    assert "required coverage is 99.00%" in message
    assert "provider fail-under output as authoritative" in message


@pytest.mark.unit
def test_failure_message_uses_fallback_evidence_when_node_ids_are_absent(
    tmp_path: Path,
) -> None:
    result = ValidationResult(
        coverage=_coverage(
            tmp_path,
            percent=99.2,
            reason_code="COVERAGE_OK",
            status="passed",
            command_result=None,
            failing_test_evidence=["FAILED tests/unit/test_widget.py - AssertionError"],
        )
    )

    message = _validation_failure_message(result)

    assert "FAILED tests/unit/test_widget.py - AssertionError" in message
    assert "coverage met the 99.0% requirement at 99.2%" in message


@pytest.mark.unit
def test_failure_message_names_unsupported_coverage_provider(tmp_path: Path) -> None:
    result = ValidationResult(
        coverage=_coverage(
            tmp_path,
            percent=None,
            reason_code="COVERAGE_PROVIDER_UNSUPPORTED",
            status="failed",
        )
    )

    assert _validation_failure_message(result) == (
        "validation failed: unsupported coverage provider python"
    )


@pytest.mark.unit
def test_failure_message_caps_gap_list_at_five(tmp_path: Path) -> None:
    gaps: list[dict[str, object]] = [
        {"file": f"src/file_{i:02d}.py", "missing_lines": [f"{i}0-{i}9"]} for i in range(10)
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


@pytest.mark.unit
def test_post_validation_conformance_fix_result_uses_attempt_from_failure_details(
    tmp_path: Path,
) -> None:
    result = _post_validation_conformance_fix_result(
        failure=_PlanningRunFailure(
            message="conformance gap",
            details={"attempt": 3},
        ),
        workspace_id="ws_conformance",
        artifacts_root=tmp_path,
    )

    command = result.commands[0]
    assert command.stdout_path.name == "post_validation_conformance.3.stdout"
    assert command.stderr_path.name == "post_validation_conformance.3.stderr"
