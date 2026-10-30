"""Recovery inventory bounds apply to live authority, not inert credential history."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, insert, select

from awf.hosted_node.identity import boot
from awf.hosted_node.lifecycle import NodeLifecycle
from awf.hosted_node.models import Grant, PreparationRow, Receipt
from awf.hosted_node.retained_work import MAX_ACTIVE_PREPARATIONS
from tests.integration.test_node_idle_gate import PREFIX, acknowledge, evidence
from tests.integration.test_node_maintenance import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node


async def seed_preparations(
    receiver, count, *, expires_at, receipt_state="installed", prefix="history"
):
    preparations, grants, receipts = [], [], []
    for index in range(count):
        key = f"{prefix}-{index}"
        preparation = {
            "namespace": "history",
            "workspace_id": "completed-history",
            "preparation_id": key,
            "purpose": "credential",
            "target": "/run/awf/secrets/agent",
            "expires_at": expires_at,
            "identity": receiver.identity.model_dump(),
        }
        preparations.append(
            {
                "id": key,
                "owner": receiver.identity.owner,
                "namespace": "history",
                "workspace_id": "completed-history",
                "preparation_id": key,
                "binding": preparation,
            }
        )
        grants.append(
            {
                "id": key,
                "preparation": key,
                "grant_id": key,
                "fence": 1,
                "revision": 1,
                "authorization_generation": 1,
                "revoked": False,
                "current": None if receipt_state == "pending" else key,
                "pending": key if receipt_state == "pending" else None,
            }
        )
        receipts.append(
            {
                "id": key,
                "owner": receiver.identity.owner,
                "idempotency_key": key,
                "grant": key,
                "binding": {
                    **preparation,
                    "grant_id": key,
                    "idempotency_key": key,
                    "fence": 1,
                    "revision": 1,
                    "authorization_generation": 1,
                },
                "operation": "install",
                "state": receipt_state,
            }
        )
    async with receiver.factory.begin() as session:
        await session.execute(insert(PreparationRow), preparations)
        await session.execute(insert(Grant), grants)
        await session.execute(insert(Receipt), receipts)


async def next_boot(client, receiver):
    receiver.identity = await boot(receiver.config, receiver.factory)
    receiver.lifecycle = NodeLifecycle(receiver.factory, receiver.identity)
    evidence(receiver)
    client.headers.update(receiver.identity.headers())


@pytest.mark.parametrize("receipt_state", ["installed", "expired", "pending"])
@pytest.mark.parametrize("offset", [0, 5.5])
async def test_expired_preparation_history_does_not_exhaust_live_cap(
    ready_node, receipt_state, offset
):
    client, receiver, _ = ready_node
    count = MAX_ACTIVE_PREPARATIONS + 1
    await seed_preparations(
        receiver,
        count,
        expires_at=(datetime.now(UTC) - timedelta(hours=2))
        .astimezone(timezone(timedelta(hours=offset)))
        .isoformat(),
        receipt_state=receipt_state,
    )
    await next_boot(client, receiver)
    response = await acknowledge(client)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "ready"
    assert response.json()["recovery_acceptance"]["attention_workspaces"] == 0
    async with receiver.factory() as session:
        assert await session.scalar(select(func.count()).select_from(PreparationRow)) == count
        old = (await session.get(Receipt, "history-0")).binding
    denied = await client.post("/v1/hosted-node/materials/status", json=old)
    assert denied.status_code == 403
    assert (await acknowledge(client)).json()["recovery_acceptance"] == response.json()[
        "recovery_acceptance"
    ]


@pytest.mark.parametrize("excess", [0, 1])
async def test_live_preparation_bound_is_unchanged_and_atomic(ready_node, excess):
    client, receiver, _ = ready_node
    count = MAX_ACTIVE_PREPARATIONS + excess
    await seed_preparations(
        receiver, count, expires_at=(datetime.now(UTC) + timedelta(minutes=20)).isoformat()
    )
    await next_boot(client, receiver)
    response = await acknowledge(client)
    assert response.status_code == (409 if excess else 200), response.text
    if excess:
        assert response.json()["error_code"] == "RECOVERY_OUTCOME_UNRESOLVED"
        assert (await client.get(PREFIX + "/readiness")).json()["recovery_acceptance"] is None
    async with receiver.factory() as session:
        revoked = await session.scalar(
            select(func.count()).select_from(Grant).where(Grant.revoked.is_(True))
        )
        assert revoked == (0 if excess else count)


@pytest.mark.parametrize(
    "expiry,code,reason",
    [
        (None, 409, "RECOVERY_OUTCOME_UNRESOLVED"),
        ("not-a-timestamp", 409, "RECOVERY_OUTCOME_UNRESOLVED"),
        ("2099-01-01T00:00:00", 409, "RECOVERY_OUTCOME_UNRESOLVED"),
        ("2000-01-01T00:00:00", 409, "RECOVERY_OUTCOME_UNRESOLVED"),
        ("not-a-timestampZ", 503, "AUTHORITY_UNAVAILABLE"),
        ("2000-02-30T00:00:00Z", 503, "AUTHORITY_UNAVAILABLE"),
        ("2000-01-01T00:00:00+99:99", 503, "AUTHORITY_UNAVAILABLE"),
    ],
)
async def test_malformed_preparation_expiry_never_becomes_inert(ready_node, expiry, code, reason):
    client, receiver, _ = ready_node
    await seed_preparations(receiver, 1, expires_at=expiry)
    await next_boot(client, receiver)
    response = await acknowledge(client)
    assert response.status_code == code, response.text
    assert response.json()["error_code"] == reason
    async with receiver.factory() as session:
        assert not (await session.get(Grant, "history-0")).revoked
    state = (await client.get(PREFIX + "/readiness")).json()
    assert state["state"] == "recovering" and state["recovery_acceptance"] is None


async def test_expired_history_preserves_current_grants_and_revokes_old_live_grants(ready_node):
    client, receiver, _ = ready_node
    await seed_preparations(
        receiver,
        MAX_ACTIVE_PREPARATIONS + 1,
        expires_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
    )
    future = (datetime.now(UTC) + timedelta(minutes=20)).isoformat()
    await seed_preparations(receiver, 1, expires_at=future, prefix="old-live")
    await next_boot(client, receiver)
    await seed_preparations(receiver, 1, expires_at=future, prefix="current-live")
    response = await acknowledge(client)
    assert response.status_code == 200, response.text
    async with receiver.factory() as session:
        old = await session.get(Grant, "old-live-0")
        current = await session.get(Grant, "current-live-0")
        assert old.revoked and old.current is None and old.pending is None
        assert not current.revoked and current.current == "current-live-0"


@pytest.mark.parametrize("fault", ["foreign", "future", "orphan"])
async def test_unresolved_live_grant_authority_still_blocks_acceptance(ready_node, fault):
    client, receiver, _ = ready_node
    await seed_preparations(
        receiver, 1, expires_at=(datetime.now(UTC) + timedelta(minutes=20)).isoformat()
    )
    async with receiver.factory.begin() as session:
        preparation = await session.get(PreparationRow, "history-0")
        if fault == "orphan":
            await session.delete(preparation)
        else:
            identity = dict(preparation.binding["identity"])
            if fault == "foreign":
                identity["owner"] = "foreign-owner"
            else:
                identity["boot_epoch"] += 100
            preparation.binding = {**preparation.binding, "identity": identity}
    await next_boot(client, receiver)
    result = await acknowledge(client)
    assert (
        result.status_code == 409 and result.json()["error_code"] == "RECOVERY_OUTCOME_UNRESOLVED"
    )
    async with receiver.factory() as session:
        assert not (await session.get(Grant, "history-0")).revoked
