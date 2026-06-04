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
import uuid
from collections.abc import Awaitable, Callable
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker.admission import _requested_admission_row_slots
from awf.control.worker.config import WorkerConfig, effective_worker_config_node_id
from awf.control.worker.constants import (
    ORDERED_MONITOR_RESUME_REASON,
    ORDERED_READY_EXECUTION_REASON,
    ORDERED_REQUESTED_PROVISIONING_REASON,
)
from awf.control.worker.logging import _log
from awf.control.worker.mixins import WorkerDelegatesMixin  # noqa: E402
from awf.control.worker.protocols import (
    BranchOpenPullRequestResolverProtocol,
    ProvisionerProtocol,
    RuntimeCleanerProtocol,
    RuntimeInspectorProtocol,
    WorkspaceExecutorProtocol,
)
from awf.control.worker.resource_broker import (
    _local_capacity_configured,
)
from awf.control.worker.types import (
    _AllocatedReservationSignature,
    _ExecutionTaskKind,
    _RequestedCapacityQueueSignature,
)
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.resilience import (
    DB_CONNECTION_TRANSIENT_ATTEMPT_REASON,
    run_db_operation_with_retry,
)
from awf.runtime.inspection import RuntimeInspector
from awf.service.scheduler import SchedulerOrderCursor

if TYPE_CHECKING:
    from awf.service.gc_reconcile import OrphanDirReconcileResult
    from awf.service.orphan_resources import OrphanReapResult


class ControlWorker(WorkerDelegatesMixin):
    """Reads pending work from the DB and dispatches it to runtime handlers."""

    def __init__(
        self: Any,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        provisioner: ProvisionerProtocol,
        executor: WorkspaceExecutorProtocol | None = None,
        runtime_inspector: RuntimeInspectorProtocol | None = None,
        runtime_cleaner: RuntimeCleanerProtocol | None = None,
        open_pr_resolver: BranchOpenPullRequestResolverProtocol | None = None,
        orphan_dir_reconciler: Callable[[], Awaitable[OrphanDirReconcileResult]] | None = None,
        classified_orphan_reaper: Callable[[], Awaitable[OrphanReapResult]] | None = None,
        config: WorkerConfig,
    ) -> None:
        self._session_factory = session_factory
        self._provisioner = provisioner
        self._executor = executor
        self._runtime_inspector = runtime_inspector or RuntimeInspector()
        self._runtime_cleaner = runtime_cleaner
        self._open_pr_resolver = open_pr_resolver
        self._orphan_dir_reconciler = orphan_dir_reconciler
        self._classified_orphan_reaper = classified_orphan_reaper
        self._config = config
        self._stopped = asyncio.Event()
        self._execution_tasks: dict[str, asyncio.Task[None]] = {}
        self._execution_task_kinds: dict[str, _ExecutionTaskKind] = {}
        self._consecutive_saturated_cycles: int = 0
        self._monitor_recovery_operation_ids: dict[str, str] = {}
        # Session-local advisory state; reset on restart and bounded below.
        self._active_salvage_monitor_recovery_operation_ids: dict[str, None] = {}
        self._active_salvage_monitor_resume_cooldowns: dict[str, float] = {}
        self._worker_id = f"control-worker-{uuid.uuid4().hex}"
        self._next_stale_active_execution_scan_at = 0.0
        self._next_secret_lease_expiration_scan_at = 0.0
        self._next_terminal_runtime_release_scan_at = 0.0
        self._next_orphan_reconcile_scan_at = 0.0
        self._next_classified_orphan_reap_scan_at = 0.0
        self._requested_capacity_resume_after: SchedulerOrderCursor | None = None
        self._requested_capacity_resume_signature: _AllocatedReservationSignature | None = None
        self._requested_capacity_resume_queue_signature: _RequestedCapacityQueueSignature | None = (
            None
        )
        self._requested_capacity_resume_provider_suppression_expires_at: datetime | None = None

    def request_stop(self: Any) -> None:
        """Signal ``run_forever`` to exit after the current batch."""
        self._stopped.set()

    async def _requested_provision_slots(self: Any) -> int:
        provision_limit = int(max(0, self._config.max_concurrent_provisions))
        if provision_limit <= 0:
            return 0
        if self._executor is None:
            return provision_limit
        task_available = int(self._available_execution_slots())
        if task_available <= 0:
            return 0
        row_available = await self._requested_admission_row_slots()
        return int(min(provision_limit, task_available, row_available))

    async def _requested_admission_row_slots(self: Any) -> int:
        async def _operation(session: AsyncSession) -> int:
            return await _requested_admission_row_slots(
                session,
                config=self._config,
            )

        return await run_db_operation_with_retry(
            self._session_factory,
            _operation,
            on_retry=self._log_transient_db_retry,
        )

    async def run_once(self: Any) -> int:
        """List + dispatch requested provisioning and workspace runtime tasks.

        Returns the number of workspaces dispatched. A zero return is a signal
        for ``run_forever`` to sleep; non-zero means we may be throughput-bound
        and should immediately loop again.
        """
        dispatched_ids: set[str] = set()
        execution_dispatched_ids: set[str] = set()

        await self._reconcile_stale_monitor_execution_tasks()

        await self._maybe_expire_due_secret_leases()
        await self._maybe_release_terminal_runtime()
        await self._maybe_reconcile_orphan_dirs()
        await self._maybe_reap_classified_orphans()

        if self._executor is not None:
            # Preserved-active-validation redispatches enqueued during recovery
            # occupy execution slots but never flow through the monitor/ready
            # dispatch paths below, so diff the tracked tasks to count them.
            # Otherwise a recovery-only cycle that fills the last slot looks idle
            # to _update_execution_slot_saturation and falsely ticks saturation.
            tracked_execution_ids_before_recovery = set(self._execution_tasks)
            await self._maybe_recover_stale_active_executions()
            execution_dispatched_ids.update(
                set(self._execution_tasks) - tracked_execution_ids_before_recovery
            )

        requested_provision_slots = await self._requested_provision_slots()
        requested_ids: list[str] = []
        if requested_provision_slots > 0 and _local_capacity_configured(self._config):
            requested_ids = await self._claim_requested_ids(limit=requested_provision_slots)
        elif requested_provision_slots > 0:
            listed_ids = await self._list_requested()
            requested_ids = await self._filter_current_status(
                listed_ids,
                expected=WorkspaceStatus.requested,
                action="provision",
            )
            requested_ids = requested_ids[:requested_provision_slots]
            if requested_ids:
                requested_ids = await self._claim_requested_ids(
                    requested_ids,
                    limit=requested_provision_slots,
                )
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
                execution_dispatched_ids.update(monitor_dispatched)

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
                execution_dispatched_ids.update(ready_dispatched)

        # Saturation reflects execution slots only; provisioning dispatches must
        # not mask a worker whose execution slots are wedged with stuck tasks.
        self._update_execution_slot_saturation(dispatched=len(execution_dispatched_ids))
        return len(dispatched_ids)

    async def wait_for_execution_tasks(self: Any) -> None:
        """Wait for ready execution or monitor-resume tasks started by this worker."""
        while self._execution_tasks:
            tasks = tuple(self._execution_tasks.values())
            # Wait for the FIRST task to finish instead of gather()-ing them all:
            # a cancelled monitor can drain (or stay wedged) indefinitely, and
            # gather() would block on it, so a genuine execution failure would
            # never surface — wait_for_execution_tasks() would hang and once=True
            # runs would exit as if successful. Inspecting tasks as they complete
            # lets a real failure raise immediately while a draining monitor's
            # CancelledError is still tolerated.
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for workspace_id, task in list(self._execution_tasks.items()):
                if task in done:
                    self._execution_tasks.pop(workspace_id, None)
                    self._execution_task_kinds.pop(workspace_id, None)
            for task in done:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    raise exc

    async def run_forever(self: Any) -> None:
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

    async def _log_transient_db_retry(self: Any, exc: BaseException, attempt: int) -> None:
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
        """Return requested candidate IDs for non-capacity claims."""
        return await self._list_by_status(
            WorkspaceStatus.requested,
            limit=self._config.max_concurrent_provisions,
            node_id=effective_worker_config_node_id(self._config),
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
        node_id: str | None = None,
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
                    node_id=node_id,
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
            node_id=node_id,
        )

    def _execution_claim_expires_at(self: Any) -> datetime:
        return datetime.now(UTC) + timedelta(seconds=self._config.execution_claim_lease_seconds)
