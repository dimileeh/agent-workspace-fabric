"""PR monitor runner constants."""

from __future__ import annotations

import re

from awf.db.enums import OperationStatus, WorkspaceStatus
from awf.runtime.pr_monitor_operations import RETRYABLE_MONITOR_OPERATION_STATUSES
from awf.service.staleness import (
    REASON_BUILD_CONFIG,
    REASON_DEPENDENCY,
    REASON_OVERLAP,
    REASON_PLAN_ARTIFACT_OVERLAP,
    REASON_SCHEMA,
    REASON_TARGET_ADVANCED,
)

_RETRYABLE_RECOVERY_TERMINAL_OPERATION_STATUSES = RETRYABLE_MONITOR_OPERATION_STATUSES

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

_GITHUB_WORKFLOW_SCOPE_REQUIRED_REASON = "GITHUB_WORKFLOW_SCOPE_REQUIRED"

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

_VALIDATION_MISSING_CURRENT_HEAD_STALE_REASON = "validation_missing_for_current_head"

_VALIDATION_RECOVERY_STALE_REASONS = frozenset(
    {
        _VALIDATION_INSUFFICIENT_STALE_REASON,
        _VALIDATION_MISSING_CURRENT_HEAD_STALE_REASON,
    }
)

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

_VERDICT_FALSE_POSITIVE = re.compile(r"\bFALSE\s+POSITIVE\s*:", re.IGNORECASE)

# Fail-safe fallback for a bare ``NEEDS_HUMAN:`` (no ``AWF-VERDICT:`` prefix).
# Without it, such output would fall through to ``fix_committed`` and the thread
# would be resolved/merged — the exact unsafe direction #305 guards against.
_VERDICT_NEEDS_HUMAN = re.compile(r"\bNEEDS[\s_]+HUMAN\s*:", re.IGNORECASE)

_VERDICT_DEFER = re.compile(r"\bDEFER\s*:", re.IGNORECASE)

_AWF_VERDICT = re.compile(
    r"\bAWF-VERDICT\s*:\s*"
    # NEEDS[\s_]+HUMAN mirrors FALSE\s+POSITIVE so ``NEEDS HUMAN`` (space) also
    # matches the primary regex and its reason is extracted cleanly instead of
    # falling through to the bare fallback (which garbles the reason).
    r"(?P<label>FIXED|FALSE\s+POSITIVE|DEFER|NEEDS[\s_]+HUMAN)"
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
    "validation_missing_for_current_head": (
        "AWF validation has not passed for the current PR head."
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
    "validation_missing_for_current_head": "VALIDATION_MISSING_FOR_CURRENT_HEAD",
    "docs_task_scope_violation": "DOCS_TASK_SCOPE_VIOLATION",
    "stale": "STALE",
}
