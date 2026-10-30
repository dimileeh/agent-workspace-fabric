"""Fixed restore transforms; uncertain external work is never replayed or cancelled."""

from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from awf.common.ids import new_event_id
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.lifecycle_schemas import RecoveryAcceptance
from awf.hosted_node.models import Grant, PreparationRow, Receipt
from awf.hosted_node.restore_reconciliation_models import RestoreReconciliationRow, RestoreRowHold
from awf.hosted_node.restore_reconciliation_mutations import Mutations
from awf.hosted_node.restore_reconciliation_schemas import (
    RestoreInventory,
    RestoreReconciliationEvidence,
)
from awf.hosted_node.schemas import Binding


async def verify_current_materials(session: AsyncSession, bindings: list[Binding]) -> None:
    if not bindings:
        return
    expected = {binding.idempotency_key: binding for binding in bindings}
    other = aliased(Grant)
    rows = (
        await session.execute(
            select(
                PreparationRow,
                Grant,
                Receipt,
                select(func.max(other.fence))
                .where(other.preparation == Grant.preparation)
                .correlate(Grant)
                .scalar_subquery(),
                select(func.max(other.authorization_generation))
                .where(other.preparation == Grant.preparation)
                .correlate(Grant)
                .scalar_subquery(),
            )
            .join(Grant, Grant.preparation == PreparationRow.id)
            .join(Receipt, Receipt.grant == Grant.id)
            .where(Receipt.idempotency_key.in_(expected))
            .limit(len(bindings) + 1)
        )
    ).all()
    if len(expected) != len(bindings) or len(rows) != len(bindings):
        raise MaterialDeniedError("RESTORE_MATERIALS_REQUIRED")
    for prep, grant, receipt, fence, generation in rows:
        binding = expected[receipt.idempotency_key]
        if (
            receipt.binding != binding.model_dump(mode="json")
            or receipt.state != "installed"
            or grant.current != receipt.id
            or grant.pending is not None
            or grant.revoked
            or grant.fence != binding.fence
            or fence != binding.fence
            or grant.revision != binding.revision
            or grant.authorization_generation != binding.authorization_generation
            or generation != binding.authorization_generation
            or prep.owner != binding.identity.owner
            or receipt.owner != binding.identity.owner
            or prep.binding["identity"] != binding.identity.model_dump()
            or prep.namespace != binding.namespace
            or prep.workspace_id != binding.workspace_id
            or prep.preparation_id != binding.preparation_id
            or grant.grant_id != binding.grant_id
            or prep.binding["target"] != binding.target
            or prep.binding["purpose"] != binding.purpose
            or not datetime.now(UTC)
            < binding.expires_at
            <= datetime.fromisoformat(prep.binding["expires_at"])
        ):
            raise MaterialDeniedError("RESTORE_MATERIALS_REQUIRED")


async def counts(session: AsyncSession, restore_id: str) -> Counter[str]:
    rows = (
        (
            await session.execute(
                text(
                    "SELECT task_policy::jsonb #>> '{hosted_recovery,disposition}', count(*) FROM workspaces "
                    "WHERE task_policy::jsonb #>> '{hosted_recovery,restore_id}'=:restore_id GROUP BY 1"
                ),
                {"restore_id": restore_id},
            )
        )
        .tuples()
        .all()
    )
    return Counter(dict(rows))


async def apply_decisions(
    session: AsyncSession,
    gate: NodeLifecycleRow,
    journal: RestoreReconciliationRow,
    proof: RestoreReconciliationEvidence,
    inventory: RestoreInventory,
    records: dict[tuple[str, str], dict[str, Any]],
) -> None:
    now = datetime.now(UTC)
    now_json = now.isoformat()
    mutations = Mutations()
    workspace_edits, operation_edits, merge_edits, events = [], [], [], []
    result_counts = await counts(session, proof.binding.restore_id)
    for decision in proof.workspaces:
        workspace = records["workspaces", decision.workspace_id]
        policy = workspace["task_policy"] or {}
        previous = policy.get("hosted_recovery", {})
        if previous.get("disposition") != "attention":
            raise MaterialDeniedError("RESTORE_PREIMAGE_CHANGED")
        related = [row for row in inventory.rows if row.workspace_id == decision.workspace_id]
        active_operations = {
            row.row_id: row
            for row in related
            if row.family == "operations" and row.status not in ("succeeded", "failed", "cancelled")
        }
        if decision.disposition != "held":
            if {item.operation_id for item in decision.operations} != set(active_operations):
                raise MaterialDeniedError("RESTORE_OPERATION_EVIDENCE_REQUIRED")
            for operation in decision.operations:
                row = active_operations[operation.operation_id]
                raw = records["operations", row.row_id]
                if (
                    operation.preimage_sha256 != row.preimage_sha256
                    or operation.fact.outcome == "unknown"
                ):
                    raise MaterialDeniedError("RESTORE_OPERATION_EVIDENCE_REQUIRED")
                if operation.fact.outcome == "never_started":
                    if (
                        raw["type"] != "create"
                        or raw["status"] != "pending"
                        or raw["started_at"] is not None
                    ):
                        raise MaterialDeniedError("RESTORE_CONTINUATION_UNSUPPORTED")
                else:
                    operation_edits.append(
                        {
                            "key": {"id": row.row_id},
                            "changes": {
                                "status": operation.fact.outcome,
                                "finished_at": operation.fact.observed_at.isoformat(),
                                "result": {
                                    "restore_evidence_ref": operation.fact.evidence_ref,
                                    "restore_evidence_sha256": operation.fact.evidence_sha256,
                                },
                                "error_code": "RESTORE_EXTERNAL_FAILURE"
                                if operation.fact.outcome == "failed"
                                else None,
                                "error_message": None,
                            },
                        }
                    )
            if any(
                row.family == "validation_runs"
                and row.status not in ("succeeded", "failed")
                or row.family == "callback_deliveries"
                and row.status not in ("succeeded", "failed", "skipped")
                for row in related
            ):
                raise MaterialDeniedError("RESTORE_CONTINUATION_UNSUPPORTED")
            for row in related:
                if row.family == "merge_candidates":
                    merge_edits.append(
                        {
                            "key": {"id": row.row_id},
                            "changes": {
                                "ready": False,
                                "stale": True,
                                "stale_reason": "RESTORE_REVALIDATION_REQUIRED",
                                "updated_at": now_json,
                            },
                        }
                    )
            assert decision.fresh_association is not None
            policy = {**policy, "hosted_node": decision.fresh_association.model_dump(mode="json")}
        disposition = "attention" if decision.disposition == "held" else decision.disposition
        if previous.get("restore_id") == proof.binding.restore_id:
            result_counts[previous["disposition"]] -= 1
        result_counts[disposition] += 1
        policy = {
            **policy,
            "hosted_recovery": {
                **previous,
                "restore_id": proof.binding.restore_id,
                "identity": proof.identity.model_dump(),
                "idempotency_key": proof.idempotency_key,
                "receipt_ref": proof.receipt_ref,
                "disposition": disposition,
                "reconciliation_revision": journal.revision,
                "evidence_sha256": decision.fact.evidence_sha256,
                "execution_claim_epoch": workspace["execution_claim_epoch"] + 1,
            },
        }
        workspace_edits.append(
            {
                "key": {"id": decision.workspace_id},
                "changes": {
                    "task_policy": policy,
                    "version": workspace["version"] + 1,
                    "event_sequence": workspace["event_sequence"] + 1,
                    "execution_claim_epoch": workspace["execution_claim_epoch"] + 1,
                    "execution_claimed_by": None,
                    "execution_claim_expires_at": None,
                    "monitor_claimed_by": None,
                    "monitor_claim_expires_at": None,
                    "updated_at": now_json,
                },
            }
        )
        events.append(
            {
                "id": new_event_id(),
                "workspace_id": decision.workspace_id,
                "event_type": "workspace.restore_reconciled",
                "old_state": workspace["status"],
                "new_state": workspace["status"],
                "reason_code": "RESTORE_RECONCILIATION_COMMITTED",
                "event_order": workspace["event_sequence"] + 1,
                "occurred_at": now_json,
                "payload": {
                    "restore_id": proof.binding.restore_id,
                    "revision": journal.revision,
                    "disposition": disposition,
                    "receipt_ref": proof.receipt_ref,
                    "evidence_sha256": decision.fact.evidence_sha256,
                },
            }
        )
    inserts, edits = [], []
    for row_decision in proof.rows:
        raw = records[row_decision.family, row_decision.row_id]
        if row_decision.disposition == "resume" and (
            row_decision.family == "callback_deliveries"
            and (raw["status"] != "pending" or raw["attempt_count"] != 0)
            or row_decision.family == "service_gc_requests"
            and (raw["status"] != "pending" or raw["claimed_at"] is not None or raw["params"] != {})
            or row_decision.family == "callback_subscriptions"
            and not raw["enabled"]
        ):
            raise MaterialDeniedError("RESTORE_CONTINUATION_UNSUPPORTED")
        key = {
            "owner": proof.identity.owner,
            "restore_id": proof.binding.restore_id,
            "family": row_decision.family,
            "row_id": row_decision.row_id,
        }
        existing = await session.get(RestoreRowHold, tuple(key.values()))
        after = {
            **key,
            "preimage_sha256": row_decision.preimage_sha256,
            "reason_code": "RESTORE_OUTCOME_UNRESOLVED"
            if row_decision.disposition == "held"
            else "RESTORE_CURRENT_EVIDENCE",
            "state": "held" if row_decision.disposition == "held" else "resolved",
            "journal_id": existing.journal_id if existing else journal.id,
            "resolved_journal_id": journal.id if row_decision.disposition == "resume" else None,
        }
        if existing is None:
            inserts.append(after)
        else:
            edits.append({"key": key, "changes": after})
    acceptance = RecoveryAcceptance(
        idempotency_key=proof.idempotency_key,
        receipt_ref=proof.receipt_ref,
        previous_identity=gate.previous_identity,
        disk_ref=gate.disk_ref,
        database_ref=gate.database_ref,
        queued_workspaces=result_counts["queued"],
        monitor_workspaces=result_counts["monitor"],
        attention_workspaces=result_counts["attention"],
    )
    journal.result = {
        "acceptance": acceptance.model_dump(mode="json"),
        "original_acceptance": gate.recovery_acceptance,
        "initial_quarantine": gate.restore_quarantine,
    }
    await mutations.insert(session, "hosted_restore_row_holds", inserts)
    await mutations.update(session, "hosted_restore_row_holds", edits)
    await mutations.insert(session, "workspace_events", events)
    await mutations.update(session, "operations", operation_edits)
    await mutations.update(session, "merge_candidates", merge_edits)
    await mutations.update(session, "workspaces", workspace_edits)
    await mutations.update(
        session,
        "hosted_node_lifecycle",
        [
            {
                "key": {"singleton": 1},
                "changes": {
                    "restore_quarantine": None,
                    "restore_reconciliation_id": journal.id,
                    "recovery_acceptance": acceptance.model_dump(mode="json"),
                    "idle_since": None,
                },
            }
        ],
    )
    journal.committed_at = now
    await mutations.apply(session, journal)
    if +(await counts(session, proof.binding.restore_id)) != +result_counts:
        raise MaterialDeniedError("RESTORE_MUTATION_INCOMPLETE")
