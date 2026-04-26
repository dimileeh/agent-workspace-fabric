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

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.logging import get_logger
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import Workspace, WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.node.provisioner import Provisioner
from awf.runtime.inspection import RuntimeInspector, RuntimeSnapshot

_log = get_logger(__name__)

_ACTIVE_EXECUTION_STATUSES: tuple[WorkspaceStatus, ...] = (
    WorkspaceStatus.running,
    WorkspaceStatus.validating,
    WorkspaceStatus.pushing,
)
_STALE_ACTIVE_EXECUTION_REASON_CODE = "STALE_ACTIVE_EXECUTION"
_STALE_ACTIVE_EXECUTION_EVENT_TYPE = "workspace.stale_active_execution_detected"


@dataclass(frozen=True)
class WorkerConfig:
    poll_interval_seconds: float = 1.0
    max_concurrent_provisions: int = 3
    max_concurrent_executions: int = 3
    monitor_claim_lease_seconds: float = 300.0
    execution_claim_lease_seconds: float = 300.0
    stale_active_execution_scan_interval_seconds: float = 300.0
    node_id: str | None = None


@dataclass(frozen=True)
class _ActiveExecutionCandidate:
    workspace_id: str
    status: WorkspaceStatus
    compose_project_name: str | None


class WorkspaceExecutorProtocol(Protocol):
    async def execute(
        self,
        workspace_id: str,
        *,
        execution_owner_id: str | None = None,
        execution_lease_expires_at: datetime | None = None,
    ) -> None: ...

    async def resume_pr_monitor(self, workspace_id: str) -> None: ...


class RuntimeInspectorProtocol(Protocol):
    async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot: ...


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
        self._worker_id = f"control-worker-{uuid.uuid4().hex}"
        self._next_stale_active_execution_scan_at = 0.0

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

        if self._executor is not None:
            await self._maybe_recover_stale_active_executions()

        requested_ids = await self._list_requested()
        requested_ids = await self._filter_current_status(
            requested_ids,
            expected=WorkspaceStatus.requested,
            action="provision",
        )
        if requested_ids:
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
                dispatched_ids.update(
                    self._dispatch_monitor_resumes(monitoring_ids, limit=execution_slots)
                )

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
                dispatched_ids.update(
                    self._dispatch_ready_executions(ready_ids, limit=execution_slots)
                )

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
            return await WorkspaceRepository(session).list_schedulable_ids(
                status=status,
                limit=limit,
                exclude_ids=exclude_ids,
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
        active_status_values = [status.value for status in _ACTIVE_EXECUTION_STATUSES]
        claim_cutoff = datetime.now(UTC)
        stmt = (
            select(Workspace.id, Workspace.status, Workspace.compose_project_name)
            .where(Workspace.status.in_(active_status_values))
            .where(Workspace.node_id == self._config.node_id)
            .where(_stale_execution_claim_filter(claim_cutoff))
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

        if candidate.compose_project_name and snapshot.stack_state == "running":
            await self._record_stale_active_execution_detected(candidate, snapshot)
            return

        await self._fail_stale_active_execution(candidate, snapshot)

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

            task = asyncio.create_task(
                self._safely_resume_claimed_pr_monitor(workspace_id),
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

    async def _safely_resume_pr_monitor(self, workspace_id: str) -> None:
        if self._executor is None:
            return
        try:
            await self._executor.resume_pr_monitor(workspace_id)
        except Exception:
            # The monitor runner owns normal terminal transitions. Recovery
            # dispatch still must not take the service worker down if a single
            # workspace hits an unexpected runtime error.
            _log.exception("worker.pr_monitor_resume_failed", workspace_id=workspace_id)

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
            claimed = await WorkspaceRepository(session).claim_monitoring_pr(
                workspace_id,
                owner_id=self._worker_id,
                lease_expires_at=lease_expires_at,
                now=now,
            )
            await session.commit()
            return claimed

    async def _safely_resume_claimed_pr_monitor(self, workspace_id: str) -> None:
        heartbeat = asyncio.create_task(
            self._refresh_monitoring_pr_claim_loop(workspace_id),
            name=f"awf-monitor-claim-{workspace_id}",
        )
        try:
            await self._safely_resume_pr_monitor(workspace_id)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            await self._release_monitoring_pr_claim(workspace_id)

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


def _execution_claim_is_stale(workspace: Workspace, claim_cutoff: datetime) -> bool:
    if workspace.execution_claimed_by is None or workspace.execution_claim_expires_at is None:
        return True

    expires_at = workspace.execution_claim_expires_at
    if expires_at.tzinfo is None and claim_cutoff.tzinfo is not None:
        claim_cutoff = claim_cutoff.replace(tzinfo=None)
    return expires_at <= claim_cutoff


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
