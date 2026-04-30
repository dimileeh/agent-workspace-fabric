"""Repository tests for external callback persistence."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from awf.db.enums import CallbackDeliveryStatus
from awf.db.repositories import (
    CallbackDeliveryRepository,
    CallbackIdempotencyConflictError,
    CallbackSubscriptionRepository,
)
from awf.db.session import make_session_factory


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = make_session_factory(engine)
    async with factory() as s:
        yield s


async def _subscription(
    session: AsyncSession,
    *,
    idempotency_key: str = "callback-subscription",
    request_hash: str = "hash-a",
    enabled: bool = True,
):
    repo = CallbackSubscriptionRepository(session)
    subscription, _created = await repo.create_idempotent(
        name="repo-test",
        target_url="https://operator.example.com/events",
        event_types=["workspace.*"],
        enabled=enabled,
        timeout_seconds=10,
        max_attempts=3,
        initial_backoff_seconds=5,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    return subscription


@pytest.mark.unit
async def test_subscription_create_idempotent_persists_hash_and_detects_conflicts(
    session: AsyncSession,
) -> None:
    repo = CallbackSubscriptionRepository(session)

    created, was_created = await repo.create_idempotent(
        name="operator",
        target_url="https://operator.example.com/events",
        event_types=["workspace.*"],
        enabled=True,
        timeout_seconds=10,
        max_attempts=3,
        initial_backoff_seconds=5,
        idempotency_key="idem-subscription",
        request_hash="hash-original",
    )
    replay, replay_created = await repo.create_idempotent(
        name="operator",
        target_url="https://operator.example.com/events",
        event_types=["workspace.*"],
        enabled=True,
        timeout_seconds=10,
        max_attempts=3,
        initial_backoff_seconds=5,
        idempotency_key="idem-subscription",
        request_hash="hash-original",
    )

    assert was_created is True
    assert replay_created is False
    assert replay.id == created.id
    assert created.request_hash == "hash-original"

    with pytest.raises(CallbackIdempotencyConflictError):
        await repo.create_idempotent(
            name="operator",
            target_url="https://operator.example.com/changed",
            event_types=["workspace.*"],
            enabled=True,
            timeout_seconds=10,
            max_attempts=3,
            initial_backoff_seconds=5,
            idempotency_key="idem-subscription",
            request_hash="hash-changed",
        )


@pytest.mark.unit
async def test_subscription_list_can_filter_enabled(session: AsyncSession) -> None:
    repo = CallbackSubscriptionRepository(session)
    enabled = await _subscription(session, idempotency_key="enabled", enabled=True)
    disabled = await _subscription(
        session,
        idempotency_key="disabled",
        request_hash="disabled-hash",
        enabled=False,
    )

    all_rows = await repo.list(limit=50)
    enabled_rows = await repo.list(enabled=True, limit=50)

    assert [row.id for row in all_rows] == [disabled.id, enabled.id]
    assert [row.id for row in enabled_rows] == [enabled.id]


@pytest.mark.unit
async def test_subscription_event_matching_uses_public_allowlist(
    session: AsyncSession,
) -> None:
    wildcard = await _subscription(
        session,
        idempotency_key="workspace-wildcard",
        request_hash="workspace-wildcard",
    )
    repo = CallbackSubscriptionRepository(session)
    exact, _created = await repo.create_idempotent(
        name="exact-workspace",
        target_url="https://operator.example.com/events",
        event_types=["workspace.state_changed"],
        enabled=True,
        timeout_seconds=10,
        max_attempts=3,
        initial_backoff_seconds=5,
        idempotency_key="workspace-exact",
        request_hash="workspace-exact",
    )

    public_matches = await repo.list_enabled_for_event_type("workspace.state_changed")
    internal_matches = await repo.list_enabled_for_event_type("workspace.internal_secret")

    assert {row.id for row in public_matches} == {wildcard.id, exact.id}
    assert internal_matches == []


@pytest.mark.unit
async def test_delivery_enqueue_once_deduplicates_subscription_source_event(
    session: AsyncSession,
) -> None:
    subscription = await _subscription(session)
    repo = CallbackDeliveryRepository(session)
    now = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)

    first, first_created = await repo.enqueue_once(
        subscription=subscription,
        event_kind="workspace",
        event_type="workspace.state_changed",
        source_id="evt_123",
        dedupe_key="workspace:evt_123",
        workspace_id="ws_123",
        operation_id=None,
        merge_candidate_id=None,
        envelope={"event": {"type": "workspace.state_changed"}},
        now=now,
    )
    replay, replay_created = await repo.enqueue_once(
        subscription=subscription,
        event_kind="workspace",
        event_type="workspace.state_changed",
        source_id="evt_123",
        dedupe_key="workspace:evt_123",
        workspace_id="ws_123",
        operation_id=None,
        merge_candidate_id=None,
        envelope={"event": {"type": "workspace.state_changed"}},
        now=now,
    )

    assert first_created is True
    assert replay_created is False
    assert replay.id == first.id
    assert first.idempotency_key == replay.idempotency_key
    assert first.envelope["delivery"]["idempotency_key"] == first.idempotency_key


@pytest.mark.unit
async def test_due_delivery_query_returns_only_due_pending_rows_oldest_first(
    session: AsyncSession,
) -> None:
    subscription = await _subscription(session)
    repo = CallbackDeliveryRepository(session)
    now = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
    old_due, _ = await repo.enqueue_once(
        subscription=subscription,
        event_kind="workspace",
        event_type="workspace.state_changed",
        source_id="evt_old",
        dedupe_key="workspace:evt_old",
        workspace_id="ws_old",
        operation_id=None,
        merge_candidate_id=None,
        envelope={"event": {"type": "workspace.state_changed"}},
        now=now - timedelta(minutes=10),
    )
    future, _ = await repo.enqueue_once(
        subscription=subscription,
        event_kind="workspace",
        event_type="workspace.state_changed",
        source_id="evt_future",
        dedupe_key="workspace:evt_future",
        workspace_id="ws_future",
        operation_id=None,
        merge_candidate_id=None,
        envelope={"event": {"type": "workspace.state_changed"}},
        now=now + timedelta(minutes=10),
    )
    newer_due, _ = await repo.enqueue_once(
        subscription=subscription,
        event_kind="workspace",
        event_type="workspace.state_changed",
        source_id="evt_newer",
        dedupe_key="workspace:evt_newer",
        workspace_id="ws_newer",
        operation_id=None,
        merge_candidate_id=None,
        envelope={"event": {"type": "workspace.state_changed"}},
        now=now - timedelta(minutes=5),
    )
    future.status = CallbackDeliveryStatus.running.value
    await session.flush()

    due = await repo.list_due(now=now, limit=10)

    assert [row.id for row in due] == [old_due.id, newer_due.id]
