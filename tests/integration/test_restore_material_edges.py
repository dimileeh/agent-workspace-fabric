"""Staging is a narrow permission, never a general closed-node write override."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from awf.hosted_node import restore_materials
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.restore_reconciliation_models import RestoreReconciliationRow
from awf.hosted_node.schemas import Binding, Preparation
from tests.integration.test_checkpoint_marker import schema_revision as schema_revision
from tests.integration.test_node_idle_gate import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node
from tests.integration.test_restore_material_staging import resumable_restore as resumable_restore
from tests.integration.test_restore_material_staging import staged


@pytest.mark.parametrize(
    "fault",
    [
        "missing_journal",
        "expired_journal",
        "no_value",
        "no_operation",
        "wrong_preparation",
        "install_preparation",
        "wrong_receipt",
    ],
)
async def test_stage_denies_unselected_or_incomplete_requests(resumable_restore, fault):
    _, receiver, _, _, captured = resumable_restore
    if fault == "missing_journal":
        # A current authority reference alone has not staged any durable decision.
        from tests.unit.test_hosted_runtime_contract import hosted_create

        value = Binding.model_validate(hosted_create()["hosted"]["credentials"]["agent"])
    else:
        journal = await staged(resumable_restore)
        selected = next(item for item in journal.plan["workspaces"] if item["fresh_association"])
        value = Binding.model_validate(selected["fresh_association"]["credentials"]["agent"])
        if fault == "expired_journal":
            async with receiver.factory.begin() as session:
                await session.execute(
                    update(RestoreReconciliationRow)
                    .where(RestoreReconciliationRow.id == journal.id)
                    .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
                )
    operation = "install"
    if fault == "no_value":
        value = None
    elif fault == "no_operation":
        operation = None
    elif fault == "wrong_preparation":
        value = value.model_copy(update={"preparation_id": "unselected"})
    elif fault == "install_preparation":
        value = Preparation.model_validate(value.model_dump(include=set(Preparation.model_fields)))
    elif fault == "wrong_receipt":
        value = value.model_copy(update={"idempotency_key": "different-receipt"})
    reason = (
        "RESTORE_MATERIAL_NOT_SELECTED"
        if fault in {"wrong_preparation", "install_preparation", "wrong_receipt"}
        else "RESTORE_QUARANTINED"
    )
    async with receiver.factory() as session:
        with pytest.raises(MaterialDeniedError, match=reason):
            await restore_materials.request(receiver, session, value, operation)


async def test_no_lifecycle_is_not_changed_by_restore_material_hook():
    receiver = SimpleNamespace(lifecycle=None)
    assert await restore_materials.request(receiver, None, None, None) is None
    assert await restore_materials.authorize(receiver, None, None) is None
    assert await restore_materials.deny_material(None) is None


async def test_previously_staged_permission_is_not_ordinary_publication(ready_node):
    _, receiver, _ = ready_node
    async with receiver.factory() as session:
        assert await restore_materials.request(receiver, session, None, None) is None
        with pytest.raises(MaterialDeniedError, match="RESTORE_MATERIAL_STATE_CHANGED"):
            await restore_materials.allow(receiver, session, None, object(), "install")


@pytest.mark.parametrize(
    "fault",
    ["duplicate", "missing", "revoked", "pending", "receipt", "fence", "revision", "generation"],
)
async def test_final_material_revalidation_does_not_trust_preflight(resumable_restore, fault):
    from awf.hosted_node.models import Grant, Receipt
    from awf.hosted_node.restore_reconciliation_apply import verify_current_materials
    from awf.hosted_node.restore_reconciliation_schemas import RestoreMaterialPermit
    from tests.integration.test_restore_material_staging import install_selected

    client, receiver, _, _, _ = resumable_restore
    journal = await staged(resumable_restore)

    async def permit(request):
        return RestoreMaterialPermit(
            request=request, expires_at=datetime.now(UTC) + timedelta(seconds=10)
        )

    receiver.verify_restore_material = permit
    values = [
        Binding.model_validate(binding)
        for row in journal.plan["workspaces"]
        if row["fresh_association"]
        for binding in row["fresh_association"]["credentials"].values()
    ]
    if fault != "missing":
        await install_selected(client, journal.plan)
    async with receiver.factory.begin() as session:
        if fault == "duplicate":
            values.append(values[0])
        elif fault not in {"missing"}:
            receipt = await session.scalar(
                select(Receipt).where(Receipt.idempotency_key == values[0].idempotency_key)
            )
            grant = await session.get(Grant, receipt.grant)
            if fault == "receipt":
                receipt.state = "stale"
            else:
                field, value = {
                    "revoked": ("revoked", True),
                    "pending": ("pending", "other-receipt"),
                    "fence": ("fence", grant.fence + 1),
                    "revision": ("revision", grant.revision + 1),
                    "generation": ("authorization_generation", grant.authorization_generation + 1),
                }[fault]
                setattr(grant, field, value)
            await session.flush()
        with pytest.raises(MaterialDeniedError, match="RESTORE_MATERIALS_REQUIRED"):
            await verify_current_materials(session, values)
