"""Repository for on-demand worker GC delegation requests (#582).

``awf service gc --execute`` resolves in the capability-less API container, so it
cannot reclaim the per-workspace Claude auth overlays or ``_shared/claude-base``.
The API writes a ``pending`` row through :class:`ServiceGCRequestRepository`; the
worker (which holds ``CAP_SYS_ADMIN``) claims it on its poll loop, runs the real
reap, and writes the reclaimed report back so the API can fold it into the gc
response. The claim uses ``SELECT ... FOR UPDATE SKIP LOCKED`` so a concurrent
worker (or the interval reaper racing the same row) never double-claims.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.ids import new_service_gc_request_id
from awf.db.enums import (
    SERVICE_GC_REQUEST_STATUS_COMPLETED,
    SERVICE_GC_REQUEST_STATUS_EXPIRED,
    SERVICE_GC_REQUEST_STATUS_FAILED,
    SERVICE_GC_REQUEST_STATUS_PENDING,
    SERVICE_GC_REQUEST_STATUS_RUNNING,
)
from awf.db.models import ServiceGCRequest
from awf.db.repositories.base import resolve_session_dialect_name


class ServiceGCRequestRepository:
    """CRUD + claim helpers for ``service_gc_requests`` delegation rows."""

    def __init__(self, session: AsyncSession, *, dialect_name: str | None = None) -> None:
        self._session = session
        self._dialect_name = resolve_session_dialect_name(session, dialect_name)

    async def create_pending(
        self,
        *,
        node_id: str | None,
        requested_at: datetime,
        deadline_at: datetime | None,
        params: dict[str, Any] | None = None,
    ) -> ServiceGCRequest:
        """Insert a fresh ``pending`` delegation request for the worker to claim."""
        request = ServiceGCRequest(
            id=new_service_gc_request_id(),
            status=SERVICE_GC_REQUEST_STATUS_PENDING,
            node_id=node_id,
            requested_at=requested_at,
            deadline_at=deadline_at,
            params=dict(params or {}),
        )
        self._session.add(request)
        await self._session.flush()
        return request

    async def get(self, request_id: str) -> ServiceGCRequest | None:
        stmt = select(ServiceGCRequest).where(ServiceGCRequest.id == request_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def claim_oldest_pending(
        self,
        *,
        node_id: str,
        now: datetime,
    ) -> ServiceGCRequest | None:
        """Claim the oldest ``pending`` request for this node, marking it ``running``.

        Uses ``SELECT ... FOR UPDATE SKIP LOCKED`` so a second worker (or the
        interval reaper racing) sees ``None`` rather than re-claiming the same row.
        Rows with a ``NULL`` ``node_id`` are claimable by any node (single-node
        fallback), mirroring the terminal-runtime release candidate query.

        Pending rows whose ``deadline_at`` (the API client's polling budget) has
        already elapsed are excluded: the operator has been told the trigger timed
        out, so running that destructive reap later — behind the operator's back —
        is the hazard expire-on-timeout rejects (#590). Such rows are swept to the
        terminal ``expired`` state by :meth:`expire_stale_requests`; the periodic
        interval reaper remains the durable backstop for the disk reclaim itself.
        Rows with a ``NULL`` ``deadline_at`` carry no budget and stay claimable.
        """
        stmt = (
            select(ServiceGCRequest)
            .where(ServiceGCRequest.status == SERVICE_GC_REQUEST_STATUS_PENDING)
            .where(
                or_(
                    ServiceGCRequest.deadline_at.is_(None),
                    ServiceGCRequest.deadline_at >= now,
                )
            )
            .where(
                or_(
                    ServiceGCRequest.node_id == node_id,
                    ServiceGCRequest.node_id.is_(None),
                )
            )
            .order_by(ServiceGCRequest.requested_at.asc(), ServiceGCRequest.id.asc())
            .limit(1)
        )
        if self._dialect_name == "postgresql":
            stmt = stmt.with_for_update(of=ServiceGCRequest, skip_locked=True)
        request = (await self._session.execute(stmt)).scalar_one_or_none()
        if request is None:
            return None
        request.status = SERVICE_GC_REQUEST_STATUS_RUNNING
        request.claimed_at = now
        request.node_id = node_id
        await self._session.flush()
        return request

    async def expire_stale_requests(
        self,
        *,
        node_id: str,
        now: datetime,
    ) -> list[str]:
        """Mark ``pending``/``running`` rows past their ``deadline_at`` terminal ``expired`` (#590).

        Expire-on-timeout: once a row's ``deadline_at`` (the API client's polling budget)
        has elapsed the operator has already been told the trigger timed out, so the reap
        must **not** run later behind their back — a destructive reap after a reported
        timeout is the hazard this rejects. Rather than recover-and-re-run, the row is
        retired to the terminal ``expired`` state (recording ``finished_at``):

        * a still-``pending`` row that no worker claimed in time would otherwise be picked
          up by a later poll/restart and run with the *current* clock + stored filters
          (deleting workspaces that only became eligible after the timeout);
        * a ``running`` row a worker abandoned mid-reap (cancelled on shutdown) would
          otherwise accumulate forever, since :meth:`claim_oldest_pending` only selects
          ``pending`` rows.

        Expiring both retires them safely; the worker's periodic (~1h) interval reaper
        stays as the durable backstop that performs the actual disk reclaim. ``SELECT ...
        FOR UPDATE SKIP LOCKED`` skips any row a concurrent claim/finish is actively
        holding. Single-node only (Phase 1): the only ``running`` rows are ones a prior
        incarnation of this node abandoned (the claim and reap run in separate
        transactions, so a genuinely in-flight reap is never one this sweep observes
        within the same worker). Rows with a ``NULL`` ``deadline_at`` carry no budget and
        are never expired. Returns the ids expired, oldest first.
        """
        stmt = (
            select(ServiceGCRequest)
            .where(
                ServiceGCRequest.status.in_(
                    (
                        SERVICE_GC_REQUEST_STATUS_PENDING,
                        SERVICE_GC_REQUEST_STATUS_RUNNING,
                    )
                )
            )
            .where(ServiceGCRequest.deadline_at.is_not(None))
            .where(ServiceGCRequest.deadline_at < now)
            .where(
                or_(
                    ServiceGCRequest.node_id == node_id,
                    ServiceGCRequest.node_id.is_(None),
                )
            )
            .order_by(ServiceGCRequest.requested_at.asc(), ServiceGCRequest.id.asc())
        )
        if self._dialect_name == "postgresql":
            stmt = stmt.with_for_update(of=ServiceGCRequest, skip_locked=True)
        rows = (await self._session.execute(stmt)).scalars().all()
        expired: list[str] = []
        for request in rows:
            request.status = SERVICE_GC_REQUEST_STATUS_EXPIRED
            request.finished_at = now
            expired.append(request.id)
        if expired:
            await self._session.flush()
        return expired

    async def mark_completed(
        self,
        *,
        request_id: str,
        result: dict[str, Any],
        now: datetime,
    ) -> ServiceGCRequest | None:
        """Record the worker's reclaimed report and mark the request ``completed``."""
        request = await self.get(request_id)
        if request is None:
            return None
        request.status = SERVICE_GC_REQUEST_STATUS_COMPLETED
        request.result = dict(result)
        request.finished_at = now
        request.error_code = None
        request.error_message = None
        await self._session.flush()
        return request

    async def mark_failed(
        self,
        *,
        request_id: str,
        error_code: str,
        error_message: str,
        now: datetime,
    ) -> ServiceGCRequest | None:
        """Mark the request ``failed`` with a structured reason code for the API."""
        request = await self.get(request_id)
        if request is None:
            return None
        request.status = SERVICE_GC_REQUEST_STATUS_FAILED
        request.finished_at = now
        request.error_code = error_code
        request.error_message = error_message[:2048]
        await self._session.flush()
        return request
