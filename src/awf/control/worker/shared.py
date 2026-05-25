"""Shared worker primitives for decomposed implementation modules."""

from __future__ import annotations

# Shared worker constants, result types, protocols, and imported domain names.
import asyncio as asyncio
import contextlib as contextlib
import hashlib as hashlib
import json as json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import partial as partial
from pathlib import Path
from typing import Any, Protocol
from typing import TypeGuard as TypeGuard

from sqlalchemy import String as String
from sqlalchemy import and_ as and_
from sqlalchemy import cast as sql_cast
from sqlalchemy import func as func
from sqlalchemy import literal as literal
from sqlalchemy import or_ as or_
from sqlalchemy import select as select
from sqlalchemy.dialects.postgresql import JSONB as JSONB
from sqlalchemy.dialects.postgresql import aggregate_order_by as aggregate_order_by
from sqlalchemy.exc import SQLAlchemyError as SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.github_client import BranchOpenPullRequest
from awf.common.github_client import RepoRef as RepoRef
from awf.common.logging import get_logger
from awf.db.enums import FailureReason as FailureReason
from awf.db.enums import OperationStatus, OperationType, TaskKind, WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.db.repositories import scheduler_order_expressions as scheduler_order_expressions
from awf.db.resilience import (
    DB_CONNECTION_TRANSIENT_ATTEMPT_REASON,
    run_db_operation_with_retry,
)
from awf.db.resilience import (
    is_transient_closed_connection_error as is_transient_closed_connection_error,
)
from awf.node.cleanup import WorkspaceCleanupResult
from awf.runtime.inspection import RuntimeInspector, RuntimeSnapshot
from awf.service.scheduler import AGE_BOOST_MAX as AGE_BOOST_MAX
from awf.service.scheduler import (
    SchedulerOrderCursor,
)
from awf.service.workspace_runtime_health import (
    ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE,
    ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
    WorkspaceRuntimeFinding,
)
from awf.service.workspace_runtime_health import (
    RUNTIME_STRANDED_EVENT_TYPE as RUNTIME_STRANDED_EVENT_TYPE,
)
from awf.service.workspace_runtime_health import RuntimeWorkspace as RuntimeWorkspace
from awf.service.workspace_runtime_health import (
    retry_policy_allows_runtime_recovery as retry_policy_allows_runtime_recovery,
)

_log = get_logger(__name__)

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
_PR_NUMBER_RE = re.compile(r"/pull/(\d+)(?=[/?#]|$)")
_PRESERVED_ACTIVE_REPLACEMENT_REMOTE_PUSH_BRANCH_TASK_KINDS = frozenset(
    {
        TaskKind.sync_release_pr.value,
        TaskKind.sync_feature_pr.value,
    }
)
# SALVAGE_BLOCKED and SALVAGE_NOT_POSSIBLE are intentionally absent:
# _recover_preserved_active_execution returns True while SALVAGE_BLOCKED is
# active, gating stale-active cleanup without needing an entry here.
# SALVAGE_NOT_POSSIBLE is written immediately before returning False to open
# the stale-active path; adding it here would create a livelock.
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
# Keep allocation snapshots in decision payloads, but dedupe on stable blocker
# identity so ordinary admissions do not rewrite every still-blocked candidate.
_CAPACITY_BLOCKER_SIGNATURE_FIELDS: tuple[str, ...] = (
    "dimension",
    "reason_code",
    "limit",
    "requested",
    "unsatisfiable",
)
_DB_CONNECTION_TRANSIENT_EVENT_TYPE = "workspace.db_connection_transient"
_TERMINAL_RUNTIME_RELEASE_EVENT_TYPE = "workspace.terminal_runtime_released"
_TERMINAL_RUNTIME_RELEASE_REASON_CODE = "TERMINAL_RUNTIME_RELEASED"
_TERMINAL_RUNTIME_RELEASE_FAILED_EVENT_TYPE = "workspace.terminal_runtime_release_failed"
_TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE = "TERMINAL_RUNTIME_RELEASE_FAILED"
# `destroyed` is included as a safety net so leaked runtime survives if
# `destroy_workspace` left a container or network behind (partial failure
# mid-cleanup); `compose down` is idempotent on already-cleaned projects.
_TERMINAL_RELEASE_STATUSES: tuple[WorkspaceStatus, ...] = (
    WorkspaceStatus.failed,
    WorkspaceStatus.cancelled,
    WorkspaceStatus.completed,
    WorkspaceStatus.destroyed,
)


def _salvage_workspace_status_values(
    workspace_status: WorkspaceStatus,
    *,
    event_type: str,
    reason_code: str,
) -> tuple[str, ...]:
    if (
        workspace_status in _ACTIVE_EXECUTION_STATUSES
        and event_type == _ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_EVENT_TYPE
        and reason_code == _ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE
    ):
        return tuple(status.value for status in _ACTIVE_EXECUTION_STATUSES)
    return (workspace_status.value,)


def _preserved_active_execution_status_values(
    workspace_status: WorkspaceStatus,
    *,
    match_active_execution_statuses: bool = False,
) -> tuple[str, ...]:
    if match_active_execution_statuses and workspace_status in _ACTIVE_EXECUTION_STATUSES:
        return tuple(status.value for status in _ACTIVE_EXECUTION_STATUSES)
    return (workspace_status.value,)


class _ExecutionTaskKind(StrEnum):
    """Kind of work tracked in ``ControlWorker._execution_tasks`` slot accounting."""

    MONITOR_RESUME = "monitor_resume"
    READY = "ready"
    PRESERVED_ACTIVE = "preserved_active"
    # A monitor resume that reconcile has cancelled but whose coroutine has not
    # yet stopped. Cancellation is cooperative, so the task can keep running
    # after ``cancel()`` returns. We keep it tracked under its workspace_id so a
    # fresh dispatch for the *same* workspace stays blocked, but exclude it from
    # the slot budget so it does not starve *other* workspaces.
    MONITOR_DRAINING = "monitor_draining"


# Emit ``worker.execution_slots_saturated`` every Nth consecutive idle cycle in
# which every execution slot stays occupied. Keeps the signal low-noise while
# still surfacing wedged-slot starvation for operators.
_EXECUTION_SLOTS_SATURATED_LOG_INTERVAL = 10


@dataclass(frozen=True)
class WorkerConfig:
    poll_interval_seconds: float = 1.0
    max_concurrent_provisions: int = 3
    max_concurrent_executions: int = 3
    monitor_claim_lease_seconds: float = 300.0
    execution_claim_lease_seconds: float = 300.0
    stale_active_execution_scan_interval_seconds: float = 300.0
    active_execution_preservation_grace_seconds: float = 900.0
    secret_lease_expiration_scan_interval_seconds: float = 60.0
    terminal_runtime_release_scan_interval_seconds: float = 300.0
    terminal_runtime_release_max_per_scan: int = 5
    node_id: str | None = None
    local_capacity_cpu_cores: float | None = None
    local_capacity_memory_gb: float | None = None
    local_capacity_dind_slots: int | None = None
    workspace_steady_cpu: float = 3.0
    workspace_steady_memory_gb: float = 10.0
    workspace_peak_cpu: float = 6.0
    workspace_peak_memory_gb: float = 16.0


_ALLOCATED_RESERVATION_SIGNATURE_SCALE = 1_000_000_000

type _AllocatedReservationSignature = tuple[int, int, int, int, int, int, int]
type _RequestedCapacityQueueSignature = tuple[
    int,
    datetime | None,
    datetime | None,
    str | None,
    str,
]


@dataclass(frozen=True)
class _RequestedCapacityClaimResult:
    workspace_ids: list[str]
    resume_after: SchedulerOrderCursor | None = None
    allocated_signature: _AllocatedReservationSignature | None = None
    requested_queue_signature: _RequestedCapacityQueueSignature | None = None
    provider_suppression_resume_expires_at: datetime | None = None


@dataclass(frozen=True)
class _SchedulerCandidateFilterResult:
    workspace_ids: list[str]
    provider_suppression_resume_expires_at: datetime | None = None


@dataclass(frozen=True)
class _ActiveExecutionCandidate:
    workspace_id: str
    status: WorkspaceStatus
    compose_project_name: str | None
    agent: str | None = None
    repo_url: str | None = None
    compose_file_path: str | None = None
    pr_url: str | None = None
    task_policy: dict[str, Any] | None = None


@dataclass(frozen=True)
class _TerminalRuntimeCandidate:
    workspace_id: str
    status: WorkspaceStatus
    # Legacy/partially persisted rows may have null compose_project_name; the
    # cleaner derives ``awf_<workspace_id>`` as the default. compose_file_path
    # can carry the runtime signal when project name was never persisted.
    compose_project_name: str | None
    compose_file_path: str | None
    repo_url: str


@dataclass(frozen=True)
class _PreservedWorktreeClassification:
    state: str
    reason: str
    worktree_path: str | None = None
    branch_name: str | None = None
    expected_branch_name: str | None = None
    base_commit: str | None = None
    head_sha: str | None = None
    commit_count: int | None = None
    status_porcelain: str | None = None
    error: str | None = None

    def to_payload(self: Any) -> dict[str, Any]:
        return {
            "state": self.state,
            "reason": self.reason,
            "worktree_path": self.worktree_path,
            "branch_name": self.branch_name,
            "expected_branch_name": self.expected_branch_name,
            "base_commit": self.base_commit,
            "head_sha": self.head_sha,
            "commit_count": self.commit_count,
            "status_porcelain": self.status_porcelain,
            "error": self.error,
        }


@dataclass(frozen=True)
class _OpenPullRequestSummary:
    pr_url: str
    pr_number: int
    head_ref: str | None
    head_sha: str | None
    head_repo_slug: str | None = None

    def to_payload(self: Any) -> dict[str, Any]:
        payload = {
            "pr_url": self.pr_url,
            "pr_number": self.pr_number,
            "head_ref": self.head_ref,
            "head_sha": self.head_sha,
        }
        if self.head_repo_slug is not None:
            payload["head_repo_slug"] = self.head_repo_slug
        return payload


@dataclass(frozen=True)
class _BranchOpenPRLookup:
    branch_name: str
    state: str
    payload: dict[str, Any]
    match: _OpenPullRequestSummary | None = None
    ambiguity_reason: str | None = None


class WorkspaceExecutorProtocol(Protocol):
    async def execute(  # pragma: no cover - Protocol method declaration only.
        self: Any,
        workspace_id: str,
        *,
        execution_owner_id: str | None = None,
        execution_lease_expires_at: datetime | None = None,
    ) -> None: ...

    async def resume_pr_monitor(  # pragma: no cover - Protocol method declaration only.
        self: Any,
        workspace_id: str,
    ) -> None: ...


class ProvisionerProtocol(Protocol):
    async def provision(  # pragma: no cover - Protocol method declaration only.
        self: Any,
        workspace_id: str,
    ) -> None: ...

    async def provision_claimed(  # pragma: no cover - Protocol method declaration only.
        self: Any,
        workspace_id: str,
    ) -> None: ...

    def get_worktree_path(  # pragma: no cover - Protocol method declaration only.
        self: Any,
        workspace_id: str,
    ) -> Path | None: ...


class BranchOpenPullRequestResolverProtocol(Protocol):
    async def resolve(  # pragma: no cover - Protocol method declaration only.
        self: Any,
        *,
        repo_url: str,
        branch_name: str,
        base_branch: str | None,
    ) -> Sequence[BranchOpenPullRequest]: ...


class RuntimeInspectorProtocol(Protocol):
    async def inspect(  # pragma: no cover - Protocol method declaration only.
        self: Any,
        compose_project_name: str | None,
    ) -> RuntimeSnapshot: ...


class RuntimeCleanerProtocol(Protocol):
    async def cleanup(  # pragma: no cover - Protocol method declaration only.
        self: Any,
        *,
        workspace_id: str,
        repo_url: str,
        compose_project_name: str | None = None,
        compose_file_path: Path | None = None,
        worktree_host_path: Path | None = None,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
    ) -> WorkspaceCleanupResult: ...


__all__ = (
    "ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE",
    "ACTIVE_EXECUTION_PRESERVED_REASON_CODE",
    "AGE_BOOST_MAX",
    "AsyncSession",
    "BranchOpenPullRequestResolverProtocol",
    "DB_CONNECTION_TRANSIENT_ATTEMPT_REASON",
    "FailureReason",
    "JSONB",
    "LOCAL_CAPACITY_DEFERRED_REASON",
    "LOCAL_CAPACITY_RESERVATION_DEFAULTED_REASON",
    "LOCAL_CAPACITY_UNSATISFIABLE_REASON",
    "Mapping",
    "ORDERED_MONITOR_RESUME_REASON",
    "ORDERED_READY_EXECUTION_REASON",
    "ORDERED_REQUESTED_PROVISIONING_REASON",
    "OperationStatus",
    "OperationType",
    "PROVIDER_MODEL_CIRCUIT_OPEN_REASON",
    "PROVIDER_RECOVERY_NOT_BEFORE_REASON",
    "Path",
    "ProvisionerProtocol",
    "QUEUE_DECISION_DEFERRED",
    "QUEUE_DECISION_ORDERED",
    "RUNTIME_STRANDED_EVENT_TYPE",
    "RepoRef",
    "RuntimeCleanerProtocol",
    "RuntimeInspector",
    "RuntimeInspectorProtocol",
    "RuntimeSnapshot",
    "RuntimeWorkspace",
    "SQLAlchemyError",
    "SchedulerOrderCursor",
    "String",
    "TypeGuard",
    "UTC",
    "WorkerConfig",
    "Workspace",
    "WorkspaceCleanupResult",
    "WorkspaceEvent",
    "WorkspaceExecutorProtocol",
    "WorkspaceRepository",
    "WorkspaceRuntimeFinding",
    "WorkspaceStatus",
    "_ACTIVE_EXECUTION_PRESERVED_CLAIM_CLEARED_REASON_CODE",
    "_ACTIVE_EXECUTION_PRESERVED_NO_CLAIM_REASON_CODE",
    "_ACTIVE_EXECUTION_PRESERVED_OWNER",
    "_ACTIVE_EXECUTION_PRESERVED_SOURCE",
    "_ACTIVE_EXECUTION_PRESERVED_SUBPHASE",
    "_ACTIVE_EXECUTION_PRESERVED_UNEXPIRED_CLAIM_PRESERVED_REASON_CODE",
    "_ACTIVE_EXECUTION_RECOVERY_EVIDENCE_EVENTS",
    "_ACTIVE_EXECUTION_SALVAGE_BLOCKED_EVENT_TYPE",
    "_ACTIVE_EXECUTION_SALVAGE_BLOCKED_REASON_CODE",
    "_ACTIVE_EXECUTION_SALVAGE_BLOCKED_SUBPHASE",
    "_ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_EVENT_TYPE",
    "_ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_REASON_CODE",
    "_ACTIVE_EXECUTION_SALVAGE_MONITOR_RESUME_COOLDOWN_EVENT_TYPE",
    "_ACTIVE_EXECUTION_SALVAGE_MONITOR_RESUME_COOLDOWN_REASON_CODE",
    "_ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE_EVENT_TYPE",
    "_ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE_REASON_CODE",
    "_ACTIVE_EXECUTION_SALVAGE_OPERATOR_REQUIRED_EVENT_TYPE",
    "_ACTIVE_EXECUTION_SALVAGE_OPERATOR_REQUIRED_REASON_CODE",
    "_ACTIVE_EXECUTION_SALVAGE_OPERATOR_SUBPHASE",
    "_ACTIVE_EXECUTION_SALVAGE_OWNER",
    "_ACTIVE_EXECUTION_SALVAGE_REPLACED_SUBPHASE",
    "_ACTIVE_EXECUTION_SALVAGE_REPLACEMENT_CREATED_EVENT_TYPE",
    "_ACTIVE_EXECUTION_SALVAGE_REPLACEMENT_CREATED_REASON_CODE",
    "_ACTIVE_EXECUTION_SALVAGE_SOURCE",
    "_ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_EVENT_TYPE",
    "_ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED_REASON_CODE",
    "_ACTIVE_EXECUTION_STALE_FAILURE_BLOCKING_SALVAGE_CHECKS",
    "_ACTIVE_EXECUTION_STATUSES",
    "_ACTIVE_SALVAGE_MONITOR_RECOVERY_OPERATION_ID_LIMIT",
    "_ACTIVE_SALVAGE_MONITOR_RESUME_COOLDOWN_LIMIT",
    "_ALLOCATED_RESERVATION_SIGNATURE_SCALE",
    "_ActiveExecutionCandidate",
    "_AllocatedReservationSignature",
    "_BranchOpenPRLookup",
    "_DB_CONNECTION_TRANSIENT_EVENT_TYPE",
    "_EXECUTION_SLOTS_SATURATED_LOG_INTERVAL",
    "_ExecutionTaskKind",
    "_MONITOR_RECOVERY_EVENT_TYPE",
    "_MONITOR_RECOVERY_EXECUTION_CLAIM_CLEARED_REASON_CODE",
    "_MONITOR_RECOVERY_EXECUTION_CLAIM_PRESERVED_REASON_CODE",
    "_MONITOR_RECOVERY_MONITOR_CLAIM_ACQUIRED_REASON_CODE",
    "_MONITOR_RECOVERY_NO_EXECUTION_CLAIM_REASON_CODE",
    "_MONITOR_RECOVERY_OWNER",
    "_MONITOR_RECOVERY_REASON_CODE",
    "_MONITOR_RECOVERY_SOURCE",
    "_OpenPullRequestSummary",
    "_PRESERVED_ACTIVE_GIT_TIMEOUT_SECONDS",
    "_PRESERVED_ACTIVE_REPLACEMENT_REMOTE_PUSH_BRANCH_TASK_KINDS",
    "_PR_NUMBER_RE",
    "_PreservedWorktreeClassification",
    "_REQUESTED_CAPACITY_QUEUE_SIGNATURE_LIMIT",
    "_RUNTIME_HEALTH_SCAN_STATUSES",
    "_RequestedCapacityClaimResult",
    "_RequestedCapacityQueueSignature",
    "_SCHEDULER_PRIORITY_REFILL_PAGES_AFTER_FILL",
    "_STALE_ACTIVE_EXECUTION_CLEANUP_FAILED_EVENT_TYPE",
    "_STALE_ACTIVE_EXECUTION_CLEANUP_FAILED_REASON_CODE",
    "_STALE_ACTIVE_EXECUTION_EVENT_TYPE",
    "_STALE_ACTIVE_EXECUTION_REASON_CODE",
    "_STALE_ACTIVE_EXECUTION_RECOVERY_FAILED_REASON_CODE",
    "_SchedulerCandidateFilterResult",
    "_TERMINAL_RELEASE_STATUSES",
    "_TERMINAL_RUNTIME_RELEASE_EVENT_TYPE",
    "_TERMINAL_RUNTIME_RELEASE_FAILED_EVENT_TYPE",
    "_TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE",
    "_TERMINAL_RUNTIME_RELEASE_REASON_CODE",
    "_TerminalRuntimeCandidate",
    "_log",
    "_preserved_active_execution_status_values",
    "_salvage_workspace_status_values",
    "aggregate_order_by",
    "and_",
    "asyncio",
    "contextlib",
    "datetime",
    "func",
    "hashlib",
    "is_transient_closed_connection_error",
    "json",
    "literal",
    "or_",
    "partial",
    "retry_policy_allows_runtime_recovery",
    "run_db_operation_with_retry",
    "scheduler_order_expressions",
    "select",
    "sql_cast",
    "timedelta",
)
