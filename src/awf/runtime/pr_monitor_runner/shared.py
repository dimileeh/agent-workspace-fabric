"""Shared PR monitor runner primitives for decomposed implementation modules."""

from __future__ import annotations

# Shared monitor-runner constants, errors, result types, and imported domain names.
import hashlib as hashlib
import json as json
import re
import time as time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from collections.abc import Iterable as Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import timedelta as timedelta
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentAdapter, AgentRunError
from awf.common.audit import redact_audit_text as redact_audit_text
from awf.common.commands import AsyncCommandRunner, CommandResult
from awf.common.compose_exec import EXEC_PROCESS_CLEANUP_FAILED as EXEC_PROCESS_CLEANUP_FAILED
from awf.common.compose_exec import ComposeExecCleanupError as ComposeExecCleanupError
from awf.common.compose_exec import cleanup_failure_message as cleanup_failure_message
from awf.common.github_client import GitHubClient, RepoRef
from awf.common.logging import get_logger
from awf.control.protected_file_diffs import (
    changed_paths_from_name_status_z as _parse_name_status_z,
)
from awf.control.quality_gates import ProtectedFileDiff, QualityGateViolation
from awf.control.state_machine import WorkspaceStateMachine as WorkspaceStateMachine
from awf.db.enums import FailureReason as FailureReason
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Operation, Workspace
from awf.db.repositories import WorkspaceEventCreate, WorkspaceRepository
from awf.db.repositories import pr_feedback_body_hash as pr_feedback_body_hash
from awf.runtime.logs import LogStore, WorkspaceLogSink
from awf.runtime.ownership import (
    AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
)
from awf.runtime.planning import CONFORMANCE_REQUIRES_AWF_VALIDATION
from awf.runtime.pr_monitor import Abort as Abort
from awf.runtime.pr_monitor import (
    AbortReason,
    CheckFailure,
    MonitorAction,
    MonitorConfig,
    MonitorState,
    PRStatus,
    ReviewComment,
    ReviewThread,
    decide,
)
from awf.runtime.pr_monitor import AddressComments as AddressComments
from awf.runtime.pr_monitor import Merge as Merge
from awf.runtime.pr_monitor import MergeStateStatus as MergeStateStatus
from awf.runtime.pr_monitor import NotifyHuman as NotifyHuman
from awf.runtime.pr_monitor import ReportCiFailure as ReportCiFailure
from awf.runtime.pr_monitor import RerunTransientCI as RerunTransientCI
from awf.runtime.pr_monitor import ShortCircuitCompleted as ShortCircuitCompleted
from awf.runtime.pr_monitor import SyncBase as SyncBase
from awf.runtime.pr_monitor import WaitForCI as WaitForCI
from awf.runtime.pr_monitor import (
    _agent_can_triage_review_comment as _agent_can_triage_review_comment,
)
from awf.runtime.pr_monitor import _ci_transient_rerun_count as _ci_transient_rerun_count
from awf.runtime.pr_monitor import _ci_transient_rerun_state_key as _ci_transient_rerun_state_key
from awf.runtime.pr_monitor import _is_bot_author as _is_bot_author
from awf.runtime.pr_monitor import _is_bot_review_thread as _is_bot_review_thread
from awf.runtime.pr_monitor import _needs_comment_attention as _needs_comment_attention
from awf.runtime.pr_monitor import _review_thread_body_hash as _review_thread_body_hash
from awf.runtime.pr_monitor import _review_thread_body_state_key as _review_thread_body_state_key
from awf.runtime.pr_monitor_operations import (
    RETRYABLE_MONITOR_OPERATION_STATUSES as RETRYABLE_MONITOR_OPERATION_STATUSES,
)
from awf.runtime.pr_monitor_operations import MonitorOperationHandle
from awf.runtime.pr_monitor_operations import (
    begin_monitor_state_operation as begin_monitor_state_operation,
)
from awf.runtime.pr_monitor_operations import (
    record_monitor_state_operation as record_monitor_state_operation,
)
from awf.service.gc import run_workspace_filesystem_gc as run_workspace_filesystem_gc
from awf.service.merge_queue import MergeQueueBlocker
from awf.service.provider_recovery import (
    PROVIDER_AUTH_FAILED,
    PROVIDER_MODEL_CIRCUIT_OPEN_REASON,
    PROVIDER_RECOVERY_STATE_KEY,
)
from awf.service.provider_recovery import (
    PROVIDER_RECOVERY_COOLDOWN_EVENT as PROVIDER_RECOVERY_COOLDOWN_EVENT,
)
from awf.service.provider_recovery import _is_auth_failure_metadata as _is_auth_failure_metadata
from awf.service.provider_recovery import (
    provider_recovery_metadata_from_failure as provider_recovery_metadata_from_failure,
)
from awf.service.staleness import (
    REASON_BUILD_CONFIG,
    REASON_DEPENDENCY,
    REASON_OVERLAP,
    REASON_PLAN_ARTIFACT_OVERLAP,
    REASON_SCHEMA,
    REASON_TARGET_ADVANCED,
)

_log = get_logger(__name__)
_RETRYABLE_RECOVERY_TERMINAL_OPERATION_STATUSES = RETRYABLE_MONITOR_OPERATION_STATUSES


# Verdicts the CLI reply parser can produce. Kept as a type alias so
# callers (and tests) can match against a closed set.


@dataclass(frozen=True)
class _BaseFetchHandlingResult:
    retry: bool
    reason_code: str


def _task_policy_with_monitor_circuit_retry_state(
    task_policy: Mapping[str, Any] | None,
    *,
    provider: str,
    model: str,
    cooldown_until: datetime | None,
    last_reason_code: str | None,
) -> dict[str, Any]:
    """Return workspace task policy with monitor retry metadata for provider recovery."""
    policy = dict(task_policy or {})
    raw_state = policy.get(PROVIDER_RECOVERY_STATE_KEY)
    recovery_state = dict(raw_state) if isinstance(raw_state, Mapping) else {}
    recovery_state.update(
        {
            "action": "retry",
            "decision_reason_code": PROVIDER_MODEL_CIRCUIT_OPEN_REASON,
            "source_provider": provider,
            "source_model": model,
            "source_reason_code": last_reason_code or PROVIDER_MODEL_CIRCUIT_OPEN_REASON,
            "target_provider": provider,
            "target_model": model,
        }
    )
    recovery_state["not_before"] = (cooldown_until or datetime.now(UTC)).isoformat()
    policy[PROVIDER_RECOVERY_STATE_KEY] = recovery_state
    return policy


class PostMergeTargetReconciler(Protocol):
    """Best-effort target-branch repair hook invoked after a PR is merged."""

    async def __call__(  # pragma: no cover - Protocol declaration only.
        self: Any, *, repo_url: str, branch: str, workspace_id: str
    ) -> object: ...


@dataclass(frozen=True)
class MonitorRunnerConfig:
    """Operational knobs for the runner (separate from MonitorConfig so
    we can tune timing without touching the decision logic)."""

    # Max number of outer loop iterations before we stop (safety net
    # against a decision-loop bug; the outer loop is uncapped for
    # ``WaitForCI`` so idle polls don't count). A legitimate monitor
    # session should always exit via a terminal action well before this.
    max_outer_iterations: int = 10_000
    # Max fix_cycle re-polls inside a single AddressComments action.
    max_fix_cycle_passes: int = 5
    # Transient GitHub outages can surface through `git fetch`, not only `gh`.
    # Keep base refresh authoritative, but retry remote 5xx/network failures
    # before declaring the monitor infrastructure-failed.
    transient_base_fetch_max_retries: int = 5
    transient_base_fetch_initial_backoff_seconds: float = 5.0
    transient_base_fetch_max_backoff_seconds: float = 120.0


_NON_TRANSIENT_GITHUB_ERROR_MARKERS = (
    "authentication",
    "auth failed",
    "bad credentials",
    "not logged in",
    "please run gh auth login",
    "not found",
    "could not resolve to a repository",
    "could not resolve to a node",
)
_TRANSIENT_GITHUB_ERROR_MARKERS = (
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "500 internal server",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "bad gateway",
    "gateway timeout",
    "internal server error",
    "returned error: 500",
    "returned error: 502",
    "returned error: 503",
    "returned error: 504",
    "service unavailable",
    "temporarily unavailable",
    "try again",
    "timed out",
    "timeout",
    "connection reset",
    "connection refused",
    "connection aborted",
    "tls handshake timeout",
    "network",
    "eof",
    "rate limit",
    "secondary rate limit",
    "abuse detection",
    "something went wrong",
)
_GITHUB_TRANSIENT_RETRY_REASON = "GITHUB_TRANSIENT_RETRY"
_PR_MONITOR_AUDIT_ACTOR = "pr_monitor"
_GIT_PUSH_FAILED_REASON = "GIT_PUSH_FAILED"
_MONITOR_POLICY_BLOCKED_REASON = "MONITOR_POLICY_BLOCKED"
_GIT_FETCH_BASE_FAILED_REASON = "GIT_FETCH_BASE_FAILED"
_GIT_BASE_FETCH_TRANSIENT_RETRY_REASON = "GIT_BASE_FETCH_TRANSIENT_RETRY"
_GIT_BASE_FETCH_TRANSIENT_RETRY_EXHAUSTED_REASON = "GIT_BASE_FETCH_TRANSIENT_RETRY_EXHAUSTED"
_GIT_BASE_BEHIND_FAILED_REASON = "GIT_BASE_BEHIND_FAILED"
_GIT_MIRROR_BROKEN_REF_REMOVED_REASON = "GIT_MIRROR_BROKEN_REF_REMOVED"
_GIT_MIRROR_BROKEN_REF_REPAIR_MAX_ATTEMPTS = 5
_REMOTE_TRACKING_REF_LOCK_RACE_RE = re.compile(
    r"cannot lock ref ['\"]?refs/remotes/[^'\"]+['\"]?: is at "
    r"[0-9a-f]{7,40} but expected [0-9a-f]{7,40}.*"
    r"unable to update local ref",
    re.IGNORECASE | re.DOTALL,
)
_CI_TRANSIENT_RERUN_REASON = "CI_TRANSIENT_RERUN"
_CI_TRANSIENT_RERUN_FAILED_REASON = "CI_TRANSIENT_RERUN_FAILED"
_PROTECTED_SCOPE_REPAIR_FAILED_REASON = "PROTECTED_SCOPE_REPAIR_FAILED"
_PROTECTED_SCOPE_PUSH_BLOCKED_REASON = "PROTECTED_SCOPE_PUSH_BLOCKED"
_PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON = "PROTECTED_SCOPE_DIFF_UNAVAILABLE"
_REPAIR_WORKTREE_STATUS_FAILED_REASON = "REPAIR_WORKTREE_STATUS_FAILED"
_REPAIR_START_HEAD_UNAVAILABLE_REASON = "REPAIR_START_HEAD_UNAVAILABLE"
_PRE_EXISTING_DIRTY_WORKTREE_REASON = "PRE_EXISTING_DIRTY_WORKTREE"
_VALIDATION_INSUFFICIENT_STALE_REASON = "validation_insufficient_tier"
# Staleness reason codes that a successful SyncBase legitimately remediates:
# the target-derived findings ``evaluate_staleness`` produces from the target
# diff. Bringing ``base_sha`` up to ``origin/<base>`` makes all of these go
# stale-free. Intrinsic reasons (e.g. ``docs_task_scope_violation``) are NOT
# in this set — a rebase does not satisfy their remediation/validation, so the
# SyncBase refresh must leave them active.
_SYNC_BASE_RESOLVABLE_STALE_REASONS: frozenset[str] = frozenset(
    {
        REASON_TARGET_ADVANCED,
        REASON_OVERLAP,
        REASON_SCHEMA,
        REASON_DEPENDENCY,
        REASON_BUILD_CONFIG,
        REASON_PLAN_ARTIFACT_OVERLAP,
    }
)
_RECOVERY_SNAPSHOT_ALREADY_HANDLED_REASON = "RECOVERY_SNAPSHOT_ALREADY_HANDLED"
_AUDIT_GIT_PUSH_EVENT = "workspace.audit.git_push"
_AUDIT_MERGE_ATTEMPT_EVENT = "workspace.audit.merge_attempt"
_AUDIT_MERGE_RESULT_EVENT = "workspace.audit.merge_result"
_AUDIT_COMMENT_RESOLUTION_EVENT = "workspace.audit.comment_resolution"
_REDACTION = "<redacted>"
_URL_CREDENTIAL_RE = re.compile(r"(https?://)([^/\s:@]+(?::[^/\s@]+)?@)")
_AUTHORIZATION_BEARER_RE = re.compile(
    r"(\bAuthorization:\s*Bearer\s+)([A-Za-z0-9._~+/=-]{8,})",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])("
    r"gh[apousr]_[A-Za-z0-9_]{8,}|"
    r"github_pat_[A-Za-z0-9_]{8,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|"
    r"sk-ant-[A-Za-z0-9_-]{8,}|"
    r"AIza[A-Za-z0-9_-]{12,}|"
    r"xox[baprs]-[A-Za-z0-9-]{8,}"
    r")(?![A-Za-z0-9])"
)
_BROKEN_AWF_REF_RE = re.compile(r"refs/heads/awf/(ws_[A-Za-z0-9_-]+)")
_BASE_FETCH_RETRY_COUNT_KEY_PREFIX = "__awf_base_fetch_retry_count:"
_SYNC_BASE_NO_PROGRESS_SIGNATURE_KEY = "__awf_sync_base_no_progress_signature"
_SYNC_BASE_NO_PROGRESS_COUNT_KEY = "__awf_sync_base_no_progress_count"
_ACTIVE_RECOVERY_OPERATION_STATUSES = frozenset(
    {
        OperationStatus.pending.value,
        OperationStatus.running.value,
    }
)
_TERMINAL_WORKSPACE_STATUSES = {
    WorkspaceStatus.completed.value,
    WorkspaceStatus.failed.value,
    WorkspaceStatus.cancelled.value,
    WorkspaceStatus.destroyed.value,
}
_PLANNING_VALIDATION_HANDOFF_EVENT = "workspace.planning_conformance_requires_awf_validation"
_POST_VALIDATION_CONFORMANCE_SATISFIED_EVENT = "workspace.post_validation_conformance_satisfied"


def _normalize_conformance_handoff_reason_code(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().upper().replace("-", "_")


def _monitor_recovery_conformance_payload(workspace: Workspace) -> dict[str, Any] | None:
    """Return conformance handoff context for monitor-dispatched validation recovery."""
    events = getattr(workspace, "events", None) or []
    for event in reversed(events):
        event_type = getattr(event, "event_type", None)
        if event_type == _POST_VALIDATION_CONFORMANCE_SATISFIED_EVENT:
            return None
        if event_type != _PLANNING_VALIDATION_HANDOFF_EVENT:
            continue
        raw_payload = getattr(event, "payload", None)
        payload = raw_payload if isinstance(raw_payload, Mapping) else {}
        reason_code = (
            _normalize_conformance_handoff_reason_code(payload.get("report_reason_code"))
            or _normalize_conformance_handoff_reason_code(payload.get("reason_code"))
            or _normalize_conformance_handoff_reason_code(getattr(event, "reason_code", None))
        )
        if reason_code != CONFORMANCE_REQUIRES_AWF_VALIDATION:
            continue
        conformance: dict[str, Any] = {
            "reason_code": reason_code,
            "report_reason_code": reason_code,
        }
        for key in ("summary", "gaps", "plan_path", "report_path", "iteration", "max_iterations"):
            if key in payload:
                conformance[key] = payload[key]
        return {"conformance": conformance}
    return None


def _is_active_pr_monitor_recovery_operation(operation: Operation) -> bool:
    """Return whether an operation is an unfinished PR monitor recovery action."""
    return (
        operation.status in _ACTIVE_RECOVERY_OPERATION_STATUSES
        and operation.type == OperationType.validate.value
        and isinstance(operation.payload, dict)
        and operation.payload.get("source") == "pr_monitor"
        and operation.payload.get("recovery_mode") is not None
    )


class BaseFetchError(Exception):
    """Base branch refresh failed; PR monitor must not use stale refs."""


class BaseBehindCountError(Exception):
    """Base-behind calculation failed; PR monitor must not assume zero."""


class ProtectedScopeDiffError(Exception):
    """Committed diff against the remote PR branch could not be verified."""


@dataclass(frozen=True)
class _ProtectedScopeRollbackDeltaEvidence:
    reverted_paths: tuple[str, ...]
    cleanup_paths: tuple[str, ...] = ()
    collection_errors: tuple[dict[str, object], ...] = ()


@dataclass
class _RunnerDeps:
    """All side-effect collaborators in one bag — easy to fake in tests."""

    session_factory: async_sessionmaker[AsyncSession]
    runner: AsyncCommandRunner
    adapter: AgentAdapter
    gh: GitHubClient
    sleep: Callable[[float], Awaitable[None]]
    provider_recovery_default_model: str | None = None
    log_store: LogStore | None = None
    post_merge_target_reconciler: PostMergeTargetReconciler | None = None


class ProviderRecoveryFallbackError(Exception):
    """Raised when a retryable provider failure triggers a fallback workspace."""


class ProviderRecoveryRetryError(Exception):
    """Raised when an operation should back off and retry later due to a provider error."""


class ProviderRecoveryAuthError(Exception):
    """Raised when PR-monitor repair cannot continue because provider auth is broken."""


class _MonitorPolicyBlockedError(Exception):
    """Raised when monitor-authored changes violate blocking workspace policy."""


class _MonitorAgentRuntimeOwnershipRepairFailedError(RuntimeError):
    """Raised when monitor cannot repair agent worktree ownership."""

    @property
    def reason_code(self: Any) -> str:
        """Return the fixed reason code for ownership repair failures."""
        return AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE


# ── Helpers ────────────────────────────────────────────────────────────────

_VERDICT_FALSE_POSITIVE = re.compile(r"\bFALSE\s+POSITIVE\s*:", re.IGNORECASE)
_VERDICT_DEFER = re.compile(r"\bDEFER\s*:", re.IGNORECASE)
_AWF_VERDICT = re.compile(
    r"\bAWF-VERDICT\s*:\s*"
    r"(?P<label>FIXED|FALSE\s+POSITIVE|DEFER|NEEDS_HUMAN)"
    r"\s*:\s*(?P<reason>[^\n\r]+)",
    re.IGNORECASE,
)

_PENDING_CHECK_STATUSES = frozenset(
    {
        "EXPECTED",
        "IN_PROGRESS",
        "PENDING",
        "QUEUED",
        "REQUESTED",
        "WAITING",
    }
)
_TERMINAL_CHECK_STATUSES = frozenset({"COMPLETED", "ERROR", "FAILURE", "SUCCESS"})
_TERMINAL_CHECK_CONCLUSIONS = frozenset(
    {
        "ACTION_REQUIRED",
        "CANCELLED",
        "FAILURE",
        "NEUTRAL",
        "SKIPPED",
        "STALE",
        "SUCCESS",
        "TIMED_OUT",
    }
)

_PR_MONITOR_STALE_REASON_MESSAGES = {
    "validation_insufficient_tier": (
        "Required validation tier has not passed for this merge candidate."
    ),
    "docs_task_scope_violation": "Changed files are outside the docs task scope.",
    "STALE_TARGET_ADVANCED": "Target branch advanced after this merge candidate was validated.",
    "STALE_OVERLAP": "Target branch changed an owned path for this merge candidate.",
    "STALE_DEPENDENCY": "Target branch changed dependency files for this merge candidate.",
    "STALE_BUILD_CONFIG": "Target branch changed build configuration for this merge candidate.",
    "STALE_SCHEMA": "Target branch changed schema files for this merge candidate.",
    "stale": "Merge candidate is stale.",
}

_PR_MONITOR_REASON_CODES_BY_STALE_REASON = {
    "validation_insufficient_tier": "VALIDATION_INSUFFICIENT_TIER",
    "docs_task_scope_violation": "DOCS_TASK_SCOPE_VIOLATION",
    "stale": "STALE",
}

__all__ = (
    "Abort",
    "AbortReason",
    "AddressComments",
    "AgentRunError",
    "BaseBehindCountError",
    "BaseFetchError",
    "CheckFailure",
    "CommandResult",
    "ComposeExecCleanupError",
    "EXEC_PROCESS_CLEANUP_FAILED",
    "FailureReason",
    "Iterable",
    "Mapping",
    "Merge",
    "MergeQueueBlocker",
    "MergeStateStatus",
    "MonitorAction",
    "MonitorConfig",
    "MonitorOperationHandle",
    "MonitorRunnerConfig",
    "MonitorState",
    "NotifyHuman",
    "OperationStatus",
    "OperationType",
    "PROVIDER_AUTH_FAILED",
    "PROVIDER_MODEL_CIRCUIT_OPEN_REASON",
    "PROVIDER_RECOVERY_COOLDOWN_EVENT",
    "PRStatus",
    "Path",
    "PostMergeTargetReconciler",
    "ProtectedFileDiff",
    "ProtectedScopeDiffError",
    "ProviderRecoveryAuthError",
    "ProviderRecoveryFallbackError",
    "ProviderRecoveryRetryError",
    "QualityGateViolation",
    "RepoRef",
    "ReportCiFailure",
    "RerunTransientCI",
    "ReviewComment",
    "ReviewThread",
    "Sequence",
    "ShortCircuitCompleted",
    "SyncBase",
    "UTC",
    "WaitForCI",
    "Workspace",
    "WorkspaceEventCreate",
    "WorkspaceLogSink",
    "WorkspaceRepository",
    "WorkspaceStateMachine",
    "WorkspaceStatus",
    "_AUDIT_COMMENT_RESOLUTION_EVENT",
    "_AUDIT_GIT_PUSH_EVENT",
    "_AUDIT_MERGE_ATTEMPT_EVENT",
    "_AUDIT_MERGE_RESULT_EVENT",
    "_AUTHORIZATION_BEARER_RE",
    "_AWF_VERDICT",
    "_BASE_FETCH_RETRY_COUNT_KEY_PREFIX",
    "_BaseFetchHandlingResult",
    "_CI_TRANSIENT_RERUN_FAILED_REASON",
    "_CI_TRANSIENT_RERUN_REASON",
    "_GITHUB_TRANSIENT_RETRY_REASON",
    "_GIT_BASE_BEHIND_FAILED_REASON",
    "_GIT_BASE_FETCH_TRANSIENT_RETRY_EXHAUSTED_REASON",
    "_GIT_BASE_FETCH_TRANSIENT_RETRY_REASON",
    "_GIT_FETCH_BASE_FAILED_REASON",
    "_GIT_MIRROR_BROKEN_REF_REMOVED_REASON",
    "_MONITOR_POLICY_BLOCKED_REASON",
    "_MonitorAgentRuntimeOwnershipRepairFailedError",
    "_MonitorPolicyBlockedError",
    "_NON_TRANSIENT_GITHUB_ERROR_MARKERS",
    "_PENDING_CHECK_STATUSES",
    "_PRE_EXISTING_DIRTY_WORKTREE_REASON",
    "_PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON",
    "_PROTECTED_SCOPE_PUSH_BLOCKED_REASON",
    "_PROTECTED_SCOPE_REPAIR_FAILED_REASON",
    "_PR_MONITOR_AUDIT_ACTOR",
    "_PR_MONITOR_REASON_CODES_BY_STALE_REASON",
    "_PR_MONITOR_STALE_REASON_MESSAGES",
    "_ProtectedScopeRollbackDeltaEvidence",
    "_REDACTION",
    "_REMOTE_TRACKING_REF_LOCK_RACE_RE",
    "_REPAIR_START_HEAD_UNAVAILABLE_REASON",
    "_REPAIR_WORKTREE_STATUS_FAILED_REASON",
    "_RunnerDeps",
    "_SYNC_BASE_NO_PROGRESS_COUNT_KEY",
    "_SYNC_BASE_NO_PROGRESS_SIGNATURE_KEY",
    "_SYNC_BASE_RESOLVABLE_STALE_REASONS",
    "_TERMINAL_CHECK_CONCLUSIONS",
    "_TERMINAL_CHECK_STATUSES",
    "_TOKEN_RE",
    "_TRANSIENT_GITHUB_ERROR_MARKERS",
    "_URL_CREDENTIAL_RE",
    "_VALIDATION_INSUFFICIENT_STALE_REASON",
    "_VERDICT_DEFER",
    "_VERDICT_FALSE_POSITIVE",
    "_agent_can_triage_review_comment",
    "_ci_transient_rerun_count",
    "_ci_transient_rerun_state_key",
    "_is_active_pr_monitor_recovery_operation",
    "_is_auth_failure_metadata",
    "_is_bot_author",
    "_is_bot_review_thread",
    "_log",
    "_monitor_recovery_conformance_payload",
    "_needs_comment_attention",
    "_parse_name_status_z",
    "_review_thread_body_hash",
    "_review_thread_body_state_key",
    "_task_policy_with_monitor_circuit_retry_state",
    "begin_monitor_state_operation",
    "cleanup_failure_message",
    "dataclass",
    "datetime",
    "decide",
    "hashlib",
    "json",
    "pr_feedback_body_hash",
    "provider_recovery_metadata_from_failure",
    "re",
    "record_monitor_state_operation",
    "redact_audit_text",
    "run_workspace_filesystem_gc",
    "time",
    "timedelta",
)
