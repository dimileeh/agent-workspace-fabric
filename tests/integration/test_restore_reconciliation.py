"""Actual HTTP/SQL restore exit: references never substitute for current proof."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.models import Grant, PreparationRow
from tests.integration.test_checkpoint_marker import schema_revision as schema_revision
from tests.integration.test_node_idle_gate import PREFIX, create
from tests.integration.test_node_idle_gate import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node
from tests.integration.test_restore_quarantine import restore


async def test_held_restore_requires_distinct_current_reconciliation_proof(ready_node):
    client, receiver, _, binding = await restore(ready_node)
    original = {"idempotency_key": binding.restore_id, "receipt_ref": "restore-receipt"}
    accepted = await client.post(PREFIX + "/recovery/acknowledge", json=original)
    assert accepted.status_code == 200, accepted.text
    before = accepted.json()["recovery_acceptance"]
    response = await client.post(
        PREFIX + "/recovery/acknowledge",
        json={"idempotency_key": "reconcile-1", "receipt_ref": "current-cloud-decision"},
    )
    assert response.status_code == 503, response.text
    assert response.json()["error_code"] == "RESTORE_RECONCILIATION_UNAVAILABLE"
    async with receiver.factory() as session:
        gate = await session.get(NodeLifecycleRow, 1)
        assert gate.restore_quarantine["state"] == "held"
        assert gate.recovery_key == original["idempotency_key"]
        assert gate.recovery_acceptance == before
    replay = await client.post(PREFIX + "/recovery/acknowledge", json=original)
    assert replay.status_code == 200 and replay.json()["admission_closed"]
    assert replay.json()["recovery_acceptance"] == before


async def test_malformed_restore_authority_is_structured_and_atomic(ready_node):
    _, receiver, _ = ready_node
    async with receiver.factory.begin() as session:
        session.add(
            PreparationRow(
                id="malformed-preparation",
                owner=receiver.identity.owner,
                namespace="old",
                workspace_id="unknown",
                preparation_id="old",
                binding={"identity": receiver.identity.model_dump()},
            )
        )
        session.add(
            Grant(
                id="malformed-grant",
                preparation="malformed-preparation",
                grant_id="old",
                fence=1,
                revision=1,
                authorization_generation=1,
            )
        )
    client, receiver, _, binding = await restore(ready_node)
    response = await client.post(
        PREFIX + "/recovery/acknowledge",
        json={"idempotency_key": binding.restore_id, "receipt_ref": "restore-receipt"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "RESTORE_INVENTORY_UNSUPPORTED"
    async with receiver.factory() as session:
        gate = await session.get(NodeLifecycleRow, 1)
        assert gate.restore_quarantine["state"] == "pending"
        assert gate.recovery_key is None and gate.recovery_acceptance is None
        assert not await session.scalar(select(Grant.revoked).where(Grant.id == "malformed-grant"))


def unknown_decisions(inventory):
    from awf.hosted_node.restore_reconciliation_schemas import (
        RestoreActionEvidence,
        RestoreReconciliationEvidence,
        RestoreRowDecision,
        RestoreWorkspaceDecision,
        action_set_sha256,
    )

    def fact(row):
        return RestoreActionEvidence(
            action_id=row.row_id,
            outcome="unknown",
            evidence_ref="current-nonrewound-unknown",
            evidence_sha256="b" * 64,
            observed_at=datetime.now(UTC),
        )

    workspaces = tuple(
        RestoreWorkspaceDecision(
            workspace_id=row.row_id,
            preimage_sha256=row.preimage_sha256,
            action_set_sha256=action_set_sha256(inventory.rows, row.row_id),
            disposition="held",
            fact=fact(row),
        )
        for row in inventory.rows
        if row.family == "workspaces"
    )
    rows = tuple(
        RestoreRowDecision(
            family=row.family,
            row_id=row.row_id,
            preimage_sha256=row.preimage_sha256,
            disposition="held",
            fact=fact(row),
        )
        for row in inventory.rows
        if row.family
        in {"tasks", "callback_subscriptions", "callback_deliveries", "service_gc_requests"}
        and row.workspace_id is None
    )
    return RestoreReconciliationEvidence(
        binding=inventory.binding,
        identity=inventory.identity,
        idempotency_key=inventory.idempotency_key,
        receipt_ref=inventory.receipt_ref,
        prior_revision=inventory.prior_revision,
        inventory_sha256=inventory.inventory_sha256,
        authority_revision=1,
        authorization_sha256="a" * 64,
        external_facts_sha256="b" * 64,
        post_checkpoint_frontier_sha256="c" * 64,
        workspaces=workspaces,
        rows=rows,
        expires_at=datetime.now(UTC) + timedelta(seconds=10),
    )


async def test_current_unknowns_can_remain_specific_while_new_work_admits(ready_node):
    client, receiver, workspaces, binding = await restore(ready_node)
    original = {"idempotency_key": binding.restore_id, "receipt_ref": "restore-receipt"}
    assert (await client.post(PREFIX + "/recovery/acknowledge", json=original)).status_code == 200
    seen = []
    decision = None

    async def current(inventory):
        nonlocal decision
        seen.append(inventory)
        if decision is None:
            decision = unknown_decisions(inventory)
        return decision.model_copy(update={"expires_at": datetime.now(UTC) + timedelta(seconds=10)})

    receiver.lifecycle.verify_restore_reconciliation = current
    request = {"idempotency_key": "reconcile-1", "receipt_ref": "current-cloud-decision"}
    response = await client.post(PREFIX + "/recovery/acknowledge", json=request)
    assert response.status_code == 200, response.text
    assert seen and seen[0].initial and seen[0].prior_revision == 0
    assert response.json()["admission_closed"]
    assert response.json()["recovery_acceptance"]["idempotency_key"] == "reconcile-1"
    async with receiver.factory() as session:
        gate = await session.get(NodeLifecycleRow, 1)
        assert gate.restore_quarantine is None and gate.restore_reconciliation_id
        assert gate.recovery_key == original["idempotency_key"]
    released = await client.post(PREFIX + "/maintenance/release", json={"generation": 7})
    assert released.status_code == 200 and not released.json()["admission_closed"]
    await create(receiver.factory)
    with pytest.raises(DBAPIError, match="RECOVERY_ATTENTION_REQUIRED"):
        async with receiver.factory.begin() as session:
            await session.execute(
                text("UPDATE workspaces SET task_title='unsafe' WHERE id=:id"),
                {"id": workspaces[0].id},
            )
    replay = await client.post(PREFIX + "/recovery/acknowledge", json=request)
    assert replay.status_code == 200
    assert replay.json()["recovery_acceptance"] == response.json()["recovery_acceptance"]
