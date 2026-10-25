"""Real ASGI/PostgreSQL node barrier; external recovery/host evidence is synthetic."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from unittest.mock import Mock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError

from awf.api.app import configure_database, create_app
from awf.common.config import Settings, get_settings
from awf.db.models import Operation, Workspace
from awf.db.repositories import WorkspaceRepository
from awf.db.repositories.service_gc_request_repo import ServiceGCRequestRepository
from awf.db.session import make_session_factory
from awf.hosted_node.config import HostedSettings
from awf.hosted_node.identity import boot
from awf.hosted_node.lifecycle_barrier import bind_factory, install_barrier
from awf.hosted_node.lifecycle_schemas import (
    HostObservation,
    RecoveryAcknowledgement,
    RecoveryEvidence,
)
from awf.hosted_node.receiver import Receiver

PREFIX = "/v1/hosted-node/lifecycle"


@pytest.fixture
async def node(engine):
    async with engine.begin() as connection:
        await connection.run_sync(install_barrier)
    config = HostedSettings(
        owner="f28674ae-72bc-49df-8a5c-a758e1099f4b/nodea",
        incarnation="18446744073709551615",
        recovery_generation=1,
        controller_token="synthetic-controller-only",
        material_root=Path("/unused-no-material-access"),
        lifecycle={"disk_ref": "disk-123", "database_ref": "node-database"},
    )
    factory = make_session_factory(engine)
    identity = await boot(config, factory)
    store_lock = RLock()
    store = Mock(_locked_root=Mock(side_effect=lambda: store_lock))
    receiver = Receiver(config, factory, identity, store)
    app = create_app(use_lifespan=False)
    configure_database(app, factory)
    app.state.hosted_receiver = receiver
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        api_token="synthetic-operator-only",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://core.test") as client:
        client.headers.update(
            {
                "Authorization": "Bearer synthetic-controller-only",
                **identity.headers(),
            }
        )
        yield client, receiver, engine


def evidence(receiver):
    service = receiver.lifecycle
    assert service is not None

    async def host(identity):
        return HostObservation(identity, datetime.now(UTC) + timedelta(seconds=30), True, True)

    async def recovery(identity, previous, receipt):
        return RecoveryEvidence(
            identity,
            previous,
            "disk-123",
            "node-database",
            receipt,
            datetime.now(UTC) + timedelta(seconds=30),
        )

    service.observe_host, service.verify_recovery = host, recovery
    return service


async def acknowledge(client):
    return await client.post(
        PREFIX + "/recovery/acknowledge",
        json={
            "idempotency_key": "recovery-1",
            "receipt_ref": "verified-recovery-receipt",
        },
    )


async def create(factory):
    async with factory.begin() as session:
        return await WorkspaceRepository(session).create(
            repo_url="https://example.invalid/repo.git",
            branch_base="main",
            task_title="synthetic",
            task_prompt="must never execute",
            agent="codex",
            test_commands=[],
        )


async def terminal_workspace(receiver):
    workspace = await create(receiver.factory)
    async with receiver.factory.begin() as session:
        await session.execute(
            update(Workspace).where(Workspace.id == workspace.id).values(status="completed")
        )
    return workspace


async def test_default_proofs_and_old_identity_ack_never_open_admission(node):
    client, receiver, _ = node
    response = await client.get(PREFIX + "/readiness")
    assert response.status_code == 200, response.text
    assert response.json()["admission_closed"]
    assert not response.json()["runtime_ready"]
    assert not response.json()["quiescent"]
    assert (await acknowledge(client)).status_code == 503
    assert (
        await client.post(
            "/v1/hosted-node/identity/acknowledge",
            json={
                "idempotency_key": "material-only",
            },
        )
    ).status_code == 200
    with pytest.raises(DBAPIError, match="NODE_ADMISSION_CLOSED"):
        await create(receiver.factory)
    assert (await client.post(PREFIX + "/drain/abort", json={"generation": 1})).json()[
        "admission_closed"
    ]


async def test_verified_recovery_drain_abort_and_replayed_generation(node):
    client, receiver, _ = node
    evidence(receiver)
    assert (await acknowledge(client)).status_code == 200
    assert (await acknowledge(client)).status_code == 200
    state = (await client.get(PREFIX + "/readiness")).json()
    assert state["node_ready"] and not state["runtime_ready"]
    assert state["quiescent"] and state["idle_since"]
    response = await client.post(PREFIX + "/drain", json={"generation": 1})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "drained"
    assert response.json()["quiescent"] and response.json()["admission_closed"]
    with pytest.raises(DBAPIError, match="NODE_ADMISSION_CLOSED"):
        await create(receiver.factory)
    assert (await client.post(PREFIX + "/drain/abort", json={"generation": 1})).json()[
        "state"
    ] == "ready"
    assert (await client.post(PREFIX + "/drain", json={"generation": 1})).status_code == 409
    assert (await client.post(PREFIX + "/drain", json={"generation": 2})).json()[
        "state"
    ] == "drained"
    # A late abort for an older token cannot reopen the newer drain.
    assert (await client.post(PREFIX + "/drain/abort", json={"generation": 1})).json()[
        "admission_closed"
    ]


async def test_expired_unclaimed_gc_allows_idle_drain_and_reboot_recovery(node):
    client, receiver, engine = node
    evidence(receiver)
    assert (await acknowledge(client)).status_code == 200
    now = datetime.now(UTC)
    async with receiver.factory.begin() as session:
        repository = ServiceGCRequestRepository(session)
        request = await repository.create_pending(
            node_id=None,
            requested_at=now - timedelta(minutes=2),
            deadline_at=now - timedelta(minutes=1),
        )
        assert request.claimed_at is None
        assert await repository.expire_stale_requests(node_id="nodea", now=now) == [request.id]
        assert request.status == "expired"
    assert (await client.get(PREFIX + "/readiness")).json()["quiescent"]
    assert (await client.post(PREFIX + "/drain", json={"generation": 1})).json()[
        "state"
    ] == "drained"
    factory = make_session_factory(engine)
    identity = await boot(receiver.config, factory)
    restarted = evidence(Receiver(receiver.config, factory, identity, Mock()))
    recovered = await restarted.acknowledge(
        RecoveryAcknowledgement(idempotency_key="reboot", receipt_ref="verified-recovery-receipt")
    )
    assert recovered.node_ready and not recovered.admission_closed


@pytest.mark.parametrize("status", ["pending", "running", "unknown"])
async def test_nonterminal_gc_still_blocks_idle_and_drain(node, status):
    client, receiver, _ = node
    evidence(receiver)
    assert (await acknowledge(client)).status_code == 200
    now = datetime.now(UTC)
    async with receiver.factory.begin() as session:
        request = await ServiceGCRequestRepository(session).create_pending(
            node_id=None, requested_at=now, deadline_at=now + timedelta(minutes=1)
        )
        request.status = status
    assert not (await client.get(PREFIX + "/readiness")).json()["quiescent"]
    response = await client.post(PREFIX + "/drain", json={"generation": 1})
    assert response.status_code == 200
    assert response.json()["state"] == "ready" and not response.json()["quiescent"]


@pytest.mark.parametrize(
    "status", ["requested", "running", "monitoring_pr", "blocked", "recovering", "unexpected"]
)
async def test_any_unresolved_work_prevents_idle_and_drain(node, status):
    client, receiver, _ = node
    evidence(receiver)
    assert (await acknowledge(client)).status_code == 200
    workspace = await create(receiver.factory)
    async with receiver.factory.begin() as session:
        await session.execute(
            update(Workspace).where(Workspace.id == workspace.id).values(status=status)
        )
    response = await client.post(PREFIX + "/drain", json={"generation": 1})
    assert response.status_code == 200, response.text
    assert not response.json()["quiescent"]
    assert response.json()["state"] == "ready"
    assert response.json()["idle_since"] is None


async def test_same_vm_reboot_fences_old_sessions_and_requires_new_recovery(node):
    client, receiver, engine = node
    evidence(receiver)
    assert (await acknowledge(client)).status_code == 200
    old_identity = receiver.identity
    new_factory = make_session_factory(engine)
    new_identity = await boot(receiver.config, new_factory)
    assert new_identity.incarnation == old_identity.incarnation
    assert new_identity.boot_epoch > old_identity.boot_epoch
    with pytest.raises(DBAPIError, match="NODE_IDENTITY_MISMATCH"):
        await create(receiver.factory)
    with pytest.raises(DBAPIError, match="NODE_ADMISSION_CLOSED"):
        await create(new_factory)
    assert (await client.get(PREFIX + "/readiness")).status_code == 403


async def test_unbound_worker_cannot_bypass_persisted_gate(node):
    client, receiver, engine = node
    evidence(receiver)
    assert (await acknowledge(client)).status_code == 200
    with pytest.raises(DBAPIError, match="NODE_IDENTITY_MISMATCH"):
        await create(make_session_factory(engine))


async def test_receiver_shutdown_closes_database_admission(node):
    client, receiver, _ = node
    evidence(receiver)
    assert (await acknowledge(client)).status_code == 200
    await receiver.close()
    assert receiver.views.closed
    assert receiver.store._locked_root.call_count == 2
    receiver.store.close.assert_called_once_with()
    with pytest.raises(DBAPIError, match="NODE_ADMISSION_CLOSED"):
        await create(receiver.factory)


async def test_admission_committing_first_is_seen_by_drain(node):
    client, receiver, _ = node
    evidence(receiver)
    assert (await acknowledge(client)).status_code == 200
    async with receiver.factory() as writer:
        await WorkspaceRepository(writer).create(
            repo_url="https://example.invalid/repo.git",
            branch_base="main",
            task_title="race",
            task_prompt="no execution",
            agent="codex",
            test_commands=[],
        )
        pending = asyncio.create_task(client.post(PREFIX + "/drain", json={"generation": 1}))
        await asyncio.sleep(0.05)
        assert not pending.done()
        await writer.commit()
        result = await asyncio.wait_for(pending, 2)
    assert not result.json()["quiescent"]
    assert result.json()["state"] == "ready"


async def test_closed_drain_wins_against_raw_claim_during_external_probe(node):
    client, receiver, engine = node
    service = evidence(receiver)
    assert (await acknowledge(client)).status_code == 200
    workspace = await terminal_workspace(receiver)
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.observe_host

    async def pause(identity):
        entered.set()
        await release.wait()
        return await original(identity)

    service.observe_host = pause
    pending = asyncio.create_task(client.post(PREFIX + "/drain", json={"generation": 1}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        peer = make_session_factory(engine)
        bind_factory(peer, receiver.identity)
        with pytest.raises(DBAPIError, match="NODE_ADMISSION_CLOSED"):
            await create(peer)
        async with peer.begin() as session:
            with pytest.raises(DBAPIError, match="NODE_ADMISSION_CLOSED"):
                await session.execute(
                    text(
                        "UPDATE workspaces SET execution_claimed_by = 'synthetic-worker' WHERE id = :id"
                    ),
                    {"id": workspace.id},
                )
    finally:
        release.set()
    result = await asyncio.wait_for(pending, 2)
    assert result.json()["state"] == "drained"


async def test_missing_or_stale_host_evidence_cannot_acknowledge_drain(node):
    client, receiver, _ = node
    service = evidence(receiver)
    assert (await acknowledge(client)).status_code == 200
    original = service.observe_host

    async def stale(identity):
        return replace(
            await original(identity), expires_at=datetime.now(UTC) - timedelta(seconds=1)
        )

    service.observe_host = stale
    result = await client.post(PREFIX + "/drain", json={"generation": 1})
    assert result.json()["state"] == "draining"
    assert result.json()["admission_closed"] and not result.json()["quiescent"]


async def test_local_core_database_without_opt_in_is_unchanged(engine):
    async with engine.begin() as connection:
        await connection.run_sync(install_barrier)
    workspace = await create(make_session_factory(engine))
    assert workspace.status == "requested"


async def test_barrier_schema_is_installed_for_all_execution_tables(node):
    _, receiver, _ = node
    async with receiver.factory() as session:
        count = await session.scalar(
            text(
                "SELECT count(*) FROM pg_trigger WHERE tgname = 'awf_node_admission' "
                "AND tgrelid IN (SELECT oid FROM pg_class WHERE relnamespace = current_schema()::regnamespace)"
            )
        )
    assert count >= 8


async def test_raw_claim_committing_first_prevents_drain(node):
    client, receiver, _ = node
    evidence(receiver)
    assert (await acknowledge(client)).status_code == 200
    workspace = await terminal_workspace(receiver)
    async with receiver.factory() as writer:
        await writer.execute(
            text(
                "UPDATE workspaces SET monitor_claimed_by = 'synthetic-monitor', "
                "monitor_claim_expires_at = now() - interval '1 day' WHERE id = :id"
            ),
            {"id": workspace.id},
        )
        pending = asyncio.create_task(client.post(PREFIX + "/drain", json={"generation": 1}))
        await asyncio.sleep(0.05)
        assert not pending.done()
        await writer.commit()
        result = await asyncio.wait_for(pending, 2)
    assert result.json()["state"] == "ready"
    assert not result.json()["quiescent"]


async def test_unknown_operation_and_retained_runtime_block_idle(node):
    client, receiver, _ = node
    evidence(receiver)
    assert (await acknowledge(client)).status_code == 200
    workspace = await terminal_workspace(receiver)
    async with receiver.factory.begin() as session:
        session.add(
            Operation(id="unknown", workspace_id=workspace.id, type="run", status="unknown")
        )
    assert not (await client.get(PREFIX + "/readiness")).json()["quiescent"]
    async with receiver.factory.begin() as session:
        await session.execute(update(Operation).values(status="succeeded"))
        await session.execute(update(Workspace).values(compose_project_name="retained-runtime"))
    assert not (await client.get(PREFIX + "/readiness")).json()["quiescent"]


async def test_work_between_observations_resets_idle_cooldown(node):
    client, receiver, _ = node
    evidence(receiver)
    assert (await acknowledge(client)).status_code == 200
    before = (await client.get(PREFIX + "/readiness")).json()["idle_since"]
    await terminal_workspace(receiver)
    after = (await client.get(PREFIX + "/readiness")).json()["idle_since"]
    assert after > before


@pytest.mark.parametrize(
    "field,value",
    [
        ("disk_ref", "wrong-disk"),
        ("database_ref", "wrong-db"),
        ("receipt_ref", "wrong-receipt"),
        ("previous_identity", "current"),
        ("identity", "other"),
        ("expires_at", "expired"),
    ],
)
async def test_recovery_evidence_must_match_every_binding(node, field, value):
    client, receiver, _ = node
    service = evidence(receiver)
    verifier = service.verify_recovery
    if field == "previous_identity":
        value = receiver.identity
    elif field == "identity":
        value = receiver.identity.model_copy(update={"incarnation": "123"})
    elif field == "expires_at":
        value = datetime.now(UTC) - timedelta(seconds=1)

    async def wrong(*args):
        return replace(await verifier(*args), **{field: value})

    service.verify_recovery = wrong
    assert (await acknowledge(client)).status_code == 503
    assert (await client.get(PREFIX + "/readiness")).json()["admission_closed"]


async def test_abort_during_probe_and_handler_restart_preserve_tombstone(node):
    from awf.hosted_node.lifecycle import NodeLifecycle

    client, receiver, engine = node
    service = evidence(receiver)
    assert (await acknowledge(client)).status_code == 200
    entered, release = asyncio.Event(), asyncio.Event()
    original = service.observe_host

    async def pause(identity):
        entered.set()
        await release.wait()
        return await original(identity)

    service.observe_host = pause
    pending = asyncio.create_task(client.post(PREFIX + "/drain", json={"generation": 7}))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert (await client.post(PREFIX + "/drain/abort", json={"generation": 7})).json()[
            "state"
        ] == "ready"
    finally:
        release.set()
    assert not (await asyncio.wait_for(pending, 2)).json()["admission_closed"]
    peer = make_session_factory(engine)
    bind_factory(peer, receiver.identity)
    receiver.lifecycle = NodeLifecycle(peer, receiver.identity)
    evidence(receiver)
    assert (await client.post(PREFIX + "/drain", json={"generation": 7})).status_code == 409
    assert (await client.post(PREFIX + "/drain", json={"generation": 8})).json()[
        "state"
    ] == "drained"
    assert (await acknowledge(client)).json()["state"] == "drained"


@pytest.mark.parametrize(
    "headers,code",
    [
        ({"Authorization": "Bearer invalid"}, 401),
        ({"Authorization": "Bearer synthetic-operator-only"}, 403),
        ({"X-AWF-Expected-Owner": "another-tenant/nodea"}, 403),
        ({"X-AWF-Expected-Incarnation": "123"}, 403),
        ({"X-AWF-Expected-Recovery-Generation": "2"}, 403),
        ({"X-AWF-Expected-Boot-Epoch": "99"}, 403),
    ],
)
async def test_lifecycle_controller_and_identity_isolation(node, headers, code):
    client, _, _ = node
    response = await client.post(PREFIX + "/drain", json={"generation": 1}, headers=headers)
    assert response.status_code == code


async def test_runtime_mutations_remain_closed_after_node_recovery(node):
    client, receiver, _ = node
    evidence(receiver)
    assert (await acknowledge(client)).status_code == 200
    result = await client.post("/v1/workspaces", json={})
    assert result.status_code == 403
    assert result.json()["error_code"] == "HOSTED_RUNTIME_INTEGRATION_REQUIRED"


async def test_boot_cannot_change_persisted_disk_or_database_binding(node):
    _, receiver, engine = node
    config = receiver.config.model_copy(
        update={
            "lifecycle": receiver.config.lifecycle.model_copy(
                update={"disk_ref": "different-disk"}
            ),
        }
    )
    from awf.hosted_node.identity import MaterialDeniedError

    with pytest.raises(MaterialDeniedError, match="NODE_STORAGE_MISMATCH"):
        await boot(config, make_session_factory(engine))


async def test_database_failure_returns_sanitized_unavailable(node, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    import awf.hosted_node.lifecycle as module

    client, _, _ = node

    async def unavailable(*args):
        raise SQLAlchemyError("synthetic-private-diagnostic")

    monkeypatch.setattr(module, "database_quiescent", unavailable)
    response = await client.get(PREFIX + "/readiness")
    assert response.status_code == 503
    assert "synthetic-private-diagnostic" not in response.text


async def test_enabling_lifecycle_keeps_previous_hosted_identity(engine):
    from awf.hosted_node.lifecycle_models import NodeLifecycleRow

    async with engine.begin() as connection:
        await connection.run_sync(install_barrier)
    config = HostedSettings(
        owner="owner",
        incarnation="123",
        recovery_generation=1,
        controller_token="synthetic-controller-only",
        material_root="/unused",
    )
    factory = make_session_factory(engine)
    previous = await boot(config, factory)
    configured = HostedSettings(
        **{
            **config.model_dump(),
            "lifecycle": {"disk_ref": "disk", "database_ref": "db"},
        }
    )
    identity = await boot(configured, factory)
    assert identity.boot_epoch > previous.boot_epoch
    async with factory() as session:
        row = await session.get(NodeLifecycleRow, 1)
        assert row.previous_identity == previous.model_dump()


async def test_reboot_during_recovery_verification_rejects_old_ack(node):
    client, receiver, engine = node
    service = evidence(receiver)
    original = service.verify_recovery
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    service.verify_recovery = pause
    pending = asyncio.create_task(acknowledge(client))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await boot(receiver.config, make_session_factory(engine))
    finally:
        release.set()
    assert (await asyncio.wait_for(pending, 2)).status_code == 403


async def test_real_repository_restart_claim_cannot_run_before_recovery(node):
    from awf.db.repositories.workspace_repo_claims import claim_worker_restart_recovery_execution

    client, receiver, engine = node
    evidence(receiver)
    assert (await acknowledge(client)).status_code == 200
    workspace = await create(receiver.factory)
    async with receiver.factory.begin() as session:
        await session.execute(
            update(Workspace).where(Workspace.id == workspace.id).values(status="running")
        )
        session.add(
            Operation(
                id="restart-recovery",
                workspace_id=workspace.id,
                type="validate",
                status="pending",
                payload={"source": "worker_restart", "recovery_mode": "validate_only"},
            )
        )
    peer = make_session_factory(engine)
    await boot(receiver.config, peer)
    async with peer.begin() as session:
        with pytest.raises(DBAPIError, match="NODE_ADMISSION_CLOSED"):
            await claim_worker_restart_recovery_execution(
                session,
                workspace.id,
                owner_id="synthetic-worker",
                lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
                claim_cutoff=datetime.now(UTC),
            )
