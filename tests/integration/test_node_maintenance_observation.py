"""Maintenance responses use actual bounded observations without resetting idle."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from awf.hosted_node.identity import boot
from awf.hosted_node.lifecycle_models import NodeLifecycleRow
from tests.integration.test_node_idle_gate import PREFIX, create
from tests.integration.test_node_maintenance import maintenance
from tests.integration.test_node_maintenance import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node


@pytest.mark.parametrize("active", [False, True])
async def test_maintenance_begin_release_and_replays_report_current_health(ready_node, active):
    client, receiver, _ = ready_node
    receiver.lifecycle.observe_runtime = AsyncMock(return_value=True)
    if active:
        await create(receiver.factory)
    before = (await client.get(PREFIX + "/readiness")).json()
    assert before["node_ready"] and before["runtime_ready"]
    for action in ("begin", "begin", "release", "release"):
        response = await maintenance(client, action)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["node_ready"] and body["runtime_ready"]
        assert body["idle_since"] == before["idle_since"]
        assert body["quiescent"] is (not active)
        assert body["admission_closed"] is (action == "begin")
        async with receiver.factory() as session:
            idle = await session.scalar(select(NodeLifecycleRow.idle_since))
            assert (idle is None) is active


@pytest.mark.parametrize("action", ["begin", "release"])
@pytest.mark.parametrize("fault", ["absent", "expired", "foreign", "unhealthy"])
async def test_maintenance_never_invents_host_readiness(ready_node, action, fault):
    client, receiver, _ = ready_node
    assert (await maintenance(client)).status_code == 200
    valid = receiver.lifecycle.observe_host

    async def unavailable(identity):
        observation = await valid(identity)
        if fault == "absent":
            return None
        if fault == "expired":
            return replace(observation, expires_at=datetime.now(UTC) - timedelta(seconds=1))
        if fault == "foreign":
            return replace(observation, identity=identity.model_copy(update={"owner": "other"}))
        return replace(observation, retained_state_verified=False)

    receiver.lifecycle.observe_host = unavailable
    receiver.lifecycle.observe_runtime = AsyncMock(return_value=True)
    response = await maintenance(client, action)
    assert response.status_code == 200, response.text
    assert not response.json()["node_ready"] and not response.json()["runtime_ready"]
    assert response.json()["idle_since"] is None
    assert response.json()["admission_closed"] is (action == "begin")


@pytest.mark.parametrize("action", ["begin", "release"])
@pytest.mark.parametrize("successor", ["drain", "boot"])
async def test_late_maintenance_observation_cannot_reopen_a_successor(
    ready_node, action, successor
):
    client, receiver, _ = ready_node
    assert (await maintenance(client)).status_code == 200
    original = receiver.lifecycle.observe_host
    entered, finish = asyncio.Event(), asyncio.Event()

    async def paused(identity):
        entered.set()
        await finish.wait()
        return await original(identity)

    receiver.lifecycle.observe_host = paused
    pending = asyncio.create_task(maintenance(client, action))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        receiver.lifecycle.observe_host = original
        if successor == "drain":
            drained = await client.post(PREFIX + "/drain", json={"generation": 11})
            assert drained.json()["state"] == "drained"
        else:
            await boot(receiver.config, receiver.factory)
        finish.set()
        result = await asyncio.wait_for(pending, 2)
        if successor == "boot":
            assert result.status_code == 403
        else:
            assert result.status_code == 200
            assert result.json()["state"] == "drained"
            assert result.json()["admission_closed"] and not result.json()["node_ready"]
    finally:
        finish.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
