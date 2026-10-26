"""Renewal denies stale, malformed, expired or untrusted controller authority."""

import asyncio
import copy
import threading
from datetime import UTC, timedelta

import pytest
from sqlalchemy import select

from awf.hosted_node.models import Grant, Owner, PreparationRow, Receipt
from awf.hosted_node.receiver import Receiver

from .test_preparation_renewal import PREFIX, durable, installed, renewal
from .test_preparation_renewal import clock as clock


@pytest.mark.parametrize(
    "field,value,status",
    [
        ("owner", "foreign", 403),
        ("incarnation", "foreign", 403),
        ("boot_epoch", 99, 403),
        ("recovery_generation", 99, 403),
        ("namespace", "foreign", 404),
        ("workspace_id", "foreign", 404),
        ("preparation_id", "foreign", 404),
        ("target", "/run/awf/secrets/foreign", 409),
        ("grant_id", "foreign", 409),
        ("idempotency_key", "foreign", 409),
        ("revision", 2, 409),
        ("fence", 2, 409),
        ("authorization_generation", 2, 409),
    ],
)
async def test_renewal_requires_exact_current_binding(hosted, clock, field, value, status):
    client, receiver, _ = hosted
    prep, binding, _ = await installed(hosted, clock)
    request = copy.deepcopy(renewal(prep, binding, clock))
    target = request["current"]["identity"] if field in binding["identity"] else request["current"]
    target[field] = value
    before = await durable(receiver)
    result = await client.post(f"{PREFIX}/preparations/renew", json=request)
    assert result.status_code == status, result.text
    assert await durable(receiver) == before


@pytest.mark.parametrize(
    "boundary", ["same", "earlier", "past", "over-hour", "naive", "old-cap", "extra"]
)
async def test_renewal_expiry_bounds_and_exact_cap_cas(hosted, clock, boundary):
    client, receiver, _ = hosted
    prep, binding, _ = await installed(hosted, clock)
    request = renewal(prep, binding, clock)
    code = 422
    if boundary == "same":
        request["expires_at"] = prep["expires_at"]
    elif boundary == "earlier":
        request["expires_at"] = (clock.now(UTC) + timedelta(minutes=49)).isoformat()
    elif boundary == "past":
        request["expires_at"] = (clock.now(UTC) - timedelta(seconds=1)).isoformat()
    elif boundary == "over-hour":
        request["expires_at"] = (clock.now(UTC) + timedelta(hours=1, microseconds=1)).isoformat()
    elif boundary == "naive":
        request["expires_at"] = "2030-01-01T00:00:00"
    elif boundary == "old-cap":
        request["expected_preparation_expires_at"] = (
            clock.now(UTC) + timedelta(minutes=49)
        ).isoformat()
        code = 409
    else:
        request["material"] = "synthetic-not-accepted"
    before = await durable(receiver)
    response = await client.post(f"{PREFIX}/preparations/renew", json=request)
    assert response.status_code == code, response.text
    assert await durable(receiver) == before


async def test_renewal_allows_exact_hour_without_using_old_lease_as_horizon(hosted, clock):
    prep, binding, _ = await installed(hosted, clock)
    request = renewal(prep, binding, clock)
    request["expires_at"] = (clock.now(UTC) + timedelta(hours=1)).isoformat()
    response = await hosted[0].post(f"{PREFIX}/preparations/renew", json=request)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "failure", ["expired", "prep-expired", "missing-file", "revoked", "superseded", "pending"]
)
@pytest.mark.parametrize("replay", [False, True])
async def test_renewal_cannot_resurrect_material_authority(
    hosted, clock, monkeypatch, failure, replay
):
    client, receiver, _ = hosted
    prep, binding, slot = await installed(hosted, clock)
    request = renewal(prep, binding, clock)
    if replay:
        assert (await client.post(f"{PREFIX}/preparations/renew", json=request)).status_code == 200
    if failure in {"expired", "prep-expired"}:
        clock.instant += timedelta(minutes=11 if failure == "expired" else 61)
    elif failure == "missing-file":
        async with receiver.factory() as session:
            row = (await session.scalars(select(PreparationRow))).one()
            directory = row.id
        receiver.store.remove(directory, slot)
    elif failure == "revoked":
        response = await client.post(
            f"{PREFIX}/materials/revoke",
            json={
                **binding,
                "idempotency_key": "revoke",
                "fence": 2,
            },
        )
        assert response.json()["status"] == "revoked"
    else:
        payload = {
            **binding,
            "idempotency_key": failure,
            "fence": 2,
            "revision": 2,
            "material": "c3ludGhldGlj",
        }
        operation = "install"
        if failure == "superseded":
            payload["grant_id"] = "successor"
        else:
            operation = "refresh"
            payload["predecessor"] = slot

            def fail(*args):
                raise OSError("synthetic interrupted write")

            monkeypatch.setattr(receiver.store, "install", fail)
        response = await client.post(f"{PREFIX}/materials/{operation}", json=payload)
        assert response.json()["status"] == (
            "current" if failure == "superseded" else "unavailable"
        )
    before = await durable(receiver) if failure != "superseded" else None
    response = await client.post(f"{PREFIX}/preparations/renew", json=request)
    assert response.status_code in {409, 503}, response.text
    if before is not None:
        after = await durable(receiver)
        assert after[:2] == before[:2]
        assert len(after[2]) == len(before[2])


@pytest.mark.parametrize("mode", ["shutdown", "reboot", "owner-loss"])
async def test_renewal_receipts_remain_safe_for_boot_and_shutdown_cleanup(hosted, clock, mode):
    client, receiver, app = hosted
    prep, binding, _ = await installed(hosted, clock)
    request = renewal(prep, binding, clock)
    assert (await client.post(f"{PREFIX}/preparations/renew", json=request)).status_code == 200
    if mode == "shutdown":
        await receiver.close()
        assert not list(receiver.store.root.glob("*/*.material"))
        assert (await client.post(f"{PREFIX}/preparations/renew", json=request)).status_code == 503
    elif mode == "owner-loss":
        async with receiver.factory.begin() as session:
            owner = await session.get(Owner, receiver.identity.owner)
            owner.incarnation = "successor"
        assert (await client.post(f"{PREFIX}/preparations/renew", json=request)).status_code == 403
    else:
        successor = await Receiver.start(receiver.config, receiver.factory)
        try:
            assert not list(receiver.store.root.glob("*/*.material"))
            app.state.hosted_receiver = successor
            client.headers.update(successor.identity.headers())
            assert (
                await client.post(f"{PREFIX}/preparations/renew", json=request)
            ).status_code == 403
        finally:
            await successor.close()


@pytest.mark.parametrize("refresh_state", ["none", "pending", "accepted"])
async def test_dominant_revoke_uses_old_expiry_after_unknown_renewal_result(
    hosted, peer, clock, monkeypatch, refresh_state
):
    client, receiver, _ = hosted
    prep, binding, slot = await installed(hosted, clock)
    request = renewal(prep, binding, clock)
    # Drop the acknowledgement at the client boundary; retry from a separate
    # handler must find the durable receipt, not generate a second extension.
    response = await client.post(f"{PREFIX}/preparations/renew", json=request)
    assert response.status_code == 200, response.text
    entered, release = threading.Event(), threading.Event()
    install = receiver.store.install

    def blocked(*args):
        entered.set()
        assert release.wait(10)
        return install(*args)

    task = None
    if refresh_state != "none":
        if refresh_state == "pending":
            monkeypatch.setattr(receiver.store, "install", blocked)
        task = asyncio.create_task(
            client.post(
                f"{PREFIX}/materials/refresh",
                json={
                    **binding,
                    "idempotency_key": "refresh",
                    "fence": 2,
                    "revision": 2,
                    "expires_at": request["expires_at"],
                    "predecessor": slot,
                    "material": "c3ludGhldGlj",
                },
            )
        )
        if refresh_state == "pending":
            assert await asyncio.to_thread(entered.wait, 5)
        else:
            assert (await task).json()["status"] == "current"
    try:
        clock.instant += timedelta(minutes=11)
        revoked = await peer[0].post(
            f"{PREFIX}/materials/revoke",
            json={
                **binding,
                "idempotency_key": "revoke",
                "fence": 3,
                "revision": 2,
            },
        )
        assert revoked.status_code == 200 and revoked.json()["status"] == "revoked", revoked.text
    finally:
        release.set()
        if task:
            result = await task
            if refresh_state == "pending":
                assert result.json()["status"] == "revoked"
    assert not list(receiver.store.root.glob("*/*.material"))
    assert (await peer[0].post(f"{PREFIX}/preparations/renew", json=request)).status_code == 409
    async with receiver.factory() as session:
        grant = (await session.scalars(select(Grant))).one()
        assert grant.revoked and grant.current is None and grant.pending is None
        assert (grant.fence, grant.revision) == (3, 2)
        assert (
            len(
                list(
                    await session.scalars(
                        select(Receipt).where(Receipt.operation == "prepare_renew")
                    )
                )
            )
            == 1
        )


async def test_absent_preparation_is_not_a_successful_cleanup(hosted, clock):
    prep, binding, _ = await installed(hosted, clock)
    binding.update(preparation_id="absent", idempotency_key="revoke", fence=9)
    result = await hosted[0].post(f"{PREFIX}/materials/revoke", json=binding)
    assert result.status_code == 404
