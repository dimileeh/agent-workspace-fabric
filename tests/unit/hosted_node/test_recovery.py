import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.models import Grant
from awf.hosted_node.receiver import Receiver

from .test_receiver_api import prepare


async def test_interrupted_install_and_delayed_replay(hosted, monkeypatch):
    client, receiver, _ = hosted
    binding = await prepare(hosted)
    original = receiver.store.install

    def interrupted(*args):
        original(*args)
        raise OSError("injected interruption")

    monkeypatch.setattr(receiver.store, "install", interrupted)
    response = await client.post(
        "/v1/hosted-node/materials/install", json={**binding, "material": "YQ=="}
    )
    assert response.json()["status"] == "unavailable"
    monkeypatch.setattr(receiver.store, "install", original)
    replay = await client.post(
        "/v1/hosted-node/materials/install", json={**binding, "material": "Yg=="}
    )
    assert replay.json()["status"] == "unavailable"
    revoke = {**binding, "fence": 2, "idempotency_key": "revoke"}
    assert (await client.post("/v1/hosted-node/materials/revoke", json=revoke)).json()[
        "status"
    ] == "revoked"
    assert not list(receiver.store.root.glob("*/*.material"))


async def test_missing_file_and_expiry_cleanup(hosted):
    client, receiver, _ = hosted
    binding = await prepare(hosted)
    binding["expires_at"] = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    response = await client.post(
        "/v1/hosted-node/materials/install", json={**binding, "material": "YQ=="}
    )
    assert response.json()["status"] == "current"
    await asyncio.sleep(1.05)
    response = await client.post("/v1/hosted-node/materials/status", json=binding)
    assert response.json()["status"] == "expired"
    assert not list(receiver.store.root.glob("*/*.material"))


async def test_missing_file_never_accepted_after_restart(hosted):
    client, receiver, app = hosted
    binding = await prepare(hosted)
    response = await client.post(
        "/v1/hosted-node/materials/install", json={**binding, "material": "YQ=="}
    )
    assert response.status_code == 200
    for path in receiver.store.root.glob("*/*.material"):
        path.unlink()
    assert (await client.post("/v1/hosted-node/materials/status", json=binding)).json()[
        "status"
    ] == "unavailable"
    successor = await Receiver.start(receiver.config, receiver.factory)
    try:
        app.state.hosted_receiver = successor
        client.headers.update(successor.identity.headers())
        assert (
            await client.post("/v1/hosted-node/materials/status", json=binding)
        ).status_code == 403
        binding["identity"] = successor.identity.model_dump()
        assert (
            await client.post("/v1/hosted-node/materials/status", json=binding)
        ).status_code == 409
    finally:
        await successor.close()


async def test_refresh_revoke_race_and_responsiveness(hosted, monkeypatch):
    client, receiver, _ = hosted
    binding = await prepare(hosted)
    first = await client.post(
        "/v1/hosted-node/materials/install", json={**binding, "material": "YQ=="}
    )
    entered, release = threading.Event(), threading.Event()
    original = receiver.store.install

    def stalled(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(receiver.store, "install", stalled)
    newer = {**binding, "fence": 2, "revision": 2, "idempotency_key": "refresh"}
    task = asyncio.create_task(
        client.post(
            "/v1/hosted-node/materials/refresh",
            json={
                **newer,
                "material": "Yg==",
                "predecessor": first.json()["receipt_id"],
            },
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        identity = await asyncio.wait_for(
            client.post("/v1/hosted-node/identity/acknowledge", json={"idempotency_key": "ack"}), 1
        )
        assert identity.status_code == 200
        assert (await client.post("/v1/hosted-node/materials/status", json=binding)).json()[
            "status"
        ] == "current"
        competing = await client.post(
            "/v1/hosted-node/materials/refresh",
            json={
                **newer,
                "fence": 3,
                "revision": 3,
                "idempotency_key": "competing",
                "material": "Yw==",
                "predecessor": first.json()["receipt_id"],
            },
        )
        assert competing.status_code == 409
        revoke = {**newer, "fence": 4, "idempotency_key": "revoke"}
        response = await asyncio.wait_for(
            client.post("/v1/hosted-node/materials/revoke", json=revoke), 1
        )
        assert response.json()["status"] == "revoked"
    finally:
        release.set()
        result = await task
    assert result.json()["status"] == "revoked"
    assert not list(receiver.store.root.glob("*/*.material"))
    async with receiver.factory() as session:
        grant = (await session.scalars(select(Grant))).one()
        assert grant.fence == 4 and grant.revoked


async def test_same_key_concurrency_and_exact_replay(hosted):
    client, _, _ = hosted
    binding = await prepare(hosted)
    payload = {**binding, "material": "YQ=="}
    results = await asyncio.gather(
        *(client.post("/v1/hosted-node/materials/install", json=payload) for _ in range(3))
    )
    assert all(r.status_code == 200 for r in results)
    assert len({r.json()["receipt_id"] for r in results}) == 1
    response = await client.post("/v1/hosted-node/materials/status", json=binding)
    assert response.json()["status"] == "current"
    for field in ("revision", "authorization_generation", "fence"):
        response = await client.post("/v1/hosted-node/materials/status", json={**binding, field: 2})
        assert response.status_code == 409


async def test_restart_cleans_old_files_and_shutdown_fences_resolvers(hosted):
    client, receiver, _ = hosted
    binding = await prepare(hosted)
    await client.post("/v1/hosted-node/materials/install", json={**binding, "material": "YQ=="})
    assert list(receiver.store.root.glob("*/*.material"))
    successor = await Receiver.start(receiver.config, receiver.factory)
    try:
        assert not list(receiver.store.root.glob("*/*.material"))
    finally:
        await successor.close()


async def test_incarnation_change_during_file_install_cleans_unpublished_file(hosted, monkeypatch):
    client, receiver, _ = hosted
    binding = await prepare(hosted)
    entered, release = threading.Event(), threading.Event()
    original = receiver.store.install

    def stalled(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(receiver.store, "install", stalled)
    task = asyncio.create_task(
        client.post("/v1/hosted-node/materials/install", json={**binding, "material": "YQ=="})
    )
    assert await asyncio.to_thread(entered.wait, 3)
    successor = await Receiver.start(receiver.config, receiver.factory)
    try:
        release.set()
        assert (await task).status_code == 403
        assert not list(receiver.store.root.glob("*/*.material"))
    finally:
        release.set()
        await successor.close()


async def test_failed_finalization_stays_uncertain_and_revoke_cleanup_replays(hosted, monkeypatch):
    from sqlalchemy.exc import SQLAlchemyError

    client, receiver, _ = hosted
    binding = await prepare(hosted)
    install, authority, remove = receiver.store.install, receiver.authority, receiver.store.remove
    fail_finalize = False

    def install_then_fail(*args):
        nonlocal fail_finalize
        result = install(*args)
        fail_finalize = True
        return result

    async def authority_failure(*args, **kwargs):
        nonlocal fail_finalize
        if fail_finalize:
            fail_finalize = False
            raise SQLAlchemyError("injected finalization failure")
        return await authority(*args, **kwargs)

    monkeypatch.setattr(receiver.store, "install", install_then_fail)
    monkeypatch.setattr(receiver, "authority", authority_failure)
    response = await client.post(
        "/v1/hosted-node/materials/install", json={**binding, "material": "YQ=="}
    )
    assert response.status_code == 503
    assert (await client.post("/v1/hosted-node/materials/status", json=binding)).json()[
        "status"
    ] == "unavailable"

    def cleanup_failure(*args):
        raise OSError("injected cleanup failure")

    monkeypatch.setattr(receiver.store, "remove", cleanup_failure)
    revoke = {**binding, "fence": 2, "idempotency_key": "revoke"}
    response = await client.post("/v1/hosted-node/materials/revoke", json=revoke)
    assert response.status_code == 503
    assert (await client.post("/v1/hosted-node/materials/status", json=binding)).json()[
        "status"
    ] == "revoked"
    monkeypatch.setattr(receiver.store, "remove", remove)
    assert (await client.post("/v1/hosted-node/materials/revoke", json=revoke)).json()[
        "status"
    ] == "revoked"
    assert not list(receiver.store.root.glob("*/*.material"))


async def test_observed_expiry_is_terminal_even_if_clock_moves_back(hosted, monkeypatch):
    from awf.hosted_node import receiver as receiver_module
    from awf.hosted_node.models import Receipt

    client, receiver, _ = hosted
    binding = await prepare(hosted)
    binding["expires_at"] = (datetime.now(UTC) + timedelta(seconds=1)).isoformat()
    await client.post("/v1/hosted-node/materials/install", json={**binding, "material": "YQ=="})
    await asyncio.sleep(1.05)
    assert (await client.post("/v1/hosted-node/materials/status", json=binding)).json()[
        "status"
    ] == "expired"
    async with receiver.factory() as session:
        row = (await session.scalars(select(Receipt))).one()
        assert row.state == "expired"
        grant = await session.get(Grant, row.grant)
        directory, slot = grant.preparation, row.id
    receiver.store.install(directory, slot, b"synthetic")

    class Earlier(datetime):
        @classmethod
        def now(cls, _tz=None):
            return datetime.fromisoformat(binding["expires_at"]) - timedelta(seconds=1)

    monkeypatch.setattr(receiver_module, "datetime", Earlier)
    assert (await client.post("/v1/hosted-node/materials/status", json=binding)).json()[
        "status"
    ] == "expired"
    assert not list(receiver.store.root.glob("*/*.material"))


@pytest.mark.parametrize("boundary", ["staged", "published"])
async def test_file_boundary_interruption_never_resolves(
    hosted, peer, interrupt_publication, protected_resolver, boundary
):
    client, receiver, _ = hosted
    other, _, _ = peer
    binding = await prepare(hosted)
    payload = {**binding, "material": "YQ=="}
    with interrupt_publication(boundary):
        response = await client.post("/v1/hosted-node/materials/install", json=payload)
    assert response.json()["status"] == "unavailable"
    assert list(receiver.store.root.glob("*/*.pending"))
    assert bool(list(receiver.store.root.glob("*/*.material"))) == (boundary == "published")
    for action in ("status", "install"):
        result = await other.post(
            f"/v1/hosted-node/materials/{action}", json=payload if action == "install" else binding
        )
        assert result.json()["status"] == "unavailable"
        assert result.json()["receipt_id"] == response.json()["receipt_id"]
    with pytest.raises(MaterialDeniedError):
        await protected_resolver(binding)
    revoke = {**binding, "fence": 2, "idempotency_key": "revoke"}
    assert (await other.post("/v1/hosted-node/materials/revoke", json=revoke)).json()[
        "status"
    ] == "revoked"
    assert not list(receiver.store.root.glob("*/*"))
    assert (await client.post("/v1/hosted-node/materials/install", json=payload)).json()[
        "status"
    ] == "revoked"
    with pytest.raises(MaterialDeniedError):
        await protected_resolver(binding)
    assert not list(receiver.store.root.glob("*/*"))


@pytest.mark.parametrize("rollback_before_finalize", [False, True])
async def test_expiry_during_pending_install_is_terminal(
    hosted, peer, monkeypatch, protected_resolver, rollback_before_finalize
):
    from awf.hosted_node import receiver as receiver_module
    from awf.hosted_node.models import Receipt

    client, receiver, _ = hosted
    other, _, _ = peer
    binding = await prepare(hosted)
    instant = datetime.now(UTC)
    binding["expires_at"] = (instant + timedelta(minutes=1)).isoformat()

    class Clock(datetime):
        @classmethod
        def now(cls, _tz=None):
            return instant

    monkeypatch.setattr(receiver_module, "datetime", Clock)
    entered, release = threading.Event(), threading.Event()
    install = receiver.store.install

    def stalled(*args):
        entered.set()
        assert release.wait(10)
        return install(*args)

    monkeypatch.setattr(receiver.store, "install", stalled)
    payload = {**binding, "material": "YQ=="}
    task = asyncio.create_task(client.post("/v1/hosted-node/materials/install", json=payload))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert (await other.post("/v1/hosted-node/materials/status", json=binding)).json()[
            "status"
        ] == "unavailable"
        with pytest.raises(MaterialDeniedError):
            await protected_resolver(binding)
        instant += timedelta(minutes=2)
        assert (await other.post("/v1/hosted-node/materials/status", json=binding)).json()[
            "status"
        ] == "expired"
        with pytest.raises(MaterialDeniedError):
            await protected_resolver(binding)
        if rollback_before_finalize:
            instant -= timedelta(minutes=2)
    finally:
        release.set()
        result = await task
    assert result.json()["status"] == "expired"
    assert (await other.post("/v1/hosted-node/materials/install", json=payload)).json()[
        "status"
    ] == "expired"
    with pytest.raises(MaterialDeniedError):
        await protected_resolver(binding)
    async with receiver.factory() as session:
        receipt = (await session.scalars(select(Receipt))).one()
        assert receipt.state == "expired"
        grant = (await session.scalars(select(Grant))).one()
        assert grant.current is None
    assert not list(receiver.store.root.glob("*/*"))
