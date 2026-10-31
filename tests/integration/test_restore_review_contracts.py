"""Current-head review regressions use the actual migrated SQL and HTTP boundary."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select

from awf.db.models import Task
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.lifecycle_schemas import RecoveryAcknowledgement
from awf.hosted_node.models import Grant
from awf.hosted_node.restore_reconciliation_inventory import inventory
from awf.hosted_node.restore_reconciliation_models import RestoreReconciliationRow
from awf.hosted_node.restore_reconciliation_schemas import (
    MAX_RESTORE_ROWS,
    RestoreMaterialPermit,
    RestoreReconciliationEvidence,
)
from tests.integration.test_checkpoint_marker import schema_revision as schema_revision
from tests.integration.test_node_idle_gate import PREFIX
from tests.integration.test_node_idle_gate import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node
from tests.integration.test_restore_material_staging import install_selected, staged
from tests.integration.test_restore_material_staging import resumable_restore as resumable_restore
from tests.integration.test_restore_quarantine import restore


@pytest.mark.parametrize("extra", [0, 1])
async def test_complete_inventory_bound_includes_aggregated_event_rows(ready_node, extra):
    _, receiver, _ = ready_node
    async with receiver.factory.begin() as session:
        await session.execute(
            insert(Task),
            [
                {
                    "id": f"retained-{n}",
                    "repo_url": "https://example.invalid/repo",
                    "base_branch": "main",
                    "title": "Unknown",
                    "prompt": "Never replay",
                }
                for n in range(MAX_RESTORE_ROWS - 7 + extra)
            ],
        )
    client, receiver, _, binding = await restore(ready_node)
    accepted = await client.post(
        PREFIX + "/recovery/acknowledge",
        json={"idempotency_key": binding.restore_id, "receipt_ref": "current"},
    )
    assert accepted.status_code == 200, accepted.text
    request = {"idempotency_key": "complete-inventory-bound", "receipt_ref": "current"}
    if extra:
        response = await client.post(PREFIX + "/recovery/acknowledge", json=request)
        assert response.status_code == 409, response.text
        assert response.json()["error_code"] == "RESTORE_INVENTORY_UNSUPPORTED"
    else:
        async with receiver.factory() as session:
            gate = await session.get(NodeLifecycleRow, 1)
            proposal, _ = await inventory(session, gate, RecoveryAcknowledgement(**request))
            assert len(proposal.rows) == MAX_RESTORE_ROWS
            assert sum(row.family == "workspace_events" for row in proposal.rows) == 3
    async with receiver.factory() as session:
        gate = await session.get(NodeLifecycleRow, 1)
        assert gate.restore_quarantine["state"] == "held"
        assert gate.restore_reconciliation_id is None
        assert await session.scalar(select(RestoreReconciliationRow.id)) is None


@pytest.mark.parametrize("replace_one", [False, True])
async def test_superseding_stage_preserves_exact_reused_materials(resumable_restore, replace_one):
    client, receiver, _, request, captured = resumable_restore
    first = await staged(resumable_restore)

    async def permit(value):
        return RestoreMaterialPermit(
            request=value, expires_at=datetime.now(UTC) + timedelta(seconds=10)
        )

    receiver.verify_restore_material = permit
    await install_selected(client, first.plan)
    previous = receiver.lifecycle.verify_restore_reconciliation
    replaced = None

    async def newer(proposal):
        nonlocal replaced
        raw = (await previous(captured[0])).model_dump(mode="json")
        raw.update(
            idempotency_key=proposal.idempotency_key,
            receipt_ref=proposal.receipt_ref,
            inventory_sha256=proposal.inventory_sha256,
            authority_revision=2,
        )
        if replace_one:
            decision = next(row for row in raw["workspaces"] if row["fresh_association"])
            value = decision["fresh_association"]["credentials"]["agent"]
            replaced = value["workspace_id"], value["preparation_id"]
            value["preparation_id"] += "-replacement"
            value["idempotency_key"] += "-replacement"
        return RestoreReconciliationEvidence.model_validate(raw)

    receiver.lifecycle.verify_restore_reconciliation = newer
    next_request = {"idempotency_key": "current-plan-2", "receipt_ref": "new-current-evidence"}
    response = await client.post(PREFIX + "/recovery/acknowledge", json=next_request)
    if replace_one:
        assert response.status_code == 409, response.text
        assert response.json()["error_code"] == "RESTORE_MATERIALS_REQUIRED"
    else:
        assert response.status_code == 200, response.text
    async with receiver.factory() as session:
        original = await session.get(RestoreReconciliationRow, first.id)
        assert original.state == "superseded" and original.plan_sha256 == first.plan_sha256
        grants = list(await session.scalars(select(Grant)))
        assert sum(row.revoked for row in grants) == int(replace_one)
        assert sum(row.current is not None for row in grants) == 4 - int(replace_one)
        current = (
            await session.scalars(
                select(RestoreReconciliationRow).where(
                    RestoreReconciliationRow.state != "superseded"
                )
            )
        ).one()
    if replace_one:
        assert replaced is not None
        await install_selected(client, current.plan)
        response = await client.post(PREFIX + "/recovery/acknowledge", json=next_request)
        assert response.status_code == 200, response.text
    replay = await client.post(PREFIX + "/recovery/acknowledge", json=request)
    assert replay.status_code == 409, replay.text
    assert replay.json()["error_code"] == "RESTORE_RECONCILIATION_CONFLICT"


async def test_changed_binding_cannot_share_a_retired_preparation(resumable_restore):
    client, receiver, _, request, captured = resumable_restore
    first = await staged(resumable_restore)

    async def permit(value):
        return RestoreMaterialPermit(
            request=value, expires_at=datetime.now(UTC) + timedelta(seconds=10)
        )

    receiver.verify_restore_material = permit
    await install_selected(client, first.plan)
    previous = receiver.lifecycle.verify_restore_reconciliation

    async def changed(proposal):
        raw = (await previous(captured[0])).model_dump(mode="json")
        raw.update(
            idempotency_key=proposal.idempotency_key,
            receipt_ref=proposal.receipt_ref,
            inventory_sha256=proposal.inventory_sha256,
            authority_revision=2,
        )
        decision = next(row for row in raw["workspaces"] if row["fresh_association"])
        decision["fresh_association"]["credentials"]["agent"]["idempotency_key"] += "-changed"
        return RestoreReconciliationEvidence.model_validate(raw)

    receiver.lifecycle.verify_restore_reconciliation = changed
    response = await client.post(
        PREFIX + "/recovery/acknowledge",
        json={
            "idempotency_key": "conflicting-stage",
            "receipt_ref": "current-evidence",
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "RESTORE_RECONCILIATION_CONFLICT"
    async with receiver.factory() as session:
        journals = list(await session.scalars(select(RestoreReconciliationRow)))
        assert len(journals) == 1 and journals[0].id == first.id and journals[0].state == "staged"
        assert all(not row.revoked and row.current for row in await session.scalars(select(Grant)))
    receiver.lifecycle.verify_restore_reconciliation = previous
    response = await client.post(PREFIX + "/recovery/acknowledge", json=request)
    assert response.status_code == 200, response.text
