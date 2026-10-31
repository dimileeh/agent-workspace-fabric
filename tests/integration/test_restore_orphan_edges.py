"""Every orphan family needs exact current evidence, including subsequent batches."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.lifecycle_schemas import RecoveryAcknowledgement
from awf.hosted_node.restore_reconciliation_inventory import context
from awf.hosted_node.restore_reconciliation_models import RestoreReconciliationRow, RestoreRowHold
from awf.hosted_node.restore_reconciliation_schemas import RestoreActionEvidence
from tests.integration.test_checkpoint_marker import schema_revision as schema_revision
from tests.integration.test_node_idle_gate import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node
from tests.integration.test_restore_reconciliation import unknown_decisions
from tests.integration.test_restore_reconciliation_guards import row_holds as row_holds


def decision_plan(inventory):
    baseline = unknown_decisions(inventory)
    outcomes = {
        "tasks": "never_started",
        "callback_subscriptions": "current_routing",
        "callback_deliveries": "never_started",
        "service_gc_requests": "safe_gc",
    }
    return baseline.model_copy(
        update={
            "authority_revision": 2,
            "rows": tuple(
                item.model_copy(
                    update={
                        "disposition": "resume",
                        "fact": RestoreActionEvidence(
                            action_id=item.row_id,
                            outcome=outcomes[item.family],
                            evidence_ref="current-selected-action",
                            evidence_sha256="e" * 64,
                            observed_at=datetime.now(UTC),
                            external_action_ref=f"current:{item.family}:{item.row_id}",
                        ),
                    }
                )
                for item in baseline.rows
            ),
        }
    )


async def test_all_four_orphan_families_resume_only_after_current_commit(row_holds):
    receiver = row_holds
    proof = None

    async def current(inventory):
        nonlocal proof
        if proof is None:
            proof = decision_plan(inventory)
        return proof

    receiver.lifecycle.verify_restore_reconciliation = current
    result = await receiver.lifecycle.acknowledge(
        RecoveryAcknowledgement(idempotency_key="all-orphans", receipt_ref="current-action-proof")
    )
    assert result.recovery_acceptance.attention_workspaces == 4
    async with receiver.factory.begin() as session:
        held = (await session.scalars(select(RestoreRowHold))).all()
        assert {row.family for row in held} == {
            "tasks",
            "callback_subscriptions",
            "callback_deliveries",
            "service_gc_requests",
        }
        assert all(row.state == "resolved" and row.resolved_journal_id for row in held)
        for family in ("callback_subscriptions", "callback_deliveries", "service_gc_requests"):
            await session.execute(text(f"UPDATE {family} SET id=id"))
        await session.execute(text("UPDATE tasks SET id=id WHERE id='no-attempt'"))


@pytest.mark.parametrize(
    "fault",
    [
        "row_preimage",
        "row_future",
        "row_old",
        "stale_revision",
        "duplicate_rows",
        "receipt_conflict",
    ],
)
async def test_later_orphan_proof_remains_exact_and_monotonic(row_holds, fault):
    receiver = row_holds

    async def current(inventory):
        proof = decision_plan(inventory)
        if fault == "stale_revision":
            return proof.model_copy(update={"authority_revision": 1})
        if fault == "duplicate_rows":
            return proof.model_copy(update={"rows": (*proof.rows, proof.rows[0])})
        if fault == "row_preimage":
            row = proof.rows[0].model_copy(update={"preimage_sha256": "0" * 64})
        else:
            observed = (
                inventory.binding.point_in_time - timedelta(seconds=1)
                if fault == "row_old"
                else datetime.now(UTC) + timedelta(seconds=60)
            )
            row = proof.rows[0].model_copy(
                update={"fact": proof.rows[0].fact.model_copy(update={"observed_at": observed})}
            )
        return proof.model_copy(update={"rows": (row, *proof.rows[1:])})

    receiver.lifecycle.verify_restore_reconciliation = current
    request = RecoveryAcknowledgement(
        idempotency_key="row-holds" if fault == "receipt_conflict" else "new-batch",
        receipt_ref="different-current-proof",
    )
    reason = (
        "RESTORE_RECONCILIATION_CONFLICT"
        if fault == "receipt_conflict"
        else "RESTORE_RECONCILIATION_STALE"
        if fault == "stale_revision"
        else "RESTORE_RECONCILIATION_UNAVAILABLE"
    )
    with pytest.raises(MaterialDeniedError, match=reason):
        await receiver.lifecycle.acknowledge(request)
    async with receiver.factory() as session:
        assert (await session.scalars(select(RestoreReconciliationRow.state))).all() == [
            "committed"
        ]
        assert set(await session.scalars(select(RestoreRowHold.state))) == {"held"}


@pytest.mark.parametrize("fault", ["pending", "absent", "uncommitted", "foreign"])
async def test_inventory_requires_held_or_exact_committed_context(row_holds, fault):
    receiver = row_holds
    async with receiver.factory() as session:
        gate = await session.get(NodeLifecycleRow, 1)
        journal = await session.get(RestoreReconciliationRow, gate.restore_reconciliation_id)
        # Detached observations test defense against stale/corrupt trusted input;
        # no ordinary SQL mutation is used to bypass the metadata trigger.
        session.expunge(gate)
        if fault == "pending":
            gate.restore_quarantine = {"state": "pending"}
        elif fault == "absent":
            gate.restore_reconciliation_id = None
        else:
            if fault == "uncommitted":
                journal.state = "staged"
            else:
                journal.owner = "foreign/node"
        with (
            session.no_autoflush,
            pytest.raises(MaterialDeniedError, match="RESTORE_STATE_CONFLICT"),
        ):
            await context(session, gate)


@pytest.mark.parametrize(
    "family", ["callback_subscriptions", "callback_deliveries", "service_gc_requests"]
)
async def test_fixed_orphan_continuation_rejects_previously_started_effects(row_holds, family):
    from awf.hosted_node.restore_reconciliation_apply import apply_decisions
    from awf.hosted_node.restore_reconciliation_inventory import inventory

    receiver = row_holds
    async with receiver.factory() as session:
        gate = await session.get(NodeLifecycleRow, 1)
        proposal, records = await inventory(
            session,
            gate,
            RecoveryAcknowledgement(idempotency_key="bad-action", receipt_ref="current"),
        )
        proof = decision_plan(proposal)
        selected = next(row for row in proof.rows if row.family == family)
        record = records[family, selected.row_id]
        field, value = {
            "callback_subscriptions": ("enabled", False),
            "callback_deliveries": ("attempt_count", 1),
            "service_gc_requests": ("params", {"unknown_action": True}),
        }[family]
        record[field] = value
        journal = RestoreReconciliationRow(id="uncommitted", revision=2)
        with pytest.raises(MaterialDeniedError, match="RESTORE_CONTINUATION_UNSUPPORTED"):
            await apply_decisions(session, gate, journal, proof, proposal, records)
        assert journal.result is None
