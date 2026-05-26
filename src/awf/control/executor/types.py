"""Executor result and exception types."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from awf.common.audit import redact_audit_text
from awf.common.commands import CommandResult
from awf.runtime.planning import PlanConformanceReport
from awf.runtime.validation import ValidationCoverageResult


@dataclass(frozen=True)
class _RebaseRecoveryResult:
    base_sha: str
    head_sha: str
    requires_pr_update: bool = False


class _PostValidationConformanceReportGitError(RuntimeError):
    def __init__(
        self: Any,
        *,
        operation: str,
        result: CommandResult,
        cleanup_operation: str | None = None,
        cleanup_result: CommandResult | None = None,
    ) -> None:
        output = redact_audit_text((result.stderr or result.stdout or "").strip(), limit=1000)
        message = (
            f"post-validation conformance report git {operation} failed "
            f"(exit={result.returncode}): {output}"
        )
        if cleanup_operation is not None and cleanup_result is not None:
            cleanup_output = redact_audit_text(
                (cleanup_result.stderr or cleanup_result.stdout or "").strip(),
                limit=1000,
            )
            message = (
                f"{message}; cleanup git {cleanup_operation} failed "
                f"(exit={cleanup_result.returncode}): {cleanup_output}"
            )
        super().__init__(message)
        self.operation = operation
        self.returncode = result.returncode
        self.command_reason_code = result.reason_code
        self.cleanup_operation = cleanup_operation
        self.cleanup_returncode = cleanup_result.returncode if cleanup_result is not None else None
        self.cleanup_command_reason_code = (
            cleanup_result.reason_code if cleanup_result is not None else None
        )


class _PostValidationConformanceReportWriteError(RuntimeError):
    def __init__(self: Any, *, report_path: Path, error: OSError) -> None:
        message = (
            f"post-validation conformance report write failed for {report_path.as_posix()}: {error}"
        )
        super().__init__(message)
        self.report_path = report_path
        self.error_type = type(error).__name__
        self.errno = error.errno


@dataclass(frozen=True)
class _CoverageEvidenceResult:
    coverage: ValidationCoverageResult | None
    evidence_status: str | None = None
    reason_code: str | None = None
    source_run_id: str | None = None


@dataclass(frozen=True)
class _PrReexecutionGuardResult:
    blocked: bool
    recovery: dict[str, Any] | None = None


@dataclass(frozen=True)
class _PlanningRunFailure:
    message: str
    reason_code: str | None = None
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class _PlanningValidationHandoff:
    report: PlanConformanceReport
    plan_path: Path
    report_path: Path
    iteration: int
    max_iterations: int


class _MonitorRebaseRecoveryError(RuntimeError):
    """Raised when monitor-driven rebase recovery cannot update the PR branch."""


@dataclass(frozen=True)
class _ConformanceSalvageExecutionResult:
    status: str
    prompt_override: str | None = None
