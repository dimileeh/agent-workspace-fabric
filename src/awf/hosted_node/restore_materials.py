"""Exact journal-bound credentials may stage without granting execution authority."""

from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.models import Workspace
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle_store import locked
from awf.hosted_node.restore_quarantine import evidence_fresh
from awf.hosted_node.restore_reconciliation_inventory import context
from awf.hosted_node.restore_reconciliation_models import RestoreReconciliationRow
from awf.hosted_node.restore_reconciliation_schemas import (
    RestoreMaterialPermit,
    RestoreMaterialRequest,
    RestoreReconciliationEvidence,
)
from awf.hosted_node.schemas import Binding, Preparation

if TYPE_CHECKING:
    from awf.hosted_node.receiver import Receiver


async def deny_material(_value: RestoreMaterialRequest) -> None:
    return None


async def request(
    receiver: "Receiver",
    session: AsyncSession,
    value: Preparation | Binding | None,
    operation: Literal["prepare", "install"] | None,
) -> RestoreMaterialRequest | None:
    if receiver.lifecycle is None:
        return None
    gate = await locked(session, receiver.identity)
    workspace = await session.get(Workspace, value.workspace_id) if value is not None else None
    marker = (workspace.task_policy or {}).get("hosted_recovery", {}) if workspace else {}
    if gate.restore_quarantine is None and not (
        marker.get("restore_id") and marker.get("disposition") == "attention"
    ):
        return None
    if value is None or operation is None:
        raise MaterialDeniedError("RESTORE_QUARANTINED")
    if gate.phase != "ready" or gate.drain_generation is not None:
        raise MaterialDeniedError("RESTORE_QUARANTINED")
    binding, revision = await context(session, gate)
    journal = await session.scalar(
        select(RestoreReconciliationRow).where(
            RestoreReconciliationRow.owner == receiver.identity.owner,
            RestoreReconciliationRow.restore_id == binding.restore_id,
            RestoreReconciliationRow.identity == receiver.identity.model_dump(),
            RestoreReconciliationRow.prior_revision == revision,
            RestoreReconciliationRow.state == "staged",
        )
    )
    if journal is None or not evidence_fresh(journal.expires_at):
        raise MaterialDeniedError("RESTORE_QUARANTINED")
    proof = RestoreReconciliationEvidence.model_validate(journal.plan)
    supplied = value.model_dump(mode="json", include=set(Preparation.model_fields))
    choices = [
        getattr(decision.fresh_association.credentials, role)
        for decision in proof.workspaces
        if decision.fresh_association is not None
        for role in ("agent", "github")
        if getattr(decision.fresh_association.credentials, role).model_dump(
            mode="json", include=set(Preparation.model_fields)
        )
        == supplied
    ]
    if len(choices) != 1 or (
        operation == "install"
        and (
            not isinstance(value, Binding)
            or receiver.binding(value) != choices[0].model_dump(mode="json")
        )
    ):
        raise MaterialDeniedError("RESTORE_MATERIAL_NOT_SELECTED")
    return RestoreMaterialRequest(
        journal_id=journal.id,
        restore_binding=binding,
        identity=receiver.identity,
        idempotency_key=journal.idempotency_key,
        receipt_ref=journal.receipt_ref,
        prior_revision=revision,
        inventory_sha256=journal.inventory_sha256,
        plan_sha256=journal.plan_sha256,
        authority_revision=journal.authority_revision,
        authorization_sha256=proof.authorization_sha256,
        operation=operation,
        proposed=choices[0],
    )


def valid(permit: object, expected: RestoreMaterialRequest) -> RestoreMaterialPermit:
    try:
        if not isinstance(permit, RestoreMaterialPermit):
            raise ValueError("missing")
        result = RestoreMaterialPermit.model_validate(permit.model_dump())
        if result.request != expected or not evidence_fresh(result.expires_at):
            raise ValueError("scope")
        return result
    except (ValueError, ValidationError, TypeError):
        raise MaterialDeniedError("RESTORE_MATERIAL_AUTHORITY_UNAVAILABLE", 503) from None


async def authorize(
    receiver: "Receiver",
    value: Preparation | Binding,
    operation: Literal["prepare", "install"] | None,
) -> RestoreMaterialPermit | None:
    from awf.hosted_node.lifecycle import _bounded

    if receiver.lifecycle is None:
        return None
    async with receiver.factory.begin() as session:
        await receiver.authority(session, lock=True)
        expected = await request(receiver, session, value, operation)
    if expected is None:
        return None
    return valid(await _bounded(receiver.verify_restore_material(expected)), expected)


async def allow(
    receiver: "Receiver",
    session: AsyncSession,
    value: Preparation | Binding | None,
    permit: RestoreMaterialPermit | None,
    operation: Literal["prepare", "install"] | None,
) -> None:
    expected = await request(receiver, session, value, operation)
    if expected is not None:
        valid(permit, expected)
    elif permit is not None:
        # Concurrent acceptance cannot turn a previously closed staged request
        # into an ordinary publication without a fresh caller attempt.
        raise MaterialDeniedError("RESTORE_MATERIAL_STATE_CHANGED")
