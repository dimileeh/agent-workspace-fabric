"""Executor constants shared by focused implementation modules."""

from __future__ import annotations

import re

from awf.db.enums import OperationStatus

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

POST_VALIDATION_CONFORMANCE_FAILED_REASON_CODE = "POST_VALIDATION_CONFORMANCE_FAILED"

POST_AGENT_GIT_ADD_FAILED_REASON_CODE = "POST_AGENT_GIT_ADD_FAILED"

POST_AGENT_COMMIT_FAILED_REASON_CODE = "POST_AGENT_COMMIT_FAILED"

POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE = "POST_AGENT_COMMIT_PRECOMMIT_FAILED"

POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE = "POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED"

PLAN_CONFORMANCE_UNSATISFIED = "PLAN_CONFORMANCE_UNSATISFIED"

POST_VALIDATION_CONFORMANCE_REPORT_CLEANUP_FAILED_REASON_CODE = (
    "POST_VALIDATION_CONFORMANCE_REPORT_CLEANUP_FAILED"
)

POST_AGENT_FORMAT_REPAIR_FAILED_REASON_CODE = "POST_AGENT_FORMAT_REPAIR_FAILED"

POST_AGENT_COMMIT_REPAIR_EVENT_TYPE = "workspace.post_agent_commit_repair"

POST_AGENT_COMMIT_FORMAT_REPAIR_EVENT_TYPE = POST_AGENT_COMMIT_REPAIR_EVENT_TYPE

SETUP_DEPENDENCY_NETWORK_RETRY_EVENT_TYPE = "workspace.setup_dependency_network_retry"

SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED_EVENT_TYPE = (
    "workspace.setup_dependency_network_retry_exhausted"
)

RUNTIME_TOOLCHAIN_UNAVAILABLE_EVENT_TYPE = "workspace.runtime_toolchain_unavailable"

_PR_MONITOR_ADOPTED_EVENT = "workspace.pr_monitor_adopted"

_PR_MONITOR_ADOPTED_REASON_CODE = "PR_MONITOR_ADOPTED"

PR_MONITOR_SETUP_FAILED_REASON_CODE = "PR_MONITOR_SETUP_FAILED"

HOSTED_MONITOR_HANDOFF_SETUP_COMPLETED_EVENT_TYPE = (
    "workspace.hosted_monitor_handoff_setup_completed"
)

HOSTED_MONITOR_HANDOFF_SETUP_COMPLETED_REASON_CODE = "HOSTED_MONITOR_HANDOFF_SETUP_COMPLETED"

RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE = "RUNTIME_SETUP_REPLAY_FAILED"

RUNTIME_SETUP_REPLAYED_REASON_CODE = "RUNTIME_SETUP_REPLAYED"

RUNTIME_SETUP_REPLAYED_EVENT_TYPE = "workspace.runtime_setup_replayed"

RUNTIME_SETUP_REPLAY_FAILED_EVENT_TYPE = "workspace.runtime_setup_replay_failed"

# Minimum gap, in seconds, before the next restart after the 1st and the 2nd
# consecutive failure, so a transient error cannot use every strike in seconds.
MONITOR_RUNTIME_RESTART_BACKOFF_SECONDS = (60, 120)

# Monitor-origin (``monitor_`` prefix) resume phase of the block a failed PR-monitor
# setup replay parks in, so ``guide`` resumes it back into the PR monitor.
MONITOR_RUNTIME_SETUP_REPLAY_RESUME_PHASE = "monitor_runtime_setup_replay"

RUNTIME_IDENTITY_PROBE_TIMEOUT_REASON_CODE = "RUNTIME_IDENTITY_PROBE_TIMEOUT"

# Monitor resume runtime restart (#936). A running dependency that is still
# ``starting``/``unhealthy`` is waited on, never recreated; the wait is bounded by
# the stack's startup budget.
MONITOR_RESUME_DEPENDENCY_WAIT_TIMEOUT_REASON_CODE = "MONITOR_RESUME_DEPENDENCY_WAIT_TIMEOUT"

MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE = "workspace.monitor_runtime_restart_failed"

# Payload key marking a restart-failure event as counted toward the streak. Events
# recorded before the streak rule lack it and act as a boundary instead.
MONITOR_RUNTIME_RESTART_STREAK_PAYLOAD_KEY = "counts_toward_restart_streak"

# Payload key listing the dependencies still ``starting``/``unhealthy`` when a
# restart failed on the dependency wait; the guided retry after the park
# force-recreates them.
MONITOR_RUNTIME_RESTART_UNSETTLED_DEPENDENCIES_PAYLOAD_KEY = "unsettled_dependencies"

# Payload key marking a restart-failure event whose precheck failed before any
# restart ran, so it leaves the dependencies a park left unsettled pending.
MONITOR_RUNTIME_RESTART_BEFORE_RESTART_PAYLOAD_KEY = "failed_before_restart"

MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE = "workspace.monitor_runtime_restart_recovered"

MONITOR_RUNTIME_RESTART_EXHAUSTED_EVENT_TYPE = "workspace.monitor_runtime_restart_exhausted"

MONITOR_RUNTIME_RESTART_EXHAUSTED_REASON_CODE = "MONITOR_RUNTIME_RESTART_EXHAUSTED"

# Consecutive failed monitor-resume runtime restarts before the workspace is parked
# in ``blocked`` instead of being retried forever.
MONITOR_RUNTIME_RESTART_FAILURE_LIMIT = 3

# Monitor-origin (``monitor_`` prefix) resume phase, so ``guide`` resumes the park
# back into the PR monitor.
MONITOR_RUNTIME_RESTART_RESUME_PHASE = "monitor_runtime_restart"

MONITOR_RUNTIME_RESTART_BLOCK_TYPE = "monitor_runtime_restart_failure"

# Block reasons a monitor resume parks ``monitoring_pr -> blocked`` with; the worker
# finalizes the recovery operation as failed with them rather than cancelled.
MONITOR_RESUME_PARK_BLOCK_REASON_CODES = frozenset(
    {RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE, MONITOR_RUNTIME_RESTART_EXHAUSTED_REASON_CODE}
)

# Adopt-pr handoff skips the coding agent (PR_ADOPTION_SKIP_AGENT), so the
# profile's ``setup`` phase is the only thing that provisions the workspace. If
# a ``validate``-phase command's executable is still not on PATH after setup,
# fail the adoption early and clearly here instead of letting the first validate
# command die ``127`` later at ``sync_base_push`` (PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING).
PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED_REASON_CODE = "PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED"

_PR_ADOPTION_SKIP_AGENT_REASON_CODE = "PR_ADOPTION_SKIP_AGENT"

_PR_ADOPTION_METADATA_MISSING_REASON_CODE = "PR_ADOPTION_METADATA_MISSING"

_PR_ADOPTION_MONITOR_UNAVAILABLE_REASON_CODE = "PR_ADOPTION_MONITOR_UNAVAILABLE"

_DEPRECATED_TASK_KIND_REASON_CODE = "DEPRECATED_TASK_KIND"

_UNSUPPORTED_TASK_KIND_REASON_CODE = "UNSUPPORTED_TASK_KIND"

_UNSUPPORTED_AGENT_RUNTIME_REASON_CODE = "UNSUPPORTED_AGENT_RUNTIME"

_SUPPORTED_TASK_KINDS = frozenset({"feature_branch_pr", "sync_feature_pr", "sync_release_pr"})

_MONITOR_ONLY_TASK_KINDS = frozenset({"sync_feature_pr", "sync_release_pr"})

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

# Forge-neutral PR-number extraction. GitHub PR URLs use ``/pull/<n>``;
# Bitbucket uses ``/pull-requests/<n>``. The optional ``-requests`` segment
# accepts both so a forge-neutral ``pr_url`` (persisted verbatim from the
# resolved ForgeClient) yields a pr_number for the monitor on either forge.
_PR_NUMBER_RE = re.compile(r"/pull(?:-requests)?/(\d+)(?=[/?#]|$)")
