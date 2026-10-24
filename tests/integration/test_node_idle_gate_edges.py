"""Failure, concurrency and boot authority edges for the public idle gate."""

import asyncio

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import DBAPIError

from awf.api.app import create_app
from awf.db.session import make_session_factory
from awf.hosted_node.identity import MaterialDeniedError, boot
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from tests.integration.test_node_idle_gate import (
    PREFIX,
    acknowledge,
    create,
    evidence,
)
from tests.integration.test_node_idle_gate import (
    node as node,
)


async def test_disabled_lifecycle_cannot_be_called(node):
    client, receiver, _ = node
    receiver.lifecycle = None
    assert (await client.get(PREFIX + "/readiness")).status_code == 404


async def test_missing_barrier_cannot_boot(node):
    _, receiver, engine = node
    async with engine.begin() as connection:
        await connection.execute(text("ALTER TABLE workspaces DISABLE TRIGGER awf_node_admission"))
    with pytest.raises(MaterialDeniedError, match="NODE_BARRIER_UNAVAILABLE"):
        await boot(receiver.config, make_session_factory(engine))


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"lifecycle": None}, "NODE_LIFECYCLE_CANNOT_DISABLE"),
        ({"owner": "other-owner/node"}, "NODE_OWNER_MISMATCH"),
    ],
)
async def test_config_cannot_disable_or_take_over_existing_node(node, change, reason):
    _, receiver, engine = node
    with pytest.raises(MaterialDeniedError, match=reason):
        await boot(receiver.config.model_copy(update=change), make_session_factory(engine))


async def test_missing_persisted_gate_is_not_ready(node):
    client, receiver, engine = node
    async with engine.begin() as connection:
        await connection.execute(delete(NodeLifecycleRow))
    assert (await client.get(PREFIX + "/readiness")).status_code == 403
    with pytest.raises(DBAPIError, match="NODE_GATE_MISSING"):
        await create(receiver.factory)


@pytest.mark.parametrize("change", [{"recovery_generation": 1}, {"incarnation": "124"}])
async def test_boot_cannot_rollback_generation_or_replace_same_generation(node, change):
    _, receiver, engine = node
    if "recovery_generation" in change:
        await boot(
            receiver.config.model_copy(update={"recovery_generation": 2}),
            make_session_factory(engine),
        )
    with pytest.raises(MaterialDeniedError, match="IDENTITY_ROLLBACK"):
        await boot(receiver.config.model_copy(update=change), make_session_factory(engine))


async def test_replacement_requires_higher_generation_and_fresh_ack(node):
    from awf.hosted_node.lifecycle import NodeLifecycle

    client, receiver, engine = node
    evidence(receiver)
    assert (await acknowledge(client)).status_code == 200
    old_identity = receiver.identity
    new_factory = make_session_factory(engine)
    config = receiver.config.model_copy(update={"incarnation": "123", "recovery_generation": 2})
    new_identity = await boot(config, new_factory)
    lifecycle = NodeLifecycle(new_factory, new_identity)
    state = await lifecycle.readiness()
    assert state.admission_closed and not state.node_ready
    async with new_factory() as session:
        row = await session.get(NodeLifecycleRow, 1)
        assert row.previous_identity == old_identity.model_dump()
    assert (await client.get(PREFIX + "/readiness")).status_code == 403


async def test_unresolved_work_on_reboot_cannot_be_acknowledged(node):
    client, receiver, engine = node
    evidence(receiver)
    assert (await acknowledge(client)).status_code == 200
    await create(receiver.factory)
    new_factory = make_session_factory(engine)
    new_identity = await boot(receiver.config, new_factory)
    receiver.identity, receiver.factory = new_identity, new_factory
    from awf.hosted_node.lifecycle import NodeLifecycle

    receiver.lifecycle = NodeLifecycle(new_factory, new_identity)
    evidence(receiver)
    client.headers.update(new_identity.headers())
    result = await acknowledge(client)
    assert result.status_code == 409
    assert result.json()["error_code"] == "RECOVERY_OUTCOME_UNRESOLVED"


async def test_drain_before_recovery_is_denied(node):
    client, _, _ = node
    result = await client.post(PREFIX + "/drain", json={"generation": 1})
    assert result.status_code == 409
    assert result.json()["error_code"] == "RECOVERY_REQUIRED"


async def test_recovery_verifier_failure_is_sanitized(node):
    client, receiver, _ = node
    service = evidence(receiver)

    async def unavailable(*args):
        raise RuntimeError("synthetic-private-diagnostic")

    service.verify_recovery = unavailable
    result = await acknowledge(client)
    assert result.status_code == 503
    assert "synthetic-private-diagnostic" not in result.text


async def test_host_probe_timeout_retains_closed_drain(node):
    client, receiver, _ = node
    service = evidence(receiver)
    assert (await acknowledge(client)).status_code == 200

    async def blocked(*args):
        await asyncio.Event().wait()

    service.observe_host = blocked
    result = await asyncio.wait_for(client.post(PREFIX + "/drain", json={"generation": 1}), 7)
    assert result.status_code == 200
    assert result.json()["state"] == "draining"
    assert result.json()["expires_at"] is None
    assert not result.json()["quiescent"]


@pytest.mark.parametrize("conflict", [False, True])
async def test_concurrent_recovery_ack_is_idempotent_or_conflicts(node, conflict):
    client, receiver, _ = node
    service = evidence(receiver)
    original = service.verify_recovery
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def pause(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return await original(*args)

    service.verify_recovery = pause
    pending = asyncio.create_task(acknowledge(client))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        second = await client.post(
            PREFIX + "/recovery/acknowledge",
            json={
                "idempotency_key": "other" if conflict else "recovery-1",
                "receipt_ref": "verified-recovery-receipt",
            },
        )
        assert second.status_code == 200
    finally:
        release.set()
    assert (await asyncio.wait_for(pending, 2)).status_code == (409 if conflict else 200)
    assert (await acknowledge(client)).status_code == (409 if conflict else 200)


async def test_response_loss_and_restart_keep_drain_closed(node):
    from awf.hosted_node.lifecycle import NodeLifecycle

    client, receiver, _ = node
    evidence(receiver)
    assert (await acknowledge(client)).status_code == 200
    await client.post(PREFIX + "/drain", json={"generation": 1})  # Deliberately lose response.
    receiver.lifecycle = NodeLifecycle(receiver.factory, receiver.identity)
    evidence(receiver)
    result = await client.post(PREFIX + "/drain", json={"generation": 1})
    assert result.json()["admission_closed"] and result.json()["state"] == "drained"


def test_openapi_declares_controller_and_identity_requirements():
    schema = create_app(use_lifespan=False).openapi()
    for path in ("/readiness", "/recovery/acknowledge", "/drain", "/drain/abort"):
        method = "get" if path == "/readiness" else "post"
        operation = schema["paths"][PREFIX + path][method]
        assert {"bearerAuth": []} in operation["security"]
        assert len([p for p in operation["parameters"] if p["in"] == "header"]) == 4
