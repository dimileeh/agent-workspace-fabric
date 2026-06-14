"""API→worker GC delegation orchestration (#582).

``awf service gc --execute`` runs in the capability-less API container, so it
delegates the reclaim of the per-workspace Claude auth overlays + ``_shared/
claude-base`` to the worker (the only ``CAP_SYS_ADMIN`` context). The API and
worker share only the control-plane DB, so this module:

1. fast-fails when there is no fresh ``worker_heartbeats`` row for the node (so an
   operator running gc with the worker down is told immediately, not after the
   full timeout);
2. otherwise writes a ``pending`` ``service_gc_requests`` row the worker consumes;
3. polls that row (fresh session per poll → READ COMMITTED sees the worker's
   commit) until it is ``completed``/``failed`` or the deadline elapses,
   returning a :class:`WorkerReclaimOutcome` the route folds into the response.

The pure summation/status logic lives in :mod:`awf.service.gc_worker_delegation`;
this module owns only the I/O.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.enums import (
    SERVICE_GC_REQUEST_STATUS_COMPLETED,
    SERVICE_GC_REQUEST_STATUS_EXPIRED,
    SERVICE_GC_REQUEST_STATUS_FAILED,
)
from awf.db.models import ServiceGCRequest, WorkerHeartbeat
from awf.db.repositories import ServiceGCRequestRepository, WorkerHeartbeatRepository
from awf.db.resilience import run_db_operation_with_retry
from awf.service.gc_worker_delegation import WorkerReclaimOutcome
from awf.service.worker_heartbeat import worker_heartbeat_is_fresh

_DEFAULT_POLL_INTERVAL_SECONDS = 0.25


async def delegate_service_gc_to_worker(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    node_id: str,
    deadline_seconds: float,
    params: dict[str, Any] | None = None,
    poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
) -> WorkerReclaimOutcome:
    """Delegate the capability-gated GC reap to the worker and await its result."""
    now = datetime.now(UTC)
    heartbeat = await _latest_heartbeat(session_factory, node_id=node_id)
    if heartbeat is None or not worker_heartbeat_is_fresh(
        heartbeat.last_heartbeat_at,
        now=now,
        poll_interval_seconds=heartbeat.poll_interval_seconds,
    ):
        return WorkerReclaimOutcome.unavailable(
            f"no fresh control-worker heartbeat for node {node_id!r}; only the worker "
            "(CAP_SYS_ADMIN) can reclaim the auth overlays + claude-base — start the "
            "worker and re-run."
        )

    deadline_at = now + timedelta(seconds=max(0.0, deadline_seconds))
    request_id = await _write_pending(
        session_factory,
        node_id=node_id,
        requested_at=now,
        deadline_at=deadline_at,
        params=params or {},
    )
    return await _poll_for_outcome(
        session_factory,
        request_id=request_id,
        deadline_seconds=deadline_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )


async def _poll_for_outcome(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    request_id: str,
    deadline_seconds: float,
    poll_interval_seconds: float,
) -> WorkerReclaimOutcome:
    budget = max(0.0, deadline_seconds)
    interval = max(0.0, poll_interval_seconds)
    start = time.monotonic()
    while True:
        request = await _get_request(session_factory, request_id)
        if request is not None:
            if request.status == SERVICE_GC_REQUEST_STATUS_COMPLETED:
                # The worker can mark a ``running`` row ``completed`` *after* its
                # ``deadline_at`` if the reap overran the budget and the expire sweep
                # had not yet raced it to ``expired`` (``mark_completed`` only refuses
                # to overwrite an already-``expired`` row). Folding that late report as
                # a successful ``execute`` gc would contradict expire-on-timeout, which
                # keys the timeout decision off ``deadline_at`` — not on which of the
                # API's monotonic budget and the row's deadline won the race. Surface
                # the timeout instead (comment 4493490434).
                if _finished_past_deadline(request):
                    return WorkerReclaimOutcome.delegation_timeout(
                        "worker completed gc reclaim only after its deadline elapsed; "
                        "the operator's budget had already run out"
                    )
                return WorkerReclaimOutcome.from_report(dict(request.result or {}))
            if request.status == SERVICE_GC_REQUEST_STATUS_FAILED:
                return WorkerReclaimOutcome.reclaim_failed(
                    request.error_message or "worker gc reclaim failed",
                    report=dict(request.result) if request.result else None,
                )
            if request.status == SERVICE_GC_REQUEST_STATUS_EXPIRED:
                # The worker swept this row to the terminal ``expired`` state once its
                # ``deadline_at`` elapsed (expire-on-timeout, #590); it will never
                # complete. The poll's monotonic budget starts after the heartbeat +
                # pending-write I/O, so it outlasts ``deadline_at`` — stop sleeping on a
                # terminal row and surface the timeout now (PRRT_kwDOSJAM6s6JbNTB).
                return WorkerReclaimOutcome.delegation_timeout(
                    "worker gc reclaim expired after its deadline elapsed before the "
                    "worker completed it"
                )
        remaining = budget - (time.monotonic() - start)
        if remaining <= 0:
            return WorkerReclaimOutcome.delegation_timeout(
                f"worker did not complete gc reclaim within {deadline_seconds:.1f}s"
            )
        await asyncio.sleep(min(interval, remaining) if interval > 0 else remaining)


def _finished_past_deadline(request: ServiceGCRequest) -> bool:
    """Whether the worker finished the reap strictly after the row's ``deadline_at``.

    A row with no ``deadline_at`` carries no budget (never late); a completed row
    always records ``finished_at`` via :meth:`ServiceGCRequestRepository.mark_completed`,
    but the ``finished_at is None`` guard keeps the comparison defensive and mypy happy.
    Kept on a single expression so the deadline comparison is one branch point.
    """
    deadline_at = request.deadline_at
    finished_at = request.finished_at
    return deadline_at is not None and finished_at is not None and finished_at > deadline_at


async def _latest_heartbeat(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    node_id: str,
) -> WorkerHeartbeat | None:
    async def _operation(session: AsyncSession) -> WorkerHeartbeat | None:
        return await WorkerHeartbeatRepository(session).latest_for_node(node_id=node_id)

    return await run_db_operation_with_retry(session_factory, _operation)


async def _write_pending(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    node_id: str,
    requested_at: datetime,
    deadline_at: datetime,
    params: dict[str, Any],
) -> str:
    async def _operation(session: AsyncSession) -> str:
        request = await ServiceGCRequestRepository(session).create_pending(
            node_id=node_id,
            requested_at=requested_at,
            deadline_at=deadline_at,
            params=params,
        )
        return request.id

    return await run_db_operation_with_retry(session_factory, _operation, commit=True)


async def _get_request(
    session_factory: async_sessionmaker[AsyncSession],
    request_id: str,
) -> ServiceGCRequest | None:
    async def _operation(session: AsyncSession) -> ServiceGCRequest | None:
        return await ServiceGCRequestRepository(session).get(request_id)

    return await run_db_operation_with_retry(session_factory, _operation)
