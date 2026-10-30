"""Actual PostgreSQL rejects unsupported inventory without widening its bounds."""

import pytest
from sqlalchemy import insert, select, text, update

from awf.db.models import CallbackDelivery, Operation, ResourceReservation, Task, ValidationRun
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.lifecycle_schemas import RecoveryAcknowledgement
from awf.hosted_node.restore_reconciliation_inventory import inventory
from awf.hosted_node.restore_reconciliation_schemas import MAX_RESTORE_ROWS
from tests.integration.test_checkpoint_marker import schema_revision as schema_revision
from tests.integration.test_node_idle_gate import PREFIX, create
from tests.integration.test_node_idle_gate import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node
from tests.integration.test_restore_quarantine import restore
from tests.integration.test_restore_row_families import seed_rows


@pytest.mark.parametrize("fault", ["callback_parents", "foreign_reservation", "attempt_workspace"])
async def test_inventory_checks_real_parent_and_owner_relations(ready_node, fault):
    _, receiver, _ = ready_node
    first = await seed_rows(receiver)
    other = await create(receiver.factory)
    async with receiver.factory.begin() as session:
        if fault == "callback_parents":
            session.add(
                Operation(
                    id="other-operation", workspace_id=other.id, type="create", status="succeeded"
                )
            )
            await session.flush()
            await session.execute(
                update(CallbackDelivery).values(
                    workspace_id=first.id, operation_id="other-operation"
                )
            )
        elif fault == "foreign_reservation":
            await session.execute(update(ResourceReservation).values(node_id="foreign/node"))
        else:
            await session.execute(update(ValidationRun).values(workspace_id=other.id))
    client, receiver, _, binding = await restore(ready_node)
    accepted = await client.post(
        PREFIX + "/recovery/acknowledge",
        json={"idempotency_key": binding.restore_id, "receipt_ref": "current"},
    )
    assert accepted.status_code == 200, accepted.text
    reason = (
        "RESTORE_OWNER_MISMATCH"
        if fault == "foreign_reservation"
        else "RESTORE_ASSOCIATION_INVALID"
    )
    async with receiver.factory() as session:
        gate = await session.get(NodeLifecycleRow, 1)
        with pytest.raises(MaterialDeniedError, match=reason):
            await inventory(
                session,
                gate,
                RecoveryAcknowledgement(idempotency_key="unsafe-inventory", receipt_ref="current"),
            )
        assert gate.restore_quarantine["state"] == "held"
        assert (
            await session.scalar(text("SELECT count(*) FROM hosted_restore_reconciliations")) == 0
        )


async def test_real_global_inventory_cap_is_not_expanded_for_restored_history(ready_node):
    _, receiver, _ = ready_node
    async with receiver.factory.begin() as session:
        await session.execute(
            insert(Task),
            [
                {
                    "id": f"retained-{n}",
                    "repo_url": "https://example.invalid/repo",
                    "base_branch": "main",
                    "title": "Unknown action",
                    "prompt": "Do not replay",
                }
                for n in range(MAX_RESTORE_ROWS + 1)
            ],
        )
    client, receiver, _, binding = await restore(ready_node)
    accepted = await client.post(
        PREFIX + "/recovery/acknowledge",
        json={"idempotency_key": binding.restore_id, "receipt_ref": "current"},
    )
    assert accepted.status_code == 200, accepted.text
    async with receiver.factory() as session:
        gate = await session.get(NodeLifecycleRow, 1)
        with pytest.raises(MaterialDeniedError, match="RESTORE_INVENTORY_UNSUPPORTED"):
            await inventory(
                session,
                gate,
                RecoveryAcknowledgement(idempotency_key="inventory-cap", receipt_ref="current"),
            )
        assert len((await session.scalars(select(Task.id))).all()) == MAX_RESTORE_ROWS + 1
        assert gate.restore_quarantine["state"] == "held" and gate.restore_reconciliation_id is None
