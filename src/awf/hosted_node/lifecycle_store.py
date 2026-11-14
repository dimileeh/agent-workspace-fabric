"""Boot identity and conservative database quiescence under the node mutex."""

from typing import Any

from sqlalchemy import ColumnElement, and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from awf.hosted_node.config import HostedSettings
from awf.hosted_node.identity import MaterialDeniedError, check
from awf.hosted_node.lifecycle_barrier import GUARDED_TABLES, verify_barrier
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.lifecycle_schemas import QuiescenceBlocker, QuiescenceBlockerName
from awf.hosted_node.schemas import Identity


async def reset_boot(
    session: AsyncSession, config: HostedSettings, identity: Identity, previous: Identity | None
) -> None:
    row = await session.get(NodeLifecycleRow, 1, with_for_update=True)
    if config.lifecycle is None:
        if row is not None:
            raise MaterialDeniedError("NODE_LIFECYCLE_CANNOT_DISABLE")
        return
    await verify_barrier(session)
    if row is None:
        # Activation must wait for writes that observed the previously absent gate.
        await session.execute(
            text("LOCK TABLE " + ", ".join(GUARDED_TABLES) + " IN SHARE ROW EXCLUSIVE MODE")
        )
        row = NodeLifecycleRow(singleton=1, identity=identity.model_dump())
        session.add(row)
        row.previous_identity = previous.model_dump() if previous else None
    else:
        if row.identity["owner"] != identity.owner:
            raise MaterialDeniedError("NODE_OWNER_MISMATCH")
        if (
            row.disk_ref != config.lifecycle.disk_ref
            or row.database_ref != config.lifecycle.database_ref
        ):
            raise MaterialDeniedError("NODE_STORAGE_MISMATCH")
        # An unacknowledged replacement must not turn into a same-VM recovery
        # merely by restarting again. Preserve the unresolved predecessor.
        if row.recovery_key is not None:
            row.previous_identity = row.identity
    row.identity = identity.model_dump()
    row.disk_ref, row.database_ref = config.lifecycle.disk_ref, config.lifecycle.database_ref
    row.phase = "recovering"
    row.recovery_key = row.receipt_ref = None
    row.recovery_acceptance = None
    row.drain_generation = row.idle_since = None
    row.aborted_through = 0
    # Maintenance and its release tombstone survive boot/replacement. Only the
    # new current identity may explicitly release the exact outstanding token.


async def locked(session: AsyncSession, identity: Identity) -> NodeLifecycleRow:
    await check(session, identity, lock=True)
    row = await session.get(NodeLifecycleRow, 1, with_for_update=True)
    if row is None or row.identity != identity.model_dump():
        raise MaterialDeniedError("IDENTITY_MISMATCH", 403)
    return row


DATABASE_BLOCKER_NAMES: tuple[QuiescenceBlockerName, ...] = (
    "WORKSPACE_ACTIVE",
    "WORKSPACE_CLAIMED",
    "COMPOSE_NOT_RELEASED",
    "ATTEMPT_ACTIVE",
    "OPERATION_ACTIVE",
    "VALIDATION_ACTIVE",
    "RESERVATION_HELD",
    "GC_PENDING",
    "CALLBACK_PENDING",
    "MERGE_CANDIDATE_OPEN",
    "SECRET_LEASE_ACTIVE",
)


def _database_predicates() -> tuple[tuple[Any, ColumnElement[bool]], ...]:
    """The conservative idle predicates, in fixed vocabulary order.

    The three workspace names split the historical single OR; their union is
    exactly the old predicate, so ``database_quiescent`` is unchanged.
    """
    # Imports are local to avoid the central ORM registry's hosted-model cycle.
    from awf.db.models import (
        CallbackDelivery,
        MergeCandidate,
        Operation,
        ResourceReservation,
        ServiceGCRequest,
        TaskAttempt,
        ValidationRun,
        Workspace,
        WorkspaceSecretLease,
    )
    from awf.db.repositories.base import terminal_runtime_effectively_released_expr

    terminal = ("completed", "failed", "cancelled", "destroyed")
    return (
        (Workspace.id, Workspace.status.not_in(terminal)),
        (
            Workspace.id,
            or_(
                Workspace.execution_claimed_by.is_not(None),
                Workspace.monitor_claimed_by.is_not(None),
            ),
        ),
        (
            Workspace.id,
            and_(
                Workspace.compose_project_name.is_not(None),
                ~terminal_runtime_effectively_released_expr(correlated_to=Workspace),
            ),
        ),
        (TaskAttempt.id, TaskAttempt.status.not_in(terminal)),
        (Operation.id, Operation.status.not_in(("succeeded", "failed", "cancelled"))),
        (ValidationRun.id, ValidationRun.status.not_in(("succeeded", "failed"))),
        (ResourceReservation.id, ResourceReservation.released_at.is_(None)),
        (
            ServiceGCRequest.id,
            ServiceGCRequest.status.not_in(("completed", "failed", "expired")),
        ),
        (
            CallbackDelivery.id,
            CallbackDelivery.status.not_in(("succeeded", "failed", "skipped")),
        ),
        (MergeCandidate.id, MergeCandidate.status.not_in(("merged", "closed"))),
        (
            WorkspaceSecretLease.id,
            WorkspaceSecretLease.status.not_in(("revoked", "released")),
        ),
    )


async def quiescence_blockers(session: AsyncSession) -> list[QuiescenceBlocker]:
    """Count each idle predicate for diagnostic responses."""
    blockers = []
    predicates = _database_predicates()
    for name, (column, predicate) in zip(DATABASE_BLOCKER_NAMES, predicates, strict=True):
        count = await session.scalar(select(func.count(column)).where(predicate))
        if count:
            blockers.append(QuiescenceBlocker(name=name, count=count))
    return blockers


async def database_quiescent(session: AsyncSession) -> bool:
    # Boolean callers hold the node lock that guarded writes need: probe for
    # existence and stop at the first busy row instead of counting everything.
    for column, predicate in _database_predicates():
        if (await session.execute(select(column).where(predicate).limit(1))).first() is not None:
            return False
    return True
