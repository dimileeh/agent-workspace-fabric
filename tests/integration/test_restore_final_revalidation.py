"""Concurrent winners and final proof changes cannot corrupt committed classification."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, text

from awf.hosted_node import restore_reconciliation as target
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.lifecycle_schemas import RecoveryAcknowledgement
from awf.hosted_node.restore_reconciliation_models import RestoreReconciliationRow
from tests.integration.test_checkpoint_marker import schema_revision as schema_revision
from tests.integration.test_node_idle_gate import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node
from tests.integration.test_restore_reconciliation_transactions import setup


@pytest.mark.parametrize("winner_at", [1, 2])
async def test_exact_committed_winner_dominates_each_slow_proof_boundary(ready_node, winner_at):
    _, receiver, request = await setup(ready_node)
    service = receiver.lifecycle
    value = RecoveryAcknowledgement(**request)
    original = service.verify_restore_reconciliation
    calls = 0
    won = False

    async def concurrent(inventory):
        nonlocal calls, won
        calls += 1
        proof = await original(inventory)
        if calls == winner_at and not won:
            won = True
            # The other request runs through all real SQL locks/guards/commit;
            # only its arrival at this external callback boundary is controlled.
            await target.reconcile_restore(service, value)
        return proof

    service.verify_restore_reconciliation = concurrent
    await target.reconcile_restore(service, value)
    assert won
    async with receiver.factory() as session:
        assert (await session.scalars(select(RestoreReconciliationRow.state))).all() == [
            "committed"
        ]
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM workspace_events WHERE event_type='workspace.restore_reconciled'"
                )
            )
            == 3
        )


@pytest.mark.parametrize(
    "fault",
    [
        "first_host",
        "changed_final_proof",
        "preimage_stage",
        "preimage_final",
        "phase_first",
        "phase_final",
        "expired_after_apply",
    ],
)
async def test_reconciliation_revalidates_every_transaction_boundary(
    ready_node, monkeypatch, fault
):
    _, receiver, request = await setup(ready_node)
    service = receiver.lifecycle
    if fault == "first_host":

        async def unhealthy(_identity):
            return None

        service.observe_host = unhealthy
        reason = "RESTORE_EVIDENCE_UNAVAILABLE"
    elif fault == "changed_final_proof":
        original = service.verify_restore_reconciliation
        calls = 0

        async def changed(inventory):
            nonlocal calls
            calls += 1
            proof = await original(inventory)
            return (
                proof
                if calls == 1
                else proof.model_copy(update={"authority_revision": proof.authority_revision + 1})
            )

        service.verify_restore_reconciliation = changed
        reason = "RESTORE_RECONCILIATION_CONFLICT"
    elif fault.startswith("preimage_"):
        original = target.inventory
        calls = 0

        async def changed(*args):
            nonlocal calls
            calls += 1
            proposal, rows = await original(*args)
            if calls == (2 if fault == "preimage_stage" else 3):
                proposal = proposal.model_copy(update={"inventory_sha256": "0" * 64})
            return proposal, rows

        monkeypatch.setattr(target, "inventory", changed)
        reason = "RESTORE_PREIMAGE_CHANGED"
    elif fault.startswith("phase_"):
        original = target.locked
        calls = 0

        async def changed(*args):
            nonlocal calls
            calls += 1
            gate = await original(*args)
            if calls == (1 if fault == "phase_first" else 3):
                # Model a stale observation without disabling a SQL trigger.
                # The raised exception must roll back even this local change.
                gate.phase = "draining"
            return gate

        monkeypatch.setattr(target, "locked", changed)
        reason = "RESTORE_STATE_CONFLICT"
    else:
        from awf.hosted_node import restore_quarantine, restore_reconciliation_apply

        original = restore_reconciliation_apply.apply_decisions

        class AfterExpiry(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(seconds=61)

        async def expire(*args):
            await original(*args)
            monkeypatch.setattr(restore_quarantine, "datetime", AfterExpiry)

        monkeypatch.setattr(restore_reconciliation_apply, "apply_decisions", expire)
        reason = "RESTORE_EVIDENCE_UNAVAILABLE"
    with pytest.raises(MaterialDeniedError, match=reason):
        await target.reconcile_restore(service, RecoveryAcknowledgement(**request))
    async with receiver.factory() as session:
        gate = await session.get(NodeLifecycleRow, 1)
        assert gate.phase == "ready" and gate.restore_quarantine is not None
        assert gate.restore_reconciliation_id is None
        assert not list(
            await session.scalars(
                select(RestoreReconciliationRow.id).where(
                    RestoreReconciliationRow.state == "committed"
                )
            )
        )
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM workspace_events WHERE event_type='workspace.restore_reconciled'"
                )
            )
            == 0
        )
