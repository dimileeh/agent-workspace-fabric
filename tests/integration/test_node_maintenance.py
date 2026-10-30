"""Admission-only maintenance via real ASGI and PostgreSQL, no provider actions."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError

from awf.db.models import Operation, Workspace, WorkspaceSecretLease
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.hosted_node.identity import boot
from awf.hosted_node.lifecycle import NodeLifecycle
from awf.hosted_node.lifecycle_barrier import bind_factory
from awf.hosted_node.lifecycle_schemas import HostBootObservation
from awf.hosted_node.lifecycle_store import locked
from tests.integration.test_node_idle_gate import (
    PREFIX,
    acknowledge,
    create,
    evidence,
)
from tests.integration.test_node_idle_gate import node as node


@pytest.fixture
async def ready_node(node):
    client, receiver, _ = node
    evidence(receiver)
    assert (await acknowledge(client)).status_code == 200
    return node


async def maintenance(client, action="begin", generation=7, **kwargs):
    return await client.post(
        PREFIX + "/maintenance/" + action, json={"generation": generation}, **kwargs
    )


async def wait_for_blocked(factory, blocker):
    async with asyncio.timeout(5):
        while True:
            async with factory() as session:
                blocked = await session.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE :blocker = ANY(pg_blocking_pids(pid)))"
                    ),
                    {"blocker": blocker},
                )
            if blocked:
                return
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("status", ["requested", "running", "monitoring_pr", "blocked"])
async def test_hold_blocks_new_work_but_not_accepted_work_or_reads(ready_node, status):
    client, receiver, _ = ready_node
    workspace = await create(receiver.factory)
    response = await maintenance(client)
    assert response.status_code == 200, response.text
    assert response.json()["maintenance_generation"] == 7
    assert response.json()["admission_closed"]
    assert response.json()["state"] == "ready"
    async with receiver.factory.begin() as session:
        await session.execute(
            update(Workspace)
            .where(Workspace.id == workspace.id)
            .values(
                status=status,
                awaiting_human_since=datetime.now(UTC),
                monitor_claimed_by="accepted-monitor",
            )
        )
        session.add(
            Operation(
                id="accepted-operation",
                workspace_id=workspace.id,
                type="validate",
                status="running",
            )
        )
    response = await client.get(
        "/v1/workspaces/" + workspace.id,
        headers={"Authorization": "Bearer synthetic-operator-only"},
    )
    assert response.status_code == 200, response.text
    with pytest.raises(DBAPIError, match="NODE_MAINTENANCE_HELD"):
        await create(receiver.factory)
    # New demand's ordinary idle abort must never release planned maintenance.
    for path in ("/drain/abort", "/drain"):
        result = await client.post(
            PREFIX + path, json={"generation": 50 if path.endswith("abort") else 51}
        )
        assert result.status_code == 200, result.text
        assert result.json()["state"] == "ready"
        assert result.json()["admission_closed"]
        assert not result.json()["database_quiescent"]
    assert (await acknowledge(client)).json()["admission_closed"]
    async with receiver.factory.begin() as session:
        await session.execute(update(Operation).values(status="succeeded"))
        await session.execute(
            update(Workspace).values(
                status="completed", awaiting_human_since=None, monitor_claimed_by=None
            )
        )
    result = await client.post(PREFIX + "/drain", json={"generation": 51})
    assert result.json()["state"] == "drained"
    assert result.json()["maintenance_generation"] == 7
    # Release does not bypass an independent drain; abort does not bypass hold.
    assert (await maintenance(client, "release")).json()["admission_closed"]
    assert not (await client.post(PREFIX + "/drain/abort", json={"generation": 51})).json()[
        "admission_closed"
    ]
    await create(receiver.factory)


async def test_generation_replay_and_exact_release(ready_node):
    client, receiver, _ = ready_node
    assert (await maintenance(client, "release")).status_code == 409
    for generation in (7, 7, 8):
        assert (await maintenance(client, generation=generation)).status_code == 200
    for generation in (6, 7, 9):
        assert (await maintenance(client, "release", generation)).status_code == 409
    assert (await maintenance(client, generation=7)).status_code == 409
    for _ in range(2):
        result = await maintenance(client, "release", 8)
        assert result.status_code == 200
        assert result.json()["maintenance_generation"] is None
        assert not result.json()["admission_closed"]
    for generation in (7, 8):
        assert (await maintenance(client, generation=generation)).status_code == 409
    await create(receiver.factory)
    assert (await maintenance(client, generation=9)).status_code == 200
    assert (await maintenance(client, "release", 8)).status_code == 409


@pytest.mark.parametrize("action", ["begin", "release"])
@pytest.mark.parametrize(
    "headers,code",
    [
        ({"Authorization": "Bearer invalid"}, 401),
        ({"Authorization": "Bearer synthetic-operator-only"}, 403),
        ({"X-AWF-Expected-Owner": "other-owner/nodea"}, 403),
        ({"X-AWF-Expected-Incarnation": "other-vm"}, 403),
        ({"X-AWF-Expected-Recovery-Generation": "9"}, 403),
        ({"X-AWF-Expected-Boot-Epoch": "999"}, 403),
    ],
)
async def test_maintenance_requires_controller_and_exact_identity(
    ready_node, action, headers, code
):
    client, _, _ = ready_node
    assert (await maintenance(client)).status_code == 200
    assert (await maintenance(client, action, headers=headers)).status_code == code
    assert (await client.get(PREFIX + "/readiness")).json()["maintenance_generation"] == 7


@pytest.mark.parametrize("action", ["begin", "release"])
@pytest.mark.parametrize("generation", [0, -1, True, "7", 9223372036854775808])
async def test_maintenance_generation_is_strict_and_bounded(ready_node, action, generation):
    client, _, _ = ready_node
    assert (await maintenance(client, action, generation=generation)).status_code == 422


async def test_maximum_maintenance_generation_persists_hold_and_release_tombstone(ready_node):
    client, _, _ = ready_node
    generation = 9223372036854775807
    assert (await maintenance(client, generation=generation)).status_code == 200
    status = (await client.get(PREFIX + "/readiness")).json()
    assert status["maintenance_generation"] == generation and status["admission_closed"]
    assert (await maintenance(client, "release", generation)).status_code == 200
    status = (await client.get(PREFIX + "/readiness")).json()
    assert status["maintenance_generation"] is None and not status["admission_closed"]
    assert status["maintenance_released_through"] == generation
    stale = await maintenance(client, generation=generation)
    assert stale.status_code == 409
    assert stale.json()["error_code"] == "MAINTENANCE_GENERATION_STALE"


@pytest.mark.parametrize("replacement", [False, True])
async def test_restart_preserves_hold_and_rejects_old_identity(ready_node, replacement):
    client, receiver, engine = ready_node
    assert (await maintenance(client)).status_code == 200
    peer = make_session_factory(engine)
    bind_factory(peer, receiver.identity)
    receiver.lifecycle = NodeLifecycle(peer, receiver.identity)
    evidence(receiver)
    assert (await client.get(PREFIX + "/readiness")).json()["maintenance_generation"] == 7
    config = (
        receiver.config.model_copy(update={"incarnation": "new-vm", "recovery_generation": 2})
        if replacement
        else receiver.config
    )
    new_identity = await boot(config, peer)
    assert (await maintenance(client, "release")).status_code == 403
    with pytest.raises(DBAPIError, match="NODE_IDENTITY_MISMATCH"):
        await create(receiver.factory)
    receiver.identity = new_identity
    receiver.lifecycle = NodeLifecycle(peer, new_identity)
    evidence(receiver)
    client.headers.update(new_identity.headers())
    assert (await client.get(PREFIX + "/readiness")).json()["maintenance_generation"] == 7
    assert (await acknowledge(client)).json()["admission_closed"]
    with pytest.raises(DBAPIError, match="NODE_MAINTENANCE_HELD"):
        await create(peer)
    assert (await maintenance(client, "release", 8)).status_code == 409
    assert (await maintenance(client, "release")).status_code == 200
    # Tombstone also survives a later boot and recovery.
    receiver.identity = await boot(config, peer)
    receiver.lifecycle = NodeLifecycle(peer, receiver.identity)
    evidence(receiver)
    client.headers.update(receiver.identity.headers())
    assert (await acknowledge(client)).status_code == 200
    assert (await maintenance(client)).status_code == 409
    await create(peer)


async def test_admission_committing_first_is_accepted_before_hold(ready_node):
    client, receiver, _ = ready_node
    async with receiver.factory() as writer:
        workspace = await WorkspaceRepository(writer).create(
            repo_url="https://example.invalid/repo.git",
            branch_base="main",
            task_title="accepted",
            task_prompt="never executed",
            agent="codex",
            test_commands=[],
        )
        pending = asyncio.create_task(maintenance(client))
        try:
            await wait_for_blocked(
                receiver.factory, await writer.scalar(text("SELECT pg_backend_pid()"))
            )
            assert not pending.done()
        finally:
            await writer.commit()
        result = await asyncio.wait_for(pending, 5)
    assert result.status_code == 200
    assert result.json()["admission_closed"] and not result.json()["database_quiescent"]
    async with receiver.factory.begin() as session:
        await session.execute(
            update(Workspace).where(Workspace.id == workspace.id).values(status="running")
        )


@pytest.mark.parametrize("release", [False, True])
async def test_mutex_serializes_hold_release_and_new_admission(ready_node, monkeypatch, release):
    import awf.hosted_node.lifecycle as module

    client, receiver, _ = ready_node
    if release:
        assert (await maintenance(client)).status_code == 200
    entered, resume = asyncio.Event(), asyncio.Event()
    original = module.locked
    blocker = None

    async def paused(session, identity):
        nonlocal blocker
        row = await original(session, identity)
        blocker = await session.scalar(text("SELECT pg_backend_pid()"))
        entered.set()
        await resume.wait()
        return row

    monkeypatch.setattr(module, "locked", paused)
    pending = asyncio.create_task(maintenance(client, "release" if release else "begin"))
    admission = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        admission = asyncio.create_task(create(receiver.factory))
        await wait_for_blocked(receiver.factory, blocker)
        assert not admission.done()
    finally:
        resume.set()
    assert (await asyncio.wait_for(pending, 5)).status_code == 200
    if release:
        assert (await asyncio.wait_for(admission, 5)).status == "requested"
    else:
        with pytest.raises(DBAPIError, match="NODE_MAINTENANCE_HELD"):
            await asyncio.wait_for(admission, 5)


async def test_concurrent_begin_and_release_cannot_clear_newer_hold(ready_node):
    client, receiver, _ = ready_node
    assert (await maintenance(client)).status_code == 200
    async with receiver.factory.begin() as session:
        await locked(session, receiver.identity)
        begin = asyncio.create_task(maintenance(client, generation=8))
        release = asyncio.create_task(maintenance(client, "release", 7))
        await wait_for_blocked(
            receiver.factory, await session.scalar(text("SELECT pg_backend_pid()"))
        )
        assert not begin.done() and not release.done()
    begun, released = await asyncio.wait_for(asyncio.gather(begin, release), 5)
    assert begun.status_code == 200
    assert released.status_code in (200, 409)
    assert (await client.get(PREFIX + "/readiness")).json()["maintenance_generation"] == 8
    with pytest.raises(DBAPIError, match="NODE_MAINTENANCE_HELD"):
        await create(receiver.factory)


async def test_hold_can_be_set_during_recovery_without_opening_it(node):
    client, receiver, _ = node
    assert (await maintenance(client)).status_code == 200
    result = await maintenance(client, "release")
    assert result.json()["state"] == "recovering" and result.json()["admission_closed"]
    with pytest.raises(DBAPIError, match="NODE_ADMISSION_CLOSED"):
        await create(receiver.factory)


@pytest.mark.parametrize("action", ["begin", "release"])
async def test_inflight_old_boot_request_cannot_change_hold(ready_node, monkeypatch, action):
    import awf.hosted_node.lifecycle as module

    client, receiver, engine = ready_node
    assert (await maintenance(client)).status_code == 200
    entered, resume = asyncio.Event(), asyncio.Event()
    original = module.locked

    async def pause(session, identity):
        entered.set()
        await resume.wait()
        return await original(session, identity)

    monkeypatch.setattr(module, "locked", pause)
    pending = asyncio.create_task(maintenance(client, action))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        current = make_session_factory(engine)
        await boot(receiver.config, current)
    finally:
        resume.set()
    assert (await asyncio.wait_for(pending, 5)).status_code == 403
    async with current() as session:
        assert (
            await session.scalar(text("SELECT maintenance_generation FROM hosted_node_lifecycle"))
            == 7
        )


async def test_hold_does_not_let_foreign_raw_writer_continue(ready_node):
    client, receiver, engine = ready_node
    workspace = await create(receiver.factory)
    assert (await maintenance(client)).status_code == 200
    peer = make_session_factory(engine)
    bind_factory(peer, receiver.identity.model_copy(update={"owner": "other/node"}))
    async with peer.begin() as session:
        with pytest.raises(DBAPIError, match="NODE_IDENTITY_MISMATCH"):
            await session.execute(
                update(Workspace).where(Workspace.id == workspace.id).values(status="running")
            )


async def test_accepted_secret_lease_can_finish_but_blocks_drain_until_revoked(ready_node):
    client, receiver, _ = ready_node
    workspace = await create(receiver.factory)
    assert (await maintenance(client)).status_code == 200
    async with receiver.factory.begin() as session:
        session.add(
            WorkspaceSecretLease(
                id="accepted-lease",
                workspace_id=workspace.id,
                secret_name="synthetic",
                kind="mount",
                target="/run/awf/secrets/synthetic",
                mode="ro",
                status="mounted",
                issued_at=datetime.now(UTC),
            )
        )
        await session.execute(update(Workspace).values(status="completed"))
    result = await client.post(PREFIX + "/drain", json={"generation": 1})
    assert result.json()["state"] == "ready" and not result.json()["database_quiescent"]
    async with receiver.factory.begin() as session:
        await session.execute(update(WorkspaceSecretLease).values(status="revoked"))
    assert (await client.post(PREFIX + "/drain", json={"generation": 1})).json()[
        "state"
    ] == "drained"


async def test_boot_evidence_and_runtime_health_distinguish_hold_from_drain(ready_node):
    client, receiver, _ = ready_node

    async def observe(identity):
        return HostBootObservation(
            identity,
            str(uuid4()),
            "disk-123",
            "node-database",
            datetime.now(UTC) + timedelta(seconds=30),
        )

    async def runtime(_identity):
        return True

    receiver.lifecycle.observe_boot = observe
    receiver.lifecycle.observe_runtime = runtime
    assert (await maintenance(client)).status_code == 200
    status = (await client.get(PREFIX + "/readiness")).json()
    assert status["state"] == "ready" and status["admission_closed"]
    assert status["node_ready"] and status["runtime_ready"]
    result = await client.post(PREFIX + "/boot/evidence", json={"challenge": str(uuid4())})
    assert result.status_code == 200
    assert result.json()["admission_closed"] and result.json()["recovery_acknowledged"]
