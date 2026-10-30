"""Reachable restored cuts retain their marker until current reconciliation."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from awf.hosted_node.checkpoint_schemas import CheckpointMarker
from awf.hosted_node.identity import MaterialDeniedError, boot
from awf.hosted_node.lifecycle import NodeLifecycle
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.restore_schemas import RestoreBinding, RestoreBootEvidence, RestoreEvidence
from tests.integration.test_checkpoint_marker import checkpoint_request, marker
from tests.integration.test_checkpoint_restore_join import migrated_database as migrated_database
from tests.integration.test_closed_host_database import runtime_database as runtime_database
from tests.integration.test_closed_host_runtime import composed as composed
from tests.integration.test_node_idle_gate import PREFIX, create, evidence, terminal_workspace
from tests.integration.test_restore_reconciliation import unknown_decisions
from tests.integration.test_restore_resumption_join import ready_node as ready_node


@pytest.fixture(params=["empty", "terminal_history"])
async def restored_node(ready_node, request):
    client, receiver, _ = ready_node
    if request.param == "terminal_history":
        await terminal_workspace(receiver)
    checkpoint = checkpoint_request()
    prepared = await client.post(PREFIX + "/maintenance/begin", json=checkpoint)
    assert prepared.status_code == 200, prepared.text
    recorded = CheckpointMarker.model_validate(await marker(receiver))
    binding = RestoreBinding(
        restore_id=str(uuid4()),
        checkpoint=recorded,
        manifest_sha256="b" * 64,
        files_sha256="c" * 64,
        schema_sha256="d" * 64,
        point_in_time=datetime.now(UTC),
        storage_mapping_sha256="e" * 64,
        fenced_identity=receiver.identity.model_copy(
            update={"incarnation": "123", "recovery_generation": 2, "boot_epoch": 90}
        ),
        candidate_incarnation="456",
        candidate_recovery_generation=3,
        boot_epoch_floor=90,
        target_disk_ref=recorded.disk_ref,
        target_database_ref=recorded.database_ref,
    )
    config = receiver.config.model_copy(update={"incarnation": "456", "recovery_generation": 3})
    identity = await boot(
        config,
        receiver.factory,
        restore=RestoreBootEvidence(binding, datetime.now(UTC) + timedelta(seconds=30)),
    )
    receiver.config, receiver.identity = config, identity
    receiver.lifecycle = NodeLifecycle(receiver.factory, identity)
    evidence(receiver)

    async def verify(actual, restore_id, receipt):
        assert actual == identity and restore_id == binding.restore_id
        return RestoreEvidence(
            binding, identity, receipt, datetime.now(UTC) + timedelta(seconds=10)
        )

    async def runtime(actual):
        assert actual == identity
        return True

    receiver.lifecycle.verify_restore = verify
    receiver.lifecycle.observe_runtime = runtime
    client.headers.update(identity.headers())
    return client, receiver, checkpoint, binding


async def persistent_state(receiver):
    async with receiver.factory() as session:
        row = await session.get(NodeLifecycleRow, 1)
        return {
            name: getattr(row, name)
            for name in (
                "phase",
                "drain_generation",
                "maintenance_generation",
                "maintenance_released_through",
                "checkpoint_marker",
                "restore_quarantine",
                "restore_reconciliation_id",
                "recovery_key",
                "receipt_ref",
                "recovery_acceptance",
            )
        }


async def accept(client, binding):
    response = await client.post(
        PREFIX + "/recovery/acknowledge",
        json={"idempotency_key": binding.restore_id, "receipt_ref": "restore-receipt"},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def reconcile(client, receiver):
    async def current(inventory):
        # No actionable rows exist in these cuts; still require current authority.
        assert not inventory.rows
        return unknown_decisions(inventory)

    receiver.lifecycle.verify_restore_reconciliation = current
    response = await client.post(
        PREFIX + "/recovery/acknowledge",
        json={"idempotency_key": "reconcile-empty", "receipt_ref": "current-cloud-proof"},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_unreconciled_restore_is_not_idle_despite_empty_database(restored_node):
    client, receiver, _, binding = restored_node
    pending = (await client.get(PREFIX + "/readiness")).json()
    assert pending["database_quiescent"] and not pending["quiescent"]
    accepted = await accept(client, binding)
    held = await persistent_state(receiver)
    assert held["restore_quarantine"]["state"] == "held"
    assert held["checkpoint_marker"] == binding.checkpoint.model_dump(mode="json")
    for status in (
        accepted,
        (await client.get(PREFIX + "/readiness")).json(),
        await accept(client, binding),
    ):
        assert status["database_quiescent"] and status["node_ready"]
        assert status["admission_closed"] and not status["runtime_ready"]
        assert not status["quiescent"]
        assert status["idle_since"] is None
        assert status["state"] == "ready" and status["drain_generation"] is None
    assert await persistent_state(receiver) == held


async def test_unreconciled_restore_cannot_drain_or_replay_into_drain(restored_node):
    client, receiver, _, binding = restored_node
    pending = await persistent_state(receiver)
    response = await client.post(PREFIX + "/drain", json={"generation": 1})
    assert response.status_code == 409 and response.json()["error_code"] == "RECOVERY_REQUIRED"
    assert await persistent_state(receiver) == pending
    await accept(client, binding)
    held = await persistent_state(receiver)
    with pytest.raises(MaterialDeniedError, match="RESTORE_QUARANTINED"):
        await receiver.lifecycle.drain(1)
    assert await persistent_state(receiver) == held
    for generation in (1, 1, 2):
        response = await client.post(PREFIX + "/drain", json={"generation": generation})
        assert response.status_code == 409, response.text
        assert response.json()["error_code"] == "RESTORE_QUARANTINED"
        assert await persistent_state(receiver) == held
    aborted = await client.post(PREFIX + "/drain/abort", json={"generation": 2})
    assert aborted.status_code == 200 and aborted.json()["admission_closed"]
    assert not aborted.json()["quiescent"]
    assert await persistent_state(receiver) == held
    assert (await accept(client, binding))["state"] == "ready"
    assert await persistent_state(receiver) == held


@pytest.mark.parametrize("accepted", [False, True])
async def test_valid_restore_marker_prevents_maintenance_rebinding(restored_node, accepted):
    client, receiver, checkpoint, binding = restored_node
    if accepted:
        await accept(client, binding)
    before = await persistent_state(receiver)
    assert before["maintenance_generation"] == 7
    assert before["maintenance_released_through"] < 7
    assert before["checkpoint_marker"] == binding.checkpoint.model_dump(mode="json")
    # Ordinary same-generation begin is a replay, not a new hold.
    for _ in range(2):
        response = await client.post(PREFIX + "/maintenance/begin", json={"generation": 7})
        assert response.status_code == 200, response.text
        assert response.json()["admission_closed"]
        assert await persistent_state(receiver) == before
    for request, reason in (
        ({"generation": 6}, "MAINTENANCE_GENERATION_STALE"),
        ({"generation": 8}, "CHECKPOINT_CONFLICT"),
        (checkpoint, "CHECKPOINT_CONFLICT"),
        (checkpoint_request(), "CHECKPOINT_CONFLICT"),
        (checkpoint_request(8), "CHECKPOINT_CONFLICT"),
    ):
        response = await client.post(PREFIX + "/maintenance/begin", json=request)
        assert response.status_code == 409, response.text
        assert response.json()["error_code"] == reason
        assert await persistent_state(receiver) == before
    for generation in (7, 8):
        response = await client.post(
            PREFIX + "/maintenance/release", json={"generation": generation}
        )
        assert (
            response.status_code == 409 and response.json()["error_code"] == "RESTORE_QUARANTINED"
        )
        assert await persistent_state(receiver) == before
    await accept(client, binding)
    await reconcile(client, receiver)
    released = await client.post(PREFIX + "/maintenance/release", json={"generation": 7})
    assert released.status_code == 200 and not released.json()["admission_closed"]
    assert await marker(receiver) == before["checkpoint_marker"]
    assert (
        await client.post(PREFIX + "/maintenance/begin", json={"generation": 8})
    ).status_code == 200
    assert (
        await client.post(PREFIX + "/maintenance/release", json={"generation": 8})
    ).status_code == 200


async def test_committed_reconciliation_restores_idle_and_drain_semantics(restored_node):
    client, receiver, _, binding = restored_node
    await accept(client, binding)
    result = await reconcile(client, receiver)
    committed = await persistent_state(receiver)
    assert committed["restore_quarantine"] is None and committed["restore_reconciliation_id"]
    assert result["quiescent"] and result["database_quiescent"] and result["idle_since"]
    assert result["node_ready"] and result["runtime_ready"] and result["admission_closed"]
    for _ in range(2):
        drained = await client.post(PREFIX + "/drain", json={"generation": 1})
        assert drained.status_code == 200, drained.text
        assert drained.json()["state"] == "drained" and drained.json()["quiescent"]
        assert drained.json()["maintenance_generation"] == 7
        assert (await accept(client, binding))["state"] == "drained"
        assert (await reconcile(client, receiver))["state"] == "drained"
    for _ in range(2):
        released = await client.post(PREFIX + "/maintenance/release", json={"generation": 7})
        assert released.status_code == 200 and released.json()["admission_closed"]
        assert released.json()["state"] == "drained"
    aborted = await client.post(PREFIX + "/drain/abort", json={"generation": 1})
    assert aborted.status_code == 200 and not aborted.json()["admission_closed"]
    assert (await client.post(PREFIX + "/drain", json={"generation": 1})).status_code == 409
    await create(receiver.factory)
    assert not (await client.get(PREFIX + "/readiness")).json()["quiescent"]
