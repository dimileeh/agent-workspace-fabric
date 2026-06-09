"""Worker constants shared by focused implementation modules."""

from __future__ import annotations

import re

from awf.db.enums import TaskKind, WorkspaceStatus
from awf.db.repositories.base import (
    TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
    TERMINAL_RUNTIME_RELEASE_REASON_CODE,
)
from awf.service.workspace_runtime_health import (
    ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE,
    ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
)

_ACTIVE_EXECUTION_STATUSES: tuple[WorkspaceStatus, ...] = (
    WorkspaceStatus.running,
    WorkspaceStatus.validating,
    WorkspaceStatus.pushing,
)

_RUNTIME_HEALTH_SCAN_STATUSES: tuple[WorkspaceStatus, ...] = (
    WorkspaceStatus.requested,
    WorkspaceStatus.provisioning,
    WorkspaceStatus.ready,
    WorkspaceStatus.running,
    WorkspaceStatus.validating,
    WorkspaceStatus.pushing,
    WorkspaceStatus.monitoring_pr,
)

_REQUESTED_ADMISSION_SLOT_STATUSES: tuple[WorkspaceStatus, ...] = (
    WorkspaceStatus.provisioning,
    WorkspaceStatus.ready,
    WorkspaceStatus.running,
    WorkspaceStatus.validating,
    WorkspaceStatus.pushing,
    WorkspaceStatus.monitoring_pr,
)
"""Workspace statuses where the workspace holds an admission slot (e.g. a host-port lock)."""

_STALE_ACTIVE_EXECUTION_REASON_CODE = "STALE_ACTIVE_EXECUTION"

_STALE_ACTIVE_EXECUTION_EVENT_TYPE = "workspace.stale_active_execution_detected"

_STALE_ACTIVE_EXECUTION_CLEANUP_FAILED_EVENT_TYPE = (
    "workspace.stale_active_execution_cleanup_failed"
)

_STALE_ACTIVE_EXECUTION_CLEANUP_FAILED_REASON_CODE = "STALE_ACTIVE_EXECUTION_CLEANUP_FAILED"

_STALE_ACTIVE_EXECUTION_RECOVERY_FAILED_REASON_CODE = "STALE_ACTIVE_EXECUTION_RECOVERY_FAILED"

_ACTIVE_EXECUTION_PRESERVED_SOURCE = "worker_restart"

_ACTIVE_EXECUTION_PRESERVED_OWNER = "control_worker"

_ACTIVE_EXECUTION_PRESERVED_SUBPHASE = "runtime_preserved_after_restart"

_ACTIVE_EXECUTION_PRESERVED_CLAIM_CLEARED_REASON_CODE = (
    "STALE_EXECUTION_CLAIM_CLEARED_DURING_ACTIVE_EXECUTION_PRESERVATION"
)

_ACTIVE_EXECUTION_PRESERVED_UNEXPIRED_CLAIM_PRESERVED_REASON_CODE = (
    "UNEXPIRED_EXECUTION_CLAIM_PRESERVED_DURING_ACTIVE_EXECUTION_PRESERVATION"
)

_ACTIVE_EXECUTION_PRESERVED_NO_CLAIM_REASON_CODE = (
    "NO_EXECUTION_CLAIM_DURING_ACTIVE_EXECUTION_PRESERVATION"
)

_ACTIVE_EXECUTION_SALVAGE_OWNER = "control_worker"

_ACTIVE_EXECUTION_SALVAGE_SOURCE = "worker_restart"

_PRESERVED_ACTIVE_GIT_TIMEOUT_SECONDS = 30.0

_ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE = (
    "ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED"
)

_ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_EVENT_TYPE = (
    "workspace.active_execution_salvage_validation_requested"
)

_ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_REASON_CODE = "ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED"

_ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_EVENT_TYPE = (
    "workspace.active_execution_salvage_monitor_attached"
)

_ACTIVE_EXECUTION_SALVAGE_REPLACEMENT_CREATED_REASON_CODE = (
    "ACTIVE_EXECUTION_SALVAGE_REPLACEMENT_CREATED"
)

_ACTIVE_EXECUTION_SALVAGE_REPLACEMENT_CREATED_EVENT_TYPE = (
    "workspace.active_execution_salvage_replacement_created"
)

_ACTIVE_EXECUTION_SALVAGE_OPERATOR_REQUIRED_REASON_CODE = (
    "ACTIVE_EXECUTION_SALVAGE_OPERATOR_REQUIRED"
)

_ACTIVE_EXECUTION_SALVAGE_OPERATOR_REQUIRED_EVENT_TYPE = (
    "workspace.active_execution_salvage_operator_required"
)

_ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE_REASON_CODE = "ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE"

_ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE_EVENT_TYPE = (
    "workspace.active_execution_salvage_not_possible"
)

_ACTIVE_EXECUTION_SALVAGE_BLOCKED_REASON_CODE = "ACTIVE_EXECUTION_SALVAGE_BLOCKED"

_ACTIVE_EXECUTION_SALVAGE_BLOCKED_EVENT_TYPE = "workspace.active_execution_salvage_blocked"

_ACTIVE_EXECUTION_SALVAGE_MONITOR_RESUME_COOLDOWN_REASON_CODE = (
    "ACTIVE_EXECUTION_SALVAGE_MONITOR_RESUME_COOLDOWN"
)

_ACTIVE_EXECUTION_SALVAGE_MONITOR_RESUME_COOLDOWN_EVENT_TYPE = (
    "workspace.active_execution_salvage_monitor_resume_cooldown"
)

_ACTIVE_EXECUTION_SALVAGE_OPERATOR_SUBPHASE = "runtime_preserved_operator_recovery_required"

_ACTIVE_EXECUTION_SALVAGE_REPLACED_SUBPHASE = "runtime_preserved_replaced"

_ACTIVE_EXECUTION_SALVAGE_BLOCKED_SUBPHASE = "runtime_preserved_salvage_blocked"

# Forge-neutral PR-number extraction. GitHub PR URLs use ``/pull/<n>``;
# Bitbucket uses ``/pull-requests/<n>``. The optional ``-requests`` segment
# accepts both so recovery can re-derive pr_number from a Bitbucket pr_url.
_PR_NUMBER_RE = re.compile(r"/pull(?:-requests)?/(\d+)(?=[/?#]|$)")

_PRESERVED_ACTIVE_REPLACEMENT_REMOTE_PUSH_BRANCH_TASK_KINDS = frozenset(
    {
        TaskKind.sync_release_pr.value,
        TaskKind.sync_feature_pr.value,
    }
)

_ACTIVE_EXECUTION_STALE_FAILURE_BLOCKING_SALVAGE_CHECKS: tuple[tuple[str, str], ...] = (
    (
        _ACTIVE_EXECUTION_SALVAGE_OPERATOR_REQUIRED_EVENT_TYPE,
        _ACTIVE_EXECUTION_SALVAGE_OPERATOR_REQUIRED_REASON_CODE,
    ),
    (
        _ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_EVENT_TYPE,
        _ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE,
    ),
    (
        _ACTIVE_EXECUTION_SALVAGE_REPLACEMENT_CREATED_EVENT_TYPE,
        _ACTIVE_EXECUTION_SALVAGE_REPLACEMENT_CREATED_REASON_CODE,
    ),
    (
        _ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_EVENT_TYPE,
        _ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_REASON_CODE,
    ),
)

_ACTIVE_EXECUTION_RECOVERY_EVIDENCE_EVENTS: tuple[tuple[str, str], ...] = (
    (
        ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE,
        ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
    ),
    *_ACTIVE_EXECUTION_STALE_FAILURE_BLOCKING_SALVAGE_CHECKS,
    (
        _ACTIVE_EXECUTION_SALVAGE_BLOCKED_EVENT_TYPE,
        _ACTIVE_EXECUTION_SALVAGE_BLOCKED_REASON_CODE,
    ),
    (
        _ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE_EVENT_TYPE,
        _ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE_REASON_CODE,
    ),
)

_MONITOR_RECOVERY_REASON_CODE = "MONITOR_RECOVERY_AFTER_RESTART"

_MONITOR_RECOVERY_EVENT_TYPE = "workspace.monitor_recovery_started"

_MONITOR_RECOVERY_SOURCE = "worker_restart"

_MONITOR_RECOVERY_OWNER = "control_worker"

_SCHEDULER_PRIORITY_REFILL_PAGES_AFTER_FILL = 1

_REQUESTED_CAPACITY_QUEUE_SIGNATURE_LIMIT = 500

_MONITOR_RECOVERY_EXECUTION_CLAIM_CLEARED_REASON_CODE = (
    "STALE_EXECUTION_CLAIM_CLEARED_DURING_MONITOR_RECOVERY"
)

_MONITOR_RECOVERY_EXECUTION_CLAIM_PRESERVED_REASON_CODE = (
    "UNEXPIRED_EXECUTION_CLAIM_PRESERVED_DURING_MONITOR_RECOVERY"
)

_MONITOR_RECOVERY_NO_EXECUTION_CLAIM_REASON_CODE = "NO_EXECUTION_CLAIM_DURING_MONITOR_RECOVERY"

_MONITOR_RECOVERY_MONITOR_CLAIM_ACQUIRED_REASON_CODE = (
    "MONITOR_CLAIM_ACQUIRED_DURING_MONITOR_RECOVERY"
)

_ACTIVE_SALVAGE_MONITOR_RECOVERY_OPERATION_ID_LIMIT = 1024

_ACTIVE_SALVAGE_MONITOR_RESUME_COOLDOWN_LIMIT = 1024

QUEUE_DECISION_ORDERED = "ordered"

QUEUE_DECISION_DEFERRED = "deferred"

ORDERED_REQUESTED_PROVISIONING_REASON = "ORDERED_REQUESTED_PROVISIONING"

ORDERED_READY_EXECUTION_REASON = "ORDERED_READY_EXECUTION"

ORDERED_MONITOR_RESUME_REASON = "ORDERED_MONITOR_RESUME"

PROVIDER_RECOVERY_NOT_BEFORE_REASON = "PROVIDER_RECOVERY_NOT_BEFORE"

PROVIDER_MODEL_CIRCUIT_OPEN_REASON = "PROVIDER_MODEL_CIRCUIT_OPEN"

LOCAL_CAPACITY_DEFERRED_REASON = "LOCAL_CAPACITY_DEFERRED"

LOCAL_CAPACITY_UNSATISFIABLE_REASON = "LOCAL_CAPACITY_UNSATISFIABLE"

LOCAL_CAPACITY_RESERVATION_DEFAULTED_REASON = "LOCAL_CAPACITY_RESERVATION_DEFAULTED"

_CAPACITY_BLOCKER_SIGNATURE_FIELDS: tuple[str, ...] = (
    "dimension",
    "reason_code",
    "limit",
    "requested",
    "unsatisfiable",
)

_DB_CONNECTION_TRANSIENT_EVENT_TYPE = "workspace.db_connection_transient"

_TERMINAL_RUNTIME_RELEASE_EVENT_TYPE = TERMINAL_RUNTIME_RELEASE_EVENT_TYPE
"""Module-local alias used by the worker to emit ``terminal_runtime_released`` events."""

_TERMINAL_RUNTIME_RELEASE_REASON_CODE = TERMINAL_RUNTIME_RELEASE_REASON_CODE
"""Module-local alias for the reason code paired with ``terminal_runtime_released`` events."""

_TERMINAL_RUNTIME_RELEASE_FAILED_EVENT_TYPE = "workspace.terminal_runtime_release_failed"
"""Event type recorded when a terminal-status workspace fails to release its runtime resources."""

_TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE = "TERMINAL_RUNTIME_RELEASE_FAILED"
"""Reason code accompanying the ``terminal_runtime_release_failed`` event."""

_ORPHAN_DIR_RECONCILE_FAILED_REASON_CODE = "ORPHAN_DIR_RECONCILE_FAILED"
"""Reason code logged when an orphan-dir reconcile sweep fails non-transiently."""

_CLASSIFIED_ORPHAN_REAP_FAILED_REASON_CODE = "CLASSIFIED_ORPHAN_REAP_FAILED"
"""Reason code logged when a classified-orphan reaper sweep fails non-transiently."""

_TERMINAL_RELEASE_STATUSES: tuple[WorkspaceStatus, ...] = (
    WorkspaceStatus.failed,
    WorkspaceStatus.cancelled,
    WorkspaceStatus.completed,
    WorkspaceStatus.destroyed,
)
"""Terminal workspace statuses that indicate runtime resources should be released."""

_EXECUTION_SLOTS_SATURATED_LOG_INTERVAL = 10

EXECUTION_CLAIM_FENCED = "EXECUTION_CLAIM_FENCED"
"""Reason code logged when a stale worker is fenced by the execution-claim epoch (D5).

Emitted when this worker discovers a newer claimant has superseded its
execution-claim epoch — at provision start (the read returns ``None``), when the
heartbeat CAS fails and cancels the provision task, or when a guarded terminal
transition updates 0 rows. Internal fence log only; not a doctor/catalog entry."""

_ALLOCATED_RESERVATION_SIGNATURE_SCALE = 1_000_000_000
