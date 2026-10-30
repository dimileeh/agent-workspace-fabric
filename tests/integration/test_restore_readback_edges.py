"""Defensive SQL readback checks complement actual concurrent transaction tests."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from awf.hosted_node import restore_materials, restore_reconciliation
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from awf.hosted_node.lifecycle_schemas import RecoveryAcknowledgement
from awf.hosted_node.restore_reconciliation_inventory import inventory
from awf.hosted_node.restore_reconciliation_models import RestoreReconciliationRow, RestoreRowHold
from awf.hosted_node.schemas import Preparation
from tests.integration.test_checkpoint_marker import schema_revision as schema_revision
from tests.integration.test_node_idle_gate import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node
from tests.integration.test_restore_quarantine import restore
from tests.integration.test_restore_reconciliation_transactions import setup
from tests.integration.test_restore_row_families import seed_rows
from tests.unit.test_hosted_runtime_contract import hosted_create


async def test_pending_restore_cannot_stage_material_before_acceptance(ready_node):
    _, receiver, _, _ = await restore(ready_node)
    binding = hosted_create()["hosted"]["credentials"]["agent"]
    value = Preparation.model_validate({key: binding[key] for key in Preparation.model_fields})
    async with receiver.factory() as session:
        with pytest.raises(MaterialDeniedError, match="RESTORE_QUARANTINED"):
            await restore_materials.request(receiver, session, value, "prepare")


async def test_direct_initial_reconciliation_inserts_all_fixed_orphan_holds(ready_node):
    _, receiver, _ = ready_node
    await seed_rows(receiver)
    _, receiver, request = await setup(ready_node)
    await restore_reconciliation.reconcile_restore(
        receiver.lifecycle, RecoveryAcknowledgement(**request)
    )
    async with receiver.factory() as session:
        rows = (await session.scalars(select(RestoreRowHold))).all()
        assert {row.family for row in rows} == {
            "tasks",
            "callback_subscriptions",
            "callback_deliveries",
            "service_gc_requests",
        }
        assert all(row.state == "held" and row.resolved_journal_id is None for row in rows)


@pytest.mark.parametrize("fault", ["stage_state", "missing_final", "changed_final"])
async def test_inconsistent_journal_readback_never_commits(ready_node, monkeypatch, fault):
    _, receiver, request = await setup(ready_node)
    service = receiver.lifecycle
    value = RecoveryAcknowledgement(**request)
    original_host = service.observe_host
    calls = 0

    async def preflight_only(identity):
        nonlocal calls
        calls += 1
        return await original_host(identity) if calls == 1 else None

    service.observe_host = preflight_only
    with pytest.raises(MaterialDeniedError, match="RESTORE_EVIDENCE_UNAVAILABLE"):
        await restore_reconciliation.reconcile_restore(service, value)
    service.observe_host = original_host
    # Inject inconsistent read results, not database writes or trigger bypasses.
    # SQL transaction and rollback still use the real PostgreSQL runtime role.
    if fault == "stage_state":
        original = AsyncSession.scalar

        async def changed(session, *args, **kwargs):
            row = await original(session, *args, **kwargs)
            if isinstance(row, RestoreReconciliationRow) and "FOR UPDATE" in str(args[0]):
                return SimpleNamespace(plan_sha256=row.plan_sha256, state="committed")
            return row

        monkeypatch.setattr(AsyncSession, "scalar", changed)
    else:
        original = AsyncSession.get

        async def changed(session, model, *args, **kwargs):
            row = await original(session, model, *args, **kwargs)
            if model is RestoreReconciliationRow and kwargs.get("with_for_update"):
                return (
                    None
                    if fault == "missing_final"
                    else SimpleNamespace(state="staged", plan_sha256="0" * 64)
                )
            return row

        monkeypatch.setattr(AsyncSession, "get", changed)
    with pytest.raises(MaterialDeniedError, match="RESTORE_RECONCILIATION_CONFLICT"):
        await restore_reconciliation.reconcile_restore(service, value)
    async with receiver.factory() as session:
        assert (await session.scalars(select(RestoreReconciliationRow.state))).all() == ["staged"]
        assert (await session.get(NodeLifecycleRow, 1)).restore_quarantine is not None


async def test_incomplete_parent_readback_is_not_an_unowned_action(ready_node, monkeypatch):
    _, receiver, request = await setup(ready_node)
    original = AsyncSession.execute

    async def missing_workspace(session, statement, *args, **kwargs):
        if "FROM workspaces t WHERE" in str(statement):
            return Mock(all=Mock(return_value=[]))
        return await original(session, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", missing_workspace)
    async with receiver.factory() as session:
        gate = await session.get(NodeLifecycleRow, 1)
        with pytest.raises(MaterialDeniedError, match="RESTORE_ASSOCIATION_INVALID"):
            await inventory(session, gate, RecoveryAcknowledgement(**request))
        assert (
            await session.scalar(text("SELECT count(*) FROM hosted_restore_reconciliations")) == 0
        )
