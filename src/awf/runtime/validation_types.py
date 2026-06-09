"""Shared validation dataclasses used by runner helper modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from awf.profiles.models import ProfileCommand


@dataclass(frozen=True)
class ProfileExecutionCommand:
    """One profile command after synthetic DB hook insertion."""

    phase: str
    command: ProfileCommand
    database_hook: bool = False
    hook_kind: str | None = None


@dataclass(frozen=True)
class ProfileValidationToolPreflightFinding:
    """A profile validation command that can lose its setup-time tooling."""

    command: str
    tool: str
    reason_code: str
    message: str

    def as_metadata(self) -> dict[str, str]:
        return {
            "command": self.command,
            "tool": self.tool,
            "reason_code": self.reason_code,
            "message": self.message,
        }


@dataclass(frozen=True)
class SetupDependencyNetworkClassification:
    """A transient dependency/index network failure detected in setup output."""

    reason_code: str
    transient_category: str
    retryable: bool
    command: str
    package: str | None
    host: str | None
    diagnostic: str

    @property
    def metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {
            "reason_code": self.reason_code,
            "command": self.command,
            "transient_category": self.transient_category,
            "retryable": self.retryable,
            "diagnostic": self.diagnostic,
        }
        if self.package is not None:
            metadata["package"] = self.package
        if self.host is not None:
            metadata["host"] = self.host
        return metadata


@dataclass(frozen=True)
class ValidationCommandResult:
    """One phase command's outcome + captured artifact paths."""

    command: str
    returncode: int
    duration_seconds: float
    stdout_path: Path
    stderr_path: Path
    phase: str = "validate"
    reason_code: str = "COMMAND_FAILED"
    stream_ids: dict[str, str | None] = field(default_factory=dict)
    retry_count: int = 0
    policy_failed: bool = False
    required: bool = True
    metadata: dict[str, object] = field(default_factory=dict)
    captured_stdout: str | None = field(default=None, repr=False, compare=False)
    captured_stderr: str | None = field(default=None, repr=False, compare=False)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.policy_failed

    @property
    def blocks_validation(self) -> bool:
        """Whether this command's outcome should fail the validation verdict.

        Advisory commands (``required: false`` in the profile) run and record
        their outcome for inspection, but a non-zero result must not block the
        workspace — see ``ValidationResult.all_passed``.
        """
        return self.required and not self.ok


@dataclass(frozen=True)
class PytestFailureEvidence:
    """Pytest failure node IDs and bounded fallback evidence parsed from output."""

    node_ids: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    @property
    def present(self) -> bool:
        return bool(self.node_ids or self.evidence)


@dataclass(frozen=True)
class CoverageCommandPlan:
    command: str
    parallel_workers_requested: int | None = None
    parallel_workers_effective: int | None = None
    parallel_distribution: str | None = None


@dataclass(frozen=True)
class ValidationCoverageResult:
    """Coverage measurement parsed from an explicit provider command."""

    provider: str
    percent: float | None
    minimum_percent: float
    enforce: bool
    status: str
    reason_code: str
    command_result: ValidationCommandResult | None = None
    gaps: list[dict[str, object]] = field(default_factory=list)
    failing_test_node_ids: list[str] = field(default_factory=list)
    failing_test_evidence: list[str] = field(default_factory=list)
    provider_failure_evidence: list[str] = field(default_factory=list)
    parallel_workers_requested: int | None = None
    parallel_workers_effective: int | None = None
    parallel_distribution: str | None = None

    @property
    def ok(self) -> bool:
        if self.status == "failed":
            return False
        if self.failing_test_node_ids or self.failing_test_evidence:
            return False
        return self.command_result is None or self.command_result.ok

    def as_metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {
            "provider": self.provider,
            "minimum_percent": float(self.minimum_percent),
            "enforce": self.enforce,
            "status": self.status,
            "reason_code": self.reason_code,
        }
        if self.percent is not None:
            metadata["percent"] = float(self.percent)
        if self.gaps:
            metadata["gaps"] = self.gaps
        if self.failing_test_node_ids:
            metadata["failing_test_node_ids"] = self.failing_test_node_ids
        if self.failing_test_evidence:
            metadata["failing_test_evidence"] = self.failing_test_evidence
        if self.provider_failure_evidence:
            metadata["provider_failure_evidence"] = self.provider_failure_evidence
        if self.parallel_workers_requested is not None:
            metadata["parallel_workers_requested"] = self.parallel_workers_requested
        if self.parallel_workers_effective is not None:
            metadata["parallel_workers_effective"] = self.parallel_workers_effective
        if self.parallel_distribution is not None:
            metadata["parallel_distribution"] = self.parallel_distribution
        return metadata


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of a profile phase sequence."""

    migration: ValidationCommandResult | None = None
    commands: list[ValidationCommandResult] = field(default_factory=list)
    coverage: ValidationCoverageResult | None = None

    @property
    def all_passed(self) -> bool:
        if self.migration is not None and not self.migration.ok:
            return False
        if self.coverage is not None and not self.coverage.ok:
            return False
        return not any(c.blocks_validation for c in self.commands)

    @property
    def total_retries(self) -> int:
        return sum(c.retry_count for c in self.commands)

    @property
    def first_failure(self) -> ValidationCommandResult | None:
        if self.migration is not None and not self.migration.ok:
            return self.migration
        first_command_failure = next((c for c in self.commands if c.blocks_validation), None)
        if first_command_failure is not None:
            return first_command_failure
        if self.coverage is not None and not self.coverage.ok:
            return self.coverage.command_result
        return None
