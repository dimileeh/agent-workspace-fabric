"""Fresh restore credentials stage through real HTTP before exact SQL acceptance."""

import base64
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from awf.db.models import MergeCandidate, Operation, Task, TaskAttempt, Workspace
from awf.db.repositories import WorkspaceRepository
from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.material_store import MaterialStore
from awf.hosted_node.restore_reconciliation_models import RestoreReconciliationRow
from awf.hosted_node.restore_reconciliation_schemas import (
    RestoreActionEvidence,
    RestoreMaterialPermit,
    RestoreOperationDecision,
    RestoreWorkspaceDecision,
)
from awf.hosted_node.runtime_authority import WorkspaceAuthority
from awf.hosted_node.runtime_schemas import HostedWorkspace
from awf.hosted_node.runtime_views import RuntimeViews
from awf.hosted_node.schemas import Preparation
from tests.integration.test_checkpoint_marker import schema_revision as schema_revision
from tests.integration.test_node_idle_gate import PREFIX, create
from tests.integration.test_node_idle_gate import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node
from tests.integration.test_restore_reconciliation import unknown_decisions
from tests.unit.test_hosted_runtime_contract import hosted_create


@pytest.fixture
async def resumable_restore(ready_node, monkeypatch, request):
    from tests.integration import test_restore_quarantine as fixture

    _, receiver, _ = ready_node
    created = []

    async def associated(factory):
        workspace = await create(factory)
        association = hosted_create()["hosted"]
        association["workspace_id"] = workspace.id
        for value in association["credentials"].values():
            value.update(workspace_id=workspace.id, identity=receiver.identity.model_dump())
        async with factory.begin() as session:
            await session.execute(
                update(Workspace)
                .where(Workspace.id == workspace.id)
                .values(
                    node_id=receiver.identity.owner,
                    task_policy={"hosted_node": association},
                    pr_url="https://github.com/example/repo/pull/1",
                    pr_number=1,
                )
            )
            if getattr(request, "param", None) in {"with_operations", "with_merge"} and len(
                created
            ) in (0, 2):
                session.add(
                    Operation(
                        id=f"op_{workspace.id}",
                        workspace_id=workspace.id,
                        type="create" if not created else "agent.run",
                        status="pending" if not created else "running",
                        started_at=None if not created else datetime.now(UTC),
                    )
                )
            if getattr(request, "param", None) == "with_merge" and len(created) == 2:
                session.add(
                    Task(
                        id="monitor-task",
                        repo_url=workspace.repo_url,
                        base_branch="main",
                        title="Monitor",
                        prompt="Current monitor",
                    )
                )
                await session.flush()
                session.add(
                    TaskAttempt(
                        id="monitor-attempt",
                        task_id="monitor-task",
                        workspace_id=workspace.id,
                        attempt_number=1,
                        agent="codex",
                        status="monitoring_pr",
                        title="Monitor",
                        repo_url=workspace.repo_url,
                        base_branch="main",
                    )
                )
                await session.flush()
                session.add(
                    MergeCandidate(
                        id="monitor-merge",
                        task_id="monitor-task",
                        attempt_id="monitor-attempt",
                        workspace_id=workspace.id,
                        pr_url="https://github.com/example/repo/pull/1",
                        pr_number=1,
                        repo_url=workspace.repo_url,
                        base_branch="main",
                        status="open",
                        ready=True,
                        stale=False,
                    )
                )
        created.append(workspace.id)
        return workspace

    monkeypatch.setattr(fixture, "create", associated)
    client, receiver, workspaces, binding = await fixture.restore(ready_node)
    directory = tempfile.TemporaryDirectory(dir="/dev/shm")
    receiver.store = MaterialStore(Path(directory.name))
    receiver.views = RuntimeViews(receiver.store, identity=receiver.identity)
    receiver.config = receiver.config.model_copy(update={"runtime_enabled": True})
    receiver.lifecycle.verify_bindings = receiver.verify_recovery_bindings
    receiver.lifecycle.observe_runtime = AsyncMock(return_value=True)
    assert (
        await client.post(
            PREFIX + "/recovery/acknowledge",
            json={
                "idempotency_key": binding.restore_id,
                "receipt_ref": "restore-receipt",
            },
        )
    ).status_code == 200
    await receiver.identity_status("current-identity")
    captured = []
    proof = None

    async def current(inventory):
        nonlocal proof
        captured.append(inventory)
        if proof is None:
            baseline = unknown_decisions(inventory)
            rows = {row.row_id: row for row in inventory.rows if row.family == "workspaces"}
            decisions = []
            for decision in baseline.workspaces:
                row = rows[decision.workspace_id]
                if row.status not in ("requested", "monitoring_pr"):
                    decisions.append(decision)
                    continue
                fresh = row.hosted.model_dump(mode="json")
                for role, value in fresh["credentials"].items():
                    value.update(
                        identity=receiver.identity.model_dump(),
                        preparation_id=f"fresh-{role}",
                        grant_id=f"fresh-{role}",
                        idempotency_key=f"{row.row_id}-{role}",
                    )
                fact = RestoreActionEvidence(
                    action_id=row.row_id,
                    outcome="never_started" if row.status == "requested" else "monitor_safe",
                    evidence_ref="synthetic-current-complete-action-frontier",
                    evidence_sha256="d" * 64,
                    observed_at=datetime.now(UTC),
                    external_action_ref=f"current-action:{row.row_id}",
                )
                operations = tuple(
                    RestoreOperationDecision(
                        operation_id=operation.row_id,
                        preimage_sha256=operation.preimage_sha256,
                        fact=fact.model_copy(
                            update={
                                "action_id": operation.row_id,
                                "outcome": "never_started"
                                if row.status == "requested"
                                else "succeeded",
                                "external_action_ref": f"current-operation:{operation.row_id}",
                            }
                        ),
                    )
                    for operation in inventory.rows
                    if operation.family == "operations" and operation.workspace_id == row.row_id
                )
                decisions.append(
                    RestoreWorkspaceDecision(
                        **decision.model_dump(
                            exclude={"disposition", "fact", "fresh_association", "operations"}
                        ),
                        disposition="queued" if row.status == "requested" else "monitor",
                        fact=fact,
                        operations=operations,
                        fresh_association=HostedWorkspace.model_validate(fresh),
                    )
                )
            proof = baseline.model_copy(update={"workspaces": tuple(decisions)})
        return proof.model_copy(update={"expires_at": datetime.now(UTC) + timedelta(seconds=10)})

    receiver.lifecycle.verify_restore_reconciliation = current
    request = {"idempotency_key": "current-plan", "receipt_ref": "current-complete-action-evidence"}
    try:
        yield client, receiver, workspaces, request, captured
    finally:
        receiver.views.clear_owned()
        receiver.store.close()
        directory.cleanup()


async def staged(resumable_restore):
    client, receiver, _, request, _ = resumable_restore
    response = await client.post(PREFIX + "/recovery/acknowledge", json=request)
    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "RESTORE_MATERIALS_REQUIRED"
    async with receiver.factory() as session:
        row = (await session.scalars(select(RestoreReconciliationRow))).one()
        assert row.state == "staged" and row.result is None
        return row


async def install_selected(client, plan):
    for workspace in plan["workspaces"]:
        if workspace["fresh_association"] is None:
            continue
        for binding in workspace["fresh_association"]["credentials"].values():
            response = await client.post(
                "/v1/hosted-node/preparations",
                json={field: binding[field] for field in Preparation.model_fields},
            )
            assert response.status_code == 200, response.text
            response = await client.post(
                "/v1/hosted-node/materials/install",
                json={
                    **binding,
                    "material": base64.b64encode(b"synthetic-test-only-key").decode(),
                },
            )
            assert response.status_code == 200 and response.json()["status"] == "current", (
                response.text
            )


async def test_selected_existing_work_stages_before_resumption_and_new_admission(resumable_restore):
    client, receiver, workspaces, request, _ = resumable_restore
    journal = await staged(resumable_restore)
    permits = []

    async def authorize(value):
        assert value.journal_id == journal.id and value.plan_sha256 == journal.plan_sha256
        permits.append(value)
        return RestoreMaterialPermit(
            request=value, expires_at=datetime.now(UTC) + timedelta(seconds=10)
        )

    receiver.verify_restore_material = authorize
    await install_selected(client, journal.plan)
    assert len(permits) == 12  # prepare, pre-install and post-file publication, for four bindings.
    authority = WorkspaceAuthority(receiver)
    for workspace in workspaces:
        with pytest.raises(MaterialDeniedError, match="RECOVERY_ATTENTION_REQUIRED"):
            await authority.workspace(workspace.id)
        assert await authority.publication(workspace.id) is None
    response = await client.post(PREFIX + "/recovery/acknowledge", json=request)
    assert response.status_code == 200, response.text
    acceptance = response.json()["recovery_acceptance"]
    assert (
        acceptance["queued_workspaces"],
        acceptance["monitor_workspaces"],
        acceptance["attention_workspaces"],
    ) == (1, 1, 1)
    assert response.json()["runtime_ready"] and response.json()["admission_closed"]
    for workspace in (workspaces[0], workspaces[2]):
        material = await authority.workspace(workspace.id)
        assert material.environment["GH_TOKEN"] == "synthetic-test-only-key"
    with pytest.raises(MaterialDeniedError, match="RECOVERY_ATTENTION_REQUIRED"):
        await authority.workspace(workspaces[1].id)
    async with receiver.factory.begin() as session:
        repo = WorkspaceRepository(session)
        assert await repo.claim_monitoring_pr(
            workspaces[2].id,
            owner_id="current-monitor",
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
        )
        assert (
            await session.scalar(
                select(Operation.status).where(Operation.id == "unknown-operation")
            )
            == "running"
        )
    with pytest.raises(DBAPIError, match="NODE_MAINTENANCE_HELD"):
        await create(receiver.factory)
    released = await client.post(PREFIX + "/maintenance/release", json={"generation": 7})
    assert released.status_code == 200 and not released.json()["admission_closed"]
    await create(receiver.factory)


@pytest.mark.parametrize("resumable_restore", ["with_operations"], indirect=True)
async def test_known_operation_outcomes_settle_exactly_without_replay(resumable_restore):
    await test_selected_existing_work_stages_before_resumption_and_new_admission(resumable_restore)
    _, receiver, workspaces, _, _ = resumable_restore
    async with receiver.factory() as session:
        queued = await session.get(Operation, f"op_{workspaces[0].id}")
        completed = await session.get(Operation, f"op_{workspaces[2].id}")
        unknown = await session.get(Operation, "unknown-operation")
        assert (
            queued.status == "pending" and queued.started_at is None and queued.finished_at is None
        )
        assert completed.status == "succeeded" and completed.finished_at is not None
        assert completed.result == {
            "restore_evidence_ref": "synthetic-current-complete-action-frontier",
            "restore_evidence_sha256": "d" * 64,
        }
        assert unknown.status == "running" and unknown.finished_at is None


async def test_staged_material_requires_distinct_exact_current_permit(resumable_restore):
    client, receiver, _, _, _ = resumable_restore
    journal = await staged(resumable_restore)
    binding = next(
        w["fresh_association"]["credentials"]["agent"]
        for w in journal.plan["workspaces"]
        if w["fresh_association"]
    )
    response = await client.post(
        "/v1/hosted-node/preparations", json={key: binding[key] for key in Preparation.model_fields}
    )
    assert response.status_code == 503, response.text
    assert response.json()["error_code"] == "RESTORE_MATERIAL_AUTHORITY_UNAVAILABLE"
    assert not list(receiver.store.root.glob("*/*.material"))


@pytest.mark.parametrize(
    "fault", ["expired", "owner", "epoch", "plan", "receipt", "binding", "journal", "authorization"]
)
async def test_wrong_restore_permit_never_installs(resumable_restore, fault):
    client, receiver, _, _, _ = resumable_restore
    journal = await staged(resumable_restore)
    binding = next(
        w["fresh_association"]["credentials"]["agent"]
        for w in journal.plan["workspaces"]
        if w["fresh_association"]
    )

    async def wrong(value):
        changes = {
            "owner": {"identity": value.identity.model_copy(update={"owner": "foreign/node"})},
            "epoch": {"identity": value.identity.model_copy(update={"boot_epoch": 1})},
            "plan": {"plan_sha256": "0" * 64},
            "receipt": {"receipt_ref": "other"},
            "binding": {"proposed": value.proposed.model_copy(update={"fence": 99})},
            "journal": {"journal_id": "other"},
            "authorization": {"authorization_sha256": "0" * 64},
        }
        return RestoreMaterialPermit(
            request=value.model_copy(update=changes.get(fault, {})),
            expires_at=datetime.now(UTC) + timedelta(seconds=-1 if fault == "expired" else 10),
        )

    receiver.verify_restore_material = wrong
    response = await client.post(
        "/v1/hosted-node/preparations", json={key: binding[key] for key in Preparation.model_fields}
    )
    assert (
        response.status_code == 503
        and response.json()["error_code"] == "RESTORE_MATERIAL_AUTHORITY_UNAVAILABLE"
    ), response.text
    assert list(receiver.store.root.iterdir()) == []


async def test_expired_or_revoked_post_io_permit_never_publishes(resumable_restore):
    from awf.hosted_node.models import Grant, Receipt

    client, receiver, _, _, _ = resumable_restore
    journal = await staged(resumable_restore)
    binding = next(
        w["fresh_association"]["credentials"]["agent"]
        for w in journal.plan["workspaces"]
        if w["fresh_association"]
    )
    calls = 0

    async def valid_then_denied(value):
        nonlocal calls
        calls += 1
        if calls == 3:
            return None
        return RestoreMaterialPermit(
            request=value, expires_at=datetime.now(UTC) + timedelta(seconds=10)
        )

    receiver.verify_restore_material = valid_then_denied
    assert (
        await client.post(
            "/v1/hosted-node/preparations",
            json={key: binding[key] for key in Preparation.model_fields},
        )
    ).status_code == 200
    response = await client.post(
        "/v1/hosted-node/materials/install",
        json={**binding, "material": base64.b64encode(b"synthetic-key").decode()},
    )
    assert (
        response.status_code == 503
        and response.json()["error_code"] == "RESTORE_MATERIAL_AUTHORITY_UNAVAILABLE"
    ), response.text
    async with receiver.factory() as session:
        grant = (await session.scalars(select(Grant))).one()
        receipt = (await session.scalars(select(Receipt))).one()
        assert grant.current is None and receipt.state == "pending"
        assert not receiver.store.exists(grant.preparation, receipt.id)


async def test_new_current_decision_supersedes_stale_stage_not_its_identity(resumable_restore):
    from awf.hosted_node.models import Grant

    client, receiver, _, request, captured = resumable_restore
    first = await staged(resumable_restore)

    async def permit(value):
        return RestoreMaterialPermit(
            request=value, expires_at=datetime.now(UTC) + timedelta(seconds=10)
        )

    receiver.verify_restore_material = permit
    await install_selected(client, first.plan)
    previous = receiver.lifecycle.verify_restore_reconciliation

    async def newer(inventory):
        prior = await previous(captured[0])
        raw = prior.model_dump(mode="json")
        raw.update(
            idempotency_key=inventory.idempotency_key,
            receipt_ref=inventory.receipt_ref,
            inventory_sha256=inventory.inventory_sha256,
            authority_revision=2,
        )
        for decision in raw["workspaces"]:
            if decision["fresh_association"]:
                for value in decision["fresh_association"]["credentials"].values():
                    value["preparation_id"] += "-replacement"
                    value["idempotency_key"] += "-replacement"
        from awf.hosted_node.restore_reconciliation_schemas import RestoreReconciliationEvidence

        return RestoreReconciliationEvidence.model_validate(raw)

    receiver.lifecycle.verify_restore_reconciliation = newer
    next_request = {
        "idempotency_key": "current-plan-2",
        "receipt_ref": "new-current-complete-action-evidence",
    }
    response = await client.post(PREFIX + "/recovery/acknowledge", json=next_request)
    assert (
        response.status_code == 409
        and response.json()["error_code"] == "RESTORE_MATERIALS_REQUIRED"
    ), response.text
    async with receiver.factory() as session:
        original = await session.get(RestoreReconciliationRow, first.id)
        assert original.state == "superseded" and original.plan_sha256 == first.plan_sha256
        assert all(
            row.revoked and row.current is None for row in await session.scalars(select(Grant))
        )
        current = (
            await session.scalars(
                select(RestoreReconciliationRow).where(RestoreReconciliationRow.state == "staged")
            )
        ).one()
        assert current.id != first.id and current.revision == first.revision
    replay = await client.post(PREFIX + "/recovery/acknowledge", json=request)
    assert (
        replay.status_code == 409
        and replay.json()["error_code"] == "RESTORE_RECONCILIATION_CONFLICT"
    ), replay.text
    await install_selected(client, current.plan)
    assert (
        await client.post(PREFIX + "/recovery/acknowledge", json=next_request)
    ).status_code == 200
