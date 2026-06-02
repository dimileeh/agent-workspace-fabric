"""Requested-workspace admission slot helpers."""

from __future__ import annotations

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from awf.control.worker.config import (
    DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID,
    WorkerConfig,
    effective_worker_config_node_id,
)
from awf.control.worker.constants import _REQUESTED_ADMISSION_SLOT_STATUSES
from awf.control.worker.resource_broker import _postgres_advisory_lock_key
from awf.db.models import Workspace


def _requested_admission_lock_node_ids(node_id: str | None) -> tuple[str, ...]:
    # Named workers count legacy null-node active rows, so they serialize with
    # null-node claimers before taking their own per-node admission lock. Stale
    # recovery scans the same null-node legacy scope so these rows cannot wedge
    # named-node admission indefinitely after an upgrade.
    if node_id is None or node_id == DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID:
        return (DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID,)
    return (DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID, node_id)


async def _acquire_requested_admission_lock(
    session: AsyncSession,
    *,
    node_id: str,
) -> None:
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        return
    lock_key = _postgres_advisory_lock_key(f"awf:requested-admission:{node_id}")
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": lock_key},
    )


async def _acquire_requested_admission_locks(
    session: AsyncSession,
    *,
    node_ids: tuple[str, ...],
) -> None:
    for node_id in dict.fromkeys(node_ids):
        await _acquire_requested_admission_lock(session, node_id=node_id)


async def _requested_admission_row_slots(
    session: AsyncSession,
    *,
    config: WorkerConfig,
) -> int:
    max_executions = int(max(0, config.max_concurrent_executions))
    if max_executions <= 0:
        return 0
    status_values = [status.value for status in _REQUESTED_ADMISSION_SLOT_STATUSES]
    stmt = select(func.count()).select_from(Workspace).where(Workspace.status.in_(status_values))
    node_id = effective_worker_config_node_id(config)
    stmt = stmt.where(
        or_(
            Workspace.node_id == node_id,
            Workspace.node_id.is_(None),
        )
    )
    occupied = await session.scalar(stmt)
    return max(0, max_executions - int(occupied or 0))
