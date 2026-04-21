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


class ControlWorker:
    """Reads pending work from the DB and dispatches it to the provisioner."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        provisioner: Provisioner,
        config: WorkerConfig,
    ) -> None:
        self._session_factory = session_factory
        self._provisioner = provisioner
        self._config = config
        self._stopped = asyncio.Event()

    def request_stop(self) -> None:
        """Signal ``run_forever`` to exit after the current batch."""
        self._stopped.set()

    async def run_once(self) -> int:
        """Claim + dispatch up to ``max_concurrent_provisions`` requested workspaces.

        Returns the number of workspaces dispatched. A zero return is a signal
        for ``run_forever`` to sleep; non-zero means we may be throughput-bound
        and should immediately loop again.
        """
        ids = await self._list_pending()
        if not ids:
            return 0

        await asyncio.gather(
            *(self._safely_provision(ws_id) for ws_id in ids),
            return_exceptions=False,
        )
        return len(ids)

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
        """Return up to ``max_concurrent_provisions`` workspace IDs in ``requested``."""
        async with self._session_factory() as session:
            stmt = (
                select(Workspace.id)
                .where(Workspace.status == WorkspaceStatus.requested.value)
                .order_by(Workspace.created_at)
                .limit(self._config.max_concurrent_provisions)
            )
            result = await session.execute(stmt)
            return [row[0] for row in result.all()]

    async def _safely_provision(self, workspace_id: str) -> None:
        try:
            await self._provisioner.provision(workspace_id)
        except Exception:
            # Provisioner.provision() already logged + transitioned to failed;
            # we swallow here so one bad workspace doesn't abort the batch.
            _log.exception("worker.provision_failed", workspace_id=workspace_id)
