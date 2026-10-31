"""Scheduling eligibility for durably contained hosted recovery outcomes."""

from typing import Literal

from sqlalchemy import SQLColumnExpression, and_, func, or_, select
from sqlalchemy.sql.elements import ColumnElement

from awf.db.models import TaskAttempt, Workspace


def restore_row_allows_work(
    family: Literal[
        "tasks", "callback_subscriptions", "callback_deliveries", "service_gc_requests"
    ],
    row_id: SQLColumnExpression[str],
) -> ColumnElement[bool]:
    from awf.hosted_node.lifecycle_models import NodeLifecycleRow
    from awf.hosted_node.restore_reconciliation_models import RestoreRowHold

    return and_(
        ~select(NodeLifecycleRow.singleton)
        .where(NodeLifecycleRow.restore_quarantine.is_not(None))
        .exists(),
        ~select(RestoreRowHold.row_id)
        .where(
            RestoreRowHold.family == family,
            RestoreRowHold.row_id == row_id,
            RestoreRowHold.state == "held",
        )
        .exists(),
    )


def restore_callback_allows_work() -> ColumnElement[bool]:
    from awf.db.models import CallbackDelivery, MergeCandidate, Operation, WorkspaceEvent

    return and_(
        restore_row_allows_work("callback_deliveries", CallbackDelivery.id),
        restore_row_allows_work("callback_subscriptions", CallbackDelivery.subscription_id),
        ~select(Workspace.id)
        .where(
            Workspace.task_policy["hosted_recovery"]["restore_id"].as_string().is_not(None),
            Workspace.task_policy["hosted_recovery"]["disposition"].as_string() == "attention",
            or_(
                Workspace.id == CallbackDelivery.workspace_id,
                select(Operation.id)
                .where(
                    Operation.id == CallbackDelivery.operation_id,
                    Operation.workspace_id == Workspace.id,
                )
                .correlate(Workspace, CallbackDelivery)
                .exists(),
                select(MergeCandidate.id)
                .where(
                    MergeCandidate.id == CallbackDelivery.merge_candidate_id,
                    MergeCandidate.workspace_id == Workspace.id,
                )
                .correlate(Workspace, CallbackDelivery)
                .exists(),
                select(WorkspaceEvent.id)
                .where(
                    WorkspaceEvent.id == CallbackDelivery.source_id,
                    WorkspaceEvent.workspace_id == Workspace.id,
                )
                .correlate(Workspace, CallbackDelivery)
                .exists(),
            ),
        )
        .exists(),
    )


def recovery_allows_work() -> ColumnElement[bool]:
    from awf.hosted_node.lifecycle_models import NodeLifecycleRow
    from awf.hosted_node.restore_reconciliation_models import RestoreRowHold

    return and_(
        ~select(NodeLifecycleRow.singleton)
        .where(NodeLifecycleRow.restore_quarantine.is_not(None))
        .exists(),
        ~select(RestoreRowHold.row_id)
        .join(TaskAttempt, TaskAttempt.task_id == RestoreRowHold.row_id)
        .where(
            RestoreRowHold.family == "tasks",
            RestoreRowHold.state == "held",
            TaskAttempt.workspace_id == Workspace.id,
        )
        .exists(),
        or_(
            and_(
                Workspace.task_policy["hosted_node"].as_string().is_(None),
                Workspace.task_policy["hosted_recovery"]["restore_id"].as_string().is_(None),
            ),
            func.coalesce(Workspace.task_policy["hosted_recovery"]["disposition"].as_string(), "")
            != "attention",
        ),
    )
