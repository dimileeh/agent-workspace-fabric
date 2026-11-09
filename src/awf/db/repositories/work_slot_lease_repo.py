"""Repository for broker-owned ``work_slot_leases`` rows (slot-release).

The resource broker (``control/worker/resource_broker.py``) is the only writer: it
serializes every lease mutation under the local-capacity advisory lock and flips
the workspace reservation phase in the same transaction. This repository only
issues the statements; like every repository it flushes and never commits.

Node scope mirrors the old requested-admission row cap and the capacity lock key:
a lease belongs to a node when its workspace runs on that node or has no node
(legacy null-node rows count against every node).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from awf.db.enums import WorkSlotLeasePhase, WorkspaceStatus
from awf.db.models import WorkSlotLease, Workspace
from awf.db.repositories.base import resolve_session_dialect_name

IN_USE_WORK_SLOT_LEASE_PHASES: tuple[str, ...] = (
    WorkSlotLeasePhase.held.value,
    WorkSlotLeasePhase.adopted.value,
)
"""Lease phases that occupy a work slot (expired and cleanup-unproven included)."""


@dataclass(frozen=True)
class WorkSlotLeaseDetail:
    """One lease row as reported by status, metrics and worker logs."""

    invocation_id: str
    workspace_id: str
    holder_worker_id: str
    holder_epoch: int
    phase: str
    cleanup_unproven: bool
    ticket_at: datetime
    acquired_at: datetime | None
    expires_at: datetime


@dataclass(frozen=True)
class WorkSlotSnapshot:
    """Work-slot usage for one node: slots in use, waiters and unsatisfiable waiters.

    ``waiting`` counts live waiters only (an expired waiter stopped re-checking);
    ``oldest_ticket_at`` is the oldest live or unsatisfiable ticket still queued.
    """

    limit: int
    in_use: int
    waiting: int
    unsatisfiable: int
    adopted: int
    cleanup_unproven: int
    oldest_ticket_at: datetime | None
    leases: tuple[WorkSlotLeaseDetail, ...]

    @property
    def free(self) -> int:
        return max(0, self.limit - self.in_use)

    def waiting_workspace_ids(self) -> list[str]:
        return [
            lease.workspace_id
            for lease in self.leases
            if lease.phase == WorkSlotLeasePhase.waiting.value
        ]

    def unsatisfiable_workspace_ids(self) -> list[str]:
        return [
            lease.workspace_id
            for lease in self.leases
            if lease.phase == WorkSlotLeasePhase.unsatisfiable.value
        ]

    def holders(self) -> list[WorkSlotLeaseDetail]:
        return [lease for lease in self.leases if lease.phase in IN_USE_WORK_SLOT_LEASE_PHASES]


def summarize_work_slot_leases(
    leases: Sequence[WorkSlotLease],
    *,
    limit: int,
    now: datetime,
) -> WorkSlotSnapshot:
    """Summarize lease rows; shared by the worker broker, status and metrics."""
    details: list[WorkSlotLeaseDetail] = []
    in_use = waiting = unsatisfiable = adopted = cleanup_unproven = 0
    oldest_ticket_at: datetime | None = None
    for lease in leases:
        expired_waiter = lease.acquired_at is None and lease.expires_at <= now
        if lease.phase in IN_USE_WORK_SLOT_LEASE_PHASES:
            in_use += 1
            adopted += int(lease.phase == WorkSlotLeasePhase.adopted.value)
            cleanup_unproven += int(bool(lease.cleanup_unproven))
        elif expired_waiter:
            continue
        elif lease.phase == WorkSlotLeasePhase.unsatisfiable.value:
            unsatisfiable += 1
        else:
            waiting += 1
        if lease.acquired_at is None and (
            oldest_ticket_at is None or lease.ticket_at < oldest_ticket_at
        ):
            oldest_ticket_at = lease.ticket_at
        details.append(
            WorkSlotLeaseDetail(
                invocation_id=lease.invocation_id,
                workspace_id=lease.workspace_id,
                holder_worker_id=lease.holder_worker_id,
                holder_epoch=lease.holder_epoch,
                phase=lease.phase,
                cleanup_unproven=bool(lease.cleanup_unproven),
                ticket_at=lease.ticket_at,
                acquired_at=lease.acquired_at,
                expires_at=lease.expires_at,
            )
        )
    return WorkSlotSnapshot(
        limit=max(0, int(limit)),
        in_use=in_use,
        waiting=waiting,
        unsatisfiable=unsatisfiable,
        adopted=adopted,
        cleanup_unproven=cleanup_unproven,
        oldest_ticket_at=oldest_ticket_at,
        leases=tuple(details),
    )


class WorkSlotLeaseRepository:
    """Flush-only query helpers for ``work_slot_leases``."""

    def __init__(self, session: AsyncSession, *, dialect_name: str | None = None) -> None:
        self._session = session
        self._dialect_name = resolve_session_dialect_name(session, dialect_name)

    async def add(self, lease: WorkSlotLease) -> WorkSlotLease:
        """Insert ``lease`` and flush so constraint violations surface here."""
        self._session.add(lease)
        await self._session.flush()
        return lease

    async def get(self, invocation_id: str) -> WorkSlotLease | None:
        stmt = select(WorkSlotLease).where(WorkSlotLease.invocation_id == invocation_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_for_node(self, *, node_id: str | None) -> list[WorkSlotLease]:
        """Return every lease in ``node_id``'s scope (all nodes when ``None``), FIFO order."""
        stmt = _node_scoped(select(WorkSlotLease), node_id=node_id).order_by(
            WorkSlotLease.ticket_at, WorkSlotLease.invocation_id
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_for_workspace(self, workspace_id: str) -> list[WorkSlotLease]:
        stmt = (
            select(WorkSlotLease)
            .where(WorkSlotLease.workspace_id == workspace_id)
            .order_by(WorkSlotLease.ticket_at, WorkSlotLease.invocation_id)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def list_with_runtime_for_node(
        self, *, node_id: str | None
    ) -> list[tuple[WorkSlotLease, str | None, str | None]]:
        """Return leases with their workspace compose project and file (probe input)."""
        stmt = (
            select(
                WorkSlotLease,
                Workspace.compose_project_name,
                Workspace.compose_file_path,
            )
            .join(Workspace, Workspace.id == WorkSlotLease.workspace_id)
            .order_by(WorkSlotLease.ticket_at, WorkSlotLease.invocation_id)
        )
        if node_id is not None:
            stmt = stmt.where(or_(Workspace.node_id == node_id, Workspace.node_id.is_(None)))
        rows = (await self._session.execute(stmt)).all()
        return [(row[0], row[1], row[2]) for row in rows]

    async def list_parked_workspace_ids(
        self,
        *,
        holder_worker_id: str,
        workspace_status: WorkspaceStatus,
    ) -> list[str]:
        """Return workspaces in ``workspace_status`` whose held lease ``holder`` parked.

        A parked lease is a plain held lease (not adopted, cleanup proven) that a
        finished provisioning task kept for READY dispatch to continue.
        """
        stmt = (
            select(WorkSlotLease.workspace_id)
            .join(Workspace, Workspace.id == WorkSlotLease.workspace_id)
            .where(
                Workspace.status == workspace_status.value,
                WorkSlotLease.holder_worker_id == holder_worker_id,
                WorkSlotLease.phase == WorkSlotLeasePhase.held.value,
                WorkSlotLease.cleanup_unproven.is_(False),
            )
            .order_by(WorkSlotLease.ticket_at, WorkSlotLease.invocation_id)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def count_in_use(self, *, node_id: str | None) -> int:
        """Count held/adopted leases on the node, including expired ones.

        Expiry never frees a slot by itself: only a live worker's startup check or
        reconciler releases a lease after proving its process is gone.
        """
        stmt = _node_scoped(
            select(func.count()).select_from(WorkSlotLease),
            node_id=node_id,
        ).where(WorkSlotLease.phase.in_(IN_USE_WORK_SLOT_LEASE_PHASES))
        return int(await self._session.scalar(stmt) or 0)

    async def held_for_workspace(self, workspace_id: str) -> WorkSlotLease | None:
        """Return the workspace's held/adopted lease, if any (at most one exists)."""
        stmt = select(WorkSlotLease).where(
            WorkSlotLease.workspace_id == workspace_id,
            WorkSlotLease.phase.in_(IN_USE_WORK_SLOT_LEASE_PHASES),
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def fifo_head(self, *, node_id: str | None, now: datetime) -> WorkSlotLease | None:
        """Return the oldest live ``waiting`` ticket on the node.

        Expired waiters (their invocation stopped re-checking), ``unsatisfiable``
        waiters and waiters whose workspace already holds a lease never block the head.
        """
        stmt = (
            _live_waiters_stmt(select(WorkSlotLease), node_id=node_id, now=now)
            .order_by(WorkSlotLease.ticket_at, WorkSlotLease.invocation_id)
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def count_live_waiters(self, *, node_id: str | None, now: datetime) -> int:
        stmt = _live_waiters_stmt(
            select(func.count()).select_from(WorkSlotLease),
            node_id=node_id,
            now=now,
        )
        return int(await self._session.scalar(stmt) or 0)

    async def count_leaseless_ready(self, *, node_id: str | None) -> int:
        """Count ``ready`` workspaces on the node that hold no work-slot lease.

        Requested admission reserves capacity for them so already-provisioned work
        is not starved by new admissions.
        """
        held_workspace_ids = select(WorkSlotLease.workspace_id).where(
            WorkSlotLease.phase.in_(IN_USE_WORK_SLOT_LEASE_PHASES)
        )
        stmt = (
            select(func.count())
            .select_from(Workspace)
            .where(
                Workspace.status == WorkspaceStatus.ready.value,
                Workspace.id.not_in(held_workspace_ids),
            )
        )
        if node_id is not None:
            stmt = stmt.where(or_(Workspace.node_id == node_id, Workspace.node_id.is_(None)))
        return int(await self._session.scalar(stmt) or 0)

    async def update_fenced(
        self,
        invocation_id: str,
        *,
        holder_worker_id: str,
        holder_epoch: int,
        values: Mapping[str, Any],
    ) -> bool:
        """Update the lease only while ``holder_worker_id``/``holder_epoch`` still own it."""
        stmt = (
            update(WorkSlotLease)
            .where(
                WorkSlotLease.invocation_id == invocation_id,
                WorkSlotLease.holder_worker_id == holder_worker_id,
                WorkSlotLease.holder_epoch == holder_epoch,
            )
            .values(**dict(values))
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return bool(getattr(result, "rowcount", 0))

    async def delete_fenced(
        self,
        invocation_id: str,
        *,
        holder_worker_id: str,
        holder_epoch: int,
    ) -> bool:
        """Delete the lease only while ``holder_worker_id``/``holder_epoch`` still own it."""
        stmt = (
            delete(WorkSlotLease)
            .where(
                WorkSlotLease.invocation_id == invocation_id,
                WorkSlotLease.holder_worker_id == holder_worker_id,
                WorkSlotLease.holder_epoch == holder_epoch,
            )
            .execution_options(synchronize_session=False)
        )
        result = await self._session.execute(stmt)
        await self._session.flush()
        return bool(getattr(result, "rowcount", 0))

    async def delete_expired_waiters(self, *, node_id: str | None, now: datetime) -> list[str]:
        """Delete expired ``waiting``/``unsatisfiable`` rows and return their ids."""
        stmt = _node_scoped(select(WorkSlotLease.invocation_id), node_id=node_id).where(
            WorkSlotLease.acquired_at.is_(None),
            WorkSlotLease.expires_at <= now,
        )
        invocation_ids = sorted((await self._session.execute(stmt)).scalars().all())
        if invocation_ids:
            await self._session.execute(
                delete(WorkSlotLease)
                .where(
                    WorkSlotLease.invocation_id.in_(invocation_ids),
                    WorkSlotLease.acquired_at.is_(None),
                )
                .execution_options(synchronize_session=False)
            )
            await self._session.flush()
        return invocation_ids


def _node_scoped(stmt: Select[Any], *, node_id: str | None) -> Select[Any]:
    if node_id is None:
        return stmt
    return stmt.join(Workspace, Workspace.id == WorkSlotLease.workspace_id).where(
        or_(Workspace.node_id == node_id, Workspace.node_id.is_(None))
    )


def _live_waiters_stmt(stmt: Select[Any], *, node_id: str | None, now: datetime) -> Select[Any]:
    # A waiter whose workspace still holds another invocation's lease cannot be
    # granted until that lease is gone, so it must not hold up the queue meanwhile.
    held = aliased(WorkSlotLease)
    held_workspace_ids = select(held.workspace_id).where(
        held.phase.in_(IN_USE_WORK_SLOT_LEASE_PHASES)
    )
    return _node_scoped(stmt, node_id=node_id).where(
        WorkSlotLease.phase == WorkSlotLeasePhase.waiting.value,
        WorkSlotLease.expires_at > now,
        WorkSlotLease.workspace_id.not_in(held_workspace_ids),
    )
