"""Focused branch-coverage tests for executor helper behavior."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.control.executor import (
    _apply_baseline_coverage_ratchet,
    _call_pr_monitor_factory,
    _coverage_preserves_below_threshold_baseline,
    _failure_reason_for_phase,
    _read_text_if_present,
    _validation_failure_message,
    _validation_run_command_records,
    _validation_run_coverage_metadata,
    _validation_run_log_stream_refs,
    _validation_tier_for_workspace,
)
from awf.db.enums import FailureReason, TaskClass
from awf.profiles.models import WorkspaceProfile
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
) -> ValidationCoverageResult:
    return ValidationCoverageResult(
        provider="python",
        percent=percent,
        minimum_percent=minimum,
        enforce=True,
        status=status,
        reason_code=reason_code,
        command_result=command_result if command_result is not None else _command_result(tmp_path),
    )


@pytest.mark.unit
def test_failure_reason_for_phase_maps_setup_timeout_and_healthcheck() -> None:
    assert (
        _failure_reason_for_phase(SimpleNamespace(phase="healthcheck", reason_code="COMMAND_FAILED"))
        == FailureReason.health_check_failure
    )
    assert (
        _failure_reason_for_phase(SimpleNamespace(phase="validate", reason_code="PHASE_TIMEOUT"))
        == FailureReason.phase_timeout
    )
    assert (
        _failure_reason_for_phase(SimpleNamespace(phase="pre_agent", reason_code="COMMAND_FAILED"))
        == FailureReason.service_startup_failure
    )
    assert _failure_reason_for_phase(None) == FailureReason.validation_failure


@pytest.mark.unit
def test_validation_run_log_stream_refs_preserve_only_string_stream_ids() -> None:
    refs = _validation_run_log_stream_refs(
        [
            {"stream_ids": {"stdout": "validation.01.stdout", "stderr": 123}},
            {"stream_ids": "not-a-dict"},
            {},
        ]
    )

    assert refs == {
        "commands": [
            {"stdout": "validation.01.stdout", "stderr": None},
            {"stdout": None, "stderr": None},
            {"stdout": None, "stderr": None},
        ]
    }


@pytest.mark.unit
def test_validation_run_command_records_include_healthchecks_and_coverage() -> None:
    profile = WorkspaceProfile.model_validate(
        {
            "name": "records",
            "phases": {
                "post_agent": ["ruff format --check"],
                "validate": ["pytest -q"],
            },
            "validation": {
                "healthchecks": [{"name": "api", "command": "curl -fsS localhost/health"}],
                "coverage": {"command": "pytest --cov=awf --cov-report=term"},
            },
        }
    )

    records = _validation_run_command_records(
        profile=profile,
        phase_names=("post_agent", "validate"),
        run_healthchecks=True,
    )

    assert [(record["phase"], record["command_index"]) for record in records] == [
        ("healthcheck", 1),
        ("post_agent", 1),
        ("validate", 1),
        ("coverage", 1),
    ]
    assert records[-1]["stream_ids"] == {
        "stdout": "validation.01_coverage.stdout",
        "stderr": "validation.01_coverage.stderr",
    }


@pytest.mark.unit
def test_validation_tier_for_workspace_uses_task_class_floor() -> None:
    profile = WorkspaceProfile.model_validate(
        {"name": "tier", "validation": {"requested_tier": 1}}
    )

    assert (
        _validation_tier_for_workspace(
            SimpleNamespace(task_class=TaskClass.migration_task.value),  # type: ignore[arg-type]
            profile,
        )
        == 3
    )
    assert (
        _validation_tier_for_workspace(
            SimpleNamespace(task_class=TaskClass.refactor_task.value),  # type: ignore[arg-type]
            profile,
        )
        == 2
    )
    assert (
        _validation_tier_for_workspace(
            SimpleNamespace(task_class=None),  # type: ignore[arg-type]
            profile,
        )
        == 1
    )


@pytest.mark.unit
def test_baseline_coverage_ratchet_accepts_no_regression(tmp_path: Path) -> None:
    command = _command_result(tmp_path, returncode=1)
    result = ValidationResult(
        commands=[command],
        coverage=_coverage(tmp_path, percent=90, command_result=command),
    )
    baseline = _coverage(tmp_path, percent=90, status="failed")

    adjusted = _apply_baseline_coverage_ratchet(result, baseline_coverage=baseline)

    assert adjusted.all_passed
    assert adjusted.coverage is not None
    assert adjusted.coverage.status == "baseline_debt"
    assert adjusted.coverage.reason_code == "COVERAGE_BASELINE_DEBT_NO_REGRESSION"
    assert adjusted.commands[0].returncode == 0
    assert adjusted.commands[0].reason_code == "COVERAGE_BASELINE_DEBT_NO_REGRESSION"


@pytest.mark.unit
def test_baseline_coverage_ratchet_accepts_no_regression_without_command_result(
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
        ),
    )
    baseline = _coverage(tmp_path, percent=90, status="failed")

    adjusted = _apply_baseline_coverage_ratchet(result, baseline_coverage=baseline)

    assert adjusted.all_passed
    assert adjusted.coverage is not None
    assert adjusted.coverage.command_result is None
    assert adjusted.coverage.reason_code == "COVERAGE_BASELINE_DEBT_NO_REGRESSION"


@pytest.mark.unit
def test_baseline_coverage_ratchet_rejects_missing_or_regressed_measurements(
    tmp_path: Path,
) -> None:
    coverage = _coverage(tmp_path, percent=88)
    baseline = _coverage(tmp_path, percent=90)

    assert not _coverage_preserves_below_threshold_baseline(None, baseline_coverage=baseline)
    assert not _coverage_preserves_below_threshold_baseline(
        _coverage(tmp_path, percent=None),
        baseline_coverage=baseline,
    )
    assert not _coverage_preserves_below_threshold_baseline(
        _coverage(tmp_path, percent=99, status="passed", reason_code="COVERAGE_OK"),
        baseline_coverage=baseline,
    )
    assert not _coverage_preserves_below_threshold_baseline(
        coverage,
        baseline_coverage=_coverage(tmp_path, percent=99, status="passed", reason_code="COVERAGE_OK"),
    )
    assert not _coverage_preserves_below_threshold_baseline(coverage, baseline_coverage=baseline)


@pytest.mark.unit
def test_validation_coverage_metadata_includes_baseline_fields(tmp_path: Path) -> None:
    result = ValidationResult(coverage=_coverage(tmp_path, percent=91, status="reported"))
    baseline = _coverage(tmp_path, percent=None, status="failed", reason_code="COVERAGE_NOT_FOUND")

    metadata = _validation_run_coverage_metadata(result, baseline_coverage=baseline)

    assert metadata is not None
    assert metadata["percent"] == 91.0
    assert metadata["baseline_percent"] is None
    assert metadata["baseline_status"] == "failed"
    assert metadata["baseline_reason_code"] == "COVERAGE_NOT_FOUND"
    assert _validation_run_coverage_metadata(ValidationResult()) is None


@pytest.mark.unit
def test_validation_failure_message_carries_coverage_context(tmp_path: Path) -> None:
    below_threshold = ValidationResult(
        coverage=_coverage(tmp_path, percent=88, minimum=99, reason_code="COVERAGE_BELOW_THRESHOLD")
    )
    command_failed = ValidationResult(
        coverage=_coverage(
            tmp_path,
            percent=99,
            minimum=99,
            reason_code="COVERAGE_COMMAND_FAILED",
        )
    )
    baseline = _coverage(tmp_path, percent=90, minimum=99)

    assert "pre-agent base coverage was 90.0%" in _validation_failure_message(
        below_threshold,
        baseline_coverage=baseline,
    )
    assert "coverage command failed" in _validation_failure_message(
        command_failed,
        baseline_coverage=baseline,
    )
    assert "unsupported coverage provider" in _validation_failure_message(
        ValidationResult(
            coverage=ValidationCoverageResult(
                provider="lcov",
                percent=None,
                minimum_percent=90,
                enforce=True,
                status="failed",
                reason_code="COVERAGE_PROVIDER_UNSUPPORTED",
            )
        )
    )
    assert (
        _validation_failure_message(
            ValidationResult(
                coverage=_coverage(
                    tmp_path,
                    percent=None,
                    minimum=99,
                    reason_code="COVERAGE_NOT_FOUND",
                )
            )
        )
        == "validation failed: coverage output was not found"
    )
    assert _validation_failure_message(
        ValidationResult(commands=[_command_result(tmp_path)])
    ) == "validation failed: pytest --cov"


@pytest.mark.unit
def test_read_text_if_present_handles_empty_missing_and_present_files(tmp_path: Path) -> None:
    missing = tmp_path / "missing.txt"
    empty = tmp_path / "empty.txt"
    present = tmp_path / "present.txt"
    empty.write_text(" \n", encoding="utf-8")
    present.write_text(" useful output \n", encoding="utf-8")

    assert _read_text_if_present(missing) is None
    assert _read_text_if_present(empty) is None
    assert _read_text_if_present(present) == "useful output"


@pytest.mark.unit
def test_read_text_if_present_returns_none_when_file_read_raises() -> None:
    class _UnreadablePath:
        def is_file(self) -> bool:
            return True

        def read_text(self, *, encoding: str) -> str:
            assert encoding == "utf-8"
            raise OSError("permission denied")

    assert _read_text_if_present(_UnreadablePath()) is None  # type: ignore[arg-type]


@pytest.mark.unit
def test_call_pr_monitor_factory_uses_widest_supported_signature() -> None:
    calls: list[tuple[object, object, object]] = []
    adapter = object()
    profile = WorkspaceProfile.model_validate({"name": "factory-profile"})
    workspace = object()

    def factory(adapter_arg: object, profile_arg: object, workspace_arg: object) -> object:
        calls.append((adapter_arg, profile_arg, workspace_arg))
        return "monitor"

    assert (
        _call_pr_monitor_factory(
            factory,
            adapter=adapter,  # type: ignore[arg-type]
            profile=profile,
            workspace=workspace,  # type: ignore[arg-type]
        )
        == "monitor"
    )
    assert calls == [(adapter, profile, workspace)]


@pytest.mark.unit
def test_call_pr_monitor_factory_surfaces_bind_error() -> None:
    adapter = object()
    profile = WorkspaceProfile.model_validate({"name": "factory-profile"})

    def factory(*, required_keyword: str) -> object:
        return required_keyword

    with pytest.raises(TypeError):
        _call_pr_monitor_factory(
            factory,
            adapter=adapter,  # type: ignore[arg-type]
            profile=profile,
            workspace=object(),  # type: ignore[arg-type]
        )
