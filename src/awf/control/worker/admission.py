"""Requested-workspace admission lock helpers.

Requested admission is bounded by broker-owned work-slot leases
(``resource_broker.new_work_capacity``, taken under the capacity lock after these
admission locks); the former workspace-row cap is gone (slot-release).
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from awf.control.worker.config import DEFAULT_LOCAL_SERVICE_WORKER_NODE_ID
from awf.control.worker.resource_broker import _postgres_advisory_lock_key


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
