"""Close rows owned only by workspaces that stopped on this node long ago.

A terminal workspace must not keep a hosted node non-quiescent forever. Rows of
non-terminal workspaces, recently stopped workspaces, workspaces with a live
claim or an active local task, and rows locked by another writer are never
touched. Every action is audited on the owning workspace with a reason code.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select, update

from awf.control.worker.config import (
    TERMINAL_OWNED_ROW_GRACE,
    effective_worker_config_node_id,
)
from awf.db.enums import OperationStatus
from awf.db.models import (
    CallbackDelivery,
    Operation,
    ResourceReservation,
    TaskAttempt,
    ValidationRun,
    Workspace,
    WorkspaceSecretLease,
)
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.repositories.base import _SECRET_LEASE_REVOCABLE_STATUSES
from awf.db.repositories.quality_repo import ResourceReservationRepository
from awf.db.repositories.workspace_recovery import recovery_allows_work
from awf.db.repositories.workspace_repo_callbacks import CallbackDeliveryRepository
from awf.db.session import session_scope
from awf.service.secret_leases import SecretLeaseService

REASON = "TERMINAL_OWNED_ROW_REAPED"
REAPED_EVENT = "workspace.terminal_owned_rows_reaped"
_TERMINAL = ("completed", "failed", "cancelled", "destroyed")
_MESSAGE = "Owning workspace is terminal and stopped on this node."


def _operations(workspace_id: Any) -> Any:
    return select(Operation).where(
        Operation.workspace_id == workspace_id,
        Operation.status.in_(("pending", "running")),
    )


def _validations(workspace_id: Any) -> Any:
    return select(ValidationRun).where(
        ValidationRun.workspace_id == workspace_id,
        ValidationRun.status.in_(("pending", "running")),
    )


def _leases(workspace_id: Any) -> Any:
    return select(WorkspaceSecretLease).where(
        WorkspaceSecretLease.workspace_id == workspace_id,
        WorkspaceSecretLease.status.in_(_SECRET_LEASE_REVOCABLE_STATUSES),
    )


def _reservations(workspace_id: Any) -> Any:
    return select(ResourceReservation).where(
        ResourceReservation.workspace_id == workspace_id,
        ResourceReservation.released_at.is_(None),
    )


def _attempts(workspace_id: Any) -> Any:
    return select(TaskAttempt).where(
        TaskAttempt.workspace_id == workspace_id,
        TaskAttempt.status.not_in(_TERMINAL),
    )


def _callbacks(workspace_id: Any, overdue: datetime) -> Any:
    # In-flight or backing-off deliveries stay with their dispatcher, so a
    # terminal notification is never dropped while it can still be sent.
    return select(CallbackDelivery).where(
        CallbackDelivery.workspace_id == workspace_id,
        CallbackDelivery.status == "pending",
        or_(
            CallbackDelivery.next_attempt_at.is_(None),
            CallbackDelivery.next_attempt_at <= overdue,
        ),
    )


def _expired_claims(now: datetime) -> Any:
    return or_(
        and_(
            Workspace.execution_claimed_by.is_not(None),
            Workspace.execution_claim_expires_at < now,
        ),
        and_(Workspace.monitor_claimed_by.is_not(None), Workspace.monitor_claim_expires_at < now),
    )


def _live_claim(now: datetime) -> Any:
    # A claim without a past expiry is not proof its holder stopped.
    return or_(
        and_(
            Workspace.execution_claimed_by.is_not(None),
            or_(
                Workspace.execution_claim_expires_at.is_(None),
                Workspace.execution_claim_expires_at >= now,
            ),
        ),
        and_(
            Workspace.monitor_claimed_by.is_not(None),
            or_(
                Workspace.monitor_claim_expires_at.is_(None),
                Workspace.monitor_claim_expires_at >= now,
            ),
        ),
    )


async def reap_terminal_owned_rows(worker: Any, *, limit: int) -> None:
    """Act on at most ``limit`` rows of eligible terminal workspaces."""
    now = datetime.now(UTC)
    stopped_before = now - TERMINAL_OWNED_ROW_GRACE
    active_ids = [key for key, task in worker._execution_tasks.items() if not task.done()]
    eligible = (
        Workspace.status.in_(_TERMINAL),
        Workspace.node_id == effective_worker_config_node_id(worker._config),
        Workspace.updated_at <= stopped_before,
        ~_live_claim(now),
        recovery_allows_work(),
        Workspace.id.not_in(active_ids),
    )
    async with session_scope(worker._session_factory) as session:
        ids = list(
            (
                await session.scalars(
                    select(Workspace.id)
                    .where(
                        *eligible,
                        or_(
                            _expired_claims(now),
                            _operations(Workspace.id).exists(),
                            _validations(Workspace.id).exists(),
                            _leases(Workspace.id).exists(),
                            _reservations(Workspace.id).exists(),
                            _attempts(Workspace.id).exists(),
                            _callbacks(Workspace.id, stopped_before).exists(),
                        ),
                    )
                    .order_by(Workspace.updated_at, Workspace.id)
                    .limit(limit)
                )
            ).all()
        )
    budget = limit
    for workspace_id in ids:
        if budget <= 0:
            return
        async with session_scope(worker._session_factory) as session:
            ws = await session.scalar(
                select(Workspace)
                .where(Workspace.id == workspace_id, *eligible)
                .with_for_update(skip_locked=True)
            )
            task = worker._execution_tasks.get(workspace_id)
            if ws is None or (task is not None and not task.done()):
                continue
            budget -= await _reap_workspace(session, ws, now=now, budget=budget)


async def _locked(session: Any, query: Any, budget: int) -> list[Any]:
    if budget <= 0:
        return []
    return list(
        (await session.scalars(query.limit(budget).with_for_update(skip_locked=True))).all()
    )


async def _all_locked(session: Any, query: Any, budget: int) -> bool:
    """Lock every matching row, or report False when any is held elsewhere or over budget.

    Guards whole-workspace service calls that would otherwise block on, or act
    beyond, rows this run did not lock.
    """
    rows = await _locked(session, query, budget + 1)
    total = len((await session.scalars(query)).all())
    return bool(rows) and len(rows) == total <= budget


async def _reap_workspace(session: Any, ws: Workspace, *, now: datetime, budget: int) -> int:
    repo = WorkspaceRepository(session)
    used = 0

    async def audit(kind: str, action: str, ids: list[str]) -> None:
        nonlocal used
        used += len(ids)
        await repo.add_event(
            ws,
            event_type=REAPED_EVENT,
            reason_code=REASON,
            payload={"kind": kind, "action": action, "ids": ids},
        )

    claims = [
        name
        for name, holder, expiry in (
            ("execution", ws.execution_claimed_by, ws.execution_claim_expires_at),
            ("monitor", ws.monitor_claimed_by, ws.monitor_claim_expires_at),
        )
        if holder is not None and expiry is not None and expiry < now
    ]
    if claims and len(claims) <= budget:
        values: dict[str, Any] = {"updated_at": Workspace.updated_at}
        if "execution" in claims:
            # Fence any stale execution holder exactly as a hosted stop does.
            values.update(
                execution_claimed_by=None,
                execution_claim_expires_at=None,
                execution_claim_epoch=Workspace.execution_claim_epoch + 1,
            )
        if "monitor" in claims:
            values.update(monitor_claimed_by=None, monitor_claim_expires_at=None)
        # Retention GC ages terminal workspaces by updated_at; never defer it.
        await session.execute(update(Workspace).where(Workspace.id == ws.id).values(**values))
        await audit("claim", "released", claims)

    operations = await _locked(session, _operations(ws.id).order_by(Operation.id), budget - used)
    for operation in operations:
        await OperationRepository(session).finish(
            operation,
            status=OperationStatus.failed,
            result=operation.result,
            error_code=REASON,
            error_message=_MESSAGE,
        )
    if operations:
        await audit("operation", "failed", [row.id for row in operations])

    validations = await _locked(
        session, _validations(ws.id).order_by(ValidationRun.id), budget - used
    )
    for validation in validations:
        validation.status = "failed"
        validation.reason_code = REASON
        validation.finished_at = now
    if validations:
        await audit("validation_run", "failed", [row.id for row in validations])

    if await _all_locked(session, _leases(ws.id), budget - used):
        revoked = await SecretLeaseService(session).revoke_workspace_secret_leases(
            ws, now=now, reason_code=REASON
        )
        await audit("secret_lease", "revoked", [row.id for row in revoked])

    if await _all_locked(session, _reservations(ws.id), budget - used):
        released = await ResourceReservationRepository(session).release_active_for_workspace(
            ws.id, released_at=now
        )
        await audit("resource_reservation", "released", [row.id for row in released])

    attempts = await _locked(session, _attempts(ws.id).order_by(TaskAttempt.id), budget - used)
    for attempt in attempts:
        attempt.status = ws.status
    if attempts:
        await audit("task_attempt", ws.status, [row.id for row in attempts])

    callbacks = await _locked(
        session,
        _callbacks(ws.id, now - TERMINAL_OWNED_ROW_GRACE).order_by(CallbackDelivery.id),
        budget - used,
    )
    for callback in callbacks:
        await CallbackDeliveryRepository(session).mark_skipped(
            callback, error_code=REASON, error_message=_MESSAGE, now=now
        )
    if callbacks:
        await audit("callback_delivery", "skipped", [row.id for row in callbacks])
    return used
