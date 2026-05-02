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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from time import monotonic
from typing import Any, Protocol

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.logging import get_logger
from awf.common.workspace_policy import agent_model_from_task_policy
from awf.db.enums import FailureReason, OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent
from awf.db.repositories import (
    OperationRepository,
    ProviderModelCircuitBreakerRepository,
    QueueDecisionRepository,
    TaskAttemptRepository,
    WorkspaceRepository,
)
from awf.node.provisioner import Provisioner
from awf.runtime.inspection import RuntimeInspector, RuntimeSnapshot
from awf.service.provider_recovery import (
    provider_cooldown_not_before,
    provider_for_agent_model,
)
from awf.service.scheduler import (
    scheduler_score_from_workspace,
    score_summary_with_suppression,
)
from awf.service.secret_leases import SecretLeaseService
from awf.service.workspace_runtime_health import (
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
_MONITOR_RECOVERY_REASON_CODE = "MONITOR_RECOVERY_AFTER_RESTART"
_MONITOR_RECOVERY_EVENT_TYPE = "workspace.monitor_recovery_started"
_MONITOR_RECOVERY_SOURCE = "worker_restart"
_MONITOR_RECOVERY_OWNER = "control_worker"
_MONITOR_RECOVERY_EXECUTION_CLAIM_CLEARED_REASON_CODE = (
    "STALE_EXECUTION_CLAIM_CLEARED_DURING_MONITOR_RECOVERY"
)
_MONITOR_RECOVERY_EXECUTION_CLAIM_PRESERVED_REASON_CODE = (
    "UNEXPIRED_EXECUTION_CLAIM_PRESERVED_DURING_MONITOR_RECOVERY"
)
_MONITOR_RECOVERY_NO_EXECUTION_CLAIM_REASON_CODE = (
    "NO_EXECUTION_CLAIM_DURING_MONITOR_RECOVERY"
)
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


@dataclass(frozen=True)
class WorkerConfig:
    poll_interval_seconds: float = 1.0
    max_concurrent_provisions: int = 3
    max_concurrent_executions: int = 3
    monitor_claim_lease_seconds: float = 300.0
    execution_claim_lease_seconds: float = 300.0
    stale_active_execution_scan_interval_seconds: float = 300.0
    secret_lease_expiration_scan_interval_seconds: float = 60.0
    node_id: str | None = None


@dataclass(frozen=True)
class _ActiveExecutionCandidate:
    workspace_id: str
    status: WorkspaceStatus
    compose_project_name: str | None
    compose_file_path: str | None = None
    pr_url: str | None = None
    task_policy: dict[str, Any] | None = None


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


class ControlWorker:
    """Reads pending work from the DB and dispatches it to runtime handlers."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        provisioner: Provisioner,
        executor: WorkspaceExecutorProtocol | None = None,
        runtime_inspector: RuntimeInspectorProtocol | None = None,
        config: WorkerConfig,
    ) -> None:
        self._session_factory = session_factory
        self._provisioner = provisioner
        self._executor = executor
        self._runtime_inspector = runtime_inspector or RuntimeInspector()
        self._config = config
        self._stopped = asyncio.Event()
        self._execution_tasks: dict[str, asyncio.Task[None]] = {}
        self._monitor_recovery_operation_ids: dict[str, str] = {}
        self._worker_id = f"control-worker-{uuid.uuid4().hex}"
        self._next_stale_active_execution_scan_at = 0.0
        self._next_secret_lease_expiration_scan_at = 0.0

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

        if self._executor is not None:
            await self._maybe_recover_stale_active_executions()

        requested_ids = await self._list_requested()
        requested_ids = await self._filter_current_status(
            requested_ids,
            expected=WorkspaceStatus.requested,
            action="provision",
        )
        if requested_ids:
            await self._record_ordered_decisions(
                requested_ids,
                reason_code=ORDERED_REQUESTED_PROVISIONING_REASON,
            )
            await asyncio.gather(
                *(self._safely_provision(ws_id) for ws_id in requested_ids),
                return_exceptions=False,
            )
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
                monitor_dispatched = self._dispatch_monitor_resumes(
                    monitoring_ids,
                    limit=execution_slots,
                )
                await self._record_ordered_decisions(
                    [ws_id for ws_id in monitoring_ids if ws_id in monitor_dispatched],
                    reason_code=ORDERED_MONITOR_RESUME_REASON,
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
                ready_dispatched = self._dispatch_ready_executions(
                    ready_ids,
                    limit=execution_slots,
                )
                await self._record_ordered_decisions(
                    [ws_id for ws_id in ready_ids if ws_id in ready_dispatched],
                    reason_code=ORDERED_READY_EXECUTION_REASON,
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

    async def _list_pending(self) -> list[str]:
        """Backward-compatible alias for the original requested query."""
        return await self._list_requested()

    async def _list_requested(self) -> list[str]:
        """Return up to ``max_concurrent_provisions`` workspace IDs in ``requested``."""
        return await self._list_by_status(
            WorkspaceStatus.requested,
            limit=self._config.max_concurrent_provisions,
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

        async with self._session_factory() as session:
            if status not in {
                WorkspaceStatus.requested,
                WorkspaceStatus.ready,
                WorkspaceStatus.monitoring_pr,
            }:
                ids = await WorkspaceRepository(session).list_schedulable_ids(
                    status=status,
                    limit=limit,
                    exclude_ids=exclude_ids,
                )
                return ids[:limit]

            filtered: list[str] = []
            seen_ids: set[str] = set()
            base_exclude_ids = set(exclude_ids or set())
            candidate_limit = _scheduler_candidate_fetch_limit(limit)
            repo = WorkspaceRepository(session)
            while len(filtered) < limit:
                ids = await repo.list_schedulable_ids(
                    status=status,
                    limit=candidate_limit,
                    exclude_ids=base_exclude_ids | seen_ids,
                )
                if not ids:
                    break
                seen_ids.update(ids)
                eligible = await self._filter_provider_recovery_suppressed(
                    session,
                    ids,
                )
                for workspace_id in eligible:
                    if workspace_id not in filtered:
                        filtered.append(workspace_id)
                    if len(filtered) >= limit:
                        break
                if len(ids) < candidate_limit:
                    break
            await session.commit()
            return filtered[:limit]

    async def _filter_provider_recovery_suppressed(
        self,
        session: AsyncSession,
        workspace_ids: list[str],
    ) -> list[str]:
        if not workspace_ids:
            return []
        now = datetime.now(UTC)
        breaker_repo = ProviderModelCircuitBreakerRepository(session)
        allowed: set[str] = set()
        stmt = select(Workspace).where(Workspace.id.in_(workspace_ids))
        rows = {workspace.id: workspace for workspace in (await session.execute(stmt)).scalars()}
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
        async with self._session_factory() as session:
            rows = {
                workspace.id: workspace
                for workspace in (
                    await session.execute(
                        select(Workspace).where(Workspace.id.in_(workspace_ids))
                    )
                ).scalars()
            }
            for workspace_id in workspace_ids:
                workspace = rows.get(workspace_id)
                if workspace is None:
                    continue
                await _record_scheduler_queue_decision(
                    session,
                    workspace,
                    decision=QUEUE_DECISION_ORDERED,
                    reason_code=reason_code,
                    decided_at=decided_at,
                )
            await session.commit()

    async def _filter_current_status(
        self,
        workspace_ids: list[str],
        *,
        expected: WorkspaceStatus,
        action: str,
    ) -> list[str]:
        if not workspace_ids:
            return []

        async with self._session_factory() as session:
            stmt = select(Workspace.id, Workspace.status).where(Workspace.id.in_(workspace_ids))
            result = await session.execute(stmt)
            statuses: dict[str, str] = {row[0]: row[1] for row in result.all()}

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

        await self._recover_stale_active_executions()
        interval = max(0.0, self._config.stale_active_execution_scan_interval_seconds)
        self._next_stale_active_execution_scan_at = monotonic() + interval

    async def _maybe_expire_due_secret_leases(self) -> None:
        now = monotonic()
        if now < self._next_secret_lease_expiration_scan_at:
            return

        try:
            await self._expire_due_secret_leases()
        except Exception:
            _log.exception(
                "worker.secret_lease_expiration_failed",
                reason_code="SECRET_LEASE_EXPIRATION_FAILED",
            )
            raise

        interval = max(0.0, self._config.secret_lease_expiration_scan_interval_seconds)
        self._next_secret_lease_expiration_scan_at = monotonic() + interval

    async def _expire_due_secret_leases(self) -> None:
        async with self._session_factory() as session:
            expired = await SecretLeaseService(session).expire_due_secret_leases()
            expired_count = len(expired)
            workspace_ids = sorted({lease.workspace_id for lease in expired})
            await session.commit()

        if expired_count:
            _log.info(
                "worker.secret_leases_expired",
                reason_code="SECRET_LEASES_EXPIRED",
                expired_count=expired_count,
                workspace_ids=workspace_ids,
            )

    async def _recover_stale_active_executions(self) -> None:
        candidates = await self._list_stale_active_execution_candidates(
            exclude_ids=set(self._execution_tasks)
        )
        for candidate in candidates:
            await self._recover_stale_active_execution(candidate)

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

        async with self._session_factory() as session:
            result = await session.execute(stmt)
            rows = result.all()

        return [
            _ActiveExecutionCandidate(
                workspace_id=row[0],
                status=WorkspaceStatus(row[1]),
                compose_project_name=row[2],
                compose_file_path=row[3],
                pr_url=row[4],
                task_policy=row[5],
            )
            for row in rows
        ]

    async def _recover_stale_active_execution(
        self,
        candidate: _ActiveExecutionCandidate,
    ) -> None:
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
        if candidate.compose_project_name and snapshot.stack_state == "running":
            await self._record_stale_active_execution_detected(candidate, snapshot)
            return

    async def _record_stale_active_execution_detected(
        self,
        candidate: _ActiveExecutionCandidate,
        snapshot: RuntimeSnapshot,
    ) -> None:
        payload = {
            "compose_project_name": candidate.compose_project_name,
            "workspace_status": candidate.status.value,
            "runtime": _runtime_snapshot_payload(snapshot),
        }

        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(candidate.workspace_id)
            if ws is None or ws.status != candidate.status.value:
                return
            if not _execution_claim_is_stale(ws, datetime.now(UTC)):
                return
            if await self._has_stale_active_execution_event(session, candidate.workspace_id):
                return

            await repo.add_event(
                ws,
                event_type=_STALE_ACTIVE_EXECUTION_EVENT_TYPE,
                reason_code=_STALE_ACTIVE_EXECUTION_REASON_CODE,
                payload=payload,
            )
            await session.commit()

        _log.warning(
            _STALE_ACTIVE_EXECUTION_EVENT_TYPE,
            workspace_id=candidate.workspace_id,
            status=candidate.status.value,
            compose_project_name=candidate.compose_project_name,
            runtime=payload["runtime"],
        )

    async def _has_stale_active_execution_event(
        self,
        session: AsyncSession,
        workspace_id: str,
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
        return (await session.execute(stmt)).scalar_one_or_none() is not None

    async def _fail_stale_active_execution(
        self,
        candidate: _ActiveExecutionCandidate,
        snapshot: RuntimeSnapshot,
    ) -> None:
        message = _stale_active_execution_failure_message(candidate, snapshot)
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.transition_if_current(
                candidate.workspace_id,
                from_status=candidate.status,
                to=WorkspaceStatus.failed,
                reason_code=_STALE_ACTIVE_EXECUTION_REASON_CODE,
                extra_conditions=(_stale_execution_claim_filter(datetime.now(UTC)),),
            )
            if ws is None:
                return
            ws.execution_claimed_by = None
            ws.execution_claim_expires_at = None
            ws.failure_reason = FailureReason.infrastructure_failure.value
            ws.failure_message = message[:2048]
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
            ws = await repo.transition_if_current(
                candidate.workspace_id,
                from_status=candidate.status,
                to=WorkspaceStatus.failed,
                reason_code=finding.reason_code,
                extra_conditions=_claim_recheck_conditions(candidate.status),
            )
            if ws is None:
                return
            ws.execution_claimed_by = None
            ws.execution_claim_expires_at = None
            ws.monitor_claimed_by = None
            ws.monitor_claim_expires_at = None
            ws.failure_reason = FailureReason.infrastructure_failure.value
            ws.failure_message = message[:2048]
            await repo.add_event(
                ws,
                event_type=RUNTIME_STRANDED_EVENT_TYPE,
                reason_code=finding.reason_code,
                payload=_runtime_stranding_event_payload(candidate, snapshot, finding),
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
            ws.execution_claimed_by = None
            ws.execution_claim_expires_at = None
            ws.monitor_claimed_by = None
            ws.monitor_claim_expires_at = None
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

    async def _safely_provision(self, workspace_id: str) -> None:
        try:
            await self._provisioner.provision(workspace_id)
        except Exception:
            # Provisioner.provision() already logged + transitioned to failed;
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
        lease_expires_at = datetime.now(UTC) + timedelta(
            seconds=self._config.monitor_claim_lease_seconds
        )
        async with self._session_factory() as session:
            refreshed = await WorkspaceRepository(session).refresh_monitoring_pr_claim(
                workspace_id,
                owner_id=self._worker_id,
                lease_expires_at=lease_expires_at,
            )
            await session.commit()
            return refreshed

    def _execution_claim_expires_at(self) -> datetime:
        return datetime.now(UTC) + timedelta(seconds=self._config.execution_claim_lease_seconds)

    async def _refresh_execution_claim(self, workspace_id: str) -> bool:
        async with self._session_factory() as session:
            refreshed = await WorkspaceRepository(session).refresh_execution_claim(
                workspace_id,
                owner_id=self._worker_id,
                lease_expires_at=self._execution_claim_expires_at(),
            )
            await session.commit()
            return refreshed

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
    latest = (await queue_repo.list_for_workspace(workspace.id, limit=1))
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


def _scheduler_candidate_fetch_limit(limit: int) -> int:
    return max(limit, min(250, limit + 16, limit * 4 if limit > 0 else 0))


def _claim_recheck_conditions(status: WorkspaceStatus) -> tuple[Any, ...]:
    now = datetime.now(UTC)
    if status in _ACTIVE_EXECUTION_STATUSES:
        return (_stale_execution_claim_filter(now),)
    if status == WorkspaceStatus.monitoring_pr:
        return (_stale_monitor_claim_filter(now),)
    return ()


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
        retry_policy_allows_recovery=retry_policy_allows_runtime_recovery(
            candidate.task_policy
        ),
    )


def _workspace_claim_snapshot(workspace: Workspace) -> dict[str, str | None]:
    return {
        "monitor_claimed_by": workspace.monitor_claimed_by,
        "monitor_claim_expires_at": _json_datetime(workspace.monitor_claim_expires_at),
        "execution_claimed_by": workspace.execution_claimed_by,
        "execution_claim_expires_at": _json_datetime(workspace.execution_claim_expires_at),
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
        "AWF marked the workspace failed without cleanup; logs, the worktree, and any "
        "surviving files were preserved for inspection. Inspect the workspace, then "
        "cancel and redispatch the task when ready."
    )
