"""Async control-plane worker.

Polls the DB for workspaces needing action and dispatches them to the
Provisioner. Postgres-backed deployments list schedulable candidate rows with
``SELECT FOR UPDATE SKIP LOCKED``; provisioning and ready-execution handlers
then claim selected rows with conditional status transitions. Active execution
and PR monitor recovery use short DB-backed leases so multiple workers do not
resume the same runtime task concurrently.

Split into two methods:

- ``run_once()`` — processes one batch of work and returns. Unit-testable.
- ``run_forever()`` — composition; sleeps ``poll_interval`` between batches.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from time import monotonic
from typing import Any, Protocol, TypeGuard

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from awf.common.logging import get_logger
from awf.common.workspace_policy import agent_model_from_task_policy
from awf.db.enums import FailureReason, OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import QueueDecision, ResourceReservation, TaskAttempt, Workspace, WorkspaceEvent
from awf.db.repositories import (
    ALLOCATED_RESOURCE_RESERVATION_STATUSES,
    SCHEDULER_SQL_AGE_BOOST_DIALECTS,
    OperationRepository,
    ProviderModelCircuitBreakerRepository,
    QueueDecisionCreate,
    QueueDecisionRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
    WorkspaceRepository,
)
from awf.db.resilience import (
    DB_CONNECTION_CLOSED_REASON,
    DB_CONNECTION_TRANSIENT_ATTEMPT_REASON,
    is_transient_closed_connection_error,
    run_db_operation_with_retry,
)
from awf.db.session import session_scope
from awf.node.cleanup import WorkspaceCleanupResult
from awf.node.provisioner import Provisioner
from awf.runtime.inspection import RuntimeInspector, RuntimeSnapshot
from awf.service.failure_causality import (
    attach_primary_failure,
    build_preserved_failure_payload,
    load_failure_causality_snapshot,
    load_primary_failure_snapshot,
    primary_failure_reason_code,
    restore_primary_failure_row_fields,
)
from awf.service.provider_recovery import (
    PROVIDER_RECOVERY_STATE_KEY,
    provider_cooldown_not_before,
    provider_for_agent_model,
)
from awf.service.resource_capacity import (
    LOCAL_CAPACITY_CONSTRAINTS,
    LocalCapacityBlocker,
    local_capacity_blocker,
    local_capacity_limit,
)
from awf.service.scheduler import (
    SchedulerOrderCursor,
    scheduler_order_key,
    scheduler_score_from_workspace,
    score_summary_with_suppression,
)
from awf.service.secret_leases import SecretLeaseService
from awf.service.workspace_runtime_health import (
    ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE,
    ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
    OPERATOR_REFRESH_EVENT_TYPE,
    OPERATOR_REFRESH_REASON_CODE,
    RUNTIME_STRANDED_EVENT_TYPE,
    RuntimeWorkspace,
    WorkspaceRuntimeFinding,
    classify_runtime_snapshot,
    has_open_pr_for_remonitor,
    retry_policy_allows_runtime_recovery,
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
_MONITOR_RECOVERY_REASON_CODE = "MONITOR_RECOVERY_AFTER_RESTART"
_MONITOR_RECOVERY_EVENT_TYPE = "workspace.monitor_recovery_started"
_MONITOR_RECOVERY_SOURCE = "worker_restart"
_MONITOR_RECOVERY_OWNER = "control_worker"
_SCHEDULER_PRIORITY_REFILL_PAGES_AFTER_FILL = 1
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
    "allocated",
    "requested",
    "after",
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


@dataclass(frozen=True)
class _ReservationDemand:
    workspace_id: str
    steady_cpu: float
    steady_memory_gb: float
    peak_cpu: float
    peak_memory_gb: float
    disk_mb: int
    dind_slots: int
    defaulted: bool = False


@dataclass
class _AllocatedReservationTotals:
    workspace_count: int = 0
    steady_cpu: float = 0.0
    steady_memory_gb: float = 0.0
    peak_cpu: float = 0.0
    peak_memory_gb: float = 0.0
    disk_mb: int = 0
    dind_slots: int = 0

    def add(self, demand: _ReservationDemand) -> None:
        self.workspace_count += 1
        self.steady_cpu += demand.steady_cpu
        self.steady_memory_gb += demand.steady_memory_gb
        self.peak_cpu += demand.peak_cpu
        self.peak_memory_gb += demand.peak_memory_gb
        self.disk_mb += demand.disk_mb
        self.dind_slots += demand.dind_slots


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
class _OrderedDecisionCandidate:
    workspace_id: str
    task_class: str | None
    task_policy: dict[str, Any] | None
    created_at: datetime
    task_id: str
    attempt_id: str

    @property
    def id(self) -> str:
        return self.workspace_id


type _OrderedDecisionKey = tuple[str, str, str]


class WorkspaceExecutorProtocol(Protocol):
    async def execute(  # pragma: no cover - Protocol method declaration only.
        self,
        workspace_id: str,
        *,
        execution_owner_id: str | None = None,
        execution_lease_expires_at: datetime | None = None,
    ) -> None: ...

    async def resume_pr_monitor(  # pragma: no cover - Protocol method declaration only.
        self,
        workspace_id: str,
    ) -> None: ...


class RuntimeInspectorProtocol(Protocol):
    async def inspect(  # pragma: no cover - Protocol method declaration only.
        self,
        compose_project_name: str | None,
    ) -> RuntimeSnapshot: ...


class RuntimeCleanerProtocol(Protocol):
    async def cleanup(  # pragma: no cover - Protocol method declaration only.
        self,
        *,
        workspace_id: str,
        repo_url: str,
        compose_project_name: str | None = None,
        compose_file_path: Path | None = None,
        worktree_host_path: Path | None = None,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
    ) -> WorkspaceCleanupResult: ...


class ControlWorker:
    """Reads pending work from the DB and dispatches it to runtime handlers."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        provisioner: Provisioner,
        executor: WorkspaceExecutorProtocol | None = None,
        runtime_inspector: RuntimeInspectorProtocol | None = None,
        runtime_cleaner: RuntimeCleanerProtocol | None = None,
        config: WorkerConfig,
    ) -> None:
        self._session_factory = session_factory
        self._provisioner = provisioner
        self._executor = executor
        self._runtime_inspector = runtime_inspector or RuntimeInspector()
        self._runtime_cleaner = runtime_cleaner
        self._config = config
        self._stopped = asyncio.Event()
        self._execution_tasks: dict[str, asyncio.Task[None]] = {}
        self._monitor_recovery_operation_ids: dict[str, str] = {}
        self._worker_id = f"control-worker-{uuid.uuid4().hex}"
        self._next_stale_active_execution_scan_at = 0.0
        self._next_secret_lease_expiration_scan_at = 0.0
        self._next_terminal_runtime_release_scan_at = 0.0

    def request_stop(self) -> None:
        """Signal ``run_forever`` to exit after the current batch."""
        self._stopped.set()

    async def run_once(self) -> int:
        """List + dispatch requested provisioning and workspace runtime tasks.

        Returns the number of workspaces dispatched. A zero return is a signal
        for ``run_forever`` to sleep; non-zero means we may be throughput-bound
        and should immediately loop again.
        """
        dispatched_ids: set[str] = set()

        await self._maybe_expire_due_secret_leases()
        await self._maybe_release_terminal_runtime()

        if self._executor is not None:
            await self._maybe_recover_stale_active_executions()

        requested_ids = await self._list_requested()
        requested_ids = await self._filter_current_status(
            requested_ids,
            expected=WorkspaceStatus.requested,
            action="provision",
        )
        if requested_ids:
            requested_ids = await self._claim_requested_ids(requested_ids)
        if requested_ids:
            await self._record_ordered_decisions(
                requested_ids,
                reason_code=ORDERED_REQUESTED_PROVISIONING_REASON,
            )
            provision_tasks = [
                asyncio.create_task(
                    self._safely_provision_claimed(ws_id),
                    name=f"awf-provision-{ws_id}",
                )
                for ws_id in requested_ids
            ]
            await asyncio.gather(*provision_tasks, return_exceptions=False)
            dispatched_ids.update(requested_ids)

        if self._executor is not None:
            execution_slots = self._available_execution_slots()
            if execution_slots > 0:
                active_execution_ids = set(self._execution_tasks)
                monitoring_ids = await self._list_monitoring_pr(
                    limit=execution_slots,
                    exclude_ids=active_execution_ids,
                )
                monitoring_ids = await self._filter_current_status(
                    monitoring_ids,
                    expected=WorkspaceStatus.monitoring_pr,
                    action="resume_pr_monitor",
                )
                monitoring_ids = await self._claim_monitoring_pr_ids(
                    monitoring_ids,
                    limit=execution_slots,
                )
                monitor_dispatch_ids = self._dispatchable_execution_ids(
                    monitoring_ids,
                    limit=execution_slots,
                )
                await self._record_ordered_decisions(
                    monitor_dispatch_ids,
                    reason_code=ORDERED_MONITOR_RESUME_REASON,
                )
                monitor_dispatched = self._dispatch_monitor_resumes(
                    monitor_dispatch_ids,
                    limit=execution_slots,
                )
                dispatched_ids.update(monitor_dispatched)

            execution_slots = self._available_execution_slots()
            if execution_slots > 0:
                ready_ids = await self._list_ready(
                    limit=execution_slots,
                    exclude_ids=set(self._execution_tasks),
                )
                ready_ids = await self._filter_current_status(
                    ready_ids,
                    expected=WorkspaceStatus.ready,
                    action="execute",
                )
                ready_dispatch_ids = self._dispatchable_execution_ids(
                    ready_ids,
                    limit=execution_slots,
                )
                await self._record_ordered_decisions(
                    ready_dispatch_ids,
                    reason_code=ORDERED_READY_EXECUTION_REASON,
                )
                ready_dispatched = self._dispatch_ready_executions(
                    ready_dispatch_ids,
                    limit=execution_slots,
                )
                dispatched_ids.update(ready_dispatched)

        return len(dispatched_ids)

    async def wait_for_execution_tasks(self) -> None:
        """Wait for ready execution or monitor-resume tasks started by this worker."""
        while self._execution_tasks:
            tasks = tuple(self._execution_tasks.values())
            await asyncio.gather(*tasks)
            for workspace_id, task in list(self._execution_tasks.items()):
                if task.done():
                    self._execution_tasks.pop(workspace_id, None)

    async def run_forever(self) -> None:
        while not self._stopped.is_set():
            try:
                dispatched = await self.run_once()
            except Exception:  # pragma: no cover - defensive: never die silently
                _log.exception("worker.run_once_failed")
                dispatched = 0

            if dispatched == 0:
                # Sleep up to poll_interval, waking early if a stop was requested.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stopped.wait(),
                        timeout=self._config.poll_interval_seconds,
                    )

    async def _log_transient_db_retry(self, exc: BaseException, attempt: int) -> None:
        _log.warning(
            "worker.db_connection_retry",
            reason_code=DB_CONNECTION_TRANSIENT_ATTEMPT_REASON,
            worker_id=self._worker_id,
            attempt=attempt,
            error_type=type(exc).__name__,
            error=str(exc)[:240],
        )

    async def _list_pending(self) -> list[str]:
        """Backward-compatible alias for the original requested query."""
        return await self._list_requested()

    async def _list_requested(self) -> list[str]:
        """Return requested candidates for the capacity-aware claim pass."""
        candidate_limit = (
            _scheduler_candidate_fetch_limit(self._config.max_concurrent_provisions)
            if _local_capacity_configured(self._config)
            else self._config.max_concurrent_provisions
        )
        return await self._list_by_status(
            WorkspaceStatus.requested,
            limit=candidate_limit,
        )

    async def _list_ready(
        self,
        *,
        limit: int | None = None,
        exclude_ids: set[str] | None = None,
    ) -> list[str]:
        """Return up to ``max_concurrent_executions`` workspace IDs in ``ready``."""
        row_limit = self._config.max_concurrent_executions if limit is None else limit
        return await self._list_by_status(
            WorkspaceStatus.ready,
            limit=row_limit,
            exclude_ids=exclude_ids,
        )

    async def _list_monitoring_pr(
        self,
        *,
        limit: int | None = None,
        exclude_ids: set[str] | None = None,
    ) -> list[str]:
        """Return up to ``max_concurrent_executions`` IDs in ``monitoring_pr``."""
        row_limit = self._config.max_concurrent_executions if limit is None else limit
        return await self._list_by_status(
            WorkspaceStatus.monitoring_pr,
            limit=row_limit,
            exclude_ids=exclude_ids,
        )

    async def _list_by_status(
        self,
        status: WorkspaceStatus,
        *,
        limit: int,
        exclude_ids: set[str] | None = None,
    ) -> list[str]:
        if limit <= 0:
            return []

        if status not in {
            WorkspaceStatus.requested,
            WorkspaceStatus.ready,
            WorkspaceStatus.monitoring_pr,
        }:

            async def _operation(session: AsyncSession) -> list[str]:
                ids = await WorkspaceRepository(session).list_schedulable_ids(
                    status=status,
                    limit=limit,
                    exclude_ids=exclude_ids,
                )
                return ids[:limit]

            return await run_db_operation_with_retry(
                self._session_factory,
                _operation,
                on_retry=self._log_transient_db_retry,
            )

        return await self._list_scheduler_dispatchable_ids(
            status=status,
            limit=limit,
            exclude_ids=exclude_ids,
        )

    async def _list_scheduler_dispatchable_ids(
        self,
        *,
        status: WorkspaceStatus,
        limit: int,
        exclude_ids: set[str] | None = None,
    ) -> list[str]:
        async def _operation(session: AsyncSession) -> list[str]:
            return await self._list_scheduler_dispatchable_ids_from_pages(
                session,
                status=status,
                limit=limit,
                exclude_ids=exclude_ids,
            )

        return await run_db_operation_with_retry(
            self._session_factory,
            _operation,
            commit=True,
            retry_commit_failures=False,
            on_retry=self._log_transient_db_retry,
        )

    async def _list_scheduler_dispatchable_ids_from_pages(
        self,
        session: AsyncSession,
        *,
        status: WorkspaceStatus,
        limit: int,
        exclude_ids: set[str] | None = None,
    ) -> list[str]:
        dispatchable_workspaces_by_id: dict[str, Workspace] = {}
        candidate_limit = _scheduler_candidate_fetch_limit(limit)
        candidate_after: SchedulerOrderCursor | None = None
        scoring_at = datetime.now(UTC)
        priority_refill_pages_remaining: int | None = None
        ordered_workspaces: list[Workspace] = []
        repo = WorkspaceRepository(session)
        while True:
            workspaces = await repo.list_schedulable_workspaces(
                status=status,
                limit=candidate_limit,
                exclude_ids=exclude_ids,
                after=candidate_after,
                scoring_at=scoring_at,
            )
            if not workspaces:
                break
            remaining_dispatch_slots = limit - len(dispatchable_workspaces_by_id)
            page_dispatchable_ids = await self._filter_scheduler_candidate_workspaces(
                session,
                workspaces,
                limit=remaining_dispatch_slots if remaining_dispatch_slots > 0 else limit,
                scoring_at=scoring_at,
            )
            workspaces_by_id = {workspace.id: workspace for workspace in workspaces}
            for workspace_id in page_dispatchable_ids:
                workspace = workspaces_by_id.get(workspace_id)
                if workspace is not None:
                    dispatchable_workspaces_by_id.setdefault(workspace_id, workspace)
            ordered_workspaces = _order_scheduler_workspaces(
                list(dispatchable_workspaces_by_id.values()),
                now=scoring_at,
            )
            if len(workspaces) < candidate_limit:
                break
            if len(ordered_workspaces) >= limit:
                # Preserve cross-page priority refill without scanning the tail
                # of a large status queue after dispatch slots are filled.
                if priority_refill_pages_remaining is None:
                    priority_refill_pages_remaining = _SCHEDULER_PRIORITY_REFILL_PAGES_AFTER_FILL
                if priority_refill_pages_remaining <= 0:
                    break
                priority_refill_pages_remaining -= 1
            candidate_after = _scheduler_candidate_cursor(
                workspaces,
                scoring_at=scoring_at,
                dialect_name=repo.dialect_name,
            )
        return [workspace.id for workspace in ordered_workspaces[:limit]]

    async def _filter_scheduler_candidate_workspaces(
        self,
        session: AsyncSession,
        candidate_workspaces: list[Workspace],
        *,
        limit: int,
        scoring_at: datetime,
    ) -> list[str]:
        if not candidate_workspaces:
            return []

        eligible = await self._filter_provider_recovery_suppressed(
            session,
            candidate_workspaces,
        )
        workspaces_by_id = {workspace.id: workspace for workspace in candidate_workspaces}
        eligible_workspaces_by_id: dict[str, Workspace] = {}
        for workspace_id in eligible:
            workspace = workspaces_by_id.get(workspace_id)
            if workspace is not None:
                eligible_workspaces_by_id.setdefault(workspace_id, workspace)
        ordered_workspaces = _order_scheduler_workspaces(
            list(eligible_workspaces_by_id.values()),
            now=scoring_at,
        )
        return [workspace.id for workspace in ordered_workspaces[:limit]]

    async def _filter_provider_recovery_suppressed(
        self,
        session: AsyncSession,
        workspaces: list[Workspace] | list[str],
    ) -> list[str]:
        if not workspaces:
            return []
        if _scheduler_items_are_workspace_ids(workspaces):
            workspace_ids = workspaces
            stmt = select(Workspace).where(Workspace.id.in_(workspace_ids))
            rows = {
                workspace.id: workspace for workspace in (await session.execute(stmt)).scalars()
            }
        elif _scheduler_items_are_workspaces(workspaces):
            workspace_rows = workspaces
            workspace_ids = [workspace.id for workspace in workspace_rows]
            rows = {workspace.id: workspace for workspace in workspace_rows}
        else:
            return []
        now = datetime.now(UTC)
        breaker_repo = ProviderModelCircuitBreakerRepository(session)
        allowed: set[str] = set()
        circuit_candidates: dict[str, tuple[str, str]] = {}
        for workspace_id in workspace_ids:
            workspace = rows.get(workspace_id)
            if workspace is None:
                continue
            not_before = provider_cooldown_not_before(workspace.task_policy)
            if not_before is not None and not_before > now:
                await _record_scheduler_queue_decision(
                    session,
                    workspace,
                    decision=QUEUE_DECISION_DEFERRED,
                    reason_code=PROVIDER_RECOVERY_NOT_BEFORE_REASON,
                    decided_at=now,
                    suppression_detail={"not_before": not_before.isoformat()},
                )
                continue
            model = agent_model_from_task_policy(workspace.task_policy)
            provider = provider_for_agent_model(workspace.agent, model)
            if provider is None or model is None:
                allowed.add(workspace_id)
                continue
            circuit_candidates[workspace_id] = (provider, model)

        open_breakers = await breaker_repo.open_breakers_for_pairs(
            pairs=circuit_candidates.values(),
            now=now,
        )
        for workspace_id, pair in circuit_candidates.items():
            if pair not in open_breakers:
                allowed.add(workspace_id)
                continue
            workspace = rows.get(workspace_id)
            if workspace is None:
                continue
            breaker = open_breakers[pair]
            await _record_scheduler_queue_decision(
                session,
                workspace,
                decision=QUEUE_DECISION_DEFERRED,
                reason_code=PROVIDER_MODEL_CIRCUIT_OPEN_REASON,
                decided_at=now,
                suppression_detail={
                    "provider": breaker.provider,
                    "model": breaker.model,
                    "cooldown_until": _json_datetime(breaker.cooldown_until),
                },
            )
        return [workspace_id for workspace_id in workspace_ids if workspace_id in allowed]

    async def _record_ordered_decisions(
        self,
        workspace_ids: list[str],
        *,
        reason_code: str,
    ) -> None:
        if not workspace_ids:
            return
        decided_at = datetime.now(UTC)

        async def _operation(session: AsyncSession) -> None:
            stmt = (
                select(
                    Workspace.id,
                    Workspace.task_class,
                    Workspace.task_policy,
                    Workspace.created_at,
                    TaskAttempt.task_id,
                    TaskAttempt.id,
                )
                .join(
                    TaskAttempt,
                    TaskAttempt.workspace_id == Workspace.id,
                )
                .where(Workspace.id.in_(workspace_ids))
            )
            rows = (await session.execute(stmt)).all()
            candidates_by_id = {
                row[0]: _OrderedDecisionCandidate(
                    workspace_id=row[0],
                    task_class=row[1],
                    task_policy=row[2],
                    created_at=row[3],
                    task_id=row[4],
                    attempt_id=row[5],
                )
                for row in rows
            }
            candidates = [
                candidates_by_id[workspace_id]
                for workspace_id in workspace_ids
                if workspace_id in candidates_by_id
            ]
            if not candidates:
                return

            queue_repo = QueueDecisionRepository(session)
            latest_by_workspace_id = await queue_repo.latest_by_workspace_ids(
                candidate.workspace_id for candidate in candidates
            )
            existing_ordered_decision_keys = await _existing_ordered_queue_decision_keys(
                session,
                candidates,
                reason_code=reason_code,
                decided_at=decided_at,
            )
            decision_rows = [
                _ordered_queue_decision_create(
                    candidate,
                    latest=latest_by_workspace_id.get(candidate.workspace_id),
                    reason_code=reason_code,
                    decided_at=decided_at,
                )
                for candidate in candidates
                if not _ordered_queue_decision_matches(
                    latest_by_workspace_id.get(candidate.workspace_id),
                    candidate,
                    reason_code=reason_code,
                    decided_at=decided_at,
                )
                and _ordered_queue_decision_key(candidate) not in existing_ordered_decision_keys
            ]
            await queue_repo.create_many(decision_rows)

        await run_db_operation_with_retry(
            self._session_factory,
            _operation,
            commit=True,
            retry_commit_failures=True,
            on_retry=self._log_transient_db_retry,
        )

    async def _filter_current_status(
        self,
        workspace_ids: list[str],
        *,
        expected: WorkspaceStatus,
        action: str,
    ) -> list[str]:
        if not workspace_ids:
            return []

        async def _operation(session: AsyncSession) -> dict[str, str]:
            stmt = select(Workspace.id, Workspace.status).where(Workspace.id.in_(workspace_ids))
            result = await session.execute(stmt)
            return {row[0]: row[1] for row in result.all()}

        statuses = await run_db_operation_with_retry(
            self._session_factory,
            _operation,
            on_retry=self._log_transient_db_retry,
        )

        current_ids: list[str] = []
        for workspace_id in workspace_ids:
            actual = statuses.get(workspace_id)
            if actual == expected.value:
                current_ids.append(workspace_id)
                continue

            _log.info(
                "worker.skip_stale_dispatch",
                workspace_id=workspace_id,
                action=action,
                expected_status=expected.value,
                status=actual,
            )
        return current_ids

    async def _maybe_recover_stale_active_executions(self) -> None:
        now = monotonic()
        if now < self._next_stale_active_execution_scan_at:
            return

        try:
            await self._recover_stale_active_executions()
        except Exception as exc:
            if _worker_exception_is_transient_db_connection(exc):
                interval = max(0.0, self._config.stale_active_execution_scan_interval_seconds)
                self._next_stale_active_execution_scan_at = monotonic() + interval
                _log.warning(
                    "worker.stale_active_execution_scan_db_connection_closed",
                    reason_code=DB_CONNECTION_CLOSED_REASON,
                    error_type=type(exc).__name__,
                    error=str(exc)[:240],
                )
                return
            raise

        interval = max(0.0, self._config.stale_active_execution_scan_interval_seconds)
        self._next_stale_active_execution_scan_at = monotonic() + interval

    async def _maybe_expire_due_secret_leases(self) -> None:
        now = monotonic()
        if now < self._next_secret_lease_expiration_scan_at:
            return

        try:
            await self._expire_due_secret_leases()
        except Exception as exc:
            if _worker_exception_is_transient_db_connection(exc):
                interval = max(0.0, self._config.secret_lease_expiration_scan_interval_seconds)
                self._next_secret_lease_expiration_scan_at = monotonic() + interval
                _log.warning(
                    "worker.secret_lease_expiration_db_connection_closed",
                    reason_code=DB_CONNECTION_CLOSED_REASON,
                    error_type=type(exc).__name__,
                    error=str(exc)[:240],
                )
                return
            _log.exception(
                "worker.secret_lease_expiration_failed",
                reason_code="SECRET_LEASE_EXPIRATION_FAILED",
            )
            raise

        interval = max(0.0, self._config.secret_lease_expiration_scan_interval_seconds)
        self._next_secret_lease_expiration_scan_at = monotonic() + interval

    async def _expire_due_secret_leases(self) -> None:
        async with session_scope(self._session_factory) as session:
            expired = await SecretLeaseService(session).expire_due_secret_leases()
            expired_count = len(expired)
            workspace_ids = sorted({lease.workspace_id for lease in expired})

        if expired_count:
            _log.info(
                "worker.secret_leases_expired",
                reason_code="SECRET_LEASES_EXPIRED",
                expired_count=expired_count,
                workspace_ids=workspace_ids,
            )

    async def _maybe_release_terminal_runtime(self) -> None:
        now = monotonic()
        if now < self._next_terminal_runtime_release_scan_at:
            return

        try:
            await self._release_terminal_runtime_resources()
        except Exception as exc:
            if _worker_exception_is_transient_db_connection(exc):
                _log.warning(
                    "worker.terminal_runtime_release_db_connection_closed",
                    reason_code=DB_CONNECTION_CLOSED_REASON,
                    error_type=type(exc).__name__,
                    error=str(exc)[:240],
                )
            else:
                _log.exception(
                    "worker.terminal_runtime_release_failed",
                    reason_code=_TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE,
                    error_type=type(exc).__name__,
                )
            interval = max(0.0, self._config.terminal_runtime_release_scan_interval_seconds)
            self._next_terminal_runtime_release_scan_at = monotonic() + interval
            return

        interval = max(0.0, self._config.terminal_runtime_release_scan_interval_seconds)
        self._next_terminal_runtime_release_scan_at = monotonic() + interval

    async def _release_terminal_runtime_resources(self) -> None:
        if self._runtime_cleaner is None:
            return
        candidates = await self._list_terminal_runtime_candidates(
            limit=self._config.terminal_runtime_release_max_per_scan,
        )
        release_errors: list[Exception] = []
        for candidate in candidates:
            try:
                await self._release_terminal_runtime_for_candidate(candidate)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if _worker_exception_is_transient_db_connection(exc):
                    _log.warning(
                        "worker.terminal_runtime_release_candidate_db_connection_closed",
                        workspace_id=candidate.workspace_id,
                        status=candidate.status.value,
                        compose_project_name=candidate.compose_project_name,
                        reason_code=DB_CONNECTION_CLOSED_REASON,
                        error_type=type(exc).__name__,
                        error=str(exc)[:240],
                    )
                else:
                    _log.exception(
                        "worker.terminal_runtime_release_candidate_failed",
                        workspace_id=candidate.workspace_id,
                        status=candidate.status.value,
                        compose_project_name=candidate.compose_project_name,
                        reason_code=_TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE,
                        error_type=type(exc).__name__,
                        error=str(exc)[:240],
                    )
                release_errors.append(exc)
        if len(release_errors) == 1:
            raise release_errors[0]
        if release_errors:
            raise ExceptionGroup(
                "terminal runtime release failed",
                release_errors,
            )

    async def _list_terminal_runtime_candidates(
        self,
        *,
        limit: int | None = None,
    ) -> list[_TerminalRuntimeCandidate]:
        if limit is not None and limit <= 0:
            return []
        terminal_status_values = [status.value for status in _TERMINAL_RELEASE_STATUSES]
        released_event_exists = (
            select(WorkspaceEvent.id)
            .where(WorkspaceEvent.workspace_id == Workspace.id)
            .where(WorkspaceEvent.event_type == _TERMINAL_RUNTIME_RELEASE_EVENT_TYPE)
            .where(WorkspaceEvent.reason_code == _TERMINAL_RUNTIME_RELEASE_REASON_CODE)
            .exists()
        )
        stmt = (
            select(
                Workspace.id,
                Workspace.status,
                Workspace.repo_url,
                Workspace.compose_project_name,
                Workspace.compose_file_path,
            )
            .where(Workspace.status.in_(terminal_status_values))
            # Include every terminal row on this node — even those where both
            # ``compose_project_name`` and ``compose_file_path`` are NULL.
            # The cleaner derives ``awf_<workspace_id>`` and falls back to
            # label-based removal, so legacy rows that predate persistence of
            # either field can still have a leaked default Compose project torn
            # down. Also include rows with NULL ``node_id``: ``Provisioner.
            # _mark_failed`` now stamps placement on the failure path so new
            # rows always carry the launching node, but legacy rows persisted
            # before that fix may still have NULL ``node_id``. In a multi-node
            # deployment the local cleaner would silently report success when
            # the resources actually live on a sibling node, so this fallback
            # is only safe while AWF is single-node (Phase 1 PRD §20.1). When
            # Phase 2 introduces multi-node, this branch must gain a node
            # ownership claim or a "found-something" precondition before it
            # records ``terminal_runtime_released``. ``~released_event_exists``
            # keeps each row to a single sweep.
            .where(
                or_(
                    Workspace.node_id == self._config.node_id,
                    Workspace.node_id.is_(None),
                )
            )
            .where(~released_event_exists)
            # Order by the retry marker if one has been recorded, falling back
            # to ``updated_at`` for rows that have never failed a release. The
            # marker lets persistently-failing workspaces rotate to the back of
            # the queue without advancing ``Workspace.updated_at`` — both
            # ``service/gc.py`` and ``service/orphan_resources.py`` use
            # ``updated_at`` as the retention cutoff, so bumping it on every
            # retry would indefinitely defer cleanup of stuck rows.
            .order_by(
                func.coalesce(Workspace.terminal_release_retry_at, Workspace.updated_at).asc(),
                Workspace.id.asc(),
            )
        )
        if limit is not None:
            stmt = stmt.limit(limit)

        async def _operation(session: AsyncSession) -> list[Any]:
            result = await session.execute(stmt)
            return list(result.all())

        rows = await run_db_operation_with_retry(
            self._session_factory,
            _operation,
            on_retry=self._log_transient_db_retry,
        )

        candidates: list[_TerminalRuntimeCandidate] = []
        for row in rows:
            (
                workspace_id,
                status_val,
                repo_url,
                compose_project_name,
                compose_file_path,
            ) = row
            if not repo_url:
                continue
            candidates.append(
                _TerminalRuntimeCandidate(
                    workspace_id=workspace_id,
                    status=WorkspaceStatus(status_val),
                    repo_url=repo_url,
                    compose_project_name=compose_project_name,
                    compose_file_path=compose_file_path,
                )
            )
        return candidates

    async def _release_terminal_runtime_for_candidate(
        self,
        candidate: _TerminalRuntimeCandidate,
    ) -> None:
        if self._runtime_cleaner is None:
            return
        try:
            cleanup = await self._runtime_cleaner.cleanup(
                workspace_id=candidate.workspace_id,
                repo_url=candidate.repo_url,
                compose_project_name=candidate.compose_project_name,
                compose_file_path=(
                    Path(candidate.compose_file_path) if candidate.compose_file_path else None
                ),
                remove_volumes=False,
                remove_worktree=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.exception(
                "worker.terminal_runtime_release_candidate_failed",
                workspace_id=candidate.workspace_id,
                status=candidate.status.value,
                compose_project_name=candidate.compose_project_name,
                reason_code=_TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE,
                error_type=type(exc).__name__,
                error=str(exc)[:240],
            )
            try:
                await self._record_terminal_runtime_release_failed(
                    candidate,
                    cleanup=None,
                    message=f"runtime cleanup raised {type(exc).__name__}: {exc}"[:480],
                )
            except asyncio.CancelledError:
                raise
            except Exception as record_exc:
                _log.exception(
                    "worker.terminal_runtime_release_event_write_failed",
                    workspace_id=candidate.workspace_id,
                    status=candidate.status.value,
                    compose_project_name=candidate.compose_project_name,
                    reason_code=_TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE,
                    error_type=type(record_exc).__name__,
                    error=str(record_exc)[:240],
                )
            return

        if cleanup.ok:
            await self._record_terminal_runtime_released(candidate, cleanup)
        else:
            try:
                await self._record_terminal_runtime_release_failed(
                    candidate,
                    cleanup=cleanup,
                    message="failed to stop or remove terminal workspace runtime",
                )
            except asyncio.CancelledError:
                raise
            except Exception as record_exc:
                # Mirror the cleanup-raised branch above: swallow + log a
                # dedicated event-write failure entry instead of letting the
                # exception propagate to ``_release_terminal_runtime_resources``
                # where it would re-log as ``candidate_failed`` and re-raise.
                # Both outcomes leave the workspace eligible for retry on the
                # next scan, so the error is recoverable without surfacing.
                _log.exception(
                    "worker.terminal_runtime_release_event_write_failed",
                    workspace_id=candidate.workspace_id,
                    status=candidate.status.value,
                    compose_project_name=candidate.compose_project_name,
                    reason_code=_TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE,
                    error_type=type(record_exc).__name__,
                    error=str(record_exc)[:240],
                )

    async def _record_terminal_runtime_released(
        self,
        candidate: _TerminalRuntimeCandidate,
        cleanup: WorkspaceCleanupResult,
    ) -> None:
        payload = {
            "compose_project_name": candidate.compose_project_name,
            "workspace_status": candidate.status.value,
            "cleanup": cleanup.to_dict(),
        }

        async def _operation(session: AsyncSession) -> bool:
            repo = WorkspaceRepository(session)
            # ``SELECT FOR UPDATE SKIP LOCKED`` serializes concurrent workers
            # racing to record the success event for the same workspace, which
            # the candidate query may surface to multiple workers when
            # ``Workspace.node_id`` is ``NULL`` (legacy rows in a Phase 2
            # multi-node deployment). The loser sees ``None`` and exits
            # without writing a duplicate ``workspace.terminal_runtime_released``
            # entry. On a single-node deployment this is a no-op.
            ws = await repo.get_for_update(candidate.workspace_id, skip_locked=True)
            if ws is None:
                return False
            if ws.status not in {status.value for status in _TERMINAL_RELEASE_STATUSES}:
                return False
            if await self._has_terminal_runtime_release_event(session, candidate.workspace_id):
                return False
            await repo.add_event(
                ws,
                event_type=_TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
                reason_code=_TERMINAL_RUNTIME_RELEASE_REASON_CODE,
                payload=payload,
            )
            return True

        recorded = await run_db_operation_with_retry(
            self._session_factory,
            _operation,
            commit=True,
            on_retry=self._log_transient_db_retry,
        )
        if not recorded:
            return

        _log.info(
            _TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
            workspace_id=candidate.workspace_id,
            status=candidate.status.value,
            compose_project_name=candidate.compose_project_name,
            reason_code=_TERMINAL_RUNTIME_RELEASE_REASON_CODE,
        )

    async def _record_terminal_runtime_release_failed(
        self,
        candidate: _TerminalRuntimeCandidate,
        *,
        cleanup: WorkspaceCleanupResult | None,
        message: str,
    ) -> None:
        payload: dict[str, Any] = {
            "compose_project_name": candidate.compose_project_name,
            "workspace_status": candidate.status.value,
            "message": message,
        }
        if cleanup is not None:
            payload["cleanup"] = cleanup.to_dict()

        async def _operation(session: AsyncSession) -> str:
            repo = WorkspaceRepository(session)
            # Mirror the ``SELECT FOR UPDATE SKIP LOCKED`` guard used by the
            # success path: when two workers surface the same NULL ``node_id``
            # candidate, both could otherwise pass the failure-event guard
            # before either commits and append duplicate
            # ``workspace.terminal_runtime_release_failed`` rows. The loser
            # sees ``None`` and exits; the winner records the single failure
            # event and bumps ``updated_at`` for backlog rotation.
            ws = await repo.get_for_update(candidate.workspace_id, skip_locked=True)
            if ws is None:
                return "skipped"
            if ws.status not in {status.value for status in _TERMINAL_RELEASE_STATUSES}:
                return "skipped"
            if await self._has_terminal_runtime_release_event(session, candidate.workspace_id):
                return "skipped"
            # Push the workspace behind newer terminal rows in the next scan: the
            # candidate query orders by
            # ``coalesce(terminal_release_retry_at, updated_at).asc()`` and
            # ``add_event`` does not touch either column, so without this bump a
            # persistently failing release would re-select the same rows every
            # scan and starve the backlog past ``terminal_runtime_release_max_per_scan``.
            # NOTE: the bump is intentionally applied *before* the idempotency
            # guard below. When a failure event already exists (the "duplicate"
            # early return), the mutation is still committed by
            # ``run_db_operation_with_retry`` (commit=True), rotating the row to
            # the back of the scan queue on every retry — preventing a single
            # persistently-failing workspace from monopolising the scan limit
            # across consecutive sweeps.
            #
            # ``Workspace.updated_at`` is intentionally NOT advanced here: GC
            # (``service/gc.py``) and orphan retention (``service/orphan_resources.py``)
            # use ``updated_at`` as the retention cutoff. Bumping it on every
            # retry would indefinitely defer cleanup of volumes/worktrees for
            # persistently-failing workspaces. Re-assigning ``ws.updated_at`` to
            # its prior value and flagging it modified suppresses the
            # ``onupdate=_now`` column default so the lifecycle timestamp stays
            # frozen across retries.
            prior_updated_at = ws.updated_at
            ws.terminal_release_retry_at = datetime.now(UTC)
            ws.updated_at = prior_updated_at
            flag_modified(ws, "updated_at")
            if await self._has_terminal_runtime_release_failure_event(
                session, candidate.workspace_id
            ):
                return "duplicate"
            primary_failure = await load_primary_failure_snapshot(session, ws)
            event_payload = attach_primary_failure(payload, primary_failure)
            await repo.add_event(
                ws,
                event_type=_TERMINAL_RUNTIME_RELEASE_FAILED_EVENT_TYPE,
                reason_code=_TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE,
                payload=event_payload,
            )
            return "recorded"

        outcome = await run_db_operation_with_retry(
            self._session_factory,
            _operation,
            commit=True,
            on_retry=self._log_transient_db_retry,
        )
        if outcome == "skipped":
            return
        if outcome == "duplicate":
            # A prior retry already wrote the failure event; suppressing the
            # duplicate keeps the event log lean, but we still bump
            # ``updated_at`` above and the candidate query keeps reselecting the
            # row because no success event exists. Emit a structured warning so
            # each retry leaves reason-code evidence in the log even when no
            # new event row is written.
            _log.warning(
                "worker.terminal_runtime_release_failed_retry",
                workspace_id=candidate.workspace_id,
                status=candidate.status.value,
                compose_project_name=candidate.compose_project_name,
                reason_code=_TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE,
                message=message,
            )
            return

        _log.error(
            _TERMINAL_RUNTIME_RELEASE_FAILED_EVENT_TYPE,
            workspace_id=candidate.workspace_id,
            status=candidate.status.value,
            compose_project_name=candidate.compose_project_name,
            reason_code=_TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE,
            message=message,
        )

    async def _has_terminal_runtime_release_event(
        self,
        session: AsyncSession,
        workspace_id: str,
    ) -> bool:
        stmt = (
            select(WorkspaceEvent.id)
            .where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.event_type == _TERMINAL_RUNTIME_RELEASE_EVENT_TYPE,
                WorkspaceEvent.reason_code == _TERMINAL_RUNTIME_RELEASE_REASON_CODE,
            )
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def _has_terminal_runtime_release_failure_event(
        self,
        session: AsyncSession,
        workspace_id: str,
    ) -> bool:
        stmt = (
            select(WorkspaceEvent.id)
            .where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.event_type == _TERMINAL_RUNTIME_RELEASE_FAILED_EVENT_TYPE,
                WorkspaceEvent.reason_code == _TERMINAL_RUNTIME_RELEASE_FAILED_REASON_CODE,
            )
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def _recover_stale_active_executions(self) -> None:
        candidates = await self._list_stale_active_execution_candidates(
            exclude_ids=set(self._execution_tasks)
        )
        recovery_errors: list[Exception] = []
        for candidate in candidates:
            try:
                await self._recover_stale_active_execution(candidate)
            except Exception as exc:
                if _worker_exception_is_transient_db_connection(exc):
                    _log.warning(
                        "worker.stale_active_execution_db_connection_closed",
                        workspace_id=candidate.workspace_id,
                        status=candidate.status.value,
                        reason_code=DB_CONNECTION_CLOSED_REASON,
                        error_type=type(exc).__name__,
                        error=str(exc)[:240],
                    )
                    await self._record_db_connection_closed_event(candidate, exc)
                    continue
                _log.exception(
                    "worker.stale_active_execution_recovery_failed",
                    workspace_id=candidate.workspace_id,
                    status=candidate.status.value,
                    reason_code=_STALE_ACTIVE_EXECUTION_RECOVERY_FAILED_REASON_CODE,
                    error_type=type(exc).__name__,
                    error=str(exc)[:240],
                )
                recovery_errors.append(exc)
        if len(recovery_errors) == 1:
            raise recovery_errors[0]
        if recovery_errors:
            raise ExceptionGroup(
                "stale active execution recovery failed",
                recovery_errors,
            )

    async def _record_db_connection_closed_event(
        self,
        candidate: _ActiveExecutionCandidate,
        exc: BaseException,
    ) -> None:
        payload = {
            "workspace_status": candidate.status.value,
            "compose_project_name": candidate.compose_project_name,
            "message": "Transient closed database connection interrupted worker recovery scan.",
            "error_type": type(exc).__name__,
            "error": str(exc)[:240],
        }
        try:
            async with session_scope(self._session_factory) as session:
                repo = WorkspaceRepository(session)
                ws = await repo.get(candidate.workspace_id)
                if ws is None or ws.status != candidate.status.value:
                    return
                await repo.add_event(
                    ws,
                    event_type=_DB_CONNECTION_TRANSIENT_EVENT_TYPE,
                    reason_code=DB_CONNECTION_CLOSED_REASON,
                    payload=payload,
                )
        except Exception:
            _log.exception(
                "worker.db_connection_closed_event_failed",
                workspace_id=candidate.workspace_id,
                reason_code=DB_CONNECTION_CLOSED_REASON,
            )

    async def _list_stale_active_execution_candidates(
        self,
        *,
        exclude_ids: set[str],
    ) -> list[_ActiveExecutionCandidate]:
        active_status_values = [status.value for status in _RUNTIME_HEALTH_SCAN_STATUSES]
        active_execution_values = [status.value for status in _ACTIVE_EXECUTION_STATUSES]
        claim_cutoff = datetime.now(UTC)
        stmt = (
            select(
                Workspace.id,
                Workspace.status,
                Workspace.repo_url,
                Workspace.agent,
                Workspace.compose_project_name,
                Workspace.compose_file_path,
                Workspace.pr_url,
                Workspace.task_policy,
            )
            .where(Workspace.status.in_(active_status_values))
            .where(Workspace.node_id == self._config.node_id)
            .where(
                or_(
                    Workspace.status.in_(
                        [
                            WorkspaceStatus.requested.value,
                            WorkspaceStatus.provisioning.value,
                            WorkspaceStatus.ready.value,
                        ]
                    ),
                    and_(
                        Workspace.status.in_(active_execution_values),
                        _stale_execution_claim_filter(claim_cutoff),
                    ),
                    and_(
                        Workspace.status == WorkspaceStatus.monitoring_pr.value,
                        _stale_monitor_claim_filter(claim_cutoff),
                    ),
                )
            )
            .order_by(Workspace.created_at.asc(), Workspace.id.asc())
        )
        if exclude_ids:
            stmt = stmt.where(~Workspace.id.in_(sorted(exclude_ids)))

        async def _operation(session: AsyncSession) -> list[Any]:
            result = await session.execute(stmt)
            return list(result.all())

        rows = await run_db_operation_with_retry(
            self._session_factory,
            _operation,
            on_retry=self._log_transient_db_retry,
        )

        return [
            _ActiveExecutionCandidate(
                workspace_id=row[0],
                status=WorkspaceStatus(row[1]),
                repo_url=row[2],
                agent=row[3],
                compose_project_name=row[4],
                compose_file_path=row[5],
                pr_url=row[6],
                task_policy=row[7],
            )
            for row in rows
        ]

    async def _recover_stale_active_execution(
        self,
        candidate: _ActiveExecutionCandidate,
    ) -> None:
        monitor_recovery_resume_pending = await _monitor_provider_recovery_resume_pending(
            self._session_factory,
            candidate,
        )
        if monitor_recovery_resume_pending:
            _log.info(
                "worker.monitor_provider_recovery_resume_pending",
                workspace_id=candidate.workspace_id,
                status=candidate.status.value,
                compose_project_name=candidate.compose_project_name,
                reason_code="PROVIDER_RECOVERY_MONITOR_RESUME_PENDING",
            )
            return
        if await self._has_current_preserved_active_execution(candidate):
            return
        try:
            snapshot = await self._runtime_inspector.inspect(candidate.compose_project_name)
        except Exception as exc:  # pragma: no cover - defensive around Docker tooling
            _log.exception(
                "worker.stale_active_execution_inspect_failed",
                workspace_id=candidate.workspace_id,
                status=candidate.status.value,
                compose_project_name=candidate.compose_project_name,
            )
            snapshot = RuntimeSnapshot(
                stack_state="unavailable",
                reason=f"runtime inspection failed: {exc}",
            )

        finding = classify_runtime_snapshot(_runtime_workspace(candidate), snapshot)
        task_policy: dict[str, Any] = candidate.task_policy or {}
        monitor_recovery_state = task_policy.get(PROVIDER_RECOVERY_STATE_KEY)
        is_retry_recovery = (
            isinstance(monitor_recovery_state, Mapping)
            and monitor_recovery_state.get("action") == "retry"
        )
        if (
            candidate.status == WorkspaceStatus.monitoring_pr
            and is_retry_recovery
            and candidate.compose_project_name
            and finding is None
            and snapshot.stack_state == "running"
        ):
            return
        if finding is not None and finding.status == "unavailable":
            if has_open_pr_for_remonitor(candidate.status, candidate.pr_url):
                recoverable_finding = WorkspaceRuntimeFinding(
                    workspace_id=finding.workspace_id,
                    workspace_status=finding.workspace_status,
                    status=finding.status,
                    reason_code=finding.reason_code,
                    decision="remonitor_workspace",
                    message=finding.message,
                    compose_project_name=finding.compose_project_name,
                    services=finding.services,
                )
                await self._record_recoverable_runtime_stranding(
                    candidate,
                    snapshot,
                    recoverable_finding,
                )
                return
            _log.warning(
                "worker.runtime_health_inspection_unavailable",
                workspace_id=candidate.workspace_id,
                status=candidate.status.value,
                compose_project_name=candidate.compose_project_name,
                runtime_reason=snapshot.reason,
                reason_code=finding.reason_code,
            )
            return
        if finding is not None and finding.decision == "fail_workspace":
            await self._fail_stranded_workspace(candidate, snapshot, finding)
            return
        if finding is not None and finding.decision == "remonitor_workspace":
            await self._record_recoverable_runtime_stranding(candidate, snapshot, finding)
            return
        if finding is not None and finding.decision == "defer_retry_policy":
            await self._record_recoverable_runtime_stranding(candidate, snapshot, finding)
            return
        if candidate.status in _ACTIVE_EXECUTION_STATUSES and _has_running_agent_runtime(snapshot):
            if await self._has_expired_preserved_active_execution(candidate):
                if not await self._record_stale_active_execution_detected(
                    candidate,
                    snapshot,
                ):
                    await self._cleanup_and_fail_stale_active_execution(candidate, snapshot)
                return
            if not await self._has_operator_refresh_after_latest_preservation(candidate):
                await self._record_preserved_active_execution_after_restart(candidate, snapshot)
                return
        if candidate.compose_project_name and snapshot.stack_state == "running":
            if not await self._record_stale_active_execution_detected(
                candidate,
                snapshot,
            ):
                await self._cleanup_and_fail_stale_active_execution(candidate, snapshot)
            return

    async def _record_stale_active_execution_detected(
        self,
        candidate: _ActiveExecutionCandidate,
        snapshot: RuntimeSnapshot,
    ) -> bool:
        payload = {
            "compose_project_name": candidate.compose_project_name,
            "workspace_status": candidate.status.value,
            "runtime": _runtime_snapshot_payload(snapshot),
        }

        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(candidate.workspace_id)
            if ws is None or ws.status != candidate.status.value:
                return False
            if not _execution_claim_is_stale(ws, datetime.now(UTC)):
                return False
            event_floor = await self._active_execution_preservation_event_floor(
                session,
                ws,
                candidate.status,
            )
            if await self._has_stale_active_execution_event(
                session,
                candidate.workspace_id,
                event_floor=event_floor,
            ):
                return False

            primary_failure = await load_primary_failure_snapshot(session, ws)
            event_payload = attach_primary_failure(payload, primary_failure)
            await repo.add_event(
                ws,
                event_type=_STALE_ACTIVE_EXECUTION_EVENT_TYPE,
                reason_code=_STALE_ACTIVE_EXECUTION_REASON_CODE,
                payload=event_payload,
            )
            await session.commit()

        _log.warning(
            _STALE_ACTIVE_EXECUTION_EVENT_TYPE,
            workspace_id=candidate.workspace_id,
            status=candidate.status.value,
            compose_project_name=candidate.compose_project_name,
            runtime=payload["runtime"],
        )
        return True

    async def _has_stale_active_execution_event(
        self,
        session: AsyncSession,
        workspace_id: str,
        *,
        event_floor: datetime | None = None,
    ) -> bool:
        stmt = (
            select(WorkspaceEvent.id)
            .where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.event_type == _STALE_ACTIVE_EXECUTION_EVENT_TYPE,
                WorkspaceEvent.reason_code == _STALE_ACTIVE_EXECUTION_REASON_CODE,
            )
            .limit(1)
        )
        if event_floor is not None:
            stmt = stmt.where(WorkspaceEvent.occurred_at >= event_floor)
        return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def _record_preserved_active_execution_after_restart(
        self,
        candidate: _ActiveExecutionCandidate,
        snapshot: RuntimeSnapshot,
    ) -> None:
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get_for_update(candidate.workspace_id)
            if ws is None or ws.status != candidate.status.value:
                return
            if not _execution_claim_is_stale(ws, now):
                return
            if await self._has_operator_refresh_after_latest_preservation_for_workspace(
                session,
                ws,
                candidate.status,
            ):
                return
            event_floor = await self._active_execution_preservation_event_floor(
                session,
                ws,
                candidate.status,
            )
            if await self._has_preserved_active_execution_event(
                session,
                candidate.workspace_id,
                candidate.status,
                event_floor=event_floor,
            ):
                return

            previous_claim = _workspace_claim_snapshot(ws)
            claim_cleanup = _active_execution_preservation_claim_cleanup_payload(
                ws,
                claim_cutoff=now,
            )
            if claim_cleanup["action"] == "cleared_stale":
                ws.execution_claimed_by = None
                ws.execution_claim_expires_at = None
            ws.subphase = _ACTIVE_EXECUTION_PRESERVED_SUBPHASE
            await repo.advance_workspace_version(ws)
            payload = _active_execution_preservation_payload(
                candidate,
                snapshot,
                worker_id=self._worker_id,
                previous_claim=previous_claim,
                claim_cleanup=claim_cleanup,
            )
            primary_failure = await load_primary_failure_snapshot(session, ws)
            # Active-execution preservation writes diagnostic refresh/non-state
            # payloads; causality lookup reads failed state-change events only.
            payload = attach_primary_failure(payload, primary_failure)
            operation = await OperationRepository(session).create(
                workspace_id=candidate.workspace_id,
                operation_type=OperationType.refresh,
                status=OperationStatus.running,
                payload=payload,
            )
            payload_with_operation = {**payload, "operation_id": operation.id}
            operation.payload = payload_with_operation
            await OperationRepository(session).finish(
                operation,
                status=OperationStatus.succeeded,
                result={
                    "decision": "preserve_runtime",
                    "reason_code": ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
                },
            )
            await repo.add_event(
                ws,
                event_type=ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE,
                reason_code=ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
                payload=payload_with_operation,
            )
            await session.commit()

        _log.warning(
            ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE,
            workspace_id=candidate.workspace_id,
            status=candidate.status.value,
            compose_project_name=candidate.compose_project_name,
            reason_code=ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
        )

    async def _has_operator_refresh_after_latest_preservation(
        self,
        candidate: _ActiveExecutionCandidate,
    ) -> bool:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(candidate.workspace_id)
            if ws is None or ws.status != candidate.status.value:
                return False
            return await self._has_operator_refresh_after_latest_preservation_for_workspace(
                session,
                ws,
                candidate.status,
            )

    async def _has_operator_refresh_after_latest_preservation_for_workspace(
        self,
        session: AsyncSession,
        workspace: Workspace,
        status: WorkspaceStatus,
    ) -> bool:
        event_floor = await self._active_execution_preservation_event_floor(
            session,
            workspace,
            status,
            include_operator_refresh=False,
            include_execution_claim_expiry=False,
        )
        latest_refresh = await self._latest_operator_refresh_requested_at(
            session,
            workspace.id,
            event_floor=event_floor,
        )
        if latest_refresh is None:
            return False
        latest_preservation = await self._latest_preserved_active_execution_at(
            session,
            workspace.id,
            status,
            event_floor=event_floor,
        )
        if latest_preservation is None:
            return False
        return _utc_datetime(latest_refresh) >= _utc_datetime(latest_preservation)

    async def _has_current_preserved_active_execution(
        self,
        candidate: _ActiveExecutionCandidate,
    ) -> bool:
        if candidate.status not in _ACTIVE_EXECUTION_STATUSES:
            return False

        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(candidate.workspace_id)
            if ws is None or ws.status != candidate.status.value:
                return False
            event_floor = await self._active_execution_preservation_event_floor(
                session,
                ws,
                candidate.status,
            )
            latest_preserved = await self._latest_preserved_active_execution_at(
                session,
                candidate.workspace_id,
                candidate.status,
                event_floor=event_floor,
            )
            if latest_preserved is None:
                return False
            return not self._active_execution_preservation_is_expired(latest_preserved)

    async def _has_expired_preserved_active_execution(
        self,
        candidate: _ActiveExecutionCandidate,
    ) -> bool:
        if candidate.status not in _ACTIVE_EXECUTION_STATUSES:
            return False

        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(candidate.workspace_id)
            if ws is None or ws.status != candidate.status.value:
                return False
            event_floor = await self._active_execution_preservation_event_floor(
                session,
                ws,
                candidate.status,
            )
            latest_preserved = await self._latest_preserved_active_execution_at(
                session,
                candidate.workspace_id,
                candidate.status,
                event_floor=event_floor,
            )
            if latest_preserved is None:
                return False
            return self._active_execution_preservation_is_expired(latest_preserved)

    def _active_execution_preservation_is_expired(self, preserved_at: datetime) -> bool:
        grace = max(0.0, self._config.active_execution_preservation_grace_seconds)
        return datetime.now(UTC) - _utc_datetime(preserved_at) >= timedelta(seconds=grace)

    async def _has_preserved_active_execution_event(
        self,
        session: AsyncSession,
        workspace_id: str,
        status: WorkspaceStatus,
        *,
        event_floor: datetime | None = None,
    ) -> bool:
        stmt = (
            select(WorkspaceEvent.id)
            .where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.event_type == ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE,
                WorkspaceEvent.reason_code == ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
                WorkspaceEvent.payload["workspace_status"].as_string() == status.value,
            )
            .limit(1)
        )
        if event_floor is not None:
            stmt = stmt.where(WorkspaceEvent.occurred_at >= event_floor)
        return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def _latest_preserved_active_execution_at(
        self,
        session: AsyncSession,
        workspace_id: str,
        status: WorkspaceStatus,
        *,
        event_floor: datetime | None = None,
    ) -> datetime | None:
        stmt = (
            select(WorkspaceEvent.occurred_at)
            .where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.event_type == ACTIVE_EXECUTION_PRESERVED_EVENT_TYPE,
                WorkspaceEvent.reason_code == ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
                WorkspaceEvent.payload["workspace_status"].as_string() == status.value,
            )
            .order_by(WorkspaceEvent.occurred_at.desc(), WorkspaceEvent.id.desc())
            .limit(1)
        )
        if event_floor is not None:
            stmt = stmt.where(WorkspaceEvent.occurred_at >= event_floor)
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _latest_operator_refresh_requested_at(
        self,
        session: AsyncSession,
        workspace_id: str,
        *,
        event_floor: datetime | None = None,
    ) -> datetime | None:
        stmt = (
            select(WorkspaceEvent.occurred_at)
            .where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.event_type == OPERATOR_REFRESH_EVENT_TYPE,
                WorkspaceEvent.reason_code == OPERATOR_REFRESH_REASON_CODE,
            )
            .order_by(WorkspaceEvent.occurred_at.desc(), WorkspaceEvent.id.desc())
            .limit(1)
        )
        if event_floor is not None:
            stmt = stmt.where(WorkspaceEvent.occurred_at >= event_floor)
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _active_execution_preservation_event_floor(
        self,
        session: AsyncSession,
        workspace: Workspace,
        status: WorkspaceStatus,
        *,
        include_operator_refresh: bool = True,
        include_execution_claim_expiry: bool = True,
    ) -> datetime | None:
        floors: list[datetime] = []
        if include_execution_claim_expiry and workspace.execution_claim_expires_at is not None:
            claim_floor = _utc_datetime(workspace.execution_claim_expires_at)
            if claim_floor <= datetime.now(UTC):
                floors.append(claim_floor)

        stmt = (
            select(WorkspaceEvent.occurred_at)
            .where(
                WorkspaceEvent.workspace_id == workspace.id,
                WorkspaceEvent.event_type == "workspace.state_changed",
                WorkspaceEvent.new_state == status.value,
            )
            .order_by(WorkspaceEvent.occurred_at.desc(), WorkspaceEvent.id.desc())
            .limit(1)
        )
        status_started_at = (await session.execute(stmt)).scalar_one_or_none()
        if status_started_at is not None:
            floors.append(_utc_datetime(status_started_at))

        # An operator refresh is an explicit recovery request for preserved runtime.
        # Preservation evidence older than that request must not keep scans skipped.
        if include_operator_refresh:
            refresh_requested_at = await self._latest_operator_refresh_requested_at(
                session,
                workspace.id,
            )
            if refresh_requested_at is not None:
                floors.append(_utc_datetime(refresh_requested_at))

        return max(floors) if floors else None

    async def _cleanup_and_fail_stale_active_execution(
        self,
        candidate: _ActiveExecutionCandidate,
        snapshot: RuntimeSnapshot,
    ) -> None:
        if not await self._stale_active_execution_can_fail(candidate):
            return
        if self._runtime_cleaner is None or not candidate.repo_url:
            await self._record_stale_active_execution_cleanup_failed(
                candidate,
                snapshot,
                cleanup=None,
                message=(
                    "runtime cleanup is not configured"
                    if self._runtime_cleaner is None
                    else "workspace has no repo_url for cleanup"
                ),
            )
            return

        cleanup = await self._runtime_cleaner.cleanup(
            workspace_id=candidate.workspace_id,
            repo_url=candidate.repo_url,
            compose_project_name=candidate.compose_project_name,
            compose_file_path=(
                Path(candidate.compose_file_path) if candidate.compose_file_path else None
            ),
            remove_volumes=True,
            remove_worktree=False,
        )
        if not cleanup.ok:
            await self._record_stale_active_execution_cleanup_failed(
                candidate,
                snapshot,
                cleanup=cleanup,
                message="failed to stop or remove stale workspace runtime",
            )
            return

        await self._fail_stale_active_execution(candidate, snapshot)

    async def _stale_active_execution_can_fail(
        self,
        candidate: _ActiveExecutionCandidate,
    ) -> bool:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(candidate.workspace_id)
            if ws is None or ws.status != candidate.status.value:
                return False
            if not _execution_claim_is_stale(ws, datetime.now(UTC)):
                return False
            event_floor = await self._active_execution_preservation_event_floor(
                session,
                ws,
                candidate.status,
            )
            latest_preserved = await self._latest_preserved_active_execution_at(
                session,
                candidate.workspace_id,
                candidate.status,
                event_floor=event_floor,
            )
            if latest_preserved is not None and not self._active_execution_preservation_is_expired(
                latest_preserved
            ):
                return False
            return await self._has_stale_active_execution_event(
                session,
                candidate.workspace_id,
                event_floor=event_floor,
            )

    async def _record_stale_active_execution_cleanup_failed(
        self,
        candidate: _ActiveExecutionCandidate,
        snapshot: RuntimeSnapshot,
        *,
        cleanup: WorkspaceCleanupResult | None,
        message: str,
    ) -> None:
        payload: dict[str, Any] = {
            "compose_project_name": candidate.compose_project_name,
            "workspace_status": candidate.status.value,
            "runtime": _runtime_snapshot_payload(snapshot),
            "message": message,
        }
        if cleanup is not None:
            payload["cleanup"] = cleanup.to_dict()

        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(candidate.workspace_id)
            if ws is None or ws.status != candidate.status.value:
                return
            if not _execution_claim_is_stale(ws, datetime.now(UTC)):
                return
            primary_failure = await load_primary_failure_snapshot(session, ws)
            event_payload = attach_primary_failure(payload, primary_failure)
            await repo.add_event(
                ws,
                event_type=_STALE_ACTIVE_EXECUTION_CLEANUP_FAILED_EVENT_TYPE,
                reason_code=_STALE_ACTIVE_EXECUTION_CLEANUP_FAILED_REASON_CODE,
                payload=event_payload,
            )
            await session.commit()

        _log.error(
            _STALE_ACTIVE_EXECUTION_CLEANUP_FAILED_EVENT_TYPE,
            workspace_id=candidate.workspace_id,
            status=candidate.status.value,
            compose_project_name=candidate.compose_project_name,
            reason_code=_STALE_ACTIVE_EXECUTION_CLEANUP_FAILED_REASON_CODE,
            message=message,
        )

    async def _fail_stale_active_execution(
        self,
        candidate: _ActiveExecutionCandidate,
        snapshot: RuntimeSnapshot,
    ) -> None:
        message = _stale_active_execution_failure_message(candidate, snapshot)
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get_for_update(candidate.workspace_id)
            if ws is None or ws.status != candidate.status.value:
                return
            claim_cutoff = datetime.now(UTC)
            if not _execution_claim_is_stale(ws, claim_cutoff):
                return
            # Keep causality and transition in one locked epoch where row
            # locks exist; the guarded transition below preserves the stale
            # claim predicate for dialects without row locks.
            failure_causality = await load_failure_causality_snapshot(session, ws)
            primary_failure = (
                failure_causality.primary_failure if failure_causality is not None else None
            )
            previous_secondary_failures = (
                failure_causality.secondary_failures if failure_causality is not None else ()
            )
            secondary_failure = _secondary_stale_active_execution_payload(
                candidate,
                snapshot,
                message=message,
            )
            reason_code = primary_failure_reason_code(
                primary_failure,
                fallback=_STALE_ACTIVE_EXECUTION_REASON_CODE,
            )
            transition_payload = (
                build_preserved_failure_payload(
                    primary_failure,
                    secondary_failure=secondary_failure,
                    previous_secondary_failures=previous_secondary_failures,
                )
                if primary_failure is not None
                else None
            )
            transitioned = await repo.transition_if_current(
                candidate.workspace_id,
                from_status=candidate.status,
                to=WorkspaceStatus.failed,
                reason_code=reason_code,
                payload=transition_payload,
                extra_conditions=(_stale_execution_claim_filter(claim_cutoff),),
            )
            if transitioned is None:
                return
            ws = transitioned
            ws.execution_claimed_by = None
            ws.execution_claim_expires_at = None
            if primary_failure is None:
                ws.failure_reason = FailureReason.infrastructure_failure.value
                ws.failure_message = message[:2048]
            else:
                restore_primary_failure_row_fields(ws, primary_failure)
            await session.commit()

        _log.error(
            "worker.stale_active_execution_failed",
            workspace_id=candidate.workspace_id,
            status=candidate.status.value,
            compose_project_name=candidate.compose_project_name,
            runtime_state=snapshot.stack_state,
            runtime_reason=snapshot.reason,
            reason_code=_STALE_ACTIVE_EXECUTION_REASON_CODE,
        )

    async def _fail_stranded_workspace(
        self,
        candidate: _ActiveExecutionCandidate,
        snapshot: RuntimeSnapshot,
        finding: WorkspaceRuntimeFinding,
    ) -> None:
        message = _runtime_stranding_failure_message(candidate, finding)
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get_for_update(candidate.workspace_id)
            if ws is None or ws.status != candidate.status.value:
                return
            claim_cutoff = datetime.now(UTC)
            if not _workspace_claim_recheck_passes(ws, candidate.status, claim_cutoff):
                return
            # Keep causality and transition in one locked epoch where row
            # locks exist; the guarded transition below preserves the stale
            # claim predicate for dialects without row locks.
            failure_causality = await load_failure_causality_snapshot(session, ws)
            primary_failure = (
                failure_causality.primary_failure if failure_causality is not None else None
            )
            previous_secondary_failures = (
                failure_causality.secondary_failures if failure_causality is not None else ()
            )
            secondary_failure = _secondary_runtime_stranding_payload(
                candidate,
                snapshot,
                finding,
                message=message,
            )
            reason_code = primary_failure_reason_code(
                primary_failure,
                fallback=finding.reason_code,
            )
            transition_payload = (
                build_preserved_failure_payload(
                    primary_failure,
                    secondary_failure=secondary_failure,
                    previous_secondary_failures=previous_secondary_failures,
                )
                if primary_failure is not None
                else None
            )
            transitioned = await repo.transition_if_current(
                candidate.workspace_id,
                from_status=candidate.status,
                to=WorkspaceStatus.failed,
                reason_code=reason_code,
                payload=transition_payload,
                extra_conditions=_claim_recheck_conditions(candidate.status, claim_cutoff),
            )
            if transitioned is None:
                return
            ws = transitioned
            ws.execution_claimed_by = None
            ws.execution_claim_expires_at = None
            ws.monitor_claimed_by = None
            ws.monitor_claim_expires_at = None
            if primary_failure is None:
                ws.failure_reason = FailureReason.infrastructure_failure.value
                ws.failure_message = message[:2048]
            else:
                restore_primary_failure_row_fields(ws, primary_failure)
            event_payload = attach_primary_failure(
                _runtime_stranding_event_payload(candidate, snapshot, finding),
                primary_failure,
            )
            await repo.add_event(
                ws,
                event_type=RUNTIME_STRANDED_EVENT_TYPE,
                reason_code=finding.reason_code,
                payload=event_payload,
            )
            await session.commit()

        _log.error(
            "worker.runtime_stranded_workspace_failed",
            workspace_id=candidate.workspace_id,
            status=candidate.status.value,
            compose_project_name=candidate.compose_project_name,
            runtime_state=snapshot.stack_state,
            runtime_reason=snapshot.reason,
            reason_code=finding.reason_code,
        )

    async def _record_recoverable_runtime_stranding(
        self,
        candidate: _ActiveExecutionCandidate,
        snapshot: RuntimeSnapshot,
        finding: WorkspaceRuntimeFinding,
    ) -> None:
        payload = _runtime_stranding_event_payload(candidate, snapshot, finding)
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(candidate.workspace_id)
            if ws is None or ws.status != candidate.status.value:
                return
            if not _candidate_claim_is_stale(ws, candidate.status, datetime.now(UTC)):
                return
            if await self._has_runtime_stranding_event(
                session,
                candidate.workspace_id,
                finding.reason_code,
            ):
                return
            claims_will_clear = any(
                value is not None
                for value in (
                    ws.execution_claimed_by,
                    ws.execution_claim_expires_at,
                    ws.monitor_claimed_by,
                    ws.monitor_claim_expires_at,
                )
            )
            ws.execution_claimed_by = None
            ws.execution_claim_expires_at = None
            ws.monitor_claimed_by = None
            ws.monitor_claim_expires_at = None
            if claims_will_clear:
                await repo.advance_workspace_version(ws)
            await repo.add_event(
                ws,
                event_type=RUNTIME_STRANDED_EVENT_TYPE,
                reason_code=finding.reason_code,
                payload=payload,
            )
            await session.commit()

        _log.warning(
            "worker.runtime_stranded_workspace_recoverable",
            workspace_id=candidate.workspace_id,
            status=candidate.status.value,
            compose_project_name=candidate.compose_project_name,
            runtime_state=snapshot.stack_state,
            runtime_reason=snapshot.reason,
            reason_code=finding.reason_code,
            decision=finding.decision,
        )

    async def _has_runtime_stranding_event(
        self,
        session: AsyncSession,
        workspace_id: str,
        reason_code: str,
    ) -> bool:
        stmt = (
            select(WorkspaceEvent.id)
            .where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.event_type == RUNTIME_STRANDED_EVENT_TYPE,
                WorkspaceEvent.reason_code == reason_code,
            )
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none() is not None

    def _available_execution_slots(self) -> int:
        return max(0, self._config.max_concurrent_executions - len(self._execution_tasks))

    def _dispatchable_execution_ids(self, workspace_ids: list[str], *, limit: int) -> list[str]:
        dispatchable: list[str] = []
        for workspace_id in workspace_ids:
            if len(dispatchable) >= limit:
                break
            if workspace_id in self._execution_tasks:
                continue
            dispatchable.append(workspace_id)
        return dispatchable

    def _dispatch_ready_executions(self, workspace_ids: list[str], *, limit: int) -> set[str]:
        dispatched: set[str] = set()
        for workspace_id in workspace_ids:
            if len(dispatched) >= limit:
                break
            if workspace_id in self._execution_tasks:
                continue

            task = asyncio.create_task(
                self._safely_execute_claimed(workspace_id),
                name=f"awf-execute-{workspace_id}",
            )
            self._execution_tasks[workspace_id] = task
            task.add_done_callback(partial(self._forget_execution_task, workspace_id))
            dispatched.add(workspace_id)
        return dispatched

    def _dispatch_monitor_resumes(self, workspace_ids: list[str], *, limit: int) -> set[str]:
        dispatched: set[str] = set()
        for workspace_id in workspace_ids:
            if len(dispatched) >= limit:
                break
            if workspace_id in self._execution_tasks:
                continue

            recovery_operation_id = self._monitor_recovery_operation_ids.get(workspace_id)
            task = asyncio.create_task(
                self._safely_resume_claimed_pr_monitor(
                    workspace_id,
                    recovery_operation_id=recovery_operation_id,
                ),
                name=f"awf-monitor-{workspace_id}",
            )
            self._execution_tasks[workspace_id] = task
            task.add_done_callback(partial(self._forget_execution_task, workspace_id))
            dispatched.add(workspace_id)
        return dispatched

    def _forget_execution_task(self, workspace_id: str, _task: asyncio.Task[None]) -> None:
        self._execution_tasks.pop(workspace_id, None)

    async def _safely_provision_claimed(self, workspace_id: str) -> None:
        try:
            await self._provisioner.provision_claimed(workspace_id)
        except Exception:
            # Provisioner.provision_claimed() already logged + transitioned to failed;
            # we swallow here so one bad workspace doesn't abort the batch.
            _log.exception("worker.provision_failed", workspace_id=workspace_id)

    async def _safely_execute(self, workspace_id: str) -> None:
        if self._executor is None:
            return
        try:
            await self._executor.execute(
                workspace_id,
                execution_owner_id=self._worker_id,
                execution_lease_expires_at=self._execution_claim_expires_at(),
            )
        except Exception:
            # WorkspaceExecutor.execute() owns state transitions, including
            # skip-if-no-longer-ready semantics. The worker must keep polling
            # even if one execution path crashes before it can mark a failure.
            _log.exception("worker.execute_failed", workspace_id=workspace_id)

    async def _safely_execute_claimed(self, workspace_id: str) -> None:
        heartbeat = asyncio.create_task(
            self._refresh_execution_claim_loop(workspace_id),
            name=f"awf-execution-claim-{workspace_id}",
        )
        try:
            await self._safely_execute(workspace_id)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            await self._release_execution_claim(workspace_id)

    async def _safely_resume_pr_monitor(
        self,
        workspace_id: str,
        *,
        recovery_operation_id: str | None = None,
    ) -> None:
        if self._executor is None:
            await self._finish_monitor_recovery_operation(
                workspace_id,
                operation_id=recovery_operation_id,
                status=OperationStatus.failed,
                error_code="MONITOR_RECOVERY_NO_EXECUTOR",
                error_message="Worker has no executor configured.",
            )
            return
        try:
            await self._executor.resume_pr_monitor(workspace_id)
        except Exception as exc:
            # The monitor runner owns normal terminal transitions. Recovery
            # dispatch still must not take the service worker down if a single
            # workspace hits an unexpected runtime error.
            _log.exception("worker.pr_monitor_resume_failed", workspace_id=workspace_id)
            await self._finish_monitor_recovery_operation(
                workspace_id,
                operation_id=recovery_operation_id,
                status=OperationStatus.failed,
                error_code="MONITOR_RECOVERY_FAILED",
                error_message=repr(exc)[:2000],
            )
            return

        await self._finish_monitor_recovery_operation(
            workspace_id,
            operation_id=recovery_operation_id,
            status=OperationStatus.succeeded,
        )

    async def _claim_requested_ids(self, workspace_ids: list[str]) -> list[str]:
        if not workspace_ids or self._config.max_concurrent_provisions <= 0:
            return []
        if not _local_capacity_configured(self._config):
            claimed: list[str] = []
            for workspace_id in workspace_ids:
                if len(claimed) >= self._config.max_concurrent_provisions:
                    break
                if await self._claim_requested_for_provisioning(workspace_id):
                    claimed.append(workspace_id)
            return claimed

        async def _operation(session: AsyncSession) -> list[str]:
            await _acquire_local_capacity_scheduler_lock(
                session,
                node_id=self._config.node_id or "local",
            )
            return await self._claim_requested_ids_with_capacity(session, workspace_ids)

        return await run_db_operation_with_retry(
            self._session_factory,
            _operation,
            commit=True,
            retry_commit_failures=False,
            on_retry=self._log_transient_db_retry,
        )

    async def _claim_requested_ids_with_capacity(
        self,
        session: AsyncSession,
        workspace_ids: list[str],
    ) -> list[str]:
        stmt = select(Workspace).where(
            Workspace.id.in_(workspace_ids),
            Workspace.status == WorkspaceStatus.requested.value,
        )
        workspaces_by_id = {
            workspace.id: workspace for workspace in (await session.execute(stmt)).scalars()
        }
        candidates = _order_scheduler_workspaces(
            [
                workspaces_by_id[workspace_id]
                for workspace_id in workspace_ids
                if workspace_id in workspaces_by_id
            ]
        )
        if not candidates:
            await self._log_stale_requested_claims(session, workspace_ids)
            return []

        reservation_repo = ResourceReservationRepository(session)
        allocated = await _allocated_totals_for_capacity_gate(
            session,
            reservation_repo=reservation_repo,
            config=self._config,
        )
        reservations = await reservation_repo.active_latest_by_workspace_ids(
            workspace.id for workspace in candidates
        )
        claimed: list[str] = []
        now = datetime.now(UTC)
        repo = WorkspaceRepository(session)
        for workspace in candidates:
            if len(claimed) >= self._config.max_concurrent_provisions:
                break
            demand = _reservation_demand_for_workspace(
                workspace,
                reservation=reservations.get(workspace.id),
                config=self._config,
            )
            blockers = _local_capacity_blockers(
                allocated=allocated,
                demand=demand,
                config=self._config,
            )
            if blockers:
                reason_code = (
                    LOCAL_CAPACITY_UNSATISFIABLE_REASON
                    if any(blocker.unsatisfiable for blocker in blockers)
                    else LOCAL_CAPACITY_DEFERRED_REASON
                )
                await _record_capacity_queue_decision(
                    session,
                    workspace,
                    decision=QUEUE_DECISION_DEFERRED,
                    reason_code=reason_code,
                    decided_at=now,
                    allocated=allocated,
                    demand=demand,
                    blockers=blockers,
                )
                continue
            if demand.defaulted:
                await _record_capacity_queue_decision(
                    session,
                    workspace,
                    decision=QUEUE_DECISION_ORDERED,
                    reason_code=LOCAL_CAPACITY_RESERVATION_DEFAULTED_REASON,
                    decided_at=now,
                    allocated=allocated,
                    demand=demand,
                    blockers=[],
                )
            ws = await repo.transition_if_current(
                workspace.id,
                from_status=WorkspaceStatus.requested,
                to=WorkspaceStatus.provisioning,
                reason_code="WORKER_CLAIMED",
            )
            if ws is None:
                await self._log_stale_requested_claims(session, [workspace.id])
                continue
            claimed.append(workspace.id)
            allocated.add(demand)
        return claimed

    async def _log_stale_requested_claims(
        self,
        session: AsyncSession,
        workspace_ids: list[str],
    ) -> None:
        repo = WorkspaceRepository(session)
        for workspace_id in workspace_ids:
            current = await repo.get(workspace_id)
            _log.info(
                "worker.skip_stale_dispatch",
                workspace_id=workspace_id,
                action="provision",
                expected_status=WorkspaceStatus.requested.value,
                status=current.status if current is not None else None,
            )

    async def _claim_requested_for_provisioning(self, workspace_id: str) -> bool:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.transition_if_current(
                workspace_id,
                from_status=WorkspaceStatus.requested,
                to=WorkspaceStatus.provisioning,
                reason_code="WORKER_CLAIMED",
            )
            if ws is not None:
                await session.commit()
                return True

            current = await repo.get(workspace_id)
            _log.info(
                "worker.skip_stale_dispatch",
                workspace_id=workspace_id,
                action="provision",
                expected_status=WorkspaceStatus.requested.value,
                status=current.status if current is not None else None,
            )
            return False

    async def _claim_monitoring_pr_ids(self, workspace_ids: list[str], *, limit: int) -> list[str]:
        claimed: list[str] = []
        for workspace_id in workspace_ids:
            if len(claimed) >= limit:
                break
            if workspace_id in self._execution_tasks:
                continue
            if await self._claim_monitoring_pr(workspace_id):
                claimed.append(workspace_id)
        return claimed

    async def _claim_monitoring_pr(self, workspace_id: str) -> bool:
        now = datetime.now(UTC)
        lease_expires_at = now + timedelta(seconds=self._config.monitor_claim_lease_seconds)
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None:
                return False
            previous_claim = _workspace_claim_snapshot(ws)
            runtime_stranding_reason = _latest_runtime_stranding_reason(ws.events)
            execution_claim_cleanup = _monitor_recovery_execution_claim_cleanup_payload(
                ws,
                claim_cutoff=now,
            )
            claimed = await repo.claim_monitoring_pr(
                workspace_id,
                owner_id=self._worker_id,
                lease_expires_at=lease_expires_at,
                now=now,
                clear_stale_execution_claim_cutoff=now,
            )
            if claimed:
                await session.refresh(ws)
                if (
                    ws.execution_claimed_by is not None
                    or ws.execution_claim_expires_at is not None
                    or execution_claim_cleanup["action"] == "preserved_unexpired"
                ):
                    execution_claim_cleanup = _monitor_recovery_execution_claim_cleanup_payload(
                        ws,
                        claim_cutoff=now,
                    )
                claim_cleanup = _monitor_recovery_claim_cleanup_payload(
                    ws,
                    claim_cutoff=now,
                    monitor_claimed_by=self._worker_id,
                    monitor_claim_expires_at=lease_expires_at,
                    execution_claim_cleanup=execution_claim_cleanup,
                )
                operation_payload = _monitor_recovery_payload(
                    ws,
                    worker_id=self._worker_id,
                    previous_claim=previous_claim,
                    claim_cleanup=claim_cleanup,
                    runtime_stranding_reason=runtime_stranding_reason,
                )
                operation = await OperationRepository(session).create(
                    workspace_id=workspace_id,
                    operation_type=OperationType.remonitor,
                    status=OperationStatus.running,
                    payload=operation_payload,
                )
                await repo.add_event(
                    ws,
                    event_type=_MONITOR_RECOVERY_EVENT_TYPE,
                    reason_code=_MONITOR_RECOVERY_REASON_CODE,
                    payload={
                        **operation_payload,
                        "operation_id": operation.id,
                    },
                )
                self._monitor_recovery_operation_ids[workspace_id] = operation.id
            await session.commit()
            return claimed

    async def _safely_resume_claimed_pr_monitor(
        self,
        workspace_id: str,
        *,
        recovery_operation_id: str | None = None,
    ) -> None:
        heartbeat = asyncio.create_task(
            self._refresh_monitoring_pr_claim_loop(workspace_id),
            name=f"awf-monitor-claim-{workspace_id}",
        )
        try:
            await self._safely_resume_pr_monitor(
                workspace_id,
                recovery_operation_id=recovery_operation_id,
            )
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            await self._release_monitoring_pr_claim(workspace_id)
            self._monitor_recovery_operation_ids.pop(workspace_id, None)

    async def _finish_monitor_recovery_operation(
        self,
        workspace_id: str,
        *,
        operation_id: str | None,
        status: OperationStatus,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if operation_id is None:
            return
        try:
            async with self._session_factory() as session:
                operation_repo = OperationRepository(session)
                operation = await operation_repo.get(operation_id)
                if operation is None or operation.workspace_id != workspace_id:
                    return
                ws = await WorkspaceRepository(session).get(workspace_id)
                result: dict[str, Any] = {
                    "requested_action": OperationType.remonitor.value,
                    "worker_id": self._worker_id,
                }
                if ws is not None:
                    result.update(
                        {
                            "status": ws.status,
                            "pr_url": ws.pr_url,
                            "pr_number": ws.pr_number,
                        }
                    )
                await operation_repo.finish(
                    operation,
                    status=status,
                    result=result,
                    error_code=error_code,
                    error_message=error_message,
                )
                await session.commit()
        except Exception:
            _log.exception(
                "worker.monitor_recovery_operation_finish_failed",
                workspace_id=workspace_id,
                operation_id=operation_id,
                status=status.value,
            )

    async def _refresh_monitoring_pr_claim_loop(self, workspace_id: str) -> None:
        interval = max(1.0, min(60.0, self._config.monitor_claim_lease_seconds / 3))
        while True:
            await asyncio.sleep(interval)
            try:
                refreshed = await self._refresh_monitoring_pr_claim(workspace_id)
            except Exception:
                _log.exception(
                    "worker.monitor_claim_refresh_failed",
                    workspace_id=workspace_id,
                    worker_id=self._worker_id,
                )
                return
            if not refreshed:
                _log.warning(
                    "worker.monitor_claim_lost",
                    workspace_id=workspace_id,
                    worker_id=self._worker_id,
                )
                return

    async def _refresh_execution_claim_loop(self, workspace_id: str) -> None:
        interval = max(1.0, min(60.0, self._config.execution_claim_lease_seconds / 3))
        while True:
            await asyncio.sleep(interval)
            try:
                refreshed = await self._refresh_execution_claim(workspace_id)
            except Exception:
                _log.exception(
                    "worker.execution_claim_refresh_failed",
                    workspace_id=workspace_id,
                    worker_id=self._worker_id,
                )
                return
            if not refreshed:
                _log.warning(
                    "worker.execution_claim_lost",
                    workspace_id=workspace_id,
                    worker_id=self._worker_id,
                )
                return

    async def _refresh_monitoring_pr_claim(self, workspace_id: str) -> bool:
        async def _operation(session: AsyncSession) -> bool:
            lease_expires_at = datetime.now(UTC) + timedelta(
                seconds=self._config.monitor_claim_lease_seconds
            )
            return await WorkspaceRepository(session).refresh_monitoring_pr_claim(
                workspace_id,
                owner_id=self._worker_id,
                lease_expires_at=lease_expires_at,
            )

        return await run_db_operation_with_retry(
            self._session_factory,
            _operation,
            commit=True,
            retry_commit_failures=True,
            on_retry=self._log_transient_db_retry,
        )

    def _execution_claim_expires_at(self) -> datetime:
        return datetime.now(UTC) + timedelta(seconds=self._config.execution_claim_lease_seconds)

    async def _refresh_execution_claim(self, workspace_id: str) -> bool:
        async def _operation(session: AsyncSession) -> bool:
            lease_expires_at = self._execution_claim_expires_at()
            return await WorkspaceRepository(session).refresh_execution_claim(
                workspace_id,
                owner_id=self._worker_id,
                lease_expires_at=lease_expires_at,
            )

        return await run_db_operation_with_retry(
            self._session_factory,
            _operation,
            commit=True,
            retry_commit_failures=True,
            on_retry=self._log_transient_db_retry,
        )

    async def _release_execution_claim(self, workspace_id: str) -> None:
        try:
            async with self._session_factory() as session:
                await WorkspaceRepository(session).release_execution_claim(
                    workspace_id,
                    owner_id=self._worker_id,
                )
                await session.commit()
        except Exception:
            _log.exception(
                "worker.execution_claim_release_failed",
                workspace_id=workspace_id,
                worker_id=self._worker_id,
            )

    async def _release_monitoring_pr_claim(self, workspace_id: str) -> None:
        try:
            async with self._session_factory() as session:
                await WorkspaceRepository(session).release_monitoring_pr_claim(
                    workspace_id,
                    owner_id=self._worker_id,
                )
                await session.commit()
        except Exception:
            _log.exception(
                "worker.monitor_claim_release_failed",
                workspace_id=workspace_id,
                worker_id=self._worker_id,
            )


def _stale_execution_claim_filter(claim_cutoff: datetime) -> Any:
    return or_(
        Workspace.execution_claimed_by.is_(None),
        Workspace.execution_claim_expires_at.is_(None),
        Workspace.execution_claim_expires_at <= claim_cutoff,
    )


def _stale_monitor_claim_filter(claim_cutoff: datetime) -> Any:
    return or_(
        Workspace.monitor_claimed_by.is_(None),
        Workspace.monitor_claim_expires_at.is_(None),
        Workspace.monitor_claim_expires_at <= claim_cutoff,
    )


async def _monitor_provider_recovery_resume_pending(
    session_factory: async_sessionmaker[AsyncSession],
    candidate: _ActiveExecutionCandidate,
) -> bool:
    if candidate.status != WorkspaceStatus.monitoring_pr:
        return False
    task_policy = candidate.task_policy if isinstance(candidate.task_policy, Mapping) else {}
    raw_state = task_policy.get(PROVIDER_RECOVERY_STATE_KEY)
    if not isinstance(raw_state, Mapping):
        return False
    action = raw_state.get("action")
    if action == "fallback":
        return True
    if action != "retry":
        return False

    not_before = provider_cooldown_not_before(candidate.task_policy)
    now = datetime.now(UTC)
    if not_before is not None and not_before > now:
        return True

    if candidate.agent is None:
        return False

    model = agent_model_from_task_policy(candidate.task_policy)
    provider = provider_for_agent_model(candidate.agent, model)
    if provider is None or model is None:
        return False

    async with session_factory() as session:
        breaker = await ProviderModelCircuitBreakerRepository(session).open_breaker(
            provider=provider,
            model=model,
            now=now,
        )
        return breaker is not None


def _claim_recheck_conditions(
    status: WorkspaceStatus,
    claim_cutoff: datetime,
) -> tuple[Any, ...]:
    if status in _ACTIVE_EXECUTION_STATUSES:
        return (_stale_execution_claim_filter(claim_cutoff),)
    if status == WorkspaceStatus.monitoring_pr:
        return (_stale_monitor_claim_filter(claim_cutoff),)
    return ()


async def _existing_ordered_queue_decision_keys(
    session: AsyncSession,
    candidates: list[_OrderedDecisionCandidate],
    *,
    reason_code: str,
    decided_at: datetime,
) -> set[_OrderedDecisionKey]:
    candidate_keys = {_ordered_queue_decision_key(candidate) for candidate in candidates}
    if not candidate_keys:
        return set()

    stmt = select(
        QueueDecision.workspace_id,
        QueueDecision.task_id,
        QueueDecision.attempt_id,
    ).where(
        QueueDecision.workspace_id.in_(
            sorted({candidate.workspace_id for candidate in candidates})
        ),
        QueueDecision.decision == QUEUE_DECISION_ORDERED,
        QueueDecision.reason_code == reason_code,
        QueueDecision.decided_at == decided_at,
    )
    rows = (await session.execute(stmt)).all()
    return {
        (workspace_id, task_id, attempt_id)
        for workspace_id, task_id, attempt_id in rows
        if (workspace_id, task_id, attempt_id) in candidate_keys
    }


def _ordered_queue_decision_key(
    candidate: _OrderedDecisionCandidate,
) -> _OrderedDecisionKey:
    return (candidate.workspace_id, candidate.task_id, candidate.attempt_id)


def _ordered_queue_decision_create(
    candidate: _OrderedDecisionCandidate,
    *,
    latest: QueueDecision | None,
    reason_code: str,
    decided_at: datetime,
) -> QueueDecisionCreate:
    score = scheduler_score_from_workspace(candidate, now=decided_at)
    return QueueDecisionCreate(
        workspace_id=candidate.workspace_id,
        task_id=candidate.task_id,
        attempt_id=candidate.attempt_id,
        decision=QUEUE_DECISION_ORDERED,
        reason_code=reason_code,
        class_priority=score.class_priority,
        computed_priority=score.effective_score,
        age_boost=score.age_boost,
        retry_bonus=score.retry_bonus,
        resource_summary=dict(latest.resource_summary) if latest else {},
        overlap_risk_summary=dict(latest.overlap_risk_summary) if latest else {},
        score_summary=score.score_summary,
        decided_at=decided_at,
    )


def _ordered_queue_decision_matches(
    decision: QueueDecision | None,
    candidate: _OrderedDecisionCandidate,
    *,
    reason_code: str,
    decided_at: datetime,
) -> bool:
    if decision is None:
        return False
    return (
        decision.workspace_id == candidate.workspace_id
        and decision.task_id == candidate.task_id
        and decision.attempt_id == candidate.attempt_id
        and decision.decision == QUEUE_DECISION_ORDERED
        and decision.reason_code == reason_code
        and _utc_datetime(decision.decided_at) == _utc_datetime(decided_at)
    )


async def _record_scheduler_queue_decision(
    session: AsyncSession,
    workspace: Workspace,
    *,
    decision: str,
    reason_code: str,
    decided_at: datetime,
    suppression_detail: dict[str, Any] | None = None,
) -> None:
    attempt = await TaskAttemptRepository(session).get_by_workspace_id(workspace.id)
    if attempt is None:
        return

    queue_repo = QueueDecisionRepository(session)
    latest = await queue_repo.list_for_workspace(workspace.id, limit=1)
    score = scheduler_score_from_workspace(workspace, now=decided_at)
    score_summary = (
        score_summary_with_suppression(
            score,
            reason_code=reason_code,
            detail=suppression_detail,
        )
        if suppression_detail is not None
        else score.score_summary
    )
    await queue_repo.create(
        workspace_id=workspace.id,
        task_id=attempt.task_id,
        attempt_id=attempt.id,
        decision=decision,
        reason_code=reason_code,
        class_priority=score.class_priority,
        computed_priority=score.effective_score,
        age_boost=score.age_boost,
        retry_bonus=score.retry_bonus,
        resource_summary=dict(latest[0].resource_summary) if latest else {},
        overlap_risk_summary=dict(latest[0].overlap_risk_summary) if latest else {},
        score_summary=score_summary,
        decided_at=decided_at,
    )


async def _record_capacity_queue_decision(
    session: AsyncSession,
    workspace: Workspace,
    *,
    decision: str,
    reason_code: str,
    decided_at: datetime,
    allocated: _AllocatedReservationTotals,
    demand: _ReservationDemand,
    blockers: list[LocalCapacityBlocker],
) -> None:
    attempt = await TaskAttemptRepository(session).get_by_workspace_id(workspace.id)
    if attempt is None:
        return

    queue_repo = QueueDecisionRepository(session)
    latest = await queue_repo.list_for_workspace(workspace.id, limit=1)
    latest_decision = latest[0] if latest else None
    if decision == QUEUE_DECISION_DEFERRED and _capacity_deferred_decision_matches(
        latest_decision,
        attempt_id=attempt.id,
        reason_code=reason_code,
        blockers=blockers,
    ):
        return

    score = scheduler_score_from_workspace(workspace, now=decided_at)
    score_summary = (
        score_summary_with_suppression(
            score,
            reason_code=reason_code,
            detail={
                "blockers": [_capacity_blocker_payload(blocker) for blocker in blockers],
            },
        )
        if decision == QUEUE_DECISION_DEFERRED
        else score.score_summary
    )
    resource_summary = _capacity_resource_summary(
        allocated=allocated,
        demand=demand,
        blockers=blockers,
    )
    if latest_decision is not None:
        resource_summary["previous"] = dict(latest_decision.resource_summary)
    await queue_repo.create(
        workspace_id=workspace.id,
        task_id=attempt.task_id,
        attempt_id=attempt.id,
        decision=decision,
        reason_code=reason_code,
        class_priority=score.class_priority,
        computed_priority=score.effective_score,
        age_boost=score.age_boost,
        retry_bonus=score.retry_bonus,
        resource_summary=resource_summary,
        overlap_risk_summary=(
            dict(latest_decision.overlap_risk_summary) if latest_decision is not None else {}
        ),
        score_summary=score_summary,
        decided_at=decided_at,
    )


def _capacity_deferred_decision_matches(
    decision: QueueDecision | None,
    *,
    attempt_id: str,
    reason_code: str,
    blockers: list[LocalCapacityBlocker],
) -> bool:
    if decision is None:
        return False
    if (
        decision.attempt_id != attempt_id
        or decision.decision != QUEUE_DECISION_DEFERRED
        or decision.reason_code != reason_code
    ):
        return False
    return _capacity_blocker_signatures_from_summary(
        decision.resource_summary
    ) == _capacity_blocker_signatures(blockers)


def _capacity_blocker_signatures(
    blockers: list[LocalCapacityBlocker],
) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        _capacity_blocker_payload_signature(_capacity_blocker_payload(blocker))
        for blocker in blockers
    )


def _capacity_blocker_signatures_from_summary(
    resource_summary: Mapping[str, Any],
) -> tuple[tuple[Any, ...], ...] | None:
    blockers = resource_summary.get("blockers")
    if not isinstance(blockers, list):
        return None
    signatures: list[tuple[Any, ...]] = []
    for blocker in blockers:
        if not isinstance(blocker, Mapping):
            return None
        signatures.append(_capacity_blocker_payload_signature(blocker))
    return tuple(signatures)


def _capacity_blocker_payload_signature(
    payload: Mapping[str, Any],
) -> tuple[Any, ...]:
    return tuple(payload.get(field) for field in _CAPACITY_BLOCKER_SIGNATURE_FIELDS)


async def _allocated_totals_for_capacity_gate(
    session: AsyncSession,
    *,
    reservation_repo: ResourceReservationRepository,
    config: WorkerConfig,
) -> _AllocatedReservationTotals:
    node_id = config.node_id or "local"
    allocated = _allocated_totals_from_repository(
        await reservation_repo.active_latest_totals(
            statuses=ALLOCATED_RESOURCE_RESERVATION_STATUSES,
            node_id=node_id,
        )
    )
    await _add_unreserved_active_workspace_defaults(
        session,
        allocated=allocated,
        config=config,
        node_id=node_id,
    )
    return allocated


async def _add_unreserved_active_workspace_defaults(
    session: AsyncSession,
    *,
    allocated: _AllocatedReservationTotals,
    config: WorkerConfig,
    node_id: str,
) -> None:
    active_reservation_exists = (
        select(ResourceReservation.id)
        .where(
            ResourceReservation.workspace_id == Workspace.id,
            ResourceReservation.released_at.is_(None),
        )
        .exists()
    )
    stmt = select(Workspace.id, Workspace.resolved_profile).where(
        Workspace.status.in_(ALLOCATED_RESOURCE_RESERVATION_STATUSES),
        or_(Workspace.node_id == node_id, Workspace.node_id.is_(None)),
        ~active_reservation_exists,
    )
    for workspace_id, resolved_profile in await session.execute(stmt):
        allocated.add(
            _default_reservation_demand_for_workspace(
                workspace_id,
                resolved_profile=resolved_profile,
                config=config,
            )
        )


def _allocated_totals_from_repository(
    totals: Mapping[str, float | int],
) -> _AllocatedReservationTotals:
    return _AllocatedReservationTotals(
        workspace_count=int(totals.get("workspace_count", 0) or 0),
        steady_cpu=float(totals.get("steady_cpu", 0.0) or 0.0),
        steady_memory_gb=float(totals.get("steady_memory_gb", 0.0) or 0.0),
        peak_cpu=float(totals.get("peak_cpu", 0.0) or 0.0),
        peak_memory_gb=float(totals.get("peak_memory_gb", 0.0) or 0.0),
        disk_mb=int(totals.get("disk_mb", 0) or 0),
        dind_slots=int(totals.get("dind_slots", 0) or 0),
    )


def _reservation_demand_for_workspace(
    workspace: Workspace,
    *,
    reservation: ResourceReservation | None,
    config: WorkerConfig,
) -> _ReservationDemand:
    if reservation is not None:
        return _ReservationDemand(
            workspace_id=workspace.id,
            steady_cpu=reservation.steady_cpu,
            steady_memory_gb=reservation.steady_memory_gb,
            peak_cpu=reservation.peak_cpu,
            peak_memory_gb=reservation.peak_memory_gb,
            disk_mb=int(reservation.disk_mb or 0),
            dind_slots=int(reservation.dind_slots or 0),
        )
    return _default_reservation_demand_for_workspace(
        workspace.id,
        resolved_profile=workspace.resolved_profile,
        config=config,
    )


def _default_reservation_demand_for_workspace(
    workspace_id: str,
    *,
    resolved_profile: object,
    config: WorkerConfig,
) -> _ReservationDemand:
    return _ReservationDemand(
        workspace_id=workspace_id,
        steady_cpu=config.workspace_steady_cpu,
        steady_memory_gb=config.workspace_steady_memory_gb,
        peak_cpu=config.workspace_peak_cpu,
        peak_memory_gb=config.workspace_peak_memory_gb,
        disk_mb=0,
        dind_slots=_default_dind_slots_from_profile(resolved_profile),
        defaulted=True,
    )


def _default_dind_slots_from_profile(profile: object) -> int:
    if not isinstance(profile, Mapping):
        return 0
    docker = profile.get("docker")
    if isinstance(docker, Mapping) and docker.get("mode") == "dind":
        return 1
    return 0


def _local_capacity_blockers(
    *,
    allocated: _AllocatedReservationTotals,
    demand: _ReservationDemand,
    config: WorkerConfig,
) -> list[LocalCapacityBlocker]:
    blockers: list[LocalCapacityBlocker] = []
    for constraint in LOCAL_CAPACITY_CONSTRAINTS:
        blocker = local_capacity_blocker(
            constraint=constraint,
            limit=local_capacity_limit(
                constraint,
                cpu_limit=config.local_capacity_cpu_cores,
                memory_limit=config.local_capacity_memory_gb,
                dind_slots=config.local_capacity_dind_slots,
            ),
            allocated=getattr(allocated, constraint.dimension),
            requested=getattr(demand, constraint.dimension),
        )
        if blocker is not None:
            blockers.append(blocker)
    return blockers


def _local_capacity_configured(config: WorkerConfig) -> bool:
    return (
        config.local_capacity_cpu_cores is not None
        or config.local_capacity_memory_gb is not None
        or config.local_capacity_dind_slots is not None
    )


def _capacity_resource_summary(
    *,
    allocated: _AllocatedReservationTotals,
    demand: _ReservationDemand,
    blockers: list[LocalCapacityBlocker],
) -> dict[str, Any]:
    return {
        "allocated": {
            "workspace_count": allocated.workspace_count,
            "steady_cpu": allocated.steady_cpu,
            "steady_memory_gb": allocated.steady_memory_gb,
            "peak_cpu": allocated.peak_cpu,
            "peak_memory_gb": allocated.peak_memory_gb,
            "disk_mb": allocated.disk_mb,
            "dind_slots": allocated.dind_slots,
        },
        "requested": {
            "workspace_id": demand.workspace_id,
            "steady_cpu": demand.steady_cpu,
            "steady_memory_gb": demand.steady_memory_gb,
            "peak_cpu": demand.peak_cpu,
            "peak_memory_gb": demand.peak_memory_gb,
            "disk_mb": demand.disk_mb,
            "dind_slots": demand.dind_slots,
            "defaulted": demand.defaulted,
        },
        "blockers": [_capacity_blocker_payload(blocker) for blocker in blockers],
    }


def _capacity_blocker_payload(blocker: LocalCapacityBlocker) -> dict[str, Any]:
    return {
        "dimension": blocker.dimension,
        "reason_code": blocker.reason_code,
        "limit": blocker.limit,
        "allocated": blocker.allocated,
        "requested": blocker.requested,
        "after": blocker.after,
        "unsatisfiable": blocker.unsatisfiable,
    }


async def _acquire_local_capacity_scheduler_lock(
    session: AsyncSession,
    *,
    node_id: str,
) -> None:
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    lock_key = _postgres_advisory_lock_key(f"awf:local-capacity:{node_id}")
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": lock_key},
    )


def _postgres_advisory_lock_key(value: str) -> int:
    raw = int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:8], "big")
    if raw >= 2**63:
        return raw - 2**64
    return raw


def _order_scheduler_workspaces(
    workspaces: list[Workspace],
    *,
    now: datetime | None = None,
) -> list[Workspace]:
    scoring_at = now or datetime.now(UTC)
    scored = sorted(
        (
            (scheduler_score_from_workspace(workspace, now=scoring_at), workspace)
            for workspace in workspaces
        ),
        key=lambda item: scheduler_order_key(item[0]),
    )
    return [workspace for _score, workspace in scored]


def _scheduler_candidate_fetch_limit(limit: int) -> int:
    """Return the candidate batch size for eligibility refill scans.

    Small slot counts get a proportional batch so a single suppressed workspace
    does not force repeated single-row fetches. Typical scheduler limits get a
    fixed 16-row cushion, with widened batches capped at 250 while still
    honoring larger requested limits.
    """
    if limit <= 0:
        return 0
    proportional_fetch_limit = limit * 4
    fixed_cushion_fetch_limit = limit + 16
    widened_fetch_limit = min(
        250,
        fixed_cushion_fetch_limit,
        proportional_fetch_limit,
    )
    return max(limit, widened_fetch_limit)


def _scheduler_candidate_cursor(
    workspaces: list[Workspace],
    *,
    scoring_at: datetime,
    dialect_name: str | None = None,
) -> SchedulerOrderCursor | None:
    if not workspaces:
        return None
    last = workspaces[-1]
    score = scheduler_score_from_workspace(last, now=scoring_at)
    effective_score = score.effective_score
    if dialect_name not in SCHEDULER_SQL_AGE_BOOST_DIALECTS:
        effective_score -= score.age_boost
    return SchedulerOrderCursor(
        class_priority=score.class_priority,
        effective_score=effective_score,
        queued_at=score.queued_at,
        workspace_id=last.id,
        scoring_at=scoring_at,
    )


def _scheduler_items_are_workspace_ids(
    workspaces: list[Workspace] | list[str],
) -> TypeGuard[list[str]]:
    return bool(workspaces) and all(isinstance(item, str) for item in workspaces)


def _scheduler_items_are_workspaces(
    workspaces: list[Workspace] | list[str],
) -> TypeGuard[list[Workspace]]:
    return bool(workspaces) and all(isinstance(item, Workspace) for item in workspaces)


def _worker_exception_is_transient_db_connection(exc: BaseException) -> bool:
    if not is_transient_closed_connection_error(
        exc,
        include_unsuppressed_context=True,
    ):
        return False
    return _exception_chain_has_sqlalchemy_error(exc)


def _exception_chain_has_sqlalchemy_error(exc: BaseException) -> bool:
    stack: list[BaseException] = [exc]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, SQLAlchemyError):
            return True
        if isinstance(current, BaseExceptionGroup):
            stack.extend(current.exceptions)
        if current.__cause__ is not None:
            stack.append(current.__cause__)
        if not current.__suppress_context__ and current.__context__ is not None:
            stack.append(current.__context__)
    return False


def _workspace_claim_recheck_passes(
    workspace: Workspace,
    status: WorkspaceStatus,
    claim_cutoff: datetime,
) -> bool:
    if status in _ACTIVE_EXECUTION_STATUSES:
        return _execution_claim_is_stale(workspace, claim_cutoff)
    if status == WorkspaceStatus.monitoring_pr:
        return _monitor_claim_is_stale(workspace, claim_cutoff)
    return True


def _execution_claim_is_stale(workspace: Workspace, claim_cutoff: datetime) -> bool:
    if workspace.execution_claimed_by is None or workspace.execution_claim_expires_at is None:
        return True

    expires_at = workspace.execution_claim_expires_at
    if expires_at.tzinfo is None and claim_cutoff.tzinfo is not None:
        claim_cutoff = claim_cutoff.replace(tzinfo=None)
    return expires_at <= claim_cutoff


def _monitor_claim_is_stale(workspace: Workspace, claim_cutoff: datetime) -> bool:
    if workspace.monitor_claimed_by is None or workspace.monitor_claim_expires_at is None:
        return True

    expires_at = workspace.monitor_claim_expires_at
    if expires_at.tzinfo is None and claim_cutoff.tzinfo is not None:
        claim_cutoff = claim_cutoff.replace(tzinfo=None)
    return expires_at <= claim_cutoff


def _candidate_claim_is_stale(
    workspace: Workspace,
    status: WorkspaceStatus,
    claim_cutoff: datetime,
) -> bool:
    if status in _ACTIVE_EXECUTION_STATUSES:
        return _execution_claim_is_stale(workspace, claim_cutoff)
    if status == WorkspaceStatus.monitoring_pr:
        return _monitor_claim_is_stale(workspace, claim_cutoff)
    return True


def _runtime_workspace(candidate: _ActiveExecutionCandidate) -> RuntimeWorkspace:
    return RuntimeWorkspace(
        workspace_id=candidate.workspace_id,
        status=candidate.status.value,
        compose_project_name=candidate.compose_project_name,
        compose_file_path=candidate.compose_file_path,
        pr_url=candidate.pr_url,
        retry_policy_allows_recovery=retry_policy_allows_runtime_recovery(candidate.task_policy),
    )


def _has_running_agent_runtime(snapshot: RuntimeSnapshot) -> bool:
    if snapshot.stack_state != "running":
        return False
    return any(
        service.name.lower() == "agent" and service.state.lower() == "running"
        for service in snapshot.services
    )


def _workspace_claim_snapshot(workspace: Workspace) -> dict[str, str | None]:
    return {
        "monitor_claimed_by": workspace.monitor_claimed_by,
        "monitor_claim_expires_at": _json_datetime(workspace.monitor_claim_expires_at),
        "execution_claimed_by": workspace.execution_claimed_by,
        "execution_claim_expires_at": _json_datetime(workspace.execution_claim_expires_at),
    }


def _active_execution_preservation_claim_cleanup_payload(
    workspace: Workspace,
    *,
    claim_cutoff: datetime,
) -> dict[str, str | None]:
    previous_claimed_by = workspace.execution_claimed_by
    previous_expires_at = _json_datetime(workspace.execution_claim_expires_at)
    payload = {
        "action": "none",
        "reason_code": _ACTIVE_EXECUTION_PRESERVED_NO_CLAIM_REASON_CODE,
        "previous_claimed_by": previous_claimed_by,
        "previous_expires_at": previous_expires_at,
    }
    if previous_claimed_by is None and workspace.execution_claim_expires_at is None:
        return payload

    if not _execution_claim_is_stale(workspace, claim_cutoff):
        return {
            **payload,
            "action": "preserved_unexpired",
            "reason_code": _ACTIVE_EXECUTION_PRESERVED_UNEXPIRED_CLAIM_PRESERVED_REASON_CODE,
        }

    return {
        **payload,
        "action": "cleared_stale",
        "reason_code": _ACTIVE_EXECUTION_PRESERVED_CLAIM_CLEARED_REASON_CODE,
    }


def _monitor_recovery_claim_cleanup_payload(
    workspace: Workspace,
    *,
    claim_cutoff: datetime,
    monitor_claimed_by: str,
    monitor_claim_expires_at: datetime,
    execution_claim_cleanup: dict[str, str | None] | None = None,
) -> dict[str, dict[str, str | None]]:
    if execution_claim_cleanup is None:
        execution_claim_cleanup = _monitor_recovery_execution_claim_cleanup_payload(
            workspace,
            claim_cutoff=claim_cutoff,
        )
    return {
        "execution_claim": execution_claim_cleanup,
        "monitor_claim": {
            "action": "acquired",
            "reason_code": _MONITOR_RECOVERY_MONITOR_CLAIM_ACQUIRED_REASON_CODE,
            "claimed_by": monitor_claimed_by,
            "expires_at": _json_datetime(monitor_claim_expires_at),
        },
    }


def _monitor_recovery_execution_claim_cleanup_payload(
    workspace: Workspace,
    *,
    claim_cutoff: datetime,
) -> dict[str, str | None]:
    previous_claimed_by = workspace.execution_claimed_by
    previous_expires_at = _json_datetime(workspace.execution_claim_expires_at)
    payload = {
        "action": "none",
        "reason_code": _MONITOR_RECOVERY_NO_EXECUTION_CLAIM_REASON_CODE,
        "previous_claimed_by": previous_claimed_by,
        "previous_expires_at": previous_expires_at,
    }
    if previous_claimed_by is None:
        return payload

    if _execution_claim_is_stale(workspace, claim_cutoff):
        return {
            **payload,
            "action": "cleared_stale",
            "reason_code": _MONITOR_RECOVERY_EXECUTION_CLAIM_CLEARED_REASON_CODE,
        }

    return {
        **payload,
        "action": "preserved_unexpired",
        "reason_code": _MONITOR_RECOVERY_EXECUTION_CLAIM_PRESERVED_REASON_CODE,
    }


def _latest_runtime_stranding_reason(events: list[WorkspaceEvent]) -> str | None:
    for event in reversed(events):
        if event.event_type == RUNTIME_STRANDED_EVENT_TYPE:
            return event.reason_code
    return None


def _monitor_recovery_payload(
    workspace: Workspace,
    *,
    worker_id: str,
    previous_claim: dict[str, str | None],
    claim_cleanup: dict[str, dict[str, str | None]],
    runtime_stranding_reason: str | None,
) -> dict[str, Any]:
    return {
        "owner": _MONITOR_RECOVERY_OWNER,
        "source": _MONITOR_RECOVERY_SOURCE,
        "requested_action": OperationType.remonitor.value,
        "reason": (
            "Worker claimed a persisted monitoring_pr workspace with an already-open "
            "pull request after service restart."
        ),
        "reason_code": _MONITOR_RECOVERY_REASON_CODE,
        "pr_url": workspace.pr_url,
        "pr_number": workspace.pr_number,
        "worker_id": worker_id,
        "previous_claim": previous_claim,
        "claim_cleanup": claim_cleanup,
        "runtime_stranding_reason": runtime_stranding_reason,
        "monitor_state": {
            "monitor_started_at": _json_datetime(workspace.monitor_started_at),
            "monitor_iter_count": workspace.monitor_iter_count,
            "monitor_threads_addressed_count": len(workspace.monitor_threads_addressed or {}),
            "monitor_last_commit_sha": workspace.monitor_last_commit_sha,
        },
    }


def _json_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _runtime_snapshot_payload(snapshot: RuntimeSnapshot) -> dict[str, Any]:
    return {
        "stack_state": snapshot.stack_state,
        "reason": snapshot.reason,
        "services": [
            {
                "name": service.name,
                "container_id": service.container_id,
                "image": service.image,
                "state": service.state,
                "status": service.status,
                "health": service.health,
                "ports": list(service.ports),
                "started_at": service.started_at,
            }
            for service in snapshot.services
        ],
    }


def _active_execution_preservation_payload(
    candidate: _ActiveExecutionCandidate,
    snapshot: RuntimeSnapshot,
    *,
    worker_id: str,
    previous_claim: dict[str, str | None],
    claim_cleanup: dict[str, str | None],
) -> dict[str, Any]:
    message = (
        "Worker restart found a persisted active execution with a live running "
        "agent runtime. AWF preserved the runtime for explicit operator recovery "
        "instead of starting a duplicate execution or stopping the compose stack."
    )
    return {
        "owner": _ACTIVE_EXECUTION_PRESERVED_OWNER,
        "source": _ACTIVE_EXECUTION_PRESERVED_SOURCE,
        "requested_action": OperationType.refresh.value,
        "reason": message,
        "message": message,
        "reason_code": ACTIVE_EXECUTION_PRESERVED_REASON_CODE,
        "decision": "preserve_runtime",
        "workspace_status": candidate.status.value,
        "subphase": _ACTIVE_EXECUTION_PRESERVED_SUBPHASE,
        "compose_project_name": candidate.compose_project_name,
        "compose_file_path": candidate.compose_file_path,
        "worker_id": worker_id,
        "previous_claim": previous_claim,
        "claim_cleanup": claim_cleanup,
        "runtime": _runtime_snapshot_payload(snapshot),
    }


def _runtime_stranding_event_payload(
    candidate: _ActiveExecutionCandidate,
    snapshot: RuntimeSnapshot,
    finding: WorkspaceRuntimeFinding,
) -> dict[str, Any]:
    return {
        "compose_project_name": candidate.compose_project_name,
        "workspace_status": candidate.status.value,
        "reason_code": finding.reason_code,
        "decision": finding.decision,
        "message": finding.message,
        "runtime": _runtime_snapshot_payload(snapshot),
    }


def _secondary_runtime_stranding_payload(
    candidate: _ActiveExecutionCandidate,
    snapshot: RuntimeSnapshot,
    finding: WorkspaceRuntimeFinding,
    *,
    message: str,
) -> dict[str, Any]:
    payload = _runtime_stranding_event_payload(candidate, snapshot, finding)
    payload["failure_reason"] = FailureReason.infrastructure_failure.value
    payload["message"] = message
    return payload


def _runtime_stranding_failure_message(
    candidate: _ActiveExecutionCandidate,
    finding: WorkspaceRuntimeFinding,
) -> str:
    return (
        f"{finding.reason_code}: {finding.message} "
        "An active execution was lost after a service or Docker restart. "
        f"The workspace is still marked {candidate.status.value!r}, but AWF detected "
        "that its managed runtime is stranded. AWF marked the workspace failed without "
        "cleanup; logs, the worktree, compose files, volumes, and surviving files were "
        "preserved for inspection. Inspect the workspace, then retry or redispatch the "
        "task when ready."
    )


def _secondary_stale_active_execution_payload(
    candidate: _ActiveExecutionCandidate,
    snapshot: RuntimeSnapshot,
    *,
    message: str,
) -> dict[str, Any]:
    return {
        "failure_reason": FailureReason.infrastructure_failure.value,
        "reason_code": _STALE_ACTIVE_EXECUTION_REASON_CODE,
        "message": message,
        "compose_project_name": candidate.compose_project_name,
        "workspace_status": candidate.status.value,
        "runtime": _runtime_snapshot_payload(snapshot),
    }


def _stale_active_execution_failure_message(
    candidate: _ActiveExecutionCandidate,
    snapshot: RuntimeSnapshot,
) -> str:
    if not candidate.compose_project_name:
        runtime_detail = "no compose project is persisted for the workspace"
    else:
        runtime_detail = f"compose runtime state is {snapshot.stack_state}"
        if snapshot.reason:
            runtime_detail = f"{runtime_detail}: {snapshot.reason.strip()}"

    return (
        "active execution was lost after a service or Docker restart. "
        f"The workspace is still marked {candidate.status.value!r}, but this worker has "
        f"no in-process execution task and {runtime_detail}. "
        "AWF stopped the stale runtime before marking the workspace failed; logs, the "
        "worktree, and retained evidence were preserved for inspection. Inspect the "
        "workspace, then redispatch the task from a clean base when ready."
    )
