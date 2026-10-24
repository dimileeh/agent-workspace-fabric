"""Actual Core ASGI/PG; synthetic Linux/Docker boundaries, no discovery authority."""

from uuid import uuid4

import pytest

from tests.integration.test_closed_host_database import runtime_database as runtime_database
from tests.integration.test_closed_host_runtime import ack
from tests.integration.test_closed_host_runtime import composed as composed
from tests.integration.test_node_idle_gate import PREFIX, create


async def evidence(client, challenge=None):
    return await client.post(
        PREFIX + "/boot/evidence", json={"challenge": challenge or str(uuid4())}
    )


async def test_boot_evidence_is_real_host_observation_not_recovery_or_readiness(composed):
    client, receiver, _, state = composed
    challenge = str(uuid4())
    result = await evidence(client, challenge)
    assert result.status_code == 200
    body = result.json()
    assert body["challenge"] == challenge
    assert body["identity"] == receiver.identity.model_dump()
    assert body["previous_identity"] is None
    assert body["kernel_boot_id"] == state["linux"].boot_id
    assert body["disk_ref"] == "disk" and body["database_ref"] == "db"
    assert body["recovery_acknowledged"] is False
    assert body["admission_closed"] is True and body["runtime_ready"] is False
    assert (await client.get(PREFIX + "/readiness")).json()["node_ready"] is False
    assert (await ack(client)).status_code == 200
    assert (await evidence(client)).json()["recovery_acknowledged"] is True


@pytest.mark.parametrize("cause", ["container", "claim", "disabled", "stale", "foreign"])
async def test_boot_evidence_never_bypasses_real_checks(composed, cause):
    client, receiver, _, state = composed
    if cause == "container":
        state["containers"] = [{"Id": "not-allowed"}]
    elif cause == "claim":
        assert (await ack(client)).status_code == 200
        await create(receiver.factory)
    elif cause == "disabled":

        async def absent(_identity):
            return None

        receiver.lifecycle.observe_boot = absent
    elif cause == "stale":
        client.headers["X-AWF-Expected-Boot-Epoch"] = "1"
    else:
        client.headers["X-AWF-Expected-Owner"] = "foreign/node"
    assert (await evidence(client)).status_code == (403 if cause in ("stale", "foreign") else 503)


@pytest.mark.parametrize("value", ["", "not-uuid", "00000000-0000-0000-0000-000000000000", 1])
async def test_boot_evidence_requires_bounded_canonical_nonce(composed, value):
    client, _, _, _ = composed
    result = await client.post(PREFIX + "/boot/evidence", json={"challenge": value})
    assert result.status_code == 422
