"""Controller renewal uses real HTTP, PostgreSQL and private material slots."""

import asyncio
import copy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from awf.hosted_node import receiver as receiver_module
from awf.hosted_node.models import Grant, PreparationRow, Receipt
from awf.hosted_node.schemas import Binding

PREFIX = "/v1/hosted-node"


@pytest.fixture
def clock(monkeypatch):
    class Clock(datetime):
        instant = datetime.now(UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(cls.instant.timestamp(), tz=tz)

    monkeypatch.setattr(receiver_module, "datetime", Clock)
    return Clock


async def installed(hosted, clock):
    client, receiver, _ = hosted
    preparation = {
        "namespace": "ns",
        "workspace_id": "ws",
        "preparation_id": "prep",
        "purpose": "credential",
        "target": "/run/awf/secrets/key",
        "expires_at": (clock.now(UTC) + timedelta(minutes=50)).isoformat(),
    }
    assert (await client.post(f"{PREFIX}/preparations", json=preparation)).status_code == 200
    binding = {
        **preparation,
        "expires_at": (clock.now(UTC) + timedelta(minutes=10)).isoformat(),
        "identity": receiver.identity.model_dump(),
        "idempotency_key": "install",
        "grant_id": "grant",
        "revision": 1,
        "authorization_generation": 1,
        "fence": 1,
    }
    response = await client.post(
        f"{PREFIX}/materials/install", json={**binding, "material": "c3ludGhldGlj"}
    )
    assert response.status_code == 200 and response.json()["status"] == "current"
    return preparation, binding, response.json()["receipt_id"]


def renewal(preparation, binding, clock):
    return {
        "idempotency_key": "renew",
        "current": binding,
        "expected_preparation_expires_at": preparation["expires_at"],
        "expires_at": (clock.now(UTC) + timedelta(minutes=55)).isoformat(),
    }


async def durable(receiver):
    async with receiver.factory() as session:
        prep = (await session.scalars(select(PreparationRow))).one()
        grant = (await session.scalars(select(Grant))).one()
        return (
            copy.deepcopy(prep.binding),
            (
                grant.id,
                grant.fence,
                grant.revision,
                grant.authorization_generation,
                grant.revoked,
                grant.current,
                grant.pending,
            ),
            {
                row.id: (row.operation, row.state, copy.deepcopy(row.binding), row.predecessor)
                for row in await session.scalars(select(Receipt))
            },
        )


async def test_renewal_is_atomic_metadata_only_and_normal_prepare_stays_strict(hosted, clock):
    client, receiver, _ = hosted
    prep, binding, slot = await installed(hosted, clock)
    request = renewal(prep, binding, clock)
    before = await durable(receiver)
    files = {path: path.read_bytes() for path in receiver.store.root.glob("*/*.material")}
    response = await client.post(f"{PREFIX}/preparations/renew", json=request)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result == {
        "status": "renewed",
        "receipt_id": result["receipt_id"],
        "preparation_id": "prep",
        "expires_at": request["expires_at"].replace("+00:00", "Z"),
    }
    after = await durable(receiver)
    assert after[0] == {**before[0], "expires_at": result["expires_at"]}
    assert after[1] == before[1]
    assert after[2][slot] == before[2][slot]
    record = after[2][result["receipt_id"]]
    assert record[0:2] == ("prepare_renew", "renewed") and record[3] == slot
    assert record[2]["identity"] == binding["identity"]
    assert record[2]["workspace_id"] == binding["workspace_id"]
    assert {path: path.read_bytes() for path in files} == files
    assert len(list(receiver.store.root.glob("*/*.material"))) == 1
    assert (await client.post(f"{PREFIX}/preparations", json=prep)).status_code == 409
    assert (await client.post(f"{PREFIX}/preparations/renew", json=request)).json() == result
    assert await durable(receiver) == after


@pytest.mark.parametrize("same_key", [True, False])
async def test_concurrent_renewal_serializes_existing_owner_lock(hosted, peer, clock, same_key):
    prep, binding, _ = await installed(hosted, clock)
    request = renewal(prep, binding, clock)
    other = request if same_key else {**request, "idempotency_key": "second"}
    results = await asyncio.gather(
        hosted[0].post(f"{PREFIX}/preparations/renew", json=request),
        peer[0].post(f"{PREFIX}/preparations/renew", json=other),
    )
    assert sorted(result.status_code for result in results) == (
        [200, 200] if same_key else [200, 409]
    )
    if same_key:
        assert results[0].json() == results[1].json()
    assert len((await durable(hosted[1]))[2]) == 2


@pytest.mark.parametrize("collision", ["renew-first", "material-first"])
@pytest.mark.parametrize("operation", ["install", "refresh", "status", "revoke"])
async def test_material_and_renewal_keys_cannot_alias(hosted, clock, collision, operation):
    client, receiver, _ = hosted
    prep, binding, slot = await installed(hosted, clock)
    request = renewal(prep, binding, clock)
    expected_status = "current"
    if collision == "material-first":
        if operation in {"refresh", "revoke"}:
            prior = {
                **binding,
                "idempotency_key": f"{operation}-first",
                "fence": 2,
                "revision": 2,
            }
            payload = dict(prior)
            if operation == "refresh":
                payload.update(predecessor=slot, material="c3ludGhldGlj")
            else:
                expected_status = "revoked"
            accepted = await client.post(f"{PREFIX}/materials/{operation}", json=payload)
            assert accepted.json()["status"] == expected_status, accepted.text
            binding = prior
            request["current"] = binding
        request["idempotency_key"] = binding["idempotency_key"]
        response = await client.post(f"{PREFIX}/preparations/renew", json=request)
    else:
        accepted = await client.post(f"{PREFIX}/preparations/renew", json=request)
        assert accepted.status_code == 200, accepted.text
        material = {**binding, "idempotency_key": request["idempotency_key"]}
        if operation in {"install", "refresh"}:
            material["material"] = "c3ludGhldGlj"
        if operation == "refresh":
            material["predecessor"] = slot
        response = await client.post(f"{PREFIX}/materials/{operation}", json=material)
    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "IDEMPOTENCY_CONFLICT"
    assert (await receiver.status(Binding.model_validate(binding))).status == expected_status


async def test_renewal_receipt_cannot_be_refresh_predecessor(hosted, clock):
    client, receiver, _ = hosted
    prep, binding, _ = await installed(hosted, clock)
    accepted = await client.post(f"{PREFIX}/preparations/renew", json=renewal(prep, binding, clock))
    assert accepted.status_code == 200, accepted.text
    before = await durable(receiver)
    response = await client.post(
        f"{PREFIX}/materials/refresh",
        json={
            **binding,
            "idempotency_key": "refresh",
            "revision": 2,
            "fence": 2,
            "material": "c3ludGhldGlj",
            "predecessor": accepted.json()["receipt_id"],
        },
    )
    assert response.status_code == 409
    assert await durable(receiver) == before


@pytest.mark.parametrize("changed", ["expires_at", "expected_preparation_expires_at", "current"])
async def test_renewal_key_rejects_different_request(hosted, clock, changed):
    client, receiver, _ = hosted
    prep, binding, _ = await installed(hosted, clock)
    request = renewal(prep, binding, clock)
    assert (await client.post(f"{PREFIX}/preparations/renew", json=request)).status_code == 200
    before = await durable(receiver)
    other = copy.deepcopy(request)
    if changed == "current":
        other[changed]["fence"] += 1
    else:
        other[changed] = (datetime.fromisoformat(other[changed]) + timedelta(seconds=1)).isoformat()
    response = await client.post(f"{PREFIX}/preparations/renew", json=other)
    assert response.status_code == 409 and response.json()["error_code"] == "IDEMPOTENCY_CONFLICT"
    assert await durable(receiver) == before


async def test_renewal_replay_after_refresh_does_not_restore_old_authority(hosted, peer, clock):
    client, receiver, _ = hosted
    prep, binding, slot = await installed(hosted, clock)
    request = renewal(prep, binding, clock)
    response = await client.post(f"{PREFIX}/preparations/renew", json=request)
    assert response.status_code == 200, response.text
    newer = {
        **binding,
        "idempotency_key": "refresh",
        "fence": 2,
        "revision": 2,
        "expires_at": request["expires_at"],
    }
    updated = await client.post(
        f"{PREFIX}/materials/refresh",
        json={
            **newer,
            "predecessor": slot,
            "material": "bmV3LXN5bnRoZXRpYw==",
        },
    )
    assert updated.json()["status"] == "current", updated.text
    clock.instant += timedelta(minutes=11)
    before = await durable(receiver)
    assert (
        await peer[0].post(f"{PREFIX}/preparations/renew", json=request)
    ).json() == response.json()
    assert await durable(receiver) == before
    revoked = await client.post(
        f"{PREFIX}/materials/revoke",
        json={
            **binding,
            "idempotency_key": "revoke",
            "fence": 3,
            "revision": 2,
        },
    )
    assert revoked.json()["status"] == "revoked", revoked.text
    before = await durable(receiver)
    assert (await peer[0].post(f"{PREFIX}/preparations/renew", json=request)).status_code == 409
    assert await durable(receiver) == before
    assert not list(receiver.store.root.glob("*/*.material"))
