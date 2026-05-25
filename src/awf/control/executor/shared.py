"""Shared executor primitives for decomposed implementation modules."""

from __future__ import annotations

# Shared executor constants, result types, and imported domain names for extracted modules.
import hashlib as hashlib
import inspect as inspect
import json as json
import re
import time
import traceback as traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC as UTC
from datetime import datetime as datetime
from pathlib import Path
from typing import Any, Protocol, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import (
    DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS,
    DEFAULT_AGENT_WALL_TIMEOUT_SECONDS,
    AgentAdapter,
    AgentDefaults,
)
from awf.adapters.base import AgentRunError as AgentRunError
from awf.adapters.base import get_adapter as get_adapter
from awf.adapters.defaults import DEFAULT_AGENT_DEFAULTS, defaults_with_model_overrides
from awf.adapters.usage import UsageSampler
from awf.common.audit import redact_audit_text as redact_audit_text
from awf.common.audit import redact_audit_value as redact_audit_value
from awf.common.commands import AsyncCommandRunner, CommandResult
from awf.common.compose_exec import EXEC_PROCESS_CLEANUP_FAILED as EXEC_PROCESS_CLEANUP_FAILED
from awf.common.compose_exec import ComposeExecCleanupError as ComposeExecCleanupError
from awf.common.compose_exec import cleanup_failure_message as cleanup_failure_message
from awf.common.git_identity import git_identity_config_args as git_identity_config_args
from awf.common.github_client import PullRequestAdoptionMetadata as PullRequestAdoptionMetadata
from awf.common.github_client import RepoRef as RepoRef
from awf.common.logging import get_logger
from awf.common.workspace_policy import release_sync_source_branch as release_sync_source_branch
from awf.control.quality_gates import PLAN_ONLY_OUTPUT_REASON_CODE as PLAN_ONLY_OUTPUT_REASON_CODE
from awf.control.quality_gates import ProtectedFileDiff
from awf.control.quality_gates import (
    changed_paths_are_only_internal_plan_artifacts as changed_paths_are_only_internal_plan_artifacts,
)
from awf.control.quality_gates import plan_only_output_message as plan_only_output_message
from awf.control.state_machine import WorkspaceStateMachine
from awf.control.validation_fix_cycle import ValidationFixContext as ValidationFixContext
from awf.control.validation_fix_cycle import build_fix_prompt as build_fix_prompt
from awf.control.validation_fix_cycle import read_output_tail as read_output_tail
from awf.db.enums import (
    AgentRuntime,
    FailureReason,
    OperationStatus,
    OperationType,
    WorkspaceStatus,
)
from awf.db.enums import TaskClass as TaskClass
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.node.compose_manager import ComposeManager, ComposeOperationError
from awf.profiles.models import WorkspaceProfile
from awf.profiles.resolver import resolve_workspace_profile as resolve_workspace_profile
from awf.runtime.alembic_validation import (
    ALEMBIC_MIGRATION_POLICY_COMMAND as ALEMBIC_MIGRATION_POLICY_COMMAND,
)
from awf.runtime.alembic_validation import (
    ALEMBIC_MIGRATION_POLICY_PHASE as ALEMBIC_MIGRATION_POLICY_PHASE,
)
from awf.runtime.logs import LogStore
from awf.runtime.planning import (
    AGENT_PLAN_PHASE_SCOPE_VIOLATION,
    CONFORMANCE_REQUIRES_AWF_VALIDATION,
    ConformanceStallEvidence,
    PlanConformanceReport,
    PlanConformanceStatus,
    render_workspace_path,
)
from awf.runtime.pr_creator import PullRequestCreator
from awf.runtime.pr_creator import PullRequestError as PullRequestError
from awf.runtime.pr_monitor_operations import MonitorOperationHandle
from awf.runtime.pr_push_remote import (
    remote_push_url_for_workspace as remote_push_url_for_workspace,
)
from awf.runtime.validation import (
    DATABASE_GENERATED_SETUP_TIMEOUT as DATABASE_GENERATED_SETUP_TIMEOUT,
)
from awf.runtime.validation import DATABASE_REFRESH_TIMEOUT as DATABASE_REFRESH_TIMEOUT
from awf.runtime.validation import DB_GENERATED_SETUP_PHASE as DB_GENERATED_SETUP_PHASE
from awf.runtime.validation import (
    PROFILE_VALIDATION_TOOL_UNAVAILABLE as PROFILE_VALIDATION_TOOL_UNAVAILABLE,
)
from awf.runtime.validation import PYTEST_TEST_FAILURE as PYTEST_TEST_FAILURE
from awf.runtime.validation import (
    ValidationCommandResult,
    ValidationCoverageResult,
    ValidationResult,
    ValidationRunner,
)
from awf.runtime.validation import profile_phase_command_plan as profile_phase_command_plan
from awf.service.supply_chain_policy import (
    SupplyChainFinding,
    SupplyChainPolicyRefreshResult,
)


class _MonitorRunnerProto(Protocol):
    """Minimum surface the executor needs from a PR monitor runner.

    Declared as a Protocol so the executor doesn't structurally depend
    on ``PullRequestMonitorRunner`` — tests can pass a tiny stub, and
    the monitor stage is a clean extension seam for Phase 2 variants
    (merge queue, release-PR monitor, etc.)."""

    async def run(
        self: Any, *, workspace_id: str, compose_project: str, compose_file: Path
    ) -> None: ...


_log = get_logger(__name__)


def _monotonic() -> float:
    return time.monotonic()


WORKTREE_MISSING_REASON_CODE = "WORKTREE_MISSING"
PR_REEXECUTION_GUARD_REASON_CODE = "PR_REEXECUTION_GUARD"
_EXECUTOR_AUDIT_ACTOR = "executor"
_AUDIT_GIT_PUSH_EVENT = "workspace.audit.git_push"
_AUDIT_PR_CREATED_EVENT = "workspace.audit.pr_created"
_GIT_PUSH_FAILED_REASON_CODE = "GIT_PUSH_FAILED"
_PR_CREATE_FAILED_REASON_CODE = "PR_CREATE_FAILED"
GIT_AGENT_WRITABILITY_FAILED_REASON_CODE = "GIT_AGENT_WRITABILITY_FAILED"
GIT_OBJECT_MISSING_REASON_CODE = "GIT_OBJECT_MISSING"
GIT_OBJECT_MISSING_RECOVERED_REASON_CODE = "GIT_OBJECT_MISSING_RECOVERED"
POST_VALIDATION_CONFORMANCE_REPORT_GIT_FAILED_REASON_CODE = (
    "POST_VALIDATION_CONFORMANCE_REPORT_GIT_FAILED"
)
POST_VALIDATION_CONFORMANCE_REPORT_WRITE_FAILED_REASON_CODE = (
    "POST_VALIDATION_CONFORMANCE_REPORT_WRITE_FAILED"
)
POST_VALIDATION_CONFORMANCE_FAILED_REASON_CODE = "POST_VALIDATION_CONFORMANCE_FAILED"
POST_AGENT_GIT_ADD_FAILED_REASON_CODE = "POST_AGENT_GIT_ADD_FAILED"
POST_AGENT_COMMIT_FAILED_REASON_CODE = "POST_AGENT_COMMIT_FAILED"
POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE = "POST_AGENT_COMMIT_PRECOMMIT_FAILED"
POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE = "POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED"
POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE = "POST_AGENT_FORMAT_REPAIR_FAILED"
POST_AGENT_COMMIT_REPAIR_EVENT_TYPE = "workspace.post_agent_commit_repair"
POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE = POST_AGENT_COMMIT_REPAIR_EVENT_TYPE
SETUP_DEPENDENCY_NETWORK_RETRY_EVENT_TYPE = "workspace.setup_dependency_network_retry"
SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED_EVENT_TYPE = (
    "workspace.setup_dependency_network_retry_exhausted"
)
_PR_MONITOR_ADOPTED_EVENT = "workspace.pr_monitor_adopted"
_PR_MONITOR_ADOPTED_REASON_CODE = "PR_MONITOR_ADOPTED"
_PR_ADOPTION_SKIP_AGENT_REASON_CODE = "PR_ADOPTION_SKIP_AGENT"
_PR_ADOPTION_METADATA_MISSING_REASON_CODE = "PR_ADOPTION_METADATA_MISSING"
_PR_ADOPTION_MONITOR_UNAVAILABLE_REASON_CODE = "PR_ADOPTION_MONITOR_UNAVAILABLE"
_DEPRECATED_TASK_KIND_REASON_CODE = "DEPRECATED_TASK_KIND"
_UNSUPPORTED_TASK_KIND_REASON_CODE = "UNSUPPORTED_TASK_KIND"
# Task kinds the executor may legitimately drive (directly or via a monitor
# handoff). Anything else — including the deprecated ``monitor_release_pr`` —
# must fail fast and never run as feature work or recovery validation.
_SUPPORTED_TASK_KINDS = frozenset({"feature_branch_pr", "sync_feature_pr", "sync_release_pr"})
_RELEASE_SYNC_REPO_INVALID_REASON_CODE = "RELEASE_SYNC_REPO_INVALID"
_RELEASE_SYNC_GITHUB_ERROR_REASON_CODE = "RELEASE_SYNC_GITHUB_ERROR"
_RELEASE_SYNC_NO_CHANGES_EVENT = "workspace.release_pr_sync_no_changes"
_DEFAULT_RELEASE_SYNC_TARGET_BRANCH = "main"
_EXCEPTION_TRACEBACK_LIMIT = 4000
_VALIDATION_EVIDENCE_JSON_LIMIT = 20000
_VALIDATION_EVIDENCE_COVERAGE_PRIORITY_KEYS = (
    "status",
    "reason_code",
    "percent",
    "minimum_percent",
    "enforce",
    "provider",
)
_VALIDATION_EVIDENCE_RETAINED_KEY_COUNT = 5
_VALIDATION_EVIDENCE_CORE_KEYS = (
    "validation_run_id",
    "status",
    "reason_code",
    "coverage",
    "workspace_head_sha",
    "target_branch",
    "target_head_sha",
    "base_commit",
    "base_sha",
    "tier",
    "retry_count",
    "command_set_hash",
)
_FILE_DIGEST_CHUNK_SIZE = 64 * 1024

_RECOVERY_ACTIVE_OPERATION_STATUSES = {
    OperationStatus.pending.value,
    OperationStatus.running.value,
}
_VALIDATE_ONLY_RECOVERY_SOURCES = {"pr_monitor", "operator_api", "worker_restart"}
_VALIDATE_ONLY_RECOVERY_MODES = {"validate_only", "rebase_only"}
_REBASE_RECOVERY_OPERATION_IDENTITY_KEYS = (
    "source",
    "recovery_mode",
    "reason_code",
    "pr_number",
    "source_head_sha",
    "source_base_sha",
)


@dataclass(frozen=True)
class _RebaseRecoveryResult:
    base_sha: str
    head_sha: str


class _PostValidationConformanceReportGitError(RuntimeError):
    def __init__(
        self: Any,
        *,
        operation: str,
        result: CommandResult,
        cleanup_operation: str | None = None,
        cleanup_result: CommandResult | None = None,
    ) -> None:
        output = (result.stderr or result.stdout or "").strip()
        message = (
            f"post-validation conformance report git {operation} failed "
            f"(exit={result.returncode}): {output}"
        )
        if cleanup_operation is not None and cleanup_result is not None:
            cleanup_output = (cleanup_result.stderr or cleanup_result.stdout or "").strip()
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


_AWF_RUFF_FORMAT_CHECK_HOOK_ID = "awf-ruff-format-check"
_AWF_RUFF_CHECK_HOOK_ID = "awf-ruff-check"
_PRE_COMMIT_DETERMINISTIC_REPAIR_HOOK_IDS = frozenset(
    {
        "trailing-whitespace",
        "end-of-file-fixer",
        _AWF_RUFF_FORMAT_CHECK_HOOK_ID,
    }
)
_PRE_COMMIT_HOOK_ID_PATTERN = re.compile(r"^-\s*hook id:\s*(?P<hook_id>\S+)", re.MULTILINE)
_PRE_COMMIT_WOULD_REFORMAT_PATTERN = re.compile(r"^Would reformat:\s*(?P<path>\S.*)$", re.MULTILINE)
_PRE_COMMIT_FIXING_PATH_PATTERN = re.compile(r"^Fixing\s+(?P<path>\S.*)$", re.MULTILINE)
_RUFF_DIAGNOSTIC_PATTERN = re.compile(r"^\s*[A-Z]+[0-9]+\s*(?P<fixable>\[\*\])?")
_RUFF_DIAGNOSTIC_PATH_PATTERN = re.compile(r"^\s*-->\s+(?P<path>.+?):\d+:\d+")


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


def _build_planning_scope_failure(
    *,
    scope_phase: str,
    required_paths: Sequence[Path],
    offending_paths: Sequence[Path],
    summary: str,
    offending_commands: Sequence[str] = (),
) -> _PlanningRunFailure:
    required = [path.as_posix() for path in required_paths]
    offending = [path.as_posix() for path in sorted(offending_paths)]
    commands = [command for command in offending_commands if command]
    recommended_action = (
        "Retry planning from a clean workspace. Discard the premature implementation "
        "by default, and salvage the preserved branch only after explicit operator approval."
    )
    artifact = required[0] if required else "the configured plan artifact"
    if offending:
        message = f"{summary}: {', '.join(offending[:10])}. {recommended_action}"
    else:
        message = f"{summary}. {recommended_action}"
    planning_scope = {
        "scope_phase": scope_phase,
        "required_paths": required,
        "offending_paths": offending,
        "offending_commands": commands,
        "recommended_action": recommended_action,
        "recovery_strategy": "discard_and_replan",
        "salvage_policy": "explicit_salvage_required",
        "plan_artifact": artifact,
    }
    return _PlanningRunFailure(
        message=message,
        reason_code=AGENT_PLAN_PHASE_SCOPE_VIOLATION,
        details={
            "planning_scope": planning_scope,
            "recommended_action": recommended_action,
            "recovery_strategy": "discard_and_replan",
            "salvage_policy": "explicit_salvage_required",
            # Temporary compatibility for older console code that read `scope`.
            "scope": {
                "scope_phase": scope_phase,
                "required_paths": required,
                "forbidden_paths": offending,
                "recommended_action": recommended_action,
            },
        },
    )


class _MonitorRebaseRecoveryError(RuntimeError):
    """Raised when monitor-driven rebase recovery cannot update the PR branch."""


def _get_active_recovery_payload(workspace: Any) -> dict[str, Any] | None:
    """Return the active monitor/operator recovery payload (or ``None``).

    Recovery operations use a pending/running operation with ``recovery_mode``
    set. Validate-only recovery is recorded as ``validate``; public operator
    rebase requests are recorded as ``rebase`` but run through the same
    rebase-only executor path.
    """
    operations = getattr(workspace, "operations", None) or []
    for operation in operations:
        if operation.status not in _RECOVERY_ACTIVE_OPERATION_STATUSES:
            continue
        operation_type = getattr(operation, "type", None)
        if operation_type not in {
            OperationType.validate.value,
            OperationType.rebase.value,
        }:
            continue
        payload = operation.payload
        if not _is_validate_only_recovery_payload(payload):
            continue
        recovery_payload = cast(dict[str, Any], payload)
        if (
            operation_type == OperationType.rebase.value
            and recovery_payload.get("recovery_mode") != "rebase_only"
        ):
            continue
        return recovery_payload
    return None


def _is_validate_only_recovery_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    return (
        payload.get("source") in _VALIDATE_ONLY_RECOVERY_SOURCES
        and payload.get("recovery_mode") in _VALIDATE_ONLY_RECOVERY_MODES
    )


def _planning_validation_handoff_from_recovery_payload(
    *,
    workspace_id: str,
    profile: WorkspaceProfile,
    recovery_payload: Mapping[str, Any],
) -> _PlanningValidationHandoff | None:
    conformance_payload = recovery_payload.get("conformance")
    conformance = conformance_payload if isinstance(conformance_payload, Mapping) else {}
    reason_code = _recovery_conformance_reason_code(recovery_payload, conformance)
    if reason_code != CONFORMANCE_REQUIRES_AWF_VALIDATION:
        return None
    try:
        plan_path = render_workspace_path(
            _recovery_conformance_path(
                conformance,
                key="plan_path",
                fallback=profile.planning.plan_path,
            ),
            workspace_id=workspace_id,
        )
        report_path = render_workspace_path(
            _recovery_conformance_path(
                conformance,
                key="report_path",
                fallback=profile.planning.conformance_report_path,
            ),
            workspace_id=workspace_id,
        )
    except ValueError:
        plan_path = render_workspace_path(
            profile.planning.plan_path,
            workspace_id=workspace_id,
        )
        report_path = render_workspace_path(
            profile.planning.conformance_report_path,
            workspace_id=workspace_id,
        )
    gaps = _recovery_conformance_gaps(conformance)
    summary_value = conformance.get("summary")
    summary = (
        summary_value.strip()
        if isinstance(summary_value, str) and summary_value.strip()
        else "Conformance requires AWF-owned validation evidence."
    )
    iteration = _int_or_none(conformance.get("iteration"))
    if iteration is None:
        iteration = _int_or_none(recovery_payload.get("iteration"))
    max_iterations = _int_or_none(conformance.get("max_iterations"))
    if max_iterations is None:
        max_iterations = _int_or_none(recovery_payload.get("max_iterations"))
    return _PlanningValidationHandoff(
        report=PlanConformanceReport(
            status=PlanConformanceStatus.needs_iteration,
            summary=summary,
            gaps=gaps or ("AWF-owned validation evidence is missing or stale.",),
            reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        ),
        plan_path=plan_path,
        report_path=report_path,
        iteration=iteration if iteration is not None else 0,
        max_iterations=(
            max_iterations if max_iterations is not None else profile.planning.max_iterations
        ),
    )


def _recovery_conformance_reason_code(
    recovery_payload: Mapping[str, Any],
    conformance: Mapping[str, Any],
) -> str | None:
    for value in (
        conformance.get("report_reason_code"),
        conformance.get("reason_code"),
        recovery_payload.get("conformance_reason_code"),
        recovery_payload.get("conformance_handoff_reason_code"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip().upper().replace("-", "_")
    return None


def _recovery_conformance_path(
    conformance: Mapping[str, Any],
    *,
    key: str,
    fallback: str,
) -> str:
    value = conformance.get(key)
    return value if isinstance(value, str) and value.strip() else fallback


def _recovery_conformance_gaps(conformance: Mapping[str, Any]) -> tuple[str, ...]:
    value = conformance.get("gaps")
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, str) and value.strip():
        return (value.strip(),)
    return ()


def _recovery_needs_existing_pr_push(
    recovery_payload: Mapping[str, Any],
    *,
    validated_workspace_head_sha: str | None,
    rebase_recovery_result: _RebaseRecoveryResult | None,
) -> bool:
    """Return true when recovery produced local commits that are not on the PR yet."""
    if not validated_workspace_head_sha:
        return False
    validated_head = validated_workspace_head_sha.strip()
    if not validated_head:
        return False
    recovery_mode = recovery_payload.get("recovery_mode")
    if recovery_mode == "validate_only":
        if rebase_recovery_result is not None:
            return False
        source_head_sha = recovery_payload.get("source_head_sha")
        if not isinstance(source_head_sha, str) or not source_head_sha.strip():
            return False
        return validated_head != source_head_sha.strip()
    if recovery_mode == "rebase_only":
        if rebase_recovery_result is None:
            return False
        # Push only when AWF-owned work, such as validation fixes or a
        # conformance report commit, advanced HEAD past the pushed rebase commit.
        return validated_head != rebase_recovery_result.head_sha
    return False


def _is_callback_terminal_status(status: str) -> bool:
    try:
        workspace_status = WorkspaceStatus(status)
    except ValueError:  # pragma: no cover - defensive for legacy bad rows
        return False
    return WorkspaceStateMachine.is_callback_terminal(workspace_status)


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _supply_chain_block_message(findings: Sequence[SupplyChainFinding]) -> str:
    blocking = [finding for finding in findings if finding.severity == "blocking"]
    if not blocking:
        return "Supply-chain policy blocked workspace output."
    lines = ["Supply-chain policy blocked workspace output:"]
    for finding in blocking[:5]:
        guidance = finding.details.get("recovery_guidance")
        subject = f" ({finding.subject_path})" if finding.subject_path else ""
        lines.append(f"- {finding.reason_code}{subject}: {finding.explanation}")
        if isinstance(guidance, str) and guidance:
            lines.append(f"  Recovery: {guidance}")
    if len(blocking) > 5:
        lines.append(f"- {len(blocking) - 5} additional blocking finding(s).")
    return "\n".join(lines)


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _metadata_str(metadata: Mapping[str, object], key: str) -> str | None:
    value = metadata.get(key)
    return value if isinstance(value, str) else None


def _metadata_int(metadata: Mapping[str, object], key: str) -> int | None:
    value = metadata.get(key)
    return value if isinstance(value, int) else None


def _metadata_number(metadata: Mapping[str, object], key: str) -> int | float | None:
    value = metadata.get(key)
    return value if isinstance(value, int | float) else None


@dataclass(frozen=True)
class ExecutorConfig:
    """Config for WorkspaceExecutor. All paths are host-absolute."""

    worktrees_root: Path
    """Parent dir containing one subdir per workspace (``<root>/<workspace_id>``)."""

    compose_projects_root: Path
    """Where per-workspace compose.yml was rendered by the Provisioner."""

    default_models: Mapping[AgentRuntime, str] | None = None
    """Legacy model-only overrides. Prefer ``agent_defaults`` for new code."""

    agent_defaults: Mapping[AgentRuntime, AgentDefaults] = DEFAULT_AGENT_DEFAULTS
    """Default model and effort policy for each agent runtime."""

    agent_wall_timeout_seconds: float = DEFAULT_AGENT_WALL_TIMEOUT_SECONDS
    """Maximum wall-clock seconds for one agent CLI run. Default: 7200 seconds."""

    agent_idle_timeout_seconds: float = DEFAULT_AGENT_IDLE_TIMEOUT_SECONDS
    """Maximum seconds with no agent stdout/stderr. Default: 3600 seconds."""

    max_validation_fix_passes: int = 5
    """Maximum fix attempts on validation failure. After the initial agent
    run + validation, if validation fails, the executor re-invokes the
    coding CLI with a fix prompt (failing command + stdout/stderr tails)
    and re-validates. ``0`` disables the loop (single-shot legacy
    behaviour); the default mirrors the PR monitor's fix-cycle cap."""

    planning_max_iterations_default: int = 3
    """Default plan-conformance remediation iterations when a profile omits
    planning.max_iterations. Explicit profile values win."""


@dataclass(frozen=True)
class _ConformanceSalvageExecutionResult:
    status: str
    prompt_override: str | None = None


_PR_NUMBER_RE = re.compile(r"/pull/(\d+)(?=[/?#]|$)")

__all__ = (
    "AGENT_PLAN_PHASE_SCOPE_VIOLATION",
    "ALEMBIC_MIGRATION_POLICY_COMMAND",
    "ALEMBIC_MIGRATION_POLICY_PHASE",
    "AgentAdapter",
    "AgentDefaults",
    "AgentRunError",
    "AgentRuntime",
    "AsyncCommandRunner",
    "AsyncSession",
    "CONFORMANCE_REQUIRES_AWF_VALIDATION",
    "Callable",
    "CommandResult",
    "ComposeExecCleanupError",
    "ComposeManager",
    "ComposeOperationError",
    "ConformanceStallEvidence",
    "DATABASE_GENERATED_SETUP_TIMEOUT",
    "DATABASE_REFRESH_TIMEOUT",
    "DB_GENERATED_SETUP_PHASE",
    "EXEC_PROCESS_CLEANUP_FAILED",
    "ExecutorConfig",
    "FailureReason",
    "GIT_OBJECT_MISSING_REASON_CODE",
    "GIT_OBJECT_MISSING_RECOVERED_REASON_CODE",
    "LogStore",
    "Mapping",
    "MonitorOperationHandle",
    "OperationStatus",
    "OperationType",
    "PLAN_ONLY_OUTPUT_REASON_CODE",
    "POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE",
    "POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE",
    "POST_AGENT_GIT_ADD_FAILED_REASON_CODE",
    "POST_VALIDATION_CONFORMANCE_FAILED_REASON_CODE",
    "POST_VALIDATION_CONFORMANCE_REPORT_GIT_FAILED_REASON_CODE",
    "POST_VALIDATION_CONFORMANCE_REPORT_WRITE_FAILED_REASON_CODE",
    "PROFILE_VALIDATION_TOOL_UNAVAILABLE",
    "PR_REEXECUTION_GUARD_REASON_CODE",
    "PYTEST_TEST_FAILURE",
    "Path",
    "PlanConformanceReport",
    "ProtectedFileDiff",
    "PullRequestAdoptionMetadata",
    "PullRequestCreator",
    "PullRequestError",
    "RepoRef",
    "SETUP_DEPENDENCY_NETWORK_RETRY_EVENT_TYPE",
    "SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED_EVENT_TYPE",
    "Sequence",
    "SupplyChainPolicyRefreshResult",
    "TaskClass",
    "UTC",
    "UsageSampler",
    "ValidationCommandResult",
    "ValidationCoverageResult",
    "ValidationResult",
    "ValidationRunner",
    "WORKTREE_MISSING_REASON_CODE",
    "Workspace",
    "WorkspaceProfile",
    "WorkspaceRepository",
    "WorkspaceStatus",
    "_AUDIT_GIT_PUSH_EVENT",
    "_AUDIT_PR_CREATED_EVENT",
    "_ConformanceSalvageExecutionResult",
    "_CoverageEvidenceResult",
    "_DEFAULT_RELEASE_SYNC_TARGET_BRANCH",
    "_DEPRECATED_TASK_KIND_REASON_CODE",
    "_EXCEPTION_TRACEBACK_LIMIT",
    "_EXECUTOR_AUDIT_ACTOR",
    "_FILE_DIGEST_CHUNK_SIZE",
    "_GIT_PUSH_FAILED_REASON_CODE",
    "_MonitorRebaseRecoveryError",
    "_MonitorRunnerProto",
    "_PR_ADOPTION_METADATA_MISSING_REASON_CODE",
    "_PR_ADOPTION_MONITOR_UNAVAILABLE_REASON_CODE",
    "_PR_ADOPTION_SKIP_AGENT_REASON_CODE",
    "_PR_CREATE_FAILED_REASON_CODE",
    "_PR_MONITOR_ADOPTED_EVENT",
    "_PR_MONITOR_ADOPTED_REASON_CODE",
    "_PR_NUMBER_RE",
    "_PlanningRunFailure",
    "_PlanningValidationHandoff",
    "_PostValidationConformanceReportGitError",
    "_PostValidationConformanceReportWriteError",
    "_PrReexecutionGuardResult",
    "_RECOVERY_ACTIVE_OPERATION_STATUSES",
    "_RELEASE_SYNC_GITHUB_ERROR_REASON_CODE",
    "_RELEASE_SYNC_NO_CHANGES_EVENT",
    "_RELEASE_SYNC_REPO_INVALID_REASON_CODE",
    "_RebaseRecoveryResult",
    "_SUPPORTED_TASK_KINDS",
    "_UNSUPPORTED_TASK_KIND_REASON_CODE",
    "_VALIDATE_ONLY_RECOVERY_SOURCES",
    "_VALIDATION_EVIDENCE_CORE_KEYS",
    "_VALIDATION_EVIDENCE_COVERAGE_PRIORITY_KEYS",
    "_VALIDATION_EVIDENCE_JSON_LIMIT",
    "_build_planning_scope_failure",
    "_get_active_recovery_payload",
    "_int_or_none",
    "_is_callback_terminal_status",
    "_is_validate_only_recovery_payload",
    "_metadata_int",
    "_metadata_number",
    "_metadata_str",
    "_monotonic",
    "_planning_validation_handoff_from_recovery_payload",
    "_recovery_needs_existing_pr_push",
    "_str_or_none",
    "_supply_chain_block_message",
    "async_sessionmaker",
    "cast",
    "changed_paths_are_only_internal_plan_artifacts",
    "cleanup_failure_message",
    "datetime",
    "defaults_with_model_overrides",
    "get_adapter",
    "git_identity_config_args",
    "hashlib",
    "inspect",
    "json",
    "plan_only_output_message",
    "profile_phase_command_plan",
    "redact_audit_text",
    "redact_audit_value",
    "release_sync_source_branch",
    "remote_push_url_for_workspace",
    "render_workspace_path",
    "resolve_workspace_profile",
    "traceback",
)
