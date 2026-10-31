"""Checkpoint intent uses real maintenance HTTP/SQL, never a new response field."""

from uuid import uuid4

import pytest
from sqlalchemy import text

from awf.hosted_node.checkpoint_schemas import CHECKPOINT_SCHEMA_REVISION
from tests.integration.test_node_idle_gate import PREFIX
from tests.integration.test_node_idle_gate import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node


@pytest.fixture(autouse=True)
async def schema_revision(engine):
    async with engine.begin() as connection:
        await connection.execute(
            text("CREATE TABLE alembic_version (version_num varchar(32) NOT NULL)")
        )
        await connection.execute(
            text("INSERT INTO alembic_version VALUES (:revision)"),
            {"revision": CHECKPOINT_SCHEMA_REVISION},
        )


def checkpoint_request(generation=7):
    return {
        "generation": generation,
        "checkpoint": {"checkpoint_id": str(uuid4()), "request_sha256": "a" * 64},
    }


async def marker(receiver):
    async with receiver.factory() as session:
        return await session.scalar(text("SELECT checkpoint_marker FROM hosted_node_lifecycle"))


async def test_marker_commits_exact_identity_and_preserves_response(ready_node):
    client, receiver, _ = ready_node
    original = (await client.get(PREFIX + "/readiness")).json()
    request = checkpoint_request()
    response = await client.post(PREFIX + "/maintenance/begin", json=request)
    assert response.status_code == 200, response.text
    assert response.json().keys() == original.keys()
    value = await marker(receiver)
    assert value["identity"] == receiver.identity.model_dump()
    assert value["disk_ref"] == "disk-123" and value["database_ref"] == "node-database"
    assert value["checkpoint_id"] == request["checkpoint"]["checkpoint_id"]
    assert value["request_sha256"] == "a" * 64
    assert value["maintenance_generation"] == 7 and value["version"] == 1
    assert value["schema_revision"] and value["prepared_at"]
    assert (await client.post(PREFIX + "/maintenance/begin", json=request)).status_code == 200
    assert await marker(receiver) == value
    assert (
        await client.post(PREFIX + "/maintenance/release", json={"generation": 7})
    ).status_code == 200
    assert await marker(receiver) == value
    assert (await client.get(PREFIX + "/readiness")).json().keys() == original.keys()


@pytest.mark.parametrize("change", ["id", "hash", "generation", "ordinary"])
async def test_checkpoint_cannot_be_rebound_while_held(ready_node, change):
    client, receiver, _ = ready_node
    request = checkpoint_request()
    assert (await client.post(PREFIX + "/maintenance/begin", json=request)).status_code == 200
    prior = await marker(receiver)
    if change == "id":
        request["checkpoint"]["checkpoint_id"] = str(uuid4())
    elif change == "hash":
        request["checkpoint"]["request_sha256"] = "b" * 64
    else:
        request["generation"] = 8
        if change == "ordinary":
            request.pop("checkpoint")
    response = await client.post(PREFIX + "/maintenance/begin", json=request)
    assert response.status_code == 409, response.text
    assert await marker(receiver) == prior


@pytest.mark.parametrize("action", ["release", "begin"])
async def test_checkpoint_fields_are_strict_and_release_cannot_rebind(ready_node, action):
    client, receiver, _ = ready_node
    request = checkpoint_request()
    if action == "begin":
        request["checkpoint"]["request_sha256"] = "A" * 64
    response = await client.post(PREFIX + "/maintenance/" + action, json=request)
    assert response.status_code == 422
    async with receiver.factory() as session:
        assert (
            await session.scalar(text("SELECT maintenance_generation FROM hosted_node_lifecycle"))
            is None
        )


async def test_checkpoint_cannot_bind_before_recovery(node):
    client, _, _ = node
    response = await client.post(PREFIX + "/maintenance/begin", json=checkpoint_request())
    assert response.status_code == 409


async def test_released_marker_does_not_change_later_ordinary_hold_semantics(ready_node):
    client, _, _ = ready_node
    assert (
        await client.post(PREFIX + "/maintenance/begin", json=checkpoint_request())
    ).status_code == 200
    assert (
        await client.post(PREFIX + "/maintenance/release", json={"generation": 7})
    ).status_code == 200
    for generation in (8, 9):
        assert (
            await client.post(PREFIX + "/maintenance/begin", json={"generation": generation})
        ).status_code == 200


async def test_released_checkpoint_id_cannot_be_rebound(ready_node):
    client, receiver, _ = ready_node
    request = checkpoint_request()
    assert (await client.post(PREFIX + "/maintenance/begin", json=request)).status_code == 200
    assert (
        await client.post(PREFIX + "/maintenance/release", json={"generation": 7})
    ).status_code == 200
    before = await marker(receiver)
    request["generation"] = 8
    assert (await client.post(PREFIX + "/maintenance/begin", json=request)).status_code == 409
    assert await marker(receiver) == before
