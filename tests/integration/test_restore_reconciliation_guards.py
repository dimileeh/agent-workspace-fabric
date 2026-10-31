"""Raw DML remains contained after replacing node-wide quarantine with row fences."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from awf.hosted_node.lifecycle_barrier import GUARDED_TABLES
from tests.integration.test_checkpoint_marker import schema_revision as schema_revision
from tests.integration.test_node_idle_gate import PREFIX, create
from tests.integration.test_node_idle_gate import node as node
from tests.integration.test_node_maintenance import ready_node as ready_node
from tests.integration.test_restore_quarantine import restore
from tests.integration.test_restore_reconciliation import unknown_decisions
from tests.integration.test_restore_row_families import seed_rows


@pytest.fixture
async def row_holds(ready_node):
    _, receiver, _ = ready_node
    await seed_rows(receiver)
    async with receiver.factory.begin() as session:
        await session.execute(
            text("UPDATE callback_subscriptions SET event_types='[\"workspace.created\"]'")
        )
    client, receiver, _, binding = await restore(ready_node)
    accepted = await client.post(
        PREFIX + "/recovery/acknowledge",
        json={
            "idempotency_key": binding.restore_id,
            "receipt_ref": "restore-receipt",
        },
    )
    assert accepted.status_code == 200, accepted.text
    proof = None

    async def current(inventory):
        nonlocal proof
        if proof is None:
            proof = unknown_decisions(inventory)
        return proof.model_copy(update={"expires_at": datetime.now(UTC) + timedelta(seconds=10)})

    receiver.lifecycle.verify_restore_reconciliation = current
    response = await client.post(
        PREFIX + "/recovery/acknowledge",
        json={
            "idempotency_key": "row-holds",
            "receipt_ref": "current-row-evidence",
        },
    )
    assert response.status_code == 200, response.text
    assert (
        await client.post(PREFIX + "/maintenance/release", json={"generation": 7})
    ).status_code == 200
    return receiver


@pytest.mark.parametrize("family", GUARDED_TABLES)
async def test_each_guarded_family_stays_held_after_new_admission_opens(row_holds, family):
    receiver = row_holds
    with pytest.raises(DBAPIError, match="RESTORE_ROW_HELD|RECOVERY_ATTENTION_REQUIRED"):
        async with receiver.factory.begin() as session:
            await session.execute(text(f"UPDATE {family} SET id=id"))
    await create(receiver.factory)


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE hosted_restore_row_holds SET state='resolved'",
        "DELETE FROM hosted_restore_row_holds",
        "UPDATE hosted_node_lifecycle SET restore_reconciliation_id=NULL",
        "DELETE FROM hosted_node_lifecycle",
        "DELETE FROM workspace_events",
    ],
)
async def test_ordinary_sql_cannot_drop_exact_restore_fences(row_holds, mutation):
    with pytest.raises(
        DBAPIError, match="RESTORE_RECONCILIATION_REQUIRED|RECOVERY_ATTENTION_REQUIRED"
    ):
        async with row_holds.factory.begin() as session:
            await session.execute(text(mutation))


@pytest.mark.parametrize("replacement", ["NULL", "'{}'::jsonb", '\'{"state":"pending"}\'::jsonb'])
async def test_held_node_latch_cannot_be_erased_or_rebound_without_journal(ready_node, replacement):
    client, receiver, _, binding = await restore(ready_node)
    assert (
        await client.post(
            PREFIX + "/recovery/acknowledge",
            json={
                "idempotency_key": binding.restore_id,
                "receipt_ref": "restore-receipt",
            },
        )
    ).status_code == 200
    with pytest.raises(DBAPIError, match="RESTORE_RECONCILIATION_REQUIRED"):
        async with receiver.factory.begin() as session:
            await session.execute(
                text(f"UPDATE hosted_node_lifecycle SET restore_quarantine={replacement}")
            )


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE hosted_restore_reconciliations SET state='staged' WHERE state='committed'",
        "UPDATE hosted_restore_reconciliations SET plan='{}'::jsonb",
        "DELETE FROM hosted_restore_reconciliations",
    ],
)
async def test_committed_journal_is_not_an_ordinary_mutable_record(row_holds, mutation):
    with pytest.raises(DBAPIError, match="RESTORE_JOURNAL_IMMUTABLE"):
        async with row_holds.factory.begin() as session:
            await session.execute(text(mutation))


async def test_unrelated_callback_and_gc_claims_skip_only_restored_holds(row_holds):
    from awf.db.models import CallbackDelivery, CallbackSubscription
    from awf.db.repositories.service_gc_request_repo import ServiceGCRequestRepository
    from awf.db.repositories.workspace_repo_callbacks import (
        CallbackDeliveryRepository,
        CallbackSubscriptionRepository,
    )

    receiver = row_holds
    now = datetime.now(UTC)
    async with receiver.factory.begin() as session:
        session.add(
            CallbackSubscription(
                id="fresh-subscription",
                name="fresh",
                target_url="https://example.invalid/new",
                timeout_seconds=1,
                max_attempts=1,
                initial_backoff_seconds=1,
                idempotency_key="fresh",
                request_hash="fresh",
                event_types=["workspace.created"],
            )
        )
        await session.flush()
        session.add(
            CallbackDelivery(
                id="fresh-callback",
                subscription_id="fresh-subscription",
                workspace_id=None,
                event_kind="workspace",
                event_type="workspace.created",
                source_id="fresh-source",
                dedupe_key="fresh",
                idempotency_key="fresh",
                max_attempts=1,
                status="pending",
            )
        )
        fresh_gc = await ServiceGCRequestRepository(session).create_pending(
            node_id=None, requested_at=now, deadline_at=now + timedelta(minutes=1)
        )
    async with receiver.factory.begin() as session:
        callbacks = await CallbackDeliveryRepository(session).list_due()
        assert [row.id for row in callbacks] == ["fresh-callback"]
        subscriptions = await CallbackSubscriptionRepository(session).list_enabled_for_event_type(
            "workspace.created"
        )
        assert [row.id for row in subscriptions] == ["fresh-subscription"]
        claimed = await ServiceGCRequestRepository(session).claim_oldest_pending(
            node_id=receiver.identity.owner, now=now
        )
        assert claimed.id == fresh_gc.id
    async with receiver.factory.begin() as session:
        assert await ServiceGCRequestRepository(session).expire_stale_requests(
            node_id=receiver.identity.owner, now=now + timedelta(hours=1)
        ) == [fresh_gc.id]
        assert (
            await session.scalar(text("SELECT status FROM service_gc_requests WHERE id='gc'"))
            == "pending"
        )
