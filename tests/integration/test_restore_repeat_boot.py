"""Committed restore fences survive another boot and another restore."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from awf.db.models import CallbackDelivery, ServiceGCRequest, Task, Workspace
from awf.hosted_node.checkpoint_schemas import CheckpointMarker
from awf.hosted_node.identity import boot
from awf.hosted_node.lifecycle import NodeLifecycle
from awf.hosted_node.lifecycle_barrier import GUARDED_TABLES
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.lifecycle_schemas import RecoveryAcknowledgement
from awf.hosted_node.restore_reconciliation_models import RestoreReconciliationRow, RestoreRowHold
from awf.hosted_node.restore_schemas import RestoreBinding, RestoreBootEvidence, RestoreEvidence
from tests.integration.test_checkpoint_marker import checkpoint_request, marker
from tests.integration.test_checkpoint_marker import schema_revision as schema_revision
from tests.integration.test_node_idle_gate import PREFIX, create, evidence
from tests.integration.test_node_idle_gate import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node
from tests.integration.test_restore_orphan_edges import decision_plan
from tests.integration.test_restore_reconciliation_guards import row_holds as row_holds


@pytest.mark.parametrize("family", GUARDED_TABLES)
async def test_service_restart_preserves_committed_restore_attention_and_all_fences(
    row_holds, ready_node, family
):
    client, receiver, _ = ready_node
    assert receiver is row_holds
    async with receiver.factory() as session:
        before = list(
            (await session.execute(text("SELECT id,task_policy FROM workspaces ORDER BY id"))).all()
        )
        holds = list(
            (
                await session.execute(
                    text(
                        "SELECT to_jsonb(h) FROM hosted_restore_row_holds h ORDER BY family,row_id"
                    )
                )
            ).scalars()
        )
    receiver.identity = await boot(receiver.config, receiver.factory)
    receiver.lifecycle = NodeLifecycle(receiver.factory, receiver.identity)
    evidence(receiver)
    client.headers.update(receiver.identity.headers())
    request = {"idempotency_key": "service-restart", "receipt_ref": "fresh-physical-proof"}
    response = await client.post(PREFIX + "/recovery/acknowledge", json=request)
    assert response.status_code == 200, response.text
    assert response.json()["recovery_acceptance"]["attention_workspaces"] == 4
    assert (await client.post(PREFIX + "/recovery/acknowledge", json=request)).status_code == 200
    async with receiver.factory() as session:
        assert (
            list(
                (
                    await session.execute(text("SELECT id,task_policy FROM workspaces ORDER BY id"))
                ).all()
            )
            == before
        )
        assert (
            list(
                (
                    await session.execute(
                        text(
                            "SELECT to_jsonb(h) FROM hosted_restore_row_holds h ORDER BY family,row_id"
                        )
                    )
                ).scalars()
            )
            == holds
        )
    with pytest.raises(DBAPIError, match="RESTORE_ROW_HELD|RECOVERY_ATTENTION_REQUIRED"):
        async with receiver.factory.begin() as session:
            await session.execute(text(f"UPDATE {family} SET id=id"))
    await create(receiver.factory)


@pytest.mark.parametrize("family", ["tasks", "callback_deliveries", "service_gc_requests"])
async def test_restore_history_is_not_a_blanket_exception_for_new_unowned_work(
    row_holds, ready_node, family
):
    client, receiver, _ = ready_node
    async with receiver.factory.begin() as session:
        if family == "tasks":
            session.add(
                Task(
                    id="unheld",
                    repo_url="https://example.invalid/repo",
                    base_branch="main",
                    title="Unknown",
                    prompt="Do not replay",
                )
            )
        elif family == "callback_deliveries":
            from awf.db.models import CallbackSubscription

            session.add(
                CallbackSubscription(
                    id="unheld",
                    name="unknown",
                    target_url="https://example.invalid/callback",
                    idempotency_key="unheld",
                    request_hash="unheld",
                    timeout_seconds=1,
                    max_attempts=1,
                    initial_backoff_seconds=1,
                )
            )
            await session.flush()
            session.add(
                CallbackDelivery(
                    id="unheld",
                    subscription_id="unheld",
                    event_kind="workspace",
                    event_type="workspace.created",
                    source_id="unknown",
                    dedupe_key="unheld",
                    idempotency_key="unheld",
                    max_attempts=1,
                    status="pending",
                )
            )
        else:
            session.add(
                ServiceGCRequest(
                    id="unheld",
                    status="pending",
                    params={},
                    requested_at=datetime.now(UTC),
                    deadline_at=datetime.now(UTC) + timedelta(minutes=1),
                )
            )
    receiver.identity = await boot(receiver.config, receiver.factory)
    receiver.lifecycle = NodeLifecycle(receiver.factory, receiver.identity)
    evidence(receiver)
    client.headers.update(receiver.identity.headers())
    response = await client.post(
        PREFIX + "/recovery/acknowledge",
        json={"idempotency_key": "unheld-restart", "receipt_ref": "fresh-physical-proof"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "RECOVERY_OUTCOME_UNRESOLVED"
    async with receiver.factory() as session:
        assert await session.scalar(text("SELECT recovery_key FROM hosted_node_lifecycle")) is None
        assert set(await session.scalars(select(RestoreRowHold.state))) == {"held"}


@pytest.mark.parametrize("fault", ["previous", "floor", "disk", "database"])
async def test_restore_containment_requires_exact_committed_storage_and_epoch(row_holds, fault):
    from awf.hosted_node.identity import MaterialDeniedError
    from awf.hosted_node.retained_work import inspect_retained_work

    receiver = row_holds
    receiver.identity = await boot(receiver.config, receiver.factory)
    async with receiver.factory() as session:
        gate = await session.get(NodeLifecycleRow, 1)
        session.expunge(gate)
        if fault == "previous":
            gate.previous_identity = None
        elif fault == "floor":
            gate.previous_identity = {**gate.previous_identity, "boot_epoch": 1}
        elif fault == "disk":
            gate.disk_ref = "wrong"
        else:
            gate.database_ref = "wrong"
        with pytest.raises(MaterialDeniedError, match="RECOVERY_OUTCOME_UNRESOLVED"):
            await inspect_retained_work(
                session,
                gate,
                RecoveryAcknowledgement(idempotency_key="invalid", receipt_ref="current"),
            )
        assert await session.scalar(text("SELECT recovery_key FROM hosted_node_lifecycle")) is None


@pytest.mark.parametrize("fault", ["node", "owner", "future_epoch"])
async def test_parked_restore_attention_cannot_supply_foreign_or_future_identity(row_holds, fault):
    from awf.hosted_node.identity import MaterialDeniedError
    from awf.hosted_node.retained_work import inspect_retained_work

    receiver = row_holds
    receiver.identity = await boot(receiver.config, receiver.factory)
    async with receiver.factory() as session:
        gate = await session.get(NodeLifecycleRow, 1)
        workspace = await session.scalar(select(Workspace).order_by(Workspace.id))
        original = workspace.task_policy
        if fault == "node":
            workspace.node_id = "foreign/node"
        else:
            prior = original["hosted_recovery"]
            identity = {
                **prior["identity"],
                **(
                    {"owner": "foreign/node"}
                    if fault == "owner"
                    else {"boot_epoch": receiver.identity.boot_epoch + 1}
                ),
            }
            workspace.task_policy = {**original, "hosted_recovery": {**prior, "identity": identity}}
        # Corrupt loaded observations are refused without bypassing or mutating
        # the durable SQL fence; the transaction never flushes these test values.
        with (
            session.no_autoflush,
            pytest.raises(MaterialDeniedError, match="RECOVERY_OUTCOME_UNRESOLVED"),
        ):
            await inspect_retained_work(
                session,
                gate,
                RecoveryAcknowledgement(idempotency_key="bad-attention", receipt_ref="current"),
            )
    async with receiver.factory() as session:
        assert (await session.get(Workspace, workspace.id)).task_policy == original
        assert await session.scalar(text("SELECT recovery_key FROM hosted_node_lifecycle")) is None


async def test_second_restore_resolves_inherited_orphan_fences_only_with_current_evidence(
    row_holds, ready_node
):
    client, receiver, _ = ready_node
    async with receiver.factory() as session:
        original = (await session.scalars(select(RestoreReconciliationRow))).one()
        original_plan = original.plan
        original_holds = list(await session.scalars(select(RestoreRowHold)))
    response = await client.post(PREFIX + "/maintenance/begin", json=checkpoint_request(8))
    assert response.status_code == 200, response.text
    recorded = CheckpointMarker.model_validate(await marker(receiver))
    binding = RestoreBinding(
        restore_id=str(uuid4()),
        checkpoint=recorded,
        manifest_sha256="b" * 64,
        files_sha256="c" * 64,
        schema_sha256="d" * 64,
        point_in_time=datetime.now(UTC),
        storage_mapping_sha256="e" * 64,
        fenced_identity=receiver.identity,
        candidate_incarnation="789",
        candidate_recovery_generation=4,
        boot_epoch_floor=receiver.identity.boot_epoch,
        target_disk_ref=recorded.disk_ref,
        target_database_ref=recorded.database_ref,
    )
    receiver.config = receiver.config.model_copy(
        update={"incarnation": "789", "recovery_generation": 4}
    )
    receiver.identity = await boot(
        receiver.config,
        receiver.factory,
        restore=RestoreBootEvidence(binding, datetime.now(UTC) + timedelta(seconds=10)),
    )
    receiver.lifecycle = NodeLifecycle(receiver.factory, receiver.identity)
    evidence(receiver)

    async def verify(identity, restore_id, receipt):
        assert restore_id == binding.restore_id
        return RestoreEvidence(
            binding, identity, receipt, datetime.now(UTC) + timedelta(seconds=10)
        )

    receiver.lifecycle.verify_restore = verify
    client.headers.update(receiver.identity.headers())
    accepted = await client.post(
        PREFIX + "/recovery/acknowledge",
        json={"idempotency_key": binding.restore_id, "receipt_ref": "second-restore"},
    )
    assert accepted.status_code == 200, accepted.text
    proof = None

    async def current(proposal):
        nonlocal proof
        if proof is None:
            proof = decision_plan(proposal)
        return proof.model_copy(update={"expires_at": datetime.now(UTC) + timedelta(seconds=10)})

    receiver.lifecycle.verify_restore_reconciliation = current
    request = {"idempotency_key": "second-current-plan", "receipt_ref": "current-action-proof"}
    response = await client.post(PREFIX + "/recovery/acknowledge", json=request)
    assert response.status_code == 200, response.text
    async with receiver.factory() as session:
        holds = list(await session.scalars(select(RestoreRowHold)))
        assert len(holds) == 2 * len(original_holds) == 8
        assert all(row.state == "resolved" and row.resolved_journal_id for row in holds)
        assert (await session.get(RestoreReconciliationRow, original.id)).plan == original_plan
    for family in ("callback_subscriptions", "callback_deliveries", "service_gc_requests"):
        async with receiver.factory.begin() as session:
            await session.execute(text(f"UPDATE {family} SET id=id"))
    async with receiver.factory.begin() as session:
        await session.execute(text("UPDATE tasks SET id=id WHERE id='no-attempt'"))


@pytest.mark.parametrize("fault", ["preimage", "bound"])
async def test_inherited_holds_fail_atomically_on_mismatch_or_excess(row_holds, monkeypatch, fault):
    from awf.hosted_node import restore_reconciliation_apply as target
    from awf.hosted_node.identity import MaterialDeniedError
    from awf.hosted_node.restore_reconciliation_inventory import inventory

    receiver = row_holds
    async with receiver.factory() as session:
        gate = await session.get(NodeLifecycleRow, 1)
        proposal, records = await inventory(
            session, gate, RecoveryAcknowledgement(idempotency_key="next", receipt_ref="current")
        )
        proof = decision_plan(proposal)
        proof = proof.model_copy(
            update={"binding": proof.binding.model_copy(update={"restore_id": str(uuid4())})}
        )
        if fault == "preimage":
            first = proof.rows[0].model_copy(update={"preimage_sha256": "0" * 64})
            proof = proof.model_copy(update={"rows": (first, *proof.rows[1:])})
            reason = "RESTORE_PREIMAGE_CHANGED"
        else:
            # Exercise the overflow guard against four real inherited SQL rows.
            monkeypatch.setattr(target, "MAX_RESTORE_ROWS", 3)
            reason = "RESTORE_INVENTORY_UNSUPPORTED"
        journal = RestoreReconciliationRow(id="uncommitted", revision=2)
        with pytest.raises(MaterialDeniedError, match=reason):
            await target.apply_decisions(session, gate, journal, proof, proposal, records)
        assert journal.result is None
        assert set(await session.scalars(select(RestoreRowHold.state))) == {"held"}
        assert list(await session.scalars(select(RestoreReconciliationRow.state))) == ["committed"]
