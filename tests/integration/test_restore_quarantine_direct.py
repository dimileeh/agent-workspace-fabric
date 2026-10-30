"""Direct trusted boot acceptance retains all classification and schema checks."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, text

from awf.db.models import Workspace
from awf.hosted_node.checkpoint_marker import prepare_marker
from awf.hosted_node.checkpoint_schemas import CheckpointRequest
from awf.hosted_node.identity import MaterialDeniedError, boot
from awf.hosted_node.lifecycle import NodeLifecycle
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.lifecycle_schemas import RecoveryAcknowledgement
from awf.hosted_node.restore_quarantine import accept_restore, deny_restore_materials
from awf.hosted_node.restore_schemas import RestoreEvidence
from awf.hosted_node.retained_work import MAX_RETAINED_WORKSPACES
from tests.integration.test_checkpoint_marker import checkpoint_request
from tests.integration.test_checkpoint_marker import schema_revision as schema_revision
from tests.integration.test_node_idle_gate import evidence
from tests.integration.test_node_idle_gate import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node
from tests.integration.test_restore_quarantine import restore


@pytest.mark.parametrize("hosted", ["valid", "owner", "workspace", "malformed"])
async def test_direct_quarantine_validates_association_and_preserves_business_state(
    ready_node, hosted
):
    _, receiver, workspaces, binding = await restore(ready_node, hosted=hosted)
    value = RecoveryAcknowledgement(idempotency_key=binding.restore_id, receipt_ref="current")
    if hosted == "valid":
        result = await receiver.lifecycle.acknowledge(value)
        assert result.recovery_acceptance.attention_workspaces == 3
        assert (
            result.recovery_acceptance.queued_workspaces
            == result.recovery_acceptance.monitor_workspaces
            == 0
        )
    else:
        with pytest.raises(
            MaterialDeniedError, match="RESTORE_(OWNER_MISMATCH|ASSOCIATION_INVALID)"
        ):
            await receiver.lifecycle.acknowledge(value)
    async with receiver.factory() as session:
        gate = await session.get(NodeLifecycleRow, 1)
        assert gate.restore_quarantine["state"] == ("held" if hosted == "valid" else "pending")
        assert set(await session.scalars(select(Workspace.status))) == {
            "requested",
            "running",
            "monitoring_pr",
        }
        assert len(workspaces) == 3


async def test_direct_restart_reacceptance_does_not_reclassify_held_work(ready_node):
    _, receiver, _, binding = await restore(ready_node)
    value = RecoveryAcknowledgement(idempotency_key=binding.restore_id, receipt_ref="current")
    await receiver.lifecycle.acknowledge(value)
    async with receiver.factory() as session:
        before = await session.scalar(text("SELECT restore_quarantine FROM hosted_node_lifecycle"))
        count = await session.scalar(text("SELECT count(*) FROM workspace_events"))
    receiver.identity = await boot(receiver.config, receiver.factory)
    receiver.lifecycle = NodeLifecycle(receiver.factory, receiver.identity)
    evidence(receiver)

    async def current(identity, restore_id, receipt):
        assert restore_id == binding.restore_id
        return RestoreEvidence(
            binding, identity, receipt, datetime.now(UTC) + timedelta(seconds=10)
        )

    receiver.lifecycle.verify_restore = current
    result = await receiver.lifecycle.acknowledge(value)
    assert result.recovery_acceptance.attention_workspaces == 3 and not result.runtime_ready
    async with receiver.factory() as session:
        assert (
            await session.scalar(text("SELECT restore_quarantine FROM hosted_node_lifecycle"))
            == before
        )
        assert await session.scalar(text("SELECT count(*) FROM workspace_events")) == count


async def test_direct_quarantine_checks_workspace_bound_before_any_classification(ready_node):
    _, receiver, _ = ready_node
    async with receiver.factory.begin() as session:
        await session.execute(
            insert(Workspace),
            [
                {
                    "id": f"ws_limit_{index}",
                    "status": "requested",
                    "agent": "codex",
                    "repo_url": "https://example.invalid/repo",
                    "branch_base": "main",
                    "task_title": "Unknown",
                    "task_prompt": "Never replay",
                    "node_id": receiver.identity.owner,
                }
                for index in range(MAX_RETAINED_WORKSPACES + 1)
            ],
        )
    _, receiver, _, binding = await restore(ready_node)
    with pytest.raises(MaterialDeniedError, match="RESTORE_INVENTORY_UNSUPPORTED"):
        await receiver.lifecycle.acknowledge(
            RecoveryAcknowledgement(idempotency_key=binding.restore_id, receipt_ref="current")
        )
    async with receiver.factory() as session:
        assert (await session.get(NodeLifecycleRow, 1)).recovery_acceptance is None
        assert (
            await session.scalar(
                text("SELECT count(*) FROM workspaces WHERE task_policy::jsonb ? 'hosted_recovery'")
            )
            == 0
        )
        from awf.hosted_node.restore_reconciliation_inventory import inventory

        gate = await session.get(NodeLifecycleRow, 1)
        session.expunge(gate)
        gate.restore_quarantine = {**gate.restore_quarantine, "state": "held"}
        with pytest.raises(MaterialDeniedError, match="RESTORE_INVENTORY_UNSUPPORTED"):
            await inventory(
                session,
                gate,
                RecoveryAcknowledgement(idempotency_key="read-bound", receipt_ref="current"),
            )
        assert (await session.get(NodeLifecycleRow, 1)).restore_quarantine["state"] == "pending"


async def test_direct_checkpoint_checks_schema_and_writes_only_current_marker(ready_node):
    _, receiver, _ = ready_node
    request = CheckpointRequest.model_validate(checkpoint_request()["checkpoint"])
    async with receiver.factory.begin() as session:
        row = await session.get(NodeLifecycleRow, 1)
        await session.execute(text("UPDATE alembic_version SET version_num='wrong'"))
        with pytest.raises(MaterialDeniedError, match="CHECKPOINT_SCHEMA_MISMATCH"):
            await prepare_marker(session, row, 7, request)
        assert row.checkpoint_marker is None
        await session.rollback()
    async with receiver.factory.begin() as session:
        row = await session.get(NodeLifecycleRow, 1)
        await prepare_marker(session, row, 7, request)
        assert row.checkpoint_marker["checkpoint_id"] == request.checkpoint_id


async def test_direct_material_latch_distinguishes_missing_ready_and_restored(ready_node):
    _, receiver, _ = ready_node
    await deny_restore_materials(AsyncMock(get=AsyncMock(return_value=None)))
    async with receiver.factory() as session:
        await deny_restore_materials(session)
    _, receiver, _, _ = await restore(ready_node)
    async with receiver.factory() as session:
        with pytest.raises(MaterialDeniedError, match="RESTORE_QUARANTINED"):
            await deny_restore_materials(session)


@pytest.mark.parametrize("context", [None, {"state": "unknown"}, {"state": "held"}])
async def test_invalid_quarantine_state_cannot_become_fabricated_acceptance(context):
    row = NodeLifecycleRow(restore_quarantine=context)
    session = AsyncMock(scalar=AsyncMock(return_value=True))
    reason = (
        "RESTORE_INVENTORY_UNSUPPORTED"
        if context == {"state": "held"}
        else "RESTORE_STATE_CONFLICT"
    )
    with pytest.raises(MaterialDeniedError, match=reason):
        await accept_restore(
            session, row, RecoveryAcknowledgement(idempotency_key="closed", receipt_ref="current")
        )
    session.execute.assert_not_called()
    assert row.recovery_acceptance is None
