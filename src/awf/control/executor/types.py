"""Executor result and exception types."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from awf.runtime.planning import PlanConformanceReport
from awf.runtime.validation import ValidationCoverageResult

# A non-terminal pause that the worker resumes in place. ``blocked`` is the
# operator-driven protected-gate pause (revert/redo directive or approve-and-keep
# grant); ``recovering`` is the auto-healing provider-failure pause (#612) that
# re-runs the agent in place once the cooldown elapses. Both flow through one
# resume concurrency path (epoch-fenced CAS + execution-claim heartbeat).
PauseResumeReason = Literal["blocked", "recovering"]


@dataclass(frozen=True)
class _RebaseRecoveryResult:
    base_sha: str
    head_sha: str
    requires_pr_update: bool = False


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
