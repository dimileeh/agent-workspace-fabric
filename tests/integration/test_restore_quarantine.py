"""Restored queued rows are not proof that an external action never happened."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError

from awf.db.models import Operation, Workspace
from awf.db.session import make_session_factory
from awf.hosted_node.identity import boot
from awf.hosted_node.lifecycle import NodeLifecycle
from tests.integration.test_checkpoint_marker import checkpoint_request, marker
from tests.integration.test_checkpoint_marker import schema_revision as schema_revision
from tests.integration.test_node_idle_gate import PREFIX, create, evidence
from tests.integration.test_node_idle_gate import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node


async def restore(ready_node, *, floor=90, boot_fault=None, hosted=None):
    from awf.hosted_node.checkpoint_schemas import CheckpointMarker
    from awf.hosted_node.restore_schemas import RestoreBinding, RestoreBootEvidence, RestoreEvidence

    client, receiver, engine = ready_node
    workspaces = [await create(receiver.factory) for _ in range(3)]
    async with receiver.factory.begin() as session:
        for workspace, status in zip(
            workspaces, ("requested", "running", "monitoring_pr"), strict=True
        ):
            await session.execute(
                update(Workspace).where(Workspace.id == workspace.id).values(status=status)
            )
        session.add(
            Operation(
                id="unknown-operation",
                workspace_id=workspaces[1].id,
                type="agent.run",
                status="running",
            )
        )
        if hosted is not None:
            from tests.unit.test_hosted_runtime_contract import hosted_create

            association = hosted_create()["hosted"]
            association["workspace_id"] = workspaces[0].id
            for credential in association["credentials"].values():
                credential["workspace_id"] = workspaces[0].id
                credential["identity"] = receiver.identity.model_dump()
                if hosted == "owner":
                    credential["identity"]["owner"] = "foreign/node"
            if hosted == "workspace":
                association["workspace_id"] = "ws_other"
            elif hosted == "malformed":
                association = {}
            await session.execute(
                update(Workspace)
                .where(Workspace.id == workspaces[0].id)
                .values(task_policy={"hosted_node": association})
            )
    assert (
        await client.post(PREFIX + "/maintenance/begin", json=checkpoint_request())
    ).status_code == 200
    recorded = CheckpointMarker.model_validate(await marker(receiver))
    binding = RestoreBinding(
        restore_id=str(uuid4()),
        checkpoint=recorded,
        manifest_sha256="b" * 64,
        files_sha256="c" * 64,
        schema_sha256="d" * 64,
        point_in_time=datetime.now(UTC),
        storage_mapping_sha256="e" * 64,
        fenced_identity=receiver.identity.model_copy(
            update={"incarnation": "123", "recovery_generation": 2, "boot_epoch": floor}
        ),
        candidate_incarnation="456",
        candidate_recovery_generation=3,
        boot_epoch_floor=floor,
        target_disk_ref=recorded.disk_ref,
        target_database_ref=recorded.database_ref,
    )
    peer = make_session_factory(engine)
    config = receiver.config.model_copy(update={"incarnation": "456", "recovery_generation": 3})
    expires = datetime.now(UTC) + timedelta(seconds=30)
    if boot_fault == "expired":
        expires = datetime.now(UTC) - timedelta(seconds=1)
    elif boot_fault is not None and boot_fault.startswith("source_"):
        assignments = {
            "source_disk": "disk_ref='different-source'",
            "source_database": "database_ref='different-source'",
            "source_phase": "phase='drained'",
            "source_released": "maintenance_released_through=7",
            "source_schema": "version_num='obsolete'",
            "source_owner": "incarnation='different-source'",
        }
        async with peer.begin() as session:
            table = {"source_schema": "alembic_version", "source_owner": "hosted_owners"}.get(
                boot_fault, "hosted_node_lifecycle"
            )
            await session.execute(text(f"UPDATE {table} SET " + assignments[boot_fault]))
    elif boot_fault is not None:
        changes = {
            "storage": {"target_database_ref": "wrong"},
            "floor": {"boot_epoch_floor": 1},
            "generation": {"candidate_recovery_generation": 1},
            "owner": {
                "fenced_identity": binding.fenced_identity.model_copy(
                    update={"owner": "other/node"}
                )
            },
        }
        binding = binding.model_copy(update=changes[boot_fault])
    identity = await boot(
        config,
        peer,
        restore=RestoreBootEvidence(binding, expires),
    )
    receiver.identity, receiver.factory, receiver.config = identity, peer, config
    receiver.lifecycle = NodeLifecycle(peer, identity)
    evidence(receiver)

    async def verify(actual, restore_id, receipt):
        assert actual == identity and restore_id == binding.restore_id
        return RestoreEvidence(
            binding, identity, receipt, datetime.now(UTC) + timedelta(seconds=10)
        )

    receiver.lifecycle.verify_restore = verify
    client.headers.update(identity.headers())
    return client, receiver, workspaces, binding


@pytest.mark.parametrize("hosted", ["valid", "owner", "workspace", "malformed"])
async def test_restore_checks_actual_hosted_association_without_resuming_credentials(
    ready_node, hosted
):
    client, receiver, workspaces, binding = await restore(ready_node, hosted=hosted)
    response = await client.post(
        PREFIX + "/recovery/acknowledge",
        json={"idempotency_key": binding.restore_id, "receipt_ref": "restore-receipt"},
    )
    if hosted == "valid":
        assert response.status_code == 200, response.text
        async with receiver.factory() as session:
            row = await session.get(Workspace, workspaces[0].id)
            assert row.status == "requested"
            assert row.task_policy["hosted_node"]["credentials"]["agent"]["identity"] == (
                binding.checkpoint.identity.model_dump()
            )
            assert row.task_policy["hosted_recovery"]["disposition"] == "attention"
    else:
        assert response.status_code == 409, response.text
        async with receiver.factory() as session:
            assert (
                await session.scalar(text("SELECT recovery_key FROM hosted_node_lifecycle")) is None
            )
            assert all(
                "hosted_recovery" not in (row.task_policy or {})
                for row in await session.scalars(select(Workspace))
            )


async def test_restore_quarantines_all_work_and_cannot_release_through_maintenance(ready_node):
    client, receiver, workspaces, binding = await restore(ready_node)
    assert receiver.identity.boot_epoch == 91
    response = await client.post(
        PREFIX + "/recovery/acknowledge",
        json={"idempotency_key": binding.restore_id, "receipt_ref": "restore-receipt"},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["admission_closed"] and not result["runtime_ready"]
    assert result["recovery_acceptance"]["queued_workspaces"] == 0
    assert result["recovery_acceptance"]["monitor_workspaces"] == 0
    assert result["recovery_acceptance"]["attention_workspaces"] == 3
    async with receiver.factory() as session:
        rows = list((await session.scalars(select(Workspace).order_by(Workspace.created_at))).all())
        assert {row.status for row in rows} == {"requested", "running", "monitoring_pr"}
        assert all(row.task_policy["hosted_recovery"]["disposition"] == "attention" for row in rows)
        assert await session.scalar(select(Operation.status)) == "running"
    assert (
        await client.post(PREFIX + "/maintenance/release", json={"generation": 7})
    ).status_code == 409
    assert (await client.post(PREFIX + "/drain/abort", json={"generation": 99})).json()[
        "admission_closed"
    ]
    assert (
        await client.post(
            PREFIX + "/recovery/acknowledge",
            json={"idempotency_key": binding.restore_id, "receipt_ref": "restore-receipt"},
        )
    ).status_code == 200
    with pytest.raises(DBAPIError, match="RESTORE_QUARANTINED"):
        await create(receiver.factory)
    with pytest.raises(DBAPIError, match="RESTORE_QUARANTINED"):
        async with receiver.factory.begin() as session:
            await session.execute(
                update(Workspace).where(Workspace.id == workspaces[0].id).values(task_policy={})
            )


@pytest.mark.parametrize("fault", ["absent", "expired", "identity", "binding"])
async def test_restore_rejects_missing_stale_or_conflicting_authority_atomically(ready_node, fault):
    from dataclasses import replace

    client, receiver, _, binding = await restore(ready_node)
    valid = receiver.lifecycle.verify_restore

    async def invalid(*args):
        result = await valid(*args)
        if fault == "absent":
            return None
        if fault == "expired":
            return replace(result, expires_at=datetime.now(UTC) - timedelta(seconds=1))
        if fault == "identity":
            return replace(result, identity=result.identity.model_copy(update={"boot_epoch": 1}))
        return replace(result, binding=binding.model_copy(update={"manifest_sha256": "f" * 64}))

    receiver.lifecycle.verify_restore = invalid
    response = await client.post(
        PREFIX + "/recovery/acknowledge",
        json={"idempotency_key": binding.restore_id, "receipt_ref": "restore-receipt"},
    )
    assert response.status_code == 503, response.text
    async with receiver.factory() as session:
        assert await session.scalar(text("SELECT phase FROM hosted_node_lifecycle")) == "recovering"
        assert await session.scalar(text("SELECT recovery_key FROM hosted_node_lifecycle")) is None
        assert all(
            "hosted_recovery" not in (row.task_policy or {})
            for row in await session.scalars(select(Workspace))
        )


async def test_node_quarantine_removes_even_unmarked_restored_work_from_candidates(ready_node):
    from awf.db.repositories.workspace_recovery import recovery_allows_work

    client, receiver, _, binding = await restore(ready_node)
    response = await client.post(
        PREFIX + "/recovery/acknowledge",
        json={"idempotency_key": binding.restore_id, "receipt_ref": "restore-receipt"},
    )
    assert response.status_code == 200, response.text
    async with receiver.factory() as session:
        assert not list(await session.scalars(select(Workspace.id).where(recovery_allows_work())))


@pytest.mark.parametrize("field", ["disk_ref", "database_ref", "checkpoint_marker"])
async def test_final_restore_revalidates_persisted_storage_and_marker(ready_node, field):
    client, receiver, _, binding = await restore(ready_node)
    async with receiver.factory.begin() as session:
        assignment = "NULL" if field == "checkpoint_marker" else "'changed'"
        await session.execute(text(f"UPDATE hosted_node_lifecycle SET {field}={assignment}"))
    response = await client.post(
        PREFIX + "/recovery/acknowledge",
        json={"idempotency_key": binding.restore_id, "receipt_ref": "restore-receipt"},
    )
    assert response.status_code in (409, 503), response.text
    async with receiver.factory() as session:
        assert await session.scalar(text("SELECT recovery_key FROM hosted_node_lifecycle")) is None


@pytest.mark.parametrize(
    "fault",
    [
        "expired",
        "storage",
        "floor",
        "generation",
        "owner",
        "source_disk",
        "source_database",
        "source_phase",
        "source_released",
        "source_schema",
        "source_owner",
    ],
)
async def test_nonrewound_authority_is_required_before_epoch_mutation(ready_node, fault):
    from pydantic import ValidationError

    from awf.hosted_node.identity import MaterialDeniedError

    _, receiver, _ = ready_node
    original = receiver.identity
    with pytest.raises((MaterialDeniedError, ValidationError)):
        await restore(ready_node, boot_fault=fault)
    async with receiver.factory() as session:
        assert (
            await session.scalar(text("SELECT boot_epoch FROM hosted_owners"))
            == original.boot_epoch
        )
        assert (
            await session.scalar(text("SELECT restore_quarantine FROM hosted_node_lifecycle"))
            is None
        )


async def test_restart_retains_quarantine_without_reissuing_restore_authority(ready_node):
    client, receiver, _, binding = await restore(ready_node)
    request = {"idempotency_key": binding.restore_id, "receipt_ref": "restore-receipt"}
    assert (await client.post(PREFIX + "/recovery/acknowledge", json=request)).status_code == 200
    receiver.identity = await boot(receiver.config, receiver.factory)
    receiver.lifecycle = NodeLifecycle(receiver.factory, receiver.identity)
    evidence(receiver)
    client.headers.update(receiver.identity.headers())
    assert (await client.post(PREFIX + "/recovery/acknowledge", json=request)).status_code == 503
    assert (await client.get(PREFIX + "/readiness")).json()["admission_closed"]


@pytest.mark.parametrize("hosted", [None, "valid"])
async def test_restore_attention_is_public_for_all_restored_work(ready_node, hosted):
    from tests.integration.test_retained_work_acceptance import public_workspace_views

    client, receiver, workspaces, binding = await restore(ready_node, hosted=hosted)
    request = {"idempotency_key": binding.restore_id, "receipt_ref": "restore-receipt"}
    assert (await client.post(PREFIX + "/recovery/acknowledge", json=request)).status_code == 200
    for workspace in workspaces:
        for body in await public_workspace_views(
            client, workspace.id, headers={"Authorization": "Bearer synthetic-operator-only"}
        ):
            assert body["attention_required"] is True
            assert body["awaiting_human_since"] is None
            assert body["awaiting_human_reason"] == "RECOVERY_ATTENTION_REQUIRED"
    async with receiver.factory() as session:
        rows = list(await session.scalars(select(Workspace)))
        assert all(
            row.task_policy["hosted_recovery"]["restore_id"] == binding.restore_id for row in rows
        )


async def test_expiry_after_actual_reconciliation_rolls_back_every_mutation(
    ready_node, monkeypatch
):
    from dataclasses import replace

    from awf.hosted_node import lifecycle

    client, receiver, _, binding = await restore(ready_node)
    verify = receiver.lifecycle.verify_restore
    accept = lifecycle.accept_restore

    async def short(*args):
        return replace(
            await verify(*args), expires_at=datetime.now(UTC) + timedelta(milliseconds=200)
        )

    async def delayed(*args):
        await accept(*args)
        await asyncio.sleep(0.25)

    receiver.lifecycle.verify_restore = short
    monkeypatch.setattr(lifecycle, "accept_restore", delayed)
    response = await client.post(
        PREFIX + "/recovery/acknowledge",
        json={"idempotency_key": binding.restore_id, "receipt_ref": "restore-receipt"},
    )
    assert response.status_code == 503, response.text
    async with receiver.factory() as session:
        assert await session.scalar(text("SELECT phase FROM hosted_node_lifecycle")) == "recovering"
        assert (
            await session.scalar(text("SELECT recovery_acceptance FROM hosted_node_lifecycle"))
            is None
        )
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM workspace_events WHERE event_type='workspace.recovery_accepted'"
                )
            )
            == 0
        )
        assert all(
            "hosted_recovery" not in (row.task_policy or {})
            for row in await session.scalars(select(Workspace))
        )


async def test_claim_and_concurrent_ack_serialize_behind_atomic_quarantine(ready_node, monkeypatch):
    from awf.hosted_node import lifecycle

    client, receiver, workspaces, binding = await restore(ready_node)
    accept = lifecycle.accept_restore
    classified, finish = asyncio.Event(), asyncio.Event()
    calls = 0

    async def paused(*args):
        nonlocal calls
        calls += 1
        await accept(*args)
        classified.set()
        await finish.wait()

    monkeypatch.setattr(lifecycle, "accept_restore", paused)
    request = {"idempotency_key": binding.restore_id, "receipt_ref": "restore-receipt"}
    first = asyncio.create_task(client.post(PREFIX + "/recovery/acknowledge", json=request))
    await asyncio.wait_for(classified.wait(), 5)

    async def claim():
        async with receiver.factory.begin() as session:
            await session.execute(
                update(Workspace)
                .where(Workspace.id == workspaces[0].id)
                .values(execution_claimed_by="ordinary-worker")
            )

    claiming = asyncio.create_task(claim())
    second = asyncio.create_task(client.post(PREFIX + "/recovery/acknowledge", json=request))
    try:
        await asyncio.sleep(0.05)
        assert not claiming.done() and not second.done()
    finally:
        finish.set()
    responses = await asyncio.gather(first, second)
    assert all(response.status_code == 200 for response in responses)
    assert responses[0].json()["recovery_acceptance"] == responses[1].json()["recovery_acceptance"]
    assert calls == 1
    with pytest.raises(DBAPIError, match="RESTORE_QUARANTINED"):
        await claiming
    async with receiver.factory() as session:
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM workspace_events WHERE event_type='workspace.recovery_accepted'"
                )
            )
            == 3
        )
        assert (
            await session.scalar(
                select(Workspace.execution_claimed_by).where(Workspace.id == workspaces[0].id)
            )
            is None
        )


@pytest.mark.parametrize("accepted", [False, True])
async def test_restore_quarantine_denies_material_writes_but_preserves_reads_and_revocation(
    ready_node, accepted
):
    import base64

    from awf.hosted_node.schemas import Preparation
    from tests.unit.test_hosted_runtime_contract import hosted_create

    client, receiver, _, binding = await restore(ready_node)
    if accepted:
        assert (
            await client.post(
                PREFIX + "/recovery/acknowledge",
                json={"idempotency_key": binding.restore_id, "receipt_ref": "restore-receipt"},
            )
        ).status_code == 200
    assert (
        await client.post(
            "/v1/hosted-node/identity/acknowledge", json={"idempotency_key": "materials"}
        )
    ).status_code == 200
    credential = hosted_create()["hosted"]["credentials"]["agent"]
    credential["identity"] = receiver.identity.model_dump()
    preparation = {field: credential[field] for field in Preparation.model_fields}
    requests = {
        "/preparations": preparation,
        "/materials/install": {
            **credential,
            "material": base64.b64encode(b"synthetic-never-installed").decode(),
        },
        "/preparations/renew": {
            "idempotency_key": "renew",
            "current": credential,
            "expected_preparation_expires_at": credential["expires_at"],
            "expires_at": (datetime.now(UTC) + timedelta(minutes=20)).isoformat(),
        },
    }
    for path, value in requests.items():
        response = await client.post("/v1/hosted-node" + path, json=value)
        assert response.status_code == 409, response.text
        assert response.json()["error_code"] == "RESTORE_QUARANTINED"
    response = await client.post("/v1/hosted-node/materials/status", json=credential)
    assert (
        response.status_code == 404 and response.json()["error_code"] == "PREPARATION_NOT_FOUND"
    ), response.text
    # Revocation is still permitted; an absent preparation is its normal denial,
    # not a quarantine gate. No secret publication was attempted.
    response = await client.post("/v1/hosted-node/materials/revoke", json=credential)
    assert (
        response.status_code == 404 and response.json()["error_code"] == "PREPARATION_NOT_FOUND"
    ), response.text
    receiver.store.install.assert_not_called()


@pytest.mark.parametrize("fault", ["over_bound", "foreign_node", "not_pending"])
async def test_restore_inventory_limits_and_state_conflicts_never_partially_accept(
    ready_node, fault
):
    from awf.hosted_node.lifecycle_models import NodeLifecycleRow
    from awf.hosted_node.retained_work import MAX_RETAINED_WORKSPACES

    _, receiver, _ = ready_node
    if fault == "over_bound":
        async with receiver.factory.begin() as session:
            await session.execute(
                insert(Workspace),
                [
                    {
                        "id": f"ws_extra_{index}",
                        "status": "requested",
                        "repo_url": "https://example.invalid/repo",
                        "branch_base": "main",
                        "task_title": "Never execute",
                        "task_prompt": "Synthetic",
                        "agent": "codex",
                    }
                    for index in range(MAX_RETAINED_WORKSPACES - 2)
                ],
            )
    elif fault == "foreign_node":
        row = await create(receiver.factory)
        async with receiver.factory.begin() as session:
            await session.execute(
                update(Workspace).where(Workspace.id == row.id).values(node_id="foreign/node")
            )
    client, receiver, _, binding = await restore(ready_node)
    if fault == "not_pending":
        async with receiver.factory.begin() as session:
            gate = await session.get(NodeLifecycleRow, 1)
            gate.restore_quarantine = {**gate.restore_quarantine, "state": "held"}
    response = await client.post(
        PREFIX + "/recovery/acknowledge",
        json={"idempotency_key": binding.restore_id, "receipt_ref": "restore-receipt"},
    )
    assert response.status_code == 409, response.text
    async with receiver.factory() as session:
        assert await session.scalar(text("SELECT recovery_key FROM hosted_node_lifecycle")) is None
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM workspace_events WHERE event_type='workspace.recovery_accepted'"
                )
            )
            == 0
        )
