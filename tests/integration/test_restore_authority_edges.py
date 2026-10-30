"""Current authority is exact even when a trusted provider returns malformed data."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.restore_reconciliation import validated
from awf.hosted_node.restore_reconciliation_models import RestoreReconciliationRow
from awf.hosted_node.restore_reconciliation_schemas import (
    RestoreInventory,
    RestoreReconciliationEvidence,
    canonical_sha256,
)
from tests.integration.test_checkpoint_marker import schema_revision as schema_revision
from tests.integration.test_node_idle_gate import PREFIX
from tests.integration.test_node_idle_gate import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node
from tests.integration.test_restore_material_staging import resumable_restore as resumable_restore
from tests.integration.test_restore_material_staging import staged


@pytest.mark.parametrize(
    "fault",
    [
        "preimage",
        "action_set",
        "unbound",
        "owner",
        "queued_status",
        "monitor_status",
        "namespace",
        "target",
        "purpose",
        "same_preparation",
        "credential_expired",
        "credential_identity",
        "old_fact",
        "future_fact",
        "decision_duplicate",
        "decision_order",
        "inventory_digest",
        "inventory_order",
    ],
)
async def test_current_provider_cannot_relax_restored_continuation(resumable_restore, fault):
    client, receiver, _, request, captured = resumable_restore
    await staged(resumable_restore)
    inventory = captured[0]
    proof = await receiver.lifecycle.verify_restore_reconciliation(inventory)
    decision = next(item for item in proof.workspaces if item.disposition == "queued")
    row = next(
        item
        for item in inventory.rows
        if item.family == "workspaces" and item.row_id == decision.workspace_id
    )
    if fault in {"preimage", "action_set"}:
        decision = decision.model_copy(
            update={"preimage_sha256" if fault == "preimage" else "action_set_sha256": "0" * 64}
        )
    elif fault in {"unbound", "owner", "queued_status", "monitor_status"}:
        if fault == "monitor_status":
            decision = next(item for item in proof.workspaces if item.disposition == "monitor")
            row = next(
                item
                for item in inventory.rows
                if item.family == "workspaces" and item.row_id == decision.workspace_id
            )
        changes = {
            "unbound": {"hosted": None},
            "owner": {"node_id": "other/node"},
            "queued_status": {"status": "running"},
            "monitor_status": {"status": "running"},
        }[fault]
        inventory = inventory.model_copy(
            update={
                "rows": tuple(
                    item.model_copy(update=changes) if item == row else item
                    for item in inventory.rows
                )
            }
        )
    elif fault in {"old_fact", "future_fact"}:
        when = (
            inventory.binding.point_in_time - timedelta(seconds=1)
            if fault == "old_fact"
            else datetime.now(UTC) + timedelta(seconds=60)
        )
        decision = decision.model_copy(
            update={"fact": decision.fact.model_copy(update={"observed_at": when})}
        )
    elif fault.startswith("decision_"):
        proof = proof.model_copy(
            update={
                "workspaces": (*proof.workspaces, proof.workspaces[0])
                if fault == "decision_duplicate"
                else tuple(reversed(proof.workspaces))
            }
        )
    elif fault.startswith("inventory_"):
        payload = inventory.model_dump(mode="json")
        if fault == "inventory_digest":
            payload["inventory_sha256"] = "0" * 64
        else:
            payload["rows"].reverse()
            payload["inventory_sha256"] = canonical_sha256(
                {key: value for key, value in payload.items() if key != "inventory_sha256"}
            )
        with pytest.raises(ValueError, match="RESTORE_INVENTORY_DIGEST_MISMATCH"):
            RestoreInventory.model_validate(payload)
        return
    else:
        fresh = decision.fresh_association.model_dump(mode="json")
        credential = fresh["credentials"]["agent"]
        if fault == "credential_identity":
            credential["identity"]["boot_epoch"] += 1
            fresh["credentials"]["github"]["identity"] = credential["identity"]
        elif fault == "credential_expired":
            credential["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        elif fault == "same_preparation":
            credential["preparation_id"] = row.hosted.credentials.agent.preparation_id
        elif fault == "target":
            credential["target"] = "/run/awf/secrets/different"
        else:
            credential[fault] = "different"
            if fault == "namespace":
                fresh["credentials"]["github"]["namespace"] = "different"
        if fault == "purpose":
            # A server provider can construct an invalid typed object; Core must
            # revalidate it rather than trusting the Python annotation.
            credentials = decision.fresh_association.credentials
            association = decision.fresh_association.model_copy(
                update={
                    "credentials": credentials.model_copy(
                        update={
                            "agent": credentials.agent.model_copy(update={"purpose": "different"})
                        }
                    )
                }
            )
        else:
            association = decision.fresh_association.model_validate(fresh)
        decision = decision.model_copy(update={"fresh_association": association})
    if not fault.startswith("decision_"):
        proof = proof.model_copy(
            update={
                "workspaces": tuple(
                    decision if item.workspace_id == decision.workspace_id else item
                    for item in proof.workspaces
                )
            }
        )
    with pytest.raises(MaterialDeniedError, match="RESTORE_RECONCILIATION_UNAVAILABLE"):
        validated(inventory, proof)
    if fault not in {"unbound", "owner", "queued_status", "monitor_status"}:

        async def malformed(_inventory):
            return proof

        receiver.lifecycle.verify_restore_reconciliation = malformed
        response = await client.post(PREFIX + "/recovery/acknowledge", json=request)
        assert response.status_code == 503, response.text
    async with receiver.factory() as session:
        assert (await session.scalars(select(RestoreReconciliationRow.state))).all() == ["staged"]


async def test_decision_plan_is_expiry_independent_but_not_authority_independent(resumable_restore):
    from awf.hosted_node.restore_reconciliation_schemas import reconciliation_plan_sha256

    _, receiver, _, _, captured = resumable_restore
    await staged(resumable_restore)
    proof = await receiver.lifecycle.verify_restore_reconciliation(captured[0])
    assert RestoreReconciliationEvidence.model_validate(proof.model_dump()) == proof
    assert reconciliation_plan_sha256(proof) == reconciliation_plan_sha256(
        proof.model_copy(update={"expires_at": datetime.now(UTC) + timedelta(seconds=20)})
    )
    assert reconciliation_plan_sha256(proof) != reconciliation_plan_sha256(
        proof.model_copy(update={"authority_revision": proof.authority_revision + 1})
    )
