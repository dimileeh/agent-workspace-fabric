"""Repository helpers for monitor-recovery and execution claim/lease management."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, case, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from awf.db.enums import OperationType, WorkspaceStatus
from awf.db.models import Operation, Workspace


async def claim_monitoring_pr(
    session: AsyncSession,
    workspace_id: str,
    *,
    owner_id: str,
    lease_expires_at: datetime,
    now: datetime | None = None,
    clear_stale_execution_claim_cutoff: datetime | None = None,
) -> bool:
    """Claim a monitor-recovery workspace unless another lease is active."""
    cutoff = now or datetime.now(UTC)
    values: dict[str, Any] = {
        "monitor_claimed_by": owner_id,
        "monitor_claim_expires_at": lease_expires_at,
        "updated_at": Workspace.updated_at,
    }
    claim_conditions: list[Any] = [
        Workspace.id == workspace_id,
        Workspace.status == WorkspaceStatus.monitoring_pr.value,
        or_(
            Workspace.monitor_claim_expires_at.is_(None),
            Workspace.monitor_claim_expires_at <= cutoff,
            Workspace.monitor_claimed_by == owner_id,
        ),
    ]
    if clear_stale_execution_claim_cutoff is not None:
        stale_execution_claim = or_(
            Workspace.execution_claimed_by.is_(None),
            Workspace.execution_claim_expires_at.is_(None),
            Workspace.execution_claim_expires_at <= clear_stale_execution_claim_cutoff,
        )
        execution_claim_available = or_(
            stale_execution_claim,
            Workspace.execution_claimed_by == owner_id,
        )
        values.update(
            execution_claimed_by=case(
                (stale_execution_claim, None),
                else_=Workspace.execution_claimed_by,
            ),
            execution_claim_expires_at=case(
                (stale_execution_claim, None),
                else_=Workspace.execution_claim_expires_at,
            ),
            # D3: bump the fencing token when clearing a stale execution
            # claim so a zombie worker whose owner string still matches is
            # fenced on its next CAS write. Untouched when the claim is
            # preserved (unexpired) so a live worker is never fenced.
            execution_claim_epoch=case(
                (stale_execution_claim, Workspace.execution_claim_epoch + 1),
                else_=Workspace.execution_claim_epoch,
            ),
        )
        claim_conditions.append(execution_claim_available)
    result = await session.execute(
        update(Workspace)
        .where(*claim_conditions)
        .values(**values)
        .returning(Workspace.id)
        .execution_options(synchronize_session=False)
    )
    return result.scalar_one_or_none() is not None


async def claim_worker_restart_recovery_execution(
    session: AsyncSession,
    workspace_id: str,
    *,
    owner_id: str,
    lease_expires_at: datetime,
    claim_cutoff: datetime,
) -> Workspace | None:
    """Atomically adopt a worker-restart execution recovery lease."""
    from awf.db.repositories.base import (
        _ACTIVE_RECOVERY_OPERATION_STATUSES,
        _VALIDATE_ONLY_RECOVERY_MODES,
        _WORKER_RESTART_RECOVERY_EXECUTION_CLAIM_STATUSES,
    )

    active_worker_restart_recovery = (
        select(literal(1))
        .select_from(Operation)
        .where(
            Operation.workspace_id == workspace_id,
            Operation.status.in_(_ACTIVE_RECOVERY_OPERATION_STATUSES),
            Operation.payload["source"].as_string() == "worker_restart",
            or_(
                and_(
                    Operation.type == OperationType.validate.value,
                    Operation.payload["recovery_mode"]
                    .as_string()
                    .in_(_VALIDATE_ONLY_RECOVERY_MODES),
                ),
                and_(
                    Operation.type == OperationType.rebase.value,
                    Operation.payload["recovery_mode"].as_string() == "rebase_only",
                ),
            ),
        )
        .exists()
    )
    claim_available = or_(
        Workspace.execution_claimed_by.is_(None),
        Workspace.execution_claim_expires_at.is_(None),
        Workspace.execution_claim_expires_at <= claim_cutoff,
        Workspace.execution_claimed_by == owner_id,
    )
    result = await session.execute(
        update(Workspace)
        .where(
            Workspace.id == workspace_id,
            Workspace.status.in_(_WORKER_RESTART_RECOVERY_EXECUTION_CLAIM_STATUSES),
            active_worker_restart_recovery,
            claim_available,
        )
        # Deliberately *not* bumping ``execution_claim_epoch`` here. Unlike the
        # requested-claim blocks (_apply_execution_claim), the three D3
        # recovery-clear sites, and the stale-clear branch in
        # claim_monitoring_pr, this is the executor-path restart recovery: the
        # executor heartbeat/release pass ``epoch=None`` and run no epoch-gated
        # CAS, so there is no fence to advance past (D6 single-worker safety).
        # Incrementing here would be a no-op the executor never reads.
        .values(
            execution_claimed_by=owner_id,
            execution_claim_expires_at=lease_expires_at,
            updated_at=Workspace.updated_at,
        )
        .returning(Workspace.id)
        .execution_options(synchronize_session=False)
    )
    if result.scalar_one_or_none() is None:
        return None

    stmt = (
        select(Workspace)
        .where(Workspace.id == workspace_id)
        .options(selectinload(Workspace.operations))
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def refresh_monitoring_pr_claim(
    session: AsyncSession,
    workspace_id: str,
    *,
    owner_id: str,
    lease_expires_at: datetime,
) -> bool:
    """Extend this worker's active monitor-recovery lease."""
    result = await session.execute(
        update(Workspace)
        .where(
            Workspace.id == workspace_id,
            Workspace.status == WorkspaceStatus.monitoring_pr.value,
            Workspace.monitor_claimed_by == owner_id,
        )
        .values(
            monitor_claim_expires_at=lease_expires_at,
            updated_at=Workspace.updated_at,
        )
        .returning(Workspace.id)
    )
    return result.scalar_one_or_none() is not None


async def refresh_execution_claim(
    session: AsyncSession,
    workspace_id: str,
    *,
    owner_id: str,
    lease_expires_at: datetime,
    execution_claim_epoch: int | None = None,
) -> bool:
    """Extend this worker's active-execution lease.

    When ``execution_claim_epoch`` is supplied (the provisioning fencing
    path), the update also requires the row's epoch to still match: a later
    claimant always holds a strictly higher epoch, so a stale worker's
    heartbeat updates 0 rows and the caller knows it has been fenced.
    ``None`` preserves the legacy owner-only behavior (the executor path).
    """
    conditions = [
        Workspace.id == workspace_id,
        Workspace.execution_claimed_by == owner_id,
    ]
    if execution_claim_epoch is not None:
        conditions.append(Workspace.execution_claim_epoch == execution_claim_epoch)
    result = await session.execute(
        update(Workspace)
        .where(*conditions)
        .values(
            execution_claim_expires_at=lease_expires_at,
            updated_at=Workspace.updated_at,
        )
        .returning(Workspace.id)
    )
    return result.scalar_one_or_none() is not None


async def release_execution_claim(
    session: AsyncSession,
    workspace_id: str,
    *,
    owner_id: str,
    execution_claim_epoch: int | None = None,
) -> bool:
    """Release this worker's active-execution lease, if it still owns it.

    When ``execution_claim_epoch`` is supplied, the release is gated on the
    epoch as well, so a release issued after a newer claimant has reclaimed
    the row (advancing the epoch) updates 0 rows instead of clobbering the
    new claimant's lease.
    """
    conditions = [
        Workspace.id == workspace_id,
        Workspace.execution_claimed_by == owner_id,
    ]
    if execution_claim_epoch is not None:
        conditions.append(Workspace.execution_claim_epoch == execution_claim_epoch)
    result = await session.execute(
        update(Workspace)
        .where(*conditions)
        .values(
            execution_claimed_by=None,
            execution_claim_expires_at=None,
            updated_at=Workspace.updated_at,
        )
        .returning(Workspace.id)
    )
    return result.scalar_one_or_none() is not None


async def read_execution_claim_epoch(
    session: AsyncSession,
    workspace_id: str,
    *,
    owner_id: str,
) -> int | None:
    """Return the current execution-claim epoch if ``owner_id`` still owns it.

    Returns ``None`` when the row is gone or the claim is no longer held by
    ``owner_id`` (e.g. a newer claimant reclaimed it). The worker reads its
    epoch back at provision start via this method and aborts when it is
    ``None`` (D2).
    """
    result = await session.execute(
        select(Workspace.execution_claim_epoch).where(
            Workspace.id == workspace_id,
            Workspace.execution_claimed_by == owner_id,
        )
    )
    return result.scalar_one_or_none()


async def read_provisioning_execution_claim_epoch(
    session: AsyncSession,
    workspace_id: str,
) -> int | None:
    """Return the execution-claim epoch only while the row is still ``provisioning``.

    A targeted point-read for the provisioner's pre-launch fencing verify
    (D4): selects the single ``execution_claim_epoch`` column rather than
    loading the full ORM row. Returns ``None`` when the workspace is gone or
    has already left ``provisioning`` (in either case the claim no longer
    applies), otherwise the current epoch — which the caller compares against
    the value it was dispatched with.
    """
    result = await session.execute(
        select(Workspace.execution_claim_epoch).where(
            Workspace.id == workspace_id,
            Workspace.status == WorkspaceStatus.provisioning.value,
        )
    )
    return result.scalar_one_or_none()


async def release_monitoring_pr_claim(
    session: AsyncSession,
    workspace_id: str,
    *,
    owner_id: str,
) -> bool:
    """Release this worker's monitor-recovery lease, if it still owns it."""
    result = await session.execute(
        update(Workspace)
        .where(
            Workspace.id == workspace_id,
            Workspace.monitor_claimed_by == owner_id,
        )
        .values(
            monitor_claimed_by=None,
            monitor_claim_expires_at=None,
            updated_at=Workspace.updated_at,
        )
        .returning(Workspace.id)
    )
    return result.scalar_one_or_none() is not None
