"""Async control-plane worker.

Polls the DB for workspaces needing action and dispatches them to the
Provisioner. Uses a single-process poll loop for the MVP; multi-node
``SELECT FOR UPDATE SKIP LOCKED`` scheduling is deferred to Phase 1.5.

Split into two methods:

- ``run_once()`` — processes one batch of work and returns. Unit-testable.
- ``run_forever()`` — composition; sleeps ``poll_interval`` between batches.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from functools import partial
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.logging import get_logger
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.node.provisioner import Provisioner

_log = get_logger(__name__)


@dataclass(frozen=True)
class WorkerConfig:
    poll_interval_seconds: float = 1.0
    max_concurrent_provisions: int = 3
    max_concurrent_executions: int = 3


class WorkspaceExecutorProtocol(Protocol):
    async def execute(self, workspace_id: str) -> None: ...


class ControlWorker:
    """Reads pending work from the DB and dispatches it to runtime handlers."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        provisioner: Provisioner,
        executor: WorkspaceExecutorProtocol | None = None,
        config: WorkerConfig,
    ) -> None:
        self._session_factory = session_factory
        self._provisioner = provisioner
        self._executor = executor
        self._config = config
        self._stopped = asyncio.Event()
        self._execution_tasks: dict[str, asyncio.Task[None]] = {}

    def request_stop(self) -> None:
        """Signal ``run_forever`` to exit after the current batch."""
        self._stopped.set()

    async def run_once(self) -> int:
        """Claim + dispatch requested provisioning and ready execution workspaces.

        Returns the number of workspaces dispatched. A zero return is a signal
        for ``run_forever`` to sleep; non-zero means we may be throughput-bound
        and should immediately loop again.
        """
        dispatched_ids: set[str] = set()

        requested_ids = await self._list_requested()
        if requested_ids:
            await asyncio.gather(
                *(self._safely_provision(ws_id) for ws_id in requested_ids),
                return_exceptions=False,
            )
            dispatched_ids.update(requested_ids)

        if self._executor is not None:
            execution_slots = self._available_execution_slots()
            ready_ids = await self._list_ready(limit=execution_slots)
            dispatched_ids.update(self._dispatch_ready_executions(ready_ids))

        return len(dispatched_ids)

    async def wait_for_execution_tasks(self) -> None:
        """Wait for ready-workspace execution tasks started by this worker."""
        while self._execution_tasks:
            await asyncio.gather(*tuple(self._execution_tasks.values()))

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

    async def _list_ready(self, *, limit: int | None = None) -> list[str]:
        """Return up to ``max_concurrent_executions`` workspace IDs in ``ready``."""
        row_limit = self._config.max_concurrent_executions if limit is None else limit
        return await self._list_by_status(WorkspaceStatus.ready, limit=row_limit)

    async def _list_by_status(self, status: WorkspaceStatus, *, limit: int) -> list[str]:
        if limit <= 0:
            return []

        async with self._session_factory() as session:
            stmt = (
                select(Workspace.id)
                .where(Workspace.status == status.value)
                .order_by(Workspace.created_at)
                .limit(limit)
            )
            result = await session.execute(stmt)
            return [row[0] for row in result.all()]

    def _available_execution_slots(self) -> int:
        return max(0, self._config.max_concurrent_executions - len(self._execution_tasks))

    def _dispatch_ready_executions(self, workspace_ids: list[str]) -> set[str]:
        dispatched: set[str] = set()
        for workspace_id in workspace_ids:
            if workspace_id in self._execution_tasks:
                continue

            task = asyncio.create_task(
                self._safely_execute(workspace_id),
                name=f"awf-execute-{workspace_id}",
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
            await self._executor.execute(workspace_id)
        except Exception:
            # WorkspaceExecutor.execute() owns state transitions, including
            # skip-if-no-longer-ready semantics. The worker must keep polling
            # even if one execution path crashes before it can mark a failure.
            _log.exception("worker.execute_failed", workspace_id=workspace_id)
