"""Independent HTTP handlers cross real owner-lock and commit boundaries."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import SQLAlchemyError

from awf.hosted_node.models import PreparationRow, Receipt
from awf.hosted_node.receiver import Receiver

from .test_preparation_renewal import PREFIX, durable, installed, renewal
from .test_preparation_renewal import clock as clock


async def test_lost_renewal_response_is_durably_replayed_by_independent_handler(
    hosted, peer, clock, monkeypatch
):
    client, receiver, _ = hosted
    prep, binding, _ = await installed(hosted, clock)
    request = renewal(prep, binding, clock)
    renew = receiver.renew_preparation
    acknowledged = None

    async def lose_response(value):
        nonlocal acknowledged
        acknowledged = await renew(value)
        raise TimeoutError("synthetic response loss after commit")

    monkeypatch.setattr(receiver, "renew_preparation", lose_response)
    response = await client.post(f"{PREFIX}/preparations/renew", json=request)
    assert response.status_code == 503
    assert "synthetic" not in response.text
    assert acknowledged is not None
    before = await durable(receiver)
    replay = await peer[0].post(f"{PREFIX}/preparations/renew", json=request)
    assert replay.status_code == 200 and replay.json() == acknowledged.model_dump(mode="json")
    assert await durable(receiver) == before


async def test_failed_receipt_commit_rolls_back_preparation_cap(hosted, clock):
    client, receiver, _ = hosted
    prep, binding, _ = await installed(hosted, clock)
    request = renewal(prep, binding, clock)
    before = await durable(receiver)

    def after_insert(_mapper, _connection, target):
        if target.operation == "prepare_renew":
            raise SQLAlchemyError("synthetic failure after actual receipt INSERT")

    event.listen(Receipt, "after_insert", after_insert)
    try:
        response = await client.post(f"{PREFIX}/preparations/renew", json=request)
    finally:
        event.remove(Receipt, "after_insert", after_insert)
    assert response.status_code == 503 and "synthetic" not in response.text
    assert await durable(receiver) == before
    assert (await client.post(f"{PREFIX}/preparations/renew", json=request)).status_code == 200


@pytest.mark.parametrize("change", ["revoke", "refresh", "expiry", "boot"])
async def test_current_material_check_is_revalidated_under_owner_lock(
    hosted, peer, clock, monkeypatch, change
):
    client, receiver, _ = hosted
    prep, binding, slot = await installed(hosted, clock)
    request = renewal(prep, binding, clock)
    entered, release = asyncio.Event(), asyncio.Event()
    status = receiver.status

    async def checked(value):
        result = await status(value)
        assert result.status == "current"
        entered.set()
        await asyncio.wait_for(release.wait(), 10)
        return result

    monkeypatch.setattr(receiver, "status", checked)
    task = asyncio.create_task(client.post(f"{PREFIX}/preparations/renew", json=request))
    successor = None
    try:
        await asyncio.wait_for(entered.wait(), 10)
        if change == "expiry":
            clock.instant += timedelta(minutes=11)
        elif change == "boot":
            successor = await Receiver.start(receiver.config, receiver.factory)
        else:
            updated = {**binding, "idempotency_key": change, "fence": 2, "revision": 2}
            if change == "refresh":
                updated.update(predecessor=slot, material="c3ludGhldGlj")
            response = await peer[0].post(f"{PREFIX}/materials/{change}", json=updated)
            assert response.json()["status"] == ("current" if change == "refresh" else "revoked")
    finally:
        release.set()
        result = await task
        if successor is not None:
            await successor.close()
    assert result.status_code == (403 if change == "boot" else 409), result.text
    async with receiver.factory() as session:
        row = (await session.scalars(select(PreparationRow))).one()
        assert row.binding["expires_at"] == prep["expires_at"].replace("+00:00", "Z")
        assert not list(
            await session.scalars(select(Receipt).where(Receipt.operation == "prepare_renew"))
        )


@pytest.mark.parametrize(
    "header",
    [
        "Authorization",
        "X-AWF-Expected-Owner",
        "X-AWF-Expected-Incarnation",
        "X-AWF-Expected-Recovery-Generation",
        "X-AWF-Expected-Boot-Epoch",
    ],
)
async def test_renewal_inherits_controller_and_identity_guard(hosted, clock, header):
    client, receiver, _ = hosted
    prep, binding, _ = await installed(hosted, clock)
    request = renewal(prep, binding, clock)
    before = await durable(receiver)
    response = await client.post(
        f"{PREFIX}/preparations/renew",
        json=request,
        headers={
            header: "Bearer operator-test-only" if header == "Authorization" else "foreign",
        },
    )
    assert response.status_code == 403
    assert await durable(receiver) == before


async def test_renewal_is_not_exposed_by_default_local_mode(client):
    result = await client.post(f"{PREFIX}/preparations/renew", json={})
    assert result.status_code == 404


@pytest.mark.parametrize("during_check", [False, True])
async def test_expiry_observed_by_renewal_remains_terminal_after_clock_rollback(
    hosted, clock, monkeypatch, during_check
):
    client, receiver, _ = hosted
    prep, binding, _ = await installed(hosted, clock)
    request = renewal(prep, binding, clock)
    instant = clock.instant
    status = receiver.status

    async def cross_expiry(value):
        result = await status(value)
        clock.instant += timedelta(minutes=11)
        return result

    if during_check:
        monkeypatch.setattr(receiver, "status", cross_expiry)
    else:
        clock.instant += timedelta(minutes=11)
    assert (await client.post(f"{PREFIX}/preparations/renew", json=request)).status_code == 409
    clock.instant = instant
    monkeypatch.setattr(receiver, "status", status)
    assert (await client.post(f"{PREFIX}/preparations/renew", json=request)).status_code == 409
    async with receiver.factory() as session:
        row = (await session.scalars(select(Receipt))).one()
        assert row.state == "expired"
