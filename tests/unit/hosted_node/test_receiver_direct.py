"""Exercise durable receiver contracts directly, independently of ASGI dispatch."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from awf.hosted_node.identity import MaterialDeniedError
from awf.hosted_node.models import Grant, PreparationRow, Receipt
from awf.hosted_node.schemas import Binding, Install, Preparation, Refresh


def preparation():
    return Preparation(
        namespace="ns",
        workspace_id="ws",
        preparation_id="prep",
        purpose="credential",
        target="/run/awf/secrets/key",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )


def installation(receiver, prep, **changes):
    return Install(
        **(
            prep.model_dump()
            | {
                "identity": receiver.identity,
                "idempotency_key": "install",
                "grant_id": "grant",
                "revision": 1,
                "authorization_generation": 1,
                "fence": 1,
                "material": "YQ==",
            }
            | changes
        )
    )


async def test_receiver_durable_refresh_replay_and_revocation(hosted):
    _, receiver, _ = hosted
    prep = preparation()
    await receiver.prepare(prep)
    await receiver.prepare(prep)
    first = installation(receiver, prep)
    accepted = await receiver.install(first)
    assert accepted.status == "current"
    assert accepted.reason == "ACCEPTED"
    assert await receiver.install(first.model_copy(update={"material": "Yg=="})) == accepted
    async with receiver.factory() as session:
        stored = (await session.scalars(select(PreparationRow))).one()
        directory = stored.id
        receipt = (await session.scalars(select(Receipt))).one()
        assert receipt.state == "installed"
        assert receipt.id == accepted.receipt_id
    assert receiver.store.path(directory, accepted.receipt_id).read_bytes() == b"a"

    newer = Refresh(
        **first.model_dump(),
        material="Yg==",
        predecessor=accepted.receipt_id,
    ).model_copy(update={"idempotency_key": "refresh", "fence": 2, "revision": 2})
    refreshed = await receiver.install(newer, refresh=True)
    assert refreshed.status == "current"
    assert refreshed.receipt_id != accepted.receipt_id
    assert refreshed.revision == 2
    assert not receiver.store.path(directory, accepted.receipt_id).exists()
    assert receiver.store.path(directory, refreshed.receipt_id).read_bytes() == b"b"
    assert (await receiver.status(first)).reason == "SUPERSEDED"
    assert await receiver.install(newer, refresh=True) == refreshed
    for value, refresh in (
        (newer, False),
        (newer.model_copy(update={"predecessor": "wrong"}), True),
    ):
        with pytest.raises(MaterialDeniedError, match="IDEMPOTENCY_CONFLICT"):
            await receiver.install(value, refresh=refresh)

    revoke = Binding.model_validate(newer.model_dump(exclude={"predecessor"})).model_copy(
        update={"idempotency_key": "revoke", "fence": 3}
    )
    denied = await receiver.revoke(revoke)
    assert denied.status == "revoked"
    assert await receiver.revoke(revoke) == denied
    assert (await receiver.install(newer, refresh=True)).reason == "GRANT_REVOKED"
    assert not list(receiver.store.root.glob("*/*"))
    async with receiver.factory() as session:
        grant = (await session.scalars(select(Grant))).one()
        assert grant.revoked and grant.current is None and grant.pending is None
        assert grant.fence == 3 and grant.revision == 2
        receipt = await session.get(Receipt, denied.receipt_id)
        assert receipt.state == "cleaned"


async def test_receiver_rejects_conflicting_authority_without_mutating_material(hosted):
    _, receiver, _ = hosted
    assert not (await receiver.identity_status()).acknowledged
    assert (await receiver.identity_status("ack")).acknowledged
    assert (await receiver.identity_status("ack")).acknowledged
    with pytest.raises(MaterialDeniedError, match="ACKNOWLEDGEMENT_CONFLICT"):
        await receiver.identity_status("other")
    prep = preparation()
    first = installation(receiver, prep)
    with pytest.raises(MaterialDeniedError, match="PREPARATION_NOT_FOUND"):
        await receiver.install(first)
    await receiver.prepare(prep)
    with pytest.raises(MaterialDeniedError, match="PREPARATION_CONFLICT"):
        await receiver.prepare(prep.model_copy(update={"target": "/run/awf/secrets/other"}))
    with pytest.raises(MaterialDeniedError, match="PREPARATION_MISMATCH"):
        await receiver.install(
            first.model_copy(update={"expires_at": prep.expires_at + timedelta(seconds=1)})
        )
    with pytest.raises(MaterialDeniedError, match="PREDECESSOR_MISMATCH"):
        await receiver.install(
            Refresh(**first.model_dump(), material="YQ==", predecessor="absent"), refresh=True
        )
    accepted = await receiver.install(first)
    for changes, reason in (
        ({"revision": 2}, "IDEMPOTENCY_CONFLICT"),
        ({"idempotency_key": "stale"}, "GRANT_CONFLICT"),
        ({"idempotency_key": "replace", "fence": 2}, "GRANT_CONFLICT"),
    ):
        with pytest.raises(MaterialDeniedError, match=reason):
            await receiver.install(first.model_copy(update=changes))
    with pytest.raises(MaterialDeniedError, match="IDEMPOTENCY_CONFLICT"):
        await receiver.revoke(Binding.model_validate(first.model_dump()))
    with pytest.raises(MaterialDeniedError, match="GRANT_CONFLICT"):
        await receiver.revoke(
            Binding.model_validate(first.model_dump()).model_copy(
                update={"idempotency_key": "stale-revoke"}
            )
        )
    assert await receiver.status(first) == accepted
    files = list(receiver.store.root.glob("*/*.material"))
    assert len(files) == 1 and files[0].read_bytes() == b"a"
    async with receiver.factory() as session:
        assert len((await session.scalars(select(Receipt))).all()) == 1


async def test_expiry_cleanup_failure_keeps_receipt_expired_until_cleanup_replays(
    hosted, monkeypatch
):
    from awf.hosted_node import receiver as receiver_module

    _, receiver, _ = hosted
    prep = preparation()
    await receiver.prepare(prep)
    first = installation(receiver, prep)
    accepted = await receiver.install(first)
    instant = prep.expires_at + timedelta(seconds=1)

    class Clock(datetime):
        @classmethod
        def now(cls, _tz=None):
            return instant

    def fail_remove(*args):
        raise OSError("injected cleanup failure")

    monkeypatch.setattr(receiver_module, "datetime", Clock)
    with monkeypatch.context() as patch:
        patch.setattr(receiver.store, "remove", fail_remove)
        expired = await receiver.status(first)
    assert expired.status == "expired"
    assert expired.reason == "EXPIRY_CLEANUP_PENDING"
    assert expired.receipt_id == accepted.receipt_id
    assert list(receiver.store.root.glob("*/*.material"))
    async with receiver.factory() as session:
        receipt = await session.get(Receipt, accepted.receipt_id)
        assert receipt.state == "expired"
    # A clock rollback cannot reactivate the durable expired receipt.
    instant = prep.expires_at - timedelta(seconds=1)
    replay = await receiver.status(first)
    assert replay.status == "expired" and replay.reason == "EXPIRY"
    assert not list(receiver.store.root.glob("*/*"))
