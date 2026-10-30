"""Staging is a narrow permission, never a general closed-node write override."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select, update

from awf.hosted_node import restore_materials
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.models import PreparationRow
from awf.hosted_node.restore_reconciliation_models import RestoreReconciliationRow
from awf.hosted_node.schemas import Binding, Preparation
from tests.integration.test_checkpoint_marker import schema_revision as schema_revision
from tests.integration.test_node_idle_gate import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node
from tests.integration.test_restore_material_staging import resumable_restore as resumable_restore
from tests.integration.test_restore_material_staging import staged
from tests.integration.test_restore_reconciliation_guards import row_holds as row_holds
from tests.unit.test_hosted_runtime_contract import hosted_create


async def test_ordinary_material_preflight_is_one_read_not_a_second_lock_transaction(ready_node):
    _, receiver, _ = ready_node
    value = Binding.model_validate(hosted_create()["hosted"]["credentials"]["agent"])
    statements = []

    def record(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    engine = receiver.factory.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        assert await restore_materials.authorize(receiver, value, "install") is None
    finally:
        event.remove(engine, "before_cursor_execute", record)
    reads = [statement for statement in statements if "set_config(" not in statement]
    assert len(reads) == 1 and len(statements) <= 2
    assert reads[0].lstrip().startswith("SELECT") and "FOR UPDATE" not in reads[0]


async def test_restore_after_ordinary_preflight_still_denies_final_mutation(
    ready_node, monkeypatch
):
    from tests.integration.test_restore_quarantine import restore

    client, receiver, _ = ready_node
    old = restore_materials.authorize

    async def interrupted(*args):
        permit = await old(*args)
        assert permit is None
        await restore(ready_node)
        return permit

    monkeypatch.setattr(restore_materials, "authorize", interrupted)
    value = hosted_create()["hosted"]["credentials"]["agent"]
    response = await client.post(
        "/v1/hosted-node/preparations",
        json={field: value[field] for field in Preparation.model_fields},
    )
    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "RESTORE_QUARANTINED"
    async with receiver.factory() as session:
        assert await session.scalar(select(PreparationRow.id)) is None


async def test_open_node_preflight_still_requires_permit_for_held_workspace(row_holds):
    from awf.db.models import Workspace

    receiver = row_holds
    async with receiver.factory() as session:
        assert (await session.get(NodeLifecycleRow, 1)).restore_quarantine is None
        workspace = await session.scalar(select(Workspace).order_by(Workspace.id))
        assert workspace.task_policy["hosted_recovery"]["disposition"] == "attention"
    value = Binding.model_validate(hosted_create()["hosted"]["credentials"]["agent"])
    value = value.model_copy(update={"workspace_id": workspace.id, "identity": receiver.identity})
    with pytest.raises(MaterialDeniedError, match="RESTORE_QUARANTINED"):
        await restore_materials.authorize(receiver, value, "install")


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
