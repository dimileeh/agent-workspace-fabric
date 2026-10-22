"""Cross-app races use independent engines, receivers and file-store objects."""

import asyncio
import threading
from pathlib import Path

import pytest
from sqlalchemy import select

from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.models import Grant, Receipt

from .test_receiver_api import prepare


async def test_same_key_across_apps(hosted, peer, monkeypatch, protected_resolver):
    client, receiver, _ = hosted
    other, independent, _ = peer
    assert independent.factory is not receiver.factory
    binding = await prepare(hosted)
    entered, release = threading.Event(), threading.Event()
    install = receiver.store.install

    def stalled(*args):
        entered.set()
        assert release.wait(10)
        return install(*args)

    monkeypatch.setattr(receiver.store, "install", stalled)
    task = asyncio.create_task(
        client.post("/v1/hosted-node/materials/install", json={**binding, "material": "YQ=="})
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        replay = await asyncio.wait_for(
            other.post("/v1/hosted-node/materials/install", json={**binding, "material": "Yg=="}), 3
        )
        assert replay.json()["status"] == "unavailable"
        with pytest.raises(MaterialDeniedError):
            await protected_resolver(binding)
    finally:
        release.set()
        result = await task
    assert result.json()["status"] == "current"
    assert result.json()["receipt_id"] == replay.json()["receipt_id"]
    assert (
        await other.post("/v1/hosted-node/materials/install", json={**binding, "material": "Yg=="})
    ).json() == result.json()
    mounts = (await protected_resolver(binding)).mounts
    assert Path(mounts[0].source).read_bytes() == b"a"
    async with independent.factory() as session:
        assert len((await session.scalars(select(Receipt))).all()) == 1


@pytest.mark.parametrize("revoke", [False, True])
async def test_competing_refresh_across_apps(hosted, peer, monkeypatch, protected_resolver, revoke):
    client, receiver, _ = hosted
    other, independent, _ = peer
    binding = await prepare(hosted)
    initial = await client.post(
        "/v1/hosted-node/materials/install", json={**binding, "material": "YQ=="}
    )
    newer = {**binding, "revision": 2, "fence": 2, "idempotency_key": "refresh"}
    payload = {**newer, "predecessor": initial.json()["receipt_id"], "material": "Yg=="}
    entered, release = threading.Event(), threading.Event()
    install = receiver.store.install

    def stalled(*args):
        entered.set()
        assert release.wait(10)
        return install(*args)

    monkeypatch.setattr(receiver.store, "install", stalled)
    task = asyncio.create_task(client.post("/v1/hosted-node/materials/refresh", json=payload))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert (
            await asyncio.wait_for(
                other.post("/v1/hosted-node/identity/acknowledge", json={"idempotency_key": "ack"}),
                3,
            )
        ).status_code == 200
        assert (await other.post("/v1/hosted-node/materials/status", json=binding)).json()[
            "status"
        ] == "current"
        competitor = {**payload, "revision": 3, "fence": 3, "idempotency_key": "competitor"}
        assert (
            await other.post("/v1/hosted-node/materials/refresh", json=competitor)
        ).status_code == 409
        if revoke:
            tombstone = {**newer, "fence": 4, "idempotency_key": "revoke"}
            assert (await other.post("/v1/hosted-node/materials/revoke", json=tombstone)).json()[
                "status"
            ] == "revoked"
    finally:
        release.set()
        result = await task
    assert result.json()["status"] == ("revoked" if revoke else "current")
    if revoke:
        with pytest.raises(MaterialDeniedError):
            await protected_resolver(newer)
        assert not list(receiver.store.root.glob("*/*"))
        assert (await other.post("/v1/hosted-node/materials/refresh", json=payload)).json()[
            "status"
        ] == "revoked"
    else:
        # A delayed loser cannot replace the winner or delete its distinct version slot.
        assert (
            await other.post("/v1/hosted-node/materials/refresh", json=competitor)
        ).status_code == 409
        async with independent.factory() as session:
            grant = (await session.scalars(select(Grant))).one()
            directory = grant.preparation
            assert grant.current == result.json()["receipt_id"]
            assert grant.pending is None
        independent.store.remove(directory, initial.json()["receipt_id"])
        assert (await other.post("/v1/hosted-node/materials/status", json=newer)).json()[
            "status"
        ] == "current"
        assert Path((await protected_resolver(newer)).mounts[0].source).read_bytes() == b"b"


async def test_losing_finalizer_and_shutdown_preserve_successor_files(hosted, peer, monkeypatch):
    from awf.hosted_node.receiver import Receiver

    client, receiver, _ = hosted
    other, independent, app = peer
    binding = await prepare(hosted)
    entered, release = threading.Event(), threading.Event()
    install = receiver.store.install

    def stalled(*args):
        entered.set()
        assert release.wait(10)
        return install(*args)

    monkeypatch.setattr(receiver.store, "install", stalled)
    task = asyncio.create_task(
        client.post("/v1/hosted-node/materials/install", json={**binding, "material": "YQ=="})
    )
    successor = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        successor = await Receiver.start(receiver.config, independent.factory)
        app.state.hosted_receiver = successor
        other.headers.update(successor.identity.headers())
        # A new boot requires a new preparation; the old preparation cannot be reused.
        preparation = {
            key: binding[key]
            for key in ("namespace", "workspace_id", "purpose", "target", "expires_at")
        }
        preparation["preparation_id"] = "successor-prep"
        assert (
            await other.post("/v1/hosted-node/preparations", json=preparation)
        ).status_code == 200
        winner = {
            **binding,
            **preparation,
            "identity": successor.identity.model_dump(),
            "idempotency_key": "successor-install",
        }
        result = await other.post(
            "/v1/hosted-node/materials/install", json={**winner, "material": "Yg=="}
        )
        assert result.json()["status"] == "current"
        release.set()
        assert (await task).status_code == 403
        await receiver.close()
        assert (await other.post("/v1/hosted-node/materials/status", json=winner)).json()[
            "status"
        ] == "current"
        files = list(successor.store.root.glob("*/*"))
        assert len(files) == 1
        assert files[0].stem == result.json()["receipt_id"]
        assert files[0].read_bytes() == b"b"
    finally:
        release.set()
        await task
        if successor is not None:
            await successor.close()


@pytest.mark.parametrize("successor", ["installed", "pending", "fence", "generation"])
async def test_delayed_install_rechecks_preparation_authority(
    hosted, peer, monkeypatch, protected_resolver, successor
):
    client, receiver, _ = hosted
    other, independent, _ = peer
    binding = await prepare(hosted)
    entered, release = threading.Event(), threading.Event()
    install = receiver.store.install

    def stalled(*args):
        entered.set()
        assert release.wait(10)
        return install(*args)

    monkeypatch.setattr(receiver.store, "install", stalled)
    task = asyncio.create_task(
        client.post("/v1/hosted-node/materials/install", json={**binding, "material": "YQ=="})
    )
    newer = {
        **binding,
        "grant_id": "successor",
        "idempotency_key": "successor",
        "fence": binding["fence"] + (successor != "generation"),
        "authorization_generation": binding["authorization_generation"]
        + (successor == "generation"),
    }
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        if successor in {"fence", "generation"}:
            result = await other.post("/v1/hosted-node/materials/revoke", json=newer)
            assert result.json()["status"] == "revoked"
        else:
            if successor == "pending":

                def fail_install(*args):
                    raise OSError("publication interrupted")

                monkeypatch.setattr(independent.store, "install", fail_install)
            result = await other.post(
                "/v1/hosted-node/materials/install", json={**newer, "material": "Yg=="}
            )
            assert result.json()["status"] == (
                "current" if successor == "installed" else "unavailable"
            )
    finally:
        release.set()
        delayed = await task
    assert delayed.json()["status"] == "unavailable"
    assert delayed.json()["reason"] == "INSTALLATION_UNCERTAIN"
    assert (await other.post("/v1/hosted-node/materials/status", json=binding)).json() == (
        delayed.json()
    )
    with pytest.raises(MaterialDeniedError, match="PROTECTED_MATERIAL_UNAVAILABLE"):
        await protected_resolver(binding)
    async with independent.factory() as session:
        receipt = await session.get(Receipt, delayed.json()["receipt_id"])
        assert receipt.state == "pending"
        grant = await session.get(Grant, receipt.grant)
        assert grant.current is None
        assert not receiver.store.exists(grant.preparation, receipt.id)
    if successor == "installed":
        assert Path((await protected_resolver(newer)).mounts[0].source).read_bytes() == b"b"
