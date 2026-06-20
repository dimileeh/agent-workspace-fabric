"""Focused branch-coverage tests for executor helper behavior."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.adapters.base import AgentDefaults
from awf.control.executor import helpers as executor_helpers
from awf.db.enums import AgentRuntime, FailureReason
from awf.profiles.models import WorkspaceProfile
from awf.runtime.validation import ValidationCommandResult, ValidationCoverageResult
from awf.runtime.validation_types import ValidationResult


def _command_result(tmp_path: Path, *, phase: str = "coverage") -> ValidationCommandResult:
    stdout = tmp_path / f"{phase}.stdout"
    stderr = tmp_path / f"{phase}.stderr"
    stdout.write_text("", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    return ValidationCommandResult(
        command="pytest --cov=awf",
        returncode=1,
        duration_seconds=0.1,
        stdout_path=stdout,
        stderr_path=stderr,
        phase=phase,
        reason_code="COVERAGE_COMMAND_FAILED",
        policy_failed=True,
    )


def _coverage(
    tmp_path: Path,
    *,
    reason_code: str,
    percent: float | None = None,
    failing_test_node_ids: list[str] | None = None,
    failing_test_evidence: list[str] | None = None,
) -> ValidationCoverageResult:
    return ValidationCoverageResult(
        provider="python",
        percent=percent,
        minimum_percent=99.0,
        enforce=True,
        status="failed",
        reason_code=reason_code,
        command_result=_command_result(tmp_path),
        failing_test_node_ids=failing_test_node_ids or [],
        failing_test_evidence=failing_test_evidence or [],
    )


@pytest.mark.unit
def test_realign_profile_from_snapshot_updates_detached_workspace_only() -> None:
    workspace = SimpleNamespace(resolved_profile={"name": "stale"})
    snapshot = {
        "name": "resolved",
        "planning": {
            "required": True,
            "max_iterations": 5,
        },
    }

    profile = executor_helpers._realign_profile_from_resolved_profile_snapshot(  # noqa: SLF001
        workspace,  # type: ignore[arg-type]
        snapshot,
        planning_max_iterations_default=2,
    )

    assert isinstance(profile, WorkspaceProfile)
    assert profile.name == "resolved"
    assert profile.planning.max_iterations == 5
    assert workspace.resolved_profile == snapshot


@pytest.mark.unit
def test_realign_profile_from_missing_snapshot_leaves_workspace_unchanged() -> None:
    resolved_profile = {"name": "kept"}
    workspace = SimpleNamespace(resolved_profile=resolved_profile)

    profile = executor_helpers._realign_profile_from_resolved_profile_snapshot(  # noqa: SLF001
        workspace,  # type: ignore[arg-type]
        None,
    )

    assert profile is None
    assert workspace.resolved_profile is resolved_profile


@pytest.mark.unit
def test_provider_recovery_default_model_uses_cursor_adapter_implicit_model() -> None:
    cursor_adapter = SimpleNamespace(
        name=AgentRuntime.cursor,
        provider_recovery_default_model="cursor-implicit",
    )
    codex_adapter = SimpleNamespace(name=AgentRuntime.codex)
    defaults = AgentDefaults(model="configured-default", effort="medium")

    assert (
        executor_helpers._provider_recovery_default_model_for_monitor_handoff(  # noqa: SLF001
            adapter=cursor_adapter,  # type: ignore[arg-type]
            defaults=defaults,
        )
        == "cursor-implicit"
    )
    assert (
        executor_helpers._provider_recovery_default_model_for_monitor_handoff(  # noqa: SLF001
            adapter=codex_adapter,  # type: ignore[arg-type]
            defaults=defaults,
        )
        == "configured-default"
    )
    assert (
        executor_helpers._provider_recovery_default_model_for_monitor_handoff(  # noqa: SLF001
            adapter=codex_adapter,  # type: ignore[arg-type]
            defaults=None,
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (
            SimpleNamespace(phase="healthcheck", reason_code="HEALTHCHECK_COMMAND_FAILED"),
            FailureReason.health_check_failure,
        ),
        (
            SimpleNamespace(phase="validate", reason_code="PHASE_TIMEOUT"),
            FailureReason.phase_timeout,
        ),
        (
            SimpleNamespace(
                phase="validate",
                reason_code="PROFILE_VALIDATION_TOOL_UNAVAILABLE",
            ),
            FailureReason.profile_resolution_failure,
        ),
        (
            SimpleNamespace(phase="setup", reason_code="COMMAND_FAILED"),
            FailureReason.service_startup_failure,
        ),
        (
            SimpleNamespace(phase="validate", reason_code="COMMAND_FAILED"),
            FailureReason.validation_failure,
        ),
    ],
)
def test_failure_reason_for_phase_maps_validation_failures(
    failure: SimpleNamespace,
    expected: FailureReason,
) -> None:
    assert executor_helpers._failure_reason_for_phase(failure) is expected  # noqa: SLF001


@pytest.mark.unit
def test_validation_failure_message_includes_baseline_debt_for_coverage_command_failure(
    tmp_path: Path,
) -> None:
    result = ValidationResult(
        coverage=_coverage(
            tmp_path,
            reason_code="COVERAGE_COMMAND_FAILED",
            percent=None,
        )
    )
    baseline = ValidationCoverageResult(
        provider="python",
        percent=98.4,
        minimum_percent=99.0,
        enforce=True,
        status="failed",
        reason_code="COVERAGE_BELOW_THRESHOLD",
    )

    message = executor_helpers._validation_failure_message(  # noqa: SLF001
        result,
        baseline_coverage=baseline,
    )

    assert message == (
        "validation failed: coverage command failed; pre-agent base coverage was "
        "98.4% against the same 99.0% requirement; fix the failing tests or add "
        "meaningful coverage, do not lower coverage thresholds"
    )


@pytest.mark.unit
def test_validation_failure_message_reports_fail_under_as_authoritative(
    tmp_path: Path,
) -> None:
    result = ValidationResult(
        coverage=_coverage(
            tmp_path,
            reason_code="COVERAGE_FAIL_UNDER_NOT_REACHED",
            percent=98.995,
        )
    )

    message = executor_helpers._validation_failure_message(result)  # noqa: SLF001

    assert message == (
        "validation failed: coverage provider reported that fail-under was not reached; "
        "displayed rounded coverage was 99.00%; required coverage is 99.00%; "
        "treat provider fail-under output as authoritative and add meaningful tests "
        "instead of relying on rounded coverage"
    )


@pytest.mark.unit
def test_validation_failure_message_prioritizes_pytest_failure_without_coverage_output(
    tmp_path: Path,
) -> None:
    result = ValidationResult(
        coverage=_coverage(
            tmp_path,
            reason_code="COVERAGE_COMMAND_FAILED",
            failing_test_node_ids=["tests/unit/test_example.py::test_fails"],
            failing_test_evidence=["AssertionError: expected clean status"],
        )
    )

    message = executor_helpers._validation_failure_message(result)  # noqa: SLF001

    assert message == (
        "validation failed: pytest reported failing tests: "
        "tests/unit/test_example.py::test_fails; evidence: "
        "AssertionError: expected clean status; coverage output was not available "
        "because the coverage-wrapped test command failed"
    )
