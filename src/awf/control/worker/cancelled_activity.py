"""Settle abandoned monitor bookkeeping after cancelled runtimes are released."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select

from awf.control.worker.config import effective_worker_config_node_id
from awf.db.enums import OperationStatus
from awf.db.models import Operation, ValidationRun, Workspace
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.repositories.base import terminal_runtime_effectively_released_expr
from awf.db.repositories.workspace_recovery import recovery_allows_work
from awf.db.session import session_scope

_REASON = "WORKSPACE_CANCELLED"
_BATCH_SIZE = 200


def _monitor_operations(workspace_id: Any) -> Any:
    return select(Operation).where(
        Operation.workspace_id == workspace_id,
        Operation.status.in_(("pending", "running")),
        or_(
            Operation.payload["owner"].as_string() == "pr_monitor",
            Operation.payload["source"].as_string() == "provider_connection_test",
        ),
    )


def _validations(workspace_id: Any) -> Any:
    return select(ValidationRun).where(
        ValidationRun.workspace_id == workspace_id,
        ValidationRun.status.in_(("pending", "running")),
    )


async def reconcile_cancelled_activity(worker: Any, *, limit: int | None) -> None:
    """Use the existing bounded cleanup scan, never infer success from cancellation."""
    active_ids = [key for key, task in worker._execution_tasks.items() if not task.done()]
    eligible = (
        Workspace.status == "cancelled",
        Workspace.node_id == effective_worker_config_node_id(worker._config),
        Workspace.execution_claimed_by.is_(None),
        Workspace.monitor_claimed_by.is_(None),
        recovery_allows_work(),
        terminal_runtime_effectively_released_expr(correlated_to=Workspace),
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
                            _monitor_operations(Workspace.id).exists(),
                            _validations(Workspace.id).exists(),
                        ),
                    )
                    .order_by(Workspace.updated_at, Workspace.id)
                    .limit(limit or _BATCH_SIZE)
                )
            ).all()
        )
    for workspace_id in ids:
        async with session_scope(worker._session_factory) as session:
            ws = await session.scalar(
                select(Workspace)
                .where(Workspace.id == workspace_id, *eligible)
                .with_for_update(skip_locked=True)
            )
            task = worker._execution_tasks.get(workspace_id)
            if ws is None or (task is not None and not task.done()):
                continue
            # Lock children without waiting for a control action holding them in
            # the opposite order. Recheck status under each lock; skipped rows
            # remain non-quiescent and are retried by the next ordinary scan.
            operations = list(
                (
                    await session.scalars(
                        _monitor_operations(workspace_id)
                        .order_by(Operation.id)
                        .limit(_BATCH_SIZE)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            validations = list(
                (
                    await session.scalars(
                        _validations(workspace_id)
                        .order_by(ValidationRun.id)
                        .limit(_BATCH_SIZE)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            from awf.service.provider_diagnostics import record_diagnostic_cancellation

            record_diagnostic_cancellation(ws)
            for operation in operations:
                await OperationRepository(session).finish(
                    operation,
                    status=OperationStatus.cancelled,
                    result=operation.result,
                    error_code=_REASON,
                    error_message="Workspace cancelled; runtime released and worker task stopped.",
                )
            for validation in validations:
                validation.status = "failed"
                validation.reason_code = _REASON
                validation.finished_at = datetime.now(UTC)
            if operations or validations:
                await WorkspaceRepository(session).add_event(
                    ws,
                    event_type="workspace.cancelled_activity_settled",
                    reason_code=_REASON,
                    payload={
                        "operation_ids": [op.id for op in operations],
                        "validation_run_ids": [run.id for run in validations],
                    },
                )
