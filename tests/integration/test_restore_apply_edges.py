"""Final transforms recheck exact records instead of trusting preflight decisions."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.lifecycle_schemas import RecoveryAcknowledgement
from awf.hosted_node.restore_reconciliation_apply import apply_decisions
from awf.hosted_node.restore_reconciliation_inventory import inventory
from awf.hosted_node.restore_reconciliation_models import RestoreReconciliationRow
from awf.hosted_node.restore_reconciliation_mutations import Mutations
from awf.hosted_node.restore_reconciliation_schemas import (
    RestoreInventoryRow,
    RestoreMaterialPermit,
    RestoreReconciliationEvidence,
)
from tests.integration.test_checkpoint_marker import schema_revision as schema_revision
from tests.integration.test_node_idle_gate import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node
from tests.integration.test_restore_material_staging import install_selected, staged
from tests.integration.test_restore_material_staging import resumable_restore as resumable_restore


@pytest.mark.parametrize("resumable_restore", ["with_operations", "with_merge"], indirect=True)
async def test_direct_commit_observes_exact_known_operation_outcomes(resumable_restore):
    client, receiver, workspaces, request, _ = resumable_restore
    journal = await staged(resumable_restore)

    async def permit(value):
        return RestoreMaterialPermit(
            request=value, expires_at=datetime.now(UTC) + timedelta(seconds=10)
        )

    receiver.verify_restore_material = permit
    await install_selected(client, journal.plan)
    result = await receiver.lifecycle.acknowledge(RecoveryAcknowledgement(**request))
    assert result.recovery_acceptance.queued_workspaces == 1
    assert result.recovery_acceptance.monitor_workspaces == 1
    assert result.recovery_acceptance.attention_workspaces == 1
    async with receiver.factory() as session:
        statuses = dict((await session.execute(text("SELECT id,status FROM operations"))).all())
        assert statuses == {
            f"op_{workspaces[0].id}": "pending",
            f"op_{workspaces[2].id}": "succeeded",
            "unknown-operation": "running",
        }
        assert (await session.get(RestoreReconciliationRow, journal.id)).state == "committed"
        merge = (
            await session.execute(text("SELECT ready,stale,stale_reason FROM merge_candidates"))
        ).first()
        if merge is not None:
            assert tuple(merge) == (False, True, "RESTORE_REVALIDATION_REQUIRED")


@pytest.mark.parametrize("resumable_restore", ["with_operations"], indirect=True)
@pytest.mark.parametrize(
    "fault",
    [
        "marker",
        "missing_operation",
        "operation_preimage",
        "unknown_operation",
        "create_type",
        "create_started",
        "active_validation",
        "active_callback",
    ],
)
async def test_fixed_transforms_refuse_unsupported_preimages_before_any_update(
    resumable_restore, fault
):
    _, receiver, _, request, _ = resumable_restore
    staged_row = await staged(resumable_restore)
    async with receiver.factory() as session:
        gate = await session.get(NodeLifecycleRow, 1)
        proposal, records = await inventory(session, gate, RecoveryAcknowledgement(**request))
        journal = await session.get(RestoreReconciliationRow, staged_row.id)
        proof = RestoreReconciliationEvidence.model_validate(journal.plan)
        decision = next(item for item in proof.workspaces if item.disposition == "queued")
        reason = "RESTORE_OPERATION_EVIDENCE_REQUIRED"
        if fault == "marker":
            records["workspaces", decision.workspace_id]["task_policy"]["hosted_recovery"][
                "disposition"
            ] = "queued"
            reason = "RESTORE_PREIMAGE_CHANGED"
        elif fault == "missing_operation":
            decision = decision.model_copy(update={"operations": ()})
        elif fault in {"operation_preimage", "unknown_operation"}:
            operation = decision.operations[0]
            changes = (
                {"preimage_sha256": "0" * 64}
                if fault == "operation_preimage"
                else {"fact": operation.fact.model_copy(update={"outcome": "unknown"})}
            )
            decision = decision.model_copy(
                update={"operations": (operation.model_copy(update=changes),)}
            )
        elif fault in {"create_type", "create_started"}:
            operation = records["operations", decision.operations[0].operation_id]
            operation["type" if fault == "create_type" else "started_at"] = (
                "execute" if fault == "create_type" else datetime.now(UTC).isoformat()
            )
            reason = "RESTORE_CONTINUATION_UNSUPPORTED"
        else:
            proposal = proposal.model_copy(
                update={
                    "rows": (
                        *proposal.rows,
                        RestoreInventoryRow(
                            family="validation_runs"
                            if fault == "active_validation"
                            else "callback_deliveries",
                            row_id="new-active",
                            workspace_id=decision.workspace_id,
                            preimage_sha256="a" * 64,
                            status="running",
                        ),
                    )
                }
            )
            reason = "RESTORE_CONTINUATION_UNSUPPORTED"
        proof = proof.model_copy(
            update={
                "workspaces": tuple(
                    decision if row.workspace_id == decision.workspace_id else row
                    for row in proof.workspaces
                )
            }
        )
        with pytest.raises(MaterialDeniedError, match=reason):
            await apply_decisions(session, gate, journal, proof, proposal, records)
        assert journal.state == "staged" and journal.result is None
        assert not list(
            await session.scalars(
                text(
                    "SELECT id FROM workspace_events WHERE event_type='workspace.restore_reconciled'"
                )
            )
        )


@pytest.mark.parametrize("operation", ["update", "insert"])
async def test_mutation_plan_rejects_unmapped_table_without_sql(ready_node, operation):
    _, receiver, _ = ready_node
    async with receiver.factory() as session:
        plan = Mutations()
        with pytest.raises(MaterialDeniedError, match="RESTORE_MUTATION_UNSUPPORTED"):
            await getattr(plan, operation)(session, "hosted_owners", [{"id": "forbidden"}])
        assert not plan.entries and not plan.inserts and not plan.updates


async def test_mutation_plan_requires_every_exact_row_to_exist(ready_node):
    _, receiver, _ = ready_node
    async with receiver.factory() as session:
        plan = Mutations()
        with pytest.raises(MaterialDeniedError, match="RESTORE_PREIMAGE_CHANGED"):
            await plan.update(
                session,
                "workspaces",
                [{"key": {"id": "missing"}, "changes": {"task_title": "unsafe"}}],
            )
        assert not plan.entries


@pytest.mark.parametrize("fault", ["unconsumed", "counts"])
async def test_final_journal_consumption_and_counts_roll_back(
    resumable_restore, monkeypatch, fault
):
    from awf.hosted_node import restore_reconciliation_apply as target

    client, receiver, _, request, _ = resumable_restore
    journal = await staged(resumable_restore)

    async def permit(value):
        return RestoreMaterialPermit(
            request=value, expires_at=datetime.now(UTC) + timedelta(seconds=10)
        )

    receiver.verify_restore_material = permit
    await install_selected(client, journal.plan)
    if fault == "unconsumed":
        original = Mutations.apply

        async def extra(self, session, row):
            self.entries.append(
                {
                    "family": "workspaces",
                    "row_id": "not-mutated",
                    "before_sha256": "0" * 64,
                    "after_sha256": "1" * 64,
                    "used": False,
                }
            )
            await original(self, session, row)

        monkeypatch.setattr(Mutations, "apply", extra)
    else:
        original = target.counts
        calls = 0

        async def mismatch(*args):
            nonlocal calls
            calls += 1
            result = await original(*args)
            if calls == 2:
                result["queued"] += 1
            return result

        monkeypatch.setattr(target, "counts", mismatch)
    with pytest.raises(MaterialDeniedError, match="RESTORE_MUTATION_INCOMPLETE"):
        await receiver.lifecycle.acknowledge(RecoveryAcknowledgement(**request))
    async with receiver.factory() as session:
        gate = await session.get(NodeLifecycleRow, 1)
        assert gate.restore_quarantine is not None and gate.restore_reconciliation_id is None
        assert (await session.get(RestoreReconciliationRow, journal.id)).state == "staged"
