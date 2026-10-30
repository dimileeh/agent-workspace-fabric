"""Later current evidence, exact replay, and rollback across final SQL commit."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.restore_reconciliation_models import RestoreReconciliationRow
from tests.integration.test_checkpoint_marker import schema_revision as schema_revision
from tests.integration.test_node_idle_gate import PREFIX
from tests.integration.test_node_idle_gate import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node
from tests.integration.test_restore_quarantine import restore
from tests.integration.test_restore_reconciliation import unknown_decisions
from tests.integration.test_restore_reconciliation_guards import row_holds as row_holds


async def setup(ready_node):
    client, receiver, _, binding = await restore(ready_node)
    response = await client.post(
        PREFIX + "/recovery/acknowledge",
        json={
            "idempotency_key": binding.restore_id,
            "receipt_ref": "restore-receipt",
        },
    )
    assert response.status_code == 200, response.text
    proof = None

    async def current(inventory):
        nonlocal proof
        if proof is None:
            proof = unknown_decisions(inventory)
        return proof.model_copy(update={"expires_at": datetime.now(UTC) + timedelta(seconds=10)})

    receiver.lifecycle.verify_restore_reconciliation = current
    return client, receiver, {"idempotency_key": "batch-1", "receipt_ref": "current-proof"}


@pytest.mark.parametrize(
    "fault",
    ["missing", "expired", "identity", "storage", "inventory", "receipt", "partial", "future_fact"],
)
async def test_invalid_current_decisions_never_partially_commit(ready_node, fault):
    client, receiver, request = await setup(ready_node)
    valid = receiver.lifecycle.verify_restore_reconciliation

    async def invalid(inventory):
        result = await valid(inventory)
        if fault == "missing":
            return None
        changes = {
            "expired": {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
            "identity": {"identity": result.identity.model_copy(update={"boot_epoch": 1})},
            "storage": {
                "binding": result.binding.model_copy(update={"target_database_ref": "different"})
            },
            "inventory": {"inventory_sha256": "0" * 64},
            "receipt": {"receipt_ref": "different"},
            "partial": {"workspaces": result.workspaces[:-1]},
            "future_fact": {
                "workspaces": (
                    result.workspaces[0].model_copy(
                        update={
                            "fact": result.workspaces[0].fact.model_copy(
                                update={"observed_at": datetime.now(UTC) + timedelta(hours=1)}
                            )
                        }
                    ),
                    *result.workspaces[1:],
                )
            },
        }
        return result.model_copy(update=changes[fault])

    receiver.lifecycle.verify_restore_reconciliation = invalid
    response = await client.post(PREFIX + "/recovery/acknowledge", json=request)
    assert response.status_code == 503, response.text
    async with receiver.factory() as session:
        gate = await session.get(NodeLifecycleRow, 1)
        assert gate.restore_quarantine["state"] == "held" and gate.restore_reconciliation_id is None
        assert not list(await session.scalars(select(RestoreReconciliationRow)))


async def test_final_physical_health_must_be_fresh_not_only_initial(ready_node):
    client, receiver, request = await setup(ready_node)
    original = receiver.lifecycle.observe_host
    calls = 0

    async def only_preflight(identity):
        nonlocal calls
        calls += 1
        return await original(identity) if calls == 1 else None

    receiver.lifecycle.observe_host = only_preflight
    response = await client.post(PREFIX + "/recovery/acknowledge", json=request)
    assert response.status_code == 503, response.text
    async with receiver.factory() as session:
        assert (await session.get(NodeLifecycleRow, 1)).restore_quarantine["state"] == "held"


async def test_expiry_after_mutations_rolls_back_classification_and_anchor(ready_node, monkeypatch):
    from awf.hosted_node import restore_reconciliation_apply as target

    client, receiver, request = await setup(ready_node)
    valid = receiver.lifecycle.verify_restore_reconciliation

    async def short(inventory):
        return (await valid(inventory)).model_copy(
            update={"expires_at": datetime.now(UTC) + timedelta(milliseconds=100)}
        )

    receiver.lifecycle.verify_restore_reconciliation = short
    original = target.apply_decisions

    async def delayed(*args):
        await original(*args)
        await asyncio.sleep(0.15)

    monkeypatch.setattr(target, "apply_decisions", delayed)
    response = await client.post(PREFIX + "/recovery/acknowledge", json=request)
    assert response.status_code == 503, response.text
    async with receiver.factory() as session:
        gate = await session.get(NodeLifecycleRow, 1)
        assert gate.restore_reconciliation_id is None and gate.restore_quarantine["state"] == "held"
        assert (await session.scalars(select(RestoreReconciliationRow.state))).all() == ["staged"]
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM workspace_events WHERE event_type='workspace.restore_reconciled'"
                )
            )
            == 0
        )


async def test_later_batch_resolves_exact_orphan_without_replaying_workspaces(row_holds):
    from awf.db.repositories.service_gc_request_repo import ServiceGCRequestRepository
    from awf.hosted_node.lifecycle_schemas import RecoveryAcknowledgement
    from awf.hosted_node.restore_reconciliation_schemas import RestoreActionEvidence

    receiver = row_holds
    proof = None

    async def current(inventory):
        nonlocal proof
        assert not inventory.initial and inventory.prior_revision == 1
        if proof is None:
            baseline = unknown_decisions(inventory)
            decisions = tuple(
                decision.model_copy(
                    update={
                        "disposition": "resume",
                        "fact": RestoreActionEvidence(
                            action_id=decision.row_id,
                            outcome="safe_gc",
                            evidence_ref="current-bounded-gc",
                            evidence_sha256="a" * 64,
                            observed_at=datetime.now(UTC),
                            external_action_ref="operator-current-gc",
                        ),
                    }
                )
                if decision.family == "service_gc_requests"
                else decision
                for decision in baseline.rows
            )
            proof = baseline.model_copy(update={"authority_revision": 2, "rows": decisions})
        return proof.model_copy(update={"expires_at": datetime.now(UTC) + timedelta(seconds=10)})

    receiver.lifecycle.verify_restore_reconciliation = current
    request = RecoveryAcknowledgement(idempotency_key="batch-2", receipt_ref="current-second-proof")
    accepted = await receiver.lifecycle.acknowledge(request)
    assert not accepted.admission_closed and accepted.recovery_acceptance.attention_workspaces == 4
    async with receiver.factory.begin() as session:
        claimed = await ServiceGCRequestRepository(session).claim_oldest_pending(
            node_id=receiver.identity.owner, now=datetime.now(UTC)
        )
        assert claimed.id == "gc"
        assert (
            await session.scalar(
                text("SELECT count(*) FROM hosted_restore_reconciliations WHERE state='committed'")
            )
            == 2
        )
        assert (
            await session.scalar(text("SELECT status FROM operations WHERE id='unknown-operation'"))
            == "running"
        )
    replay = await receiver.lifecycle.acknowledge(request)
    assert replay.recovery_acceptance == accepted.recovery_acceptance


async def test_restart_while_quarantined_reaccepts_only_current_restore_authority(ready_node):
    from awf.hosted_node.identity import boot
    from awf.hosted_node.lifecycle import NodeLifecycle
    from awf.hosted_node.restore_schemas import RestoreBinding, RestoreEvidence
    from tests.integration.test_node_idle_gate import evidence

    client, receiver, request = await setup(ready_node)
    original_verify = receiver.lifecycle.verify_restore
    previous = receiver.identity
    receiver.identity = await boot(receiver.config, receiver.factory)
    receiver.lifecycle = NodeLifecycle(receiver.factory, receiver.identity)
    evidence(receiver)
    client.headers.update(receiver.identity.headers())
    receiver.lifecycle.verify_restore = original_verify
    response = await client.post(PREFIX + "/recovery/acknowledge", json=request)
    assert response.status_code == 503, response.text
    async with receiver.factory() as session:
        held = (await session.get(NodeLifecycleRow, 1)).restore_quarantine

    async def current(identity, restore_id, receipt):
        assert identity == receiver.identity and identity.boot_epoch == previous.boot_epoch + 1
        assert restore_id == held["binding"]["restore_id"]
        return RestoreEvidence(
            RestoreBinding.model_validate(held["binding"]),
            identity,
            receipt,
            datetime.now(UTC) + timedelta(seconds=10),
        )

    receiver.lifecycle.verify_restore = current
    reaccepted = await client.post(PREFIX + "/recovery/acknowledge", json=request)
    assert reaccepted.status_code == 200, reaccepted.text
    assert reaccepted.json()["admission_closed"] and not reaccepted.json()["runtime_ready"]
    assert reaccepted.json()["recovery_acceptance"]["attention_workspaces"] == 3
    async with receiver.factory() as session:
        gate = await session.get(NodeLifecycleRow, 1)
        assert gate.restore_quarantine == held and gate.phase == "ready"
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM workspace_events WHERE event_type='workspace.recovery_accepted'"
                )
            )
            == 3
        )


async def test_identical_concurrent_decisions_commit_once_and_both_read_back(ready_node):
    client, receiver, request = await setup(ready_node)
    verify = receiver.lifecycle.verify_restore_reconciliation
    rendezvous = asyncio.Barrier(2)
    calls = 0

    async def together(inventory):
        nonlocal calls
        calls += 1
        proof = await verify(inventory)
        if calls <= 2:
            await asyncio.wait_for(rendezvous.wait(), 2)
        return proof

    receiver.lifecycle.verify_restore_reconciliation = together
    first, second = await asyncio.gather(
        *(client.post(PREFIX + "/recovery/acknowledge", json=request) for _ in range(2))
    )
    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert first.json()["recovery_acceptance"] == second.json()["recovery_acceptance"]
    async with receiver.factory() as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM hosted_restore_reconciliations WHERE state='committed'")
            )
            == 1
        )
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM workspace_events WHERE event_type='workspace.restore_reconciled'"
                )
            )
            == 3
        )


async def test_boot_change_during_current_callback_cannot_accept_old_proof(ready_node):
    from awf.hosted_node.identity import boot

    client, receiver, request = await setup(ready_node)
    verify = receiver.lifecycle.verify_restore_reconciliation
    observed, finish = asyncio.Event(), asyncio.Event()

    async def paused(inventory):
        result = await verify(inventory)
        observed.set()
        await finish.wait()
        return result

    receiver.lifecycle.verify_restore_reconciliation = paused
    accepting = asyncio.create_task(client.post(PREFIX + "/recovery/acknowledge", json=request))
    try:
        await asyncio.wait_for(observed.wait(), 2)
        identity = await boot(receiver.config, receiver.factory)
        assert identity.boot_epoch > receiver.identity.boot_epoch
    finally:
        finish.set()
    response = await accepting
    assert (
        response.status_code == 403 and response.json()["error_code"] == "IDENTITY_UNAVAILABLE"
    ), response.text
    async with receiver.factory() as session:
        assert not list(await session.scalars(select(RestoreReconciliationRow)))
