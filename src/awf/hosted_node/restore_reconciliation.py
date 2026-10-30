"""Current external decisions replace global quarantine with exact durable fences."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import cast, select, tuple_, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle_schemas import RecoveryAcknowledgement
from awf.hosted_node.lifecycle_store import locked
from awf.hosted_node.models import Grant, PreparationRow
from awf.hosted_node.restore_quarantine import evidence_fresh
from awf.hosted_node.restore_reconciliation_inventory import context, inventory
from awf.hosted_node.restore_reconciliation_models import RestoreReconciliationRow
from awf.hosted_node.restore_reconciliation_schemas import (
    RestoreInventory,
    RestoreReconciliationEvidence,
    action_set_sha256,
    reconciliation_plan_sha256,
)
from awf.hosted_node.schemas import Binding, Identity

if TYPE_CHECKING:
    from awf.hosted_node.lifecycle import NodeLifecycle


def validated(inventory: RestoreInventory, proof: object) -> RestoreReconciliationEvidence:
    try:
        if not isinstance(proof, RestoreReconciliationEvidence):
            raise ValueError("missing")
        evidence = RestoreReconciliationEvidence.model_validate(proof.model_dump())
        if (
            evidence.binding != inventory.binding
            or evidence.identity != inventory.identity
            or evidence.idempotency_key != inventory.idempotency_key
            or evidence.receipt_ref != inventory.receipt_ref
            or evidence.prior_revision != inventory.prior_revision
            or evidence.inventory_sha256 != inventory.inventory_sha256
            or not evidence_fresh(evidence.expires_at)
        ):
            raise ValueError("scope")
        by_key = {(row.family, row.row_id): row for row in inventory.rows}
        workspaces = {row.row_id for row in inventory.rows if row.family == "workspaces"}
        orphans = {
            (row.family, row.row_id)
            for row in inventory.rows
            if row.family
            in {"tasks", "callback_subscriptions", "callback_deliveries", "service_gc_requests"}
            and row.workspace_id is None
        }
        if {row.workspace_id for row in evidence.workspaces} != workspaces or {
            (row.family, row.row_id) for row in evidence.rows
        } != orphans:
            raise ValueError("incomplete")
        for decision in evidence.workspaces:
            row = by_key["workspaces", decision.workspace_id]
            if (
                decision.preimage_sha256 != row.preimage_sha256
                or decision.action_set_sha256 != action_set_sha256(inventory.rows, row.row_id)
            ):
                raise ValueError("preimage")
            if decision.disposition != "held":
                if row.hosted is None or row.node_id != inventory.identity.owner:
                    raise ValueError("unbound")
                if (
                    decision.disposition == "queued" and row.status not in ("requested", "blocked")
                ) or (decision.disposition == "monitor" and row.status != "monitoring_pr"):
                    raise ValueError("continuation")
                fresh = decision.fresh_association
                assert fresh is not None
                for role in ("agent", "github"):
                    old, new = (
                        getattr(row.hosted.credentials, role),
                        getattr(fresh.credentials, role),
                    )
                    if (
                        new.identity != inventory.identity
                        or new.namespace != old.namespace
                        or new.purpose != old.purpose
                        or new.target != old.target
                        or new.preparation_id == old.preparation_id
                        or new.expires_at <= datetime.now(UTC)
                    ):
                        raise ValueError("binding")
            facts = [decision.fact, *(operation.fact for operation in decision.operations)]
            if any(
                fact.observed_at > datetime.now(UTC)
                or (
                    fact.outcome != "unknown" and fact.observed_at < inventory.binding.point_in_time
                )
                for fact in facts
            ):
                raise ValueError("fact-time")
        for row_decision in evidence.rows:
            if (
                row_decision.preimage_sha256
                != by_key[row_decision.family, row_decision.row_id].preimage_sha256
            ):
                raise ValueError("preimage")
            if row_decision.fact.observed_at > datetime.now(UTC) or (
                row_decision.disposition == "resume"
                and row_decision.fact.observed_at < inventory.binding.point_in_time
            ):
                raise ValueError("fact-time")
        return evidence
    except (ValidationError, ValueError, KeyError, TypeError, AssertionError):
        raise MaterialDeniedError("RESTORE_RECONCILIATION_UNAVAILABLE", 503) from None


def bindings(proof: RestoreReconciliationEvidence) -> list[Binding]:
    return [
        getattr(row.fresh_association.credentials, role)
        for row in proof.workspaces
        if row.fresh_association is not None
        for role in ("agent", "github")
    ]


async def supersede(session: AsyncSession, journal: RestoreReconciliationRow) -> None:
    retired = bindings(RestoreReconciliationEvidence.model_validate(journal.plan))
    if retired:
        preparations = select(PreparationRow.id).where(
            PreparationRow.owner == journal.owner,
            cast(PreparationRow.binding["identity"], JSONB) == journal.identity,
            tuple_(
                PreparationRow.namespace, PreparationRow.workspace_id, PreparationRow.preparation_id
            ).in_([(item.namespace, item.workspace_id, item.preparation_id) for item in retired]),
        )
        await session.execute(
            update(Grant)
            .where(Grant.preparation.in_(preparations))
            .values(
                revoked=True,
                current=None,
                pending=None,
            )
        )
    journal.state = "superseded"
    await session.flush()


async def committed(
    session: AsyncSession,
    identity: Identity,
    restore_id: str,
    value: RecoveryAcknowledgement,
) -> bool:
    recorded = await session.scalar(
        select(RestoreReconciliationRow).where(
            RestoreReconciliationRow.owner == identity.owner,
            RestoreReconciliationRow.restore_id == restore_id,
            RestoreReconciliationRow.idempotency_key == value.idempotency_key,
        )
    )
    if recorded is None:
        return False
    if (
        recorded.identity != identity.model_dump()
        or recorded.receipt_ref != value.receipt_ref
        or recorded.state == "superseded"
    ):
        raise MaterialDeniedError("RESTORE_RECONCILIATION_CONFLICT")
    return recorded.state == "committed"


async def reconcile_restore(service: "NodeLifecycle", value: RecoveryAcknowledgement) -> None:
    from awf.hosted_node.lifecycle import _bounded
    from awf.hosted_node.restore_reconciliation_apply import (
        apply_decisions,
        verify_current_materials,
    )

    async with service.factory.begin() as session:
        gate = await locked(session, service.identity)
        binding, revision = await context(session, gate)
        if await committed(session, service.identity, binding.restore_id, value):
            return
        if gate.phase != "ready" or gate.drain_generation is not None:
            raise MaterialDeniedError("RESTORE_STATE_CONFLICT")
        proposal, _ = await inventory(session, gate, value)
    host = await _bounded(service.observe_host(service.identity))
    if not (
        host
        and host.identity == service.identity
        and host.retained_state_verified
        and evidence_fresh(host.expires_at)
        and (not proposal.initial or host.quiescent)
    ):
        raise MaterialDeniedError("RESTORE_EVIDENCE_UNAVAILABLE", 503)
    proof = validated(proposal, await _bounded(service.verify_restore_reconciliation(proposal)))
    plan_hash = reconciliation_plan_sha256(proof)
    async with service.factory.begin() as session:
        gate = await locked(session, service.identity)
        if await committed(session, service.identity, binding.restore_id, value):
            return
        current, _ = await inventory(session, gate, value)
        if current != proposal:
            raise MaterialDeniedError("RESTORE_PREIMAGE_CHANGED")
        predecessor = (
            await session.get(RestoreReconciliationRow, gate.restore_reconciliation_id)
            if gate.restore_reconciliation_id
            else None
        )
        if predecessor is not None and proof.authority_revision <= predecessor.authority_revision:
            raise MaterialDeniedError("RESTORE_RECONCILIATION_STALE")
        staged = await session.scalar(
            select(RestoreReconciliationRow)
            .where(
                RestoreReconciliationRow.owner == service.identity.owner,
                RestoreReconciliationRow.restore_id == binding.restore_id,
                RestoreReconciliationRow.revision == revision + 1,
                RestoreReconciliationRow.state != "superseded",
            )
            .with_for_update()
        )
        if staged is not None and staged.plan_sha256 != plan_hash:
            if (
                staged.state != "staged"
                or proof.authority_revision <= staged.authority_revision
                or staged.idempotency_key == value.idempotency_key
            ):
                raise MaterialDeniedError("RESTORE_RECONCILIATION_CONFLICT")
            await supersede(session, staged)
            staged = None
        if staged is not None:
            if staged.plan_sha256 != plan_hash or staged.state != "staged":
                raise MaterialDeniedError("RESTORE_RECONCILIATION_CONFLICT")
            staged.expires_at = proof.expires_at
        else:
            staged = RestoreReconciliationRow(
                id=str(uuid4()),
                owner=service.identity.owner,
                restore_id=binding.restore_id,
                **value.model_dump(),
                identity=service.identity.model_dump(),
                binding=binding.model_dump(mode="json"),
                prior_revision=revision,
                revision=revision + 1,
                authority_revision=proof.authority_revision,
                inventory_sha256=proposal.inventory_sha256,
                plan_sha256=plan_hash,
                inventory=proposal.model_dump(mode="json"),
                plan=proof.model_dump(mode="json"),
                state="staged",
                expires_at=proof.expires_at,
                mutations=[],
            )
            session.add(staged)
        journal_id = staged.id
    fresh_bindings = bindings(proof)
    if fresh_bindings and await _bounded(service.verify_bindings(fresh_bindings)) is not True:
        raise MaterialDeniedError("RESTORE_MATERIALS_REQUIRED")
    final = validated(proposal, await _bounded(service.verify_restore_reconciliation(proposal)))
    if reconciliation_plan_sha256(final) != plan_hash:
        raise MaterialDeniedError("RESTORE_RECONCILIATION_CONFLICT")
    host = await _bounded(service.observe_host(service.identity))
    if not (
        host
        and host.identity == service.identity
        and host.retained_state_verified
        and evidence_fresh(host.expires_at)
        and (not proposal.initial or host.quiescent)
    ):
        raise MaterialDeniedError("RESTORE_EVIDENCE_UNAVAILABLE", 503)
    async with service.factory.begin() as session:
        gate = await locked(session, service.identity)
        if await committed(session, service.identity, binding.restore_id, value):
            return
        if gate.phase != "ready" or gate.drain_generation is not None:
            raise MaterialDeniedError("RESTORE_STATE_CONFLICT")
        current, rows = await inventory(session, gate, value)
        if current != proposal:
            raise MaterialDeniedError("RESTORE_PREIMAGE_CHANGED")
        staged = await session.get(RestoreReconciliationRow, journal_id, with_for_update=True)
        if staged is None or staged.state != "staged" or staged.plan_sha256 != plan_hash:
            raise MaterialDeniedError("RESTORE_RECONCILIATION_CONFLICT")
        await verify_current_materials(session, fresh_bindings)
        await apply_decisions(session, gate, staged, final, current, rows)
        if not evidence_fresh(final.expires_at) or not evidence_fresh(host.expires_at):
            raise MaterialDeniedError("RESTORE_EVIDENCE_UNAVAILABLE", 503)
