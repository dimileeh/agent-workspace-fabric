"""Trusted in-process callers obey the same closed credential and journal contract."""

import base64
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle_schemas import RecoveryAcknowledgement
from awf.hosted_node.restore_reconciliation_models import RestoreReconciliationRow
from awf.hosted_node.restore_reconciliation_schemas import RestoreMaterialPermit
from awf.hosted_node.schemas import Install, Preparation
from tests.integration.test_checkpoint_marker import schema_revision as schema_revision
from tests.integration.test_node_idle_gate import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node
from tests.integration.test_restore_material_staging import resumable_restore as resumable_restore
from tests.integration.test_restore_material_staging import staged
from tests.integration.test_restore_reconciliation import unknown_decisions
from tests.integration.test_restore_reconciliation_guards import row_holds as row_holds


async def test_direct_staging_is_idempotent_and_replay_never_rechecks_authority(resumable_restore):
    _, receiver, _, request, _ = resumable_restore
    journal = await staged(resumable_restore)
    value = RecoveryAcknowledgement(**request)
    with pytest.raises(MaterialDeniedError, match="RESTORE_MATERIALS_REQUIRED"):
        await receiver.lifecycle.acknowledge(value)
    async with receiver.factory() as session:
        assert list(await session.scalars(select(RestoreReconciliationRow.id))) == [journal.id]
    bindings = [
        binding
        for row in journal.plan["workspaces"]
        if row["fresh_association"]
        for binding in row["fresh_association"]["credentials"].values()
    ]
    preparation = Preparation.model_validate(
        {key: bindings[0][key] for key in Preparation.model_fields}
    )
    with pytest.raises(MaterialDeniedError, match="RESTORE_MATERIAL_AUTHORITY_UNAVAILABLE"):
        await receiver.prepare(preparation)
    permits = []

    async def current(request):
        permits.append(request)
        return RestoreMaterialPermit(
            request=request, expires_at=datetime.now(UTC) + timedelta(seconds=10)
        )

    receiver.verify_restore_material = current
    for binding in bindings:
        await receiver.prepare(
            Preparation.model_validate({key: binding[key] for key in Preparation.model_fields})
        )
        installed = await receiver.install(
            Install.model_validate(
                {**binding, "material": base64.b64encode(b"synthetic-test-only-key").decode()}
            )
        )
        assert installed.status == "current"
    assert len(permits) == 12
    result = await receiver.lifecycle.acknowledge(value)
    assert result.recovery_acceptance.queued_workspaces == 1
    assert result.recovery_acceptance.monitor_workspaces == 1
    assert result.recovery_acceptance.attention_workspaces == 1

    async def no_new_proof(_inventory):
        pytest.fail("committed replay must read durable exact acceptance")

    receiver.lifecycle.verify_restore_reconciliation = no_new_proof
    assert (
        await receiver.lifecycle.acknowledge(value)
    ).recovery_acceptance == result.recovery_acceptance


async def test_same_staged_key_cannot_rebind_to_new_decision(resumable_restore):
    _, receiver, _, request, _ = resumable_restore
    journal = await staged(resumable_restore)
    verify = receiver.lifecycle.verify_restore_reconciliation

    async def changed(inventory):
        return (await verify(inventory)).model_copy(update={"authority_revision": 2})

    receiver.lifecycle.verify_restore_reconciliation = changed
    with pytest.raises(MaterialDeniedError, match="RESTORE_RECONCILIATION_CONFLICT"):
        await receiver.lifecycle.acknowledge(RecoveryAcknowledgement(**request))
    async with receiver.factory() as session:
        assert (
            await session.get(RestoreReconciliationRow, journal.id)
        ).plan_sha256 == journal.plan_sha256


async def test_empty_material_stage_can_be_superseded_only_by_higher_current_decision(row_holds):
    receiver = row_holds
    host = receiver.lifecycle.observe_host
    calls = 0

    async def preflight_only(identity):
        nonlocal calls
        calls += 1
        return await host(identity) if calls == 1 else None

    receiver.lifecycle.observe_host = preflight_only
    proof = None
    revision = 2

    async def current(inventory):
        nonlocal proof
        if proof is None:
            proof = unknown_decisions(inventory).model_copy(update={"authority_revision": revision})
        return proof.model_copy(update={"expires_at": datetime.now(UTC) + timedelta(seconds=10)})

    receiver.lifecycle.verify_restore_reconciliation = current
    with pytest.raises(MaterialDeniedError, match="RESTORE_EVIDENCE_UNAVAILABLE"):
        await receiver.lifecycle.acknowledge(
            RecoveryAcknowledgement(idempotency_key="stage-2", receipt_ref="current-2")
        )
    async with receiver.factory() as session:
        assert sorted(await session.scalars(select(RestoreReconciliationRow.state))) == [
            "committed",
            "staged",
        ]
    receiver.lifecycle.observe_host = host
    proof, revision = None, 3
    result = await receiver.lifecycle.acknowledge(
        RecoveryAcknowledgement(idempotency_key="stage-3", receipt_ref="current-3")
    )
    assert result.recovery_acceptance.attention_workspaces == 4
    async with receiver.factory() as session:
        assert sorted(await session.scalars(select(RestoreReconciliationRow.state))) == [
            "committed",
            "committed",
            "superseded",
        ]
    with pytest.raises(MaterialDeniedError, match="RESTORE_RECONCILIATION_CONFLICT"):
        await receiver.lifecycle.acknowledge(
            RecoveryAcknowledgement(idempotency_key="stage-2", receipt_ref="current-2")
        )
