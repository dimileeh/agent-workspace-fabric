"""A pending callback owns its exact historical parent, not the whole node."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from awf.common.ids import new_event_id
from awf.db.models import (
    CallbackDelivery,
    CallbackSubscription,
    MergeCandidate,
    Operation,
    Task,
    TaskAttempt,
    Workspace,
    WorkspaceEvent,
)
from tests.integration.test_checkpoint_marker import schema_revision as schema_revision
from tests.integration.test_node_idle_gate import PREFIX, create
from tests.integration.test_node_idle_gate import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node
from tests.integration.test_restore_quarantine import restore
from tests.integration.test_restore_reconciliation import unknown_decisions


@pytest.mark.parametrize("parent", ["operation", "merge", "event"])
async def test_uncertain_callback_parks_its_terminal_parent_without_blocking_node(
    ready_node, parent
):
    _, receiver, _ = ready_node
    workspace = await create(receiver.factory)
    event_id = new_event_id()
    async with receiver.factory.begin() as session:
        await session.execute(
            update(Workspace).where(Workspace.id == workspace.id).values(status="completed")
        )
        session.add(
            Task(
                id="historic-task",
                repo_url="https://example.invalid/repo",
                base_branch="main",
                title="Historic",
                prompt="Never replay",
            )
        )
        await session.flush()
        session.add(
            TaskAttempt(
                id="historic-attempt",
                task_id="historic-task",
                workspace_id=workspace.id,
                attempt_number=1,
                agent="codex",
                status="completed",
                title="Historic",
                repo_url="https://example.invalid/repo",
                base_branch="main",
            )
        )
        session.add(
            Operation(
                id="historic-operation",
                workspace_id=workspace.id,
                type="execute",
                status="succeeded",
            )
        )
        session.add(
            WorkspaceEvent(
                id=event_id,
                workspace_id=workspace.id,
                event_type="workspace.completed",
                new_state="completed",
                event_order=1,
            )
        )
        await session.flush()
        session.add(
            MergeCandidate(
                id="historic-merge",
                task_id="historic-task",
                attempt_id="historic-attempt",
                workspace_id=workspace.id,
                pr_url="https://example.invalid/pull/1",
                repo_url="https://example.invalid/repo",
                base_branch="main",
                status="merged",
            )
        )
        session.add(
            CallbackSubscription(
                id="history-subscription",
                name="retained",
                target_url="https://example.invalid/callback",
                timeout_seconds=1,
                max_attempts=1,
                initial_backoff_seconds=1,
                idempotency_key="history",
                request_hash="history",
            )
        )
        await session.flush()
        session.add(
            CallbackDelivery(
                id="historic-callback",
                subscription_id="history-subscription",
                workspace_id=None,
                operation_id="historic-operation" if parent == "operation" else None,
                merge_candidate_id="historic-merge" if parent == "merge" else None,
                source_id=event_id if parent == "event" else "historic-source",
                event_kind="workspace",
                event_type="workspace.completed",
                dedupe_key="history",
                idempotency_key="history",
                max_attempts=1,
                status="pending",
            )
        )
    client, receiver, _, binding = await restore(ready_node)
    initial = await client.post(
        PREFIX + "/recovery/acknowledge",
        json={"idempotency_key": binding.restore_id, "receipt_ref": "restore-proof"},
    )
    assert initial.status_code == 200, initial.text
    assert initial.json()["recovery_acceptance"]["attention_workspaces"] == 4
    proof = None

    async def current(inventory):
        nonlocal proof
        callback = next(row for row in inventory.rows if row.family == "callback_deliveries")
        assert callback.workspace_id == workspace.id
        if proof is None:
            proof = unknown_decisions(inventory)
        return proof.model_copy(update={"expires_at": datetime.now(UTC) + timedelta(seconds=10)})

    receiver.lifecycle.verify_restore_reconciliation = current
    response = await client.post(
        PREFIX + "/recovery/acknowledge",
        json={"idempotency_key": "current-history", "receipt_ref": "current-history-proof"},
    )
    assert response.status_code == 200, response.text
    assert (
        await client.post(PREFIX + "/maintenance/release", json={"generation": 7})
    ).status_code == 200
    await create(receiver.factory)
    async with receiver.factory() as session:
        row = await session.get(Workspace, workspace.id)
        assert (
            row.status == "completed"
            and row.task_policy["hosted_recovery"]["disposition"] == "attention"
        )
        assert await session.scalar(select(CallbackDelivery.status)) == "pending"
        assert (await session.get(WorkspaceEvent, event_id)).new_state == "completed"
