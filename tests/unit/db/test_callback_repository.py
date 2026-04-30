"""Repository tests for external callback persistence."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

import awf.db.repositories as repository_module
from awf.db.enums import CallbackDeliveryStatus
from awf.db.models import CallbackSubscription
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
    event_types: list[str] | None = None,
):
    repo = CallbackSubscriptionRepository(session)
    subscription, _created = await repo.create_idempotent(
        name="repo-test",
        target_url="https://operator.example.com/events",
        event_types=event_types or ["workspace.*"],
        enabled=enabled,
        timeout_seconds=10,
        max_attempts=3,
        initial_backoff_seconds=5,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    return subscription


@pytest.mark.unit
def test_callback_repository_insert_helpers_cover_postgresql_and_fallback_dialects() -> None:
    assert repository_module._callback_delivery_insert_if_absent_stmt("postgresql") is not None
    assert repository_module._callback_subscription_insert_if_absent_stmt("postgresql") is not None
    assert repository_module._callback_delivery_insert_if_absent_stmt("mysql") is None
    assert repository_module._callback_subscription_insert_if_absent_stmt("mysql") is None
    assert repository_module._secret_lease_insert_if_absent_stmt("mysql") is None
    assert repository_module._callback_subscription_event_type_candidates("internal.event") == ()
    assert (
        repository_module._callback_subscription_event_type_filter(
            ("workspace.created",),
            "postgresql",
        )
        is not None
    )


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
async def test_subscription_create_idempotent_replays_after_duplicate_key_race(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    winner = await _subscription(
        session,
        idempotency_key="idem-race",
        request_hash="hash-original",
    )
    repo = CallbackSubscriptionRepository(session)
    original_get = repo.get_by_idempotency_key
    lookups = 0

    async def miss_once_then_get(key: str) -> CallbackSubscription | None:
        nonlocal lookups
        lookups += 1
        if lookups == 1:
            return None
        return await original_get(key)

    monkeypatch.setattr(repo, "get_by_idempotency_key", miss_once_then_get)

    replay, was_created = await repo.create_idempotent(
        name="operator",
        target_url="https://operator.example.com/events",
        event_types=["workspace.*"],
        enabled=True,
        timeout_seconds=10,
        max_attempts=3,
        initial_backoff_seconds=5,
        idempotency_key="idem-race",
        request_hash="hash-original",
    )

    assert was_created is False
    assert replay.id == winner.id
    assert lookups == 2


@pytest.mark.unit
async def test_subscription_create_idempotent_falls_back_without_conflict_helper(
    session: AsyncSession,
) -> None:
    repo = CallbackSubscriptionRepository(session, dialect_name="unsupported")

    created, was_created = await repo.create_idempotent(
        name="operator",
        target_url="https://operator.example.com/events",
        event_types=["workspace.*"],
        enabled=True,
        timeout_seconds=10,
        max_attempts=3,
        initial_backoff_seconds=5,
        idempotency_key="idem-fallback",
        request_hash="hash-fallback",
    )

    assert was_created is True
    assert created.idempotency_key == "idem-fallback"
    assert created.disabled_at is None


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
async def test_subscription_event_matching_filters_nonmatches_in_database(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matching = await _subscription(
        session,
        idempotency_key="matching-workspace",
        request_hash="matching-workspace",
        event_types=["workspace.*"],
    )
    nonmatching = await _subscription(
        session,
        idempotency_key="nonmatching-operation",
        request_hash="nonmatching-operation",
        event_types=["operation.*"],
    )
    disabled_match = await _subscription(
        session,
        idempotency_key="disabled-workspace",
        request_hash="disabled-workspace",
        enabled=False,
        event_types=["workspace.*"],
    )
    await session.flush()
    session.expunge_all()

    def fail_python_filtering(subscription_event_type: str, event_type: str) -> bool:
        raise AssertionError(
            "subscription event matching should be pushed into the database query"
        )

    monkeypatch.setattr(
        repository_module,
        "callback_subscription_matches_event_type",
        fail_python_filtering,
        raising=False,
    )

    rows = await CallbackSubscriptionRepository(session).list_enabled_for_event_type(
        "workspace.state_changed"
    )

    loaded_subscription_ids = {
        row.id
        for row in session.identity_map.values()
        if isinstance(row, CallbackSubscription)
    }
    assert [row.id for row in rows] == [matching.id]
    assert nonmatching.id not in loaded_subscription_ids
    assert disabled_match.id not in loaded_subscription_ids


@pytest.mark.unit
@pytest.mark.parametrize("repository_call", ["list", "list_enabled_for_event_type"])
async def test_subscription_queries_do_not_eager_load_delivery_history(
    session: AsyncSession,
    repository_call: str,
) -> None:
    subscription = await _subscription(session)
    subscription_id = subscription.id
    now = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
    await CallbackDeliveryRepository(session).enqueue_once(
        subscription=subscription,
        event_kind="workspace",
        event_type="workspace.state_changed",
        source_id="evt_history",
        dedupe_key="workspace:evt_history",
        workspace_id="ws_history",
        operation_id=None,
        merge_candidate_id=None,
        envelope={"event": {"type": "workspace.state_changed"}},
        now=now,
    )
    await session.flush()
    session.expunge_all()

    statements: list[str] = []
    bind = session.get_bind()

    def record_sql(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del conn, cursor, parameters, context, executemany
        statements.append(" ".join(statement.lower().split()))

    event.listen(bind, "before_cursor_execute", record_sql)
    try:
        repo = CallbackSubscriptionRepository(session)
        if repository_call == "list":
            rows = await repo.list(limit=50)
        else:
            rows = await repo.list_enabled_for_event_type("workspace.state_changed")
    finally:
        event.remove(bind, "before_cursor_execute", record_sql)

    assert [row.id for row in rows] == [subscription_id]
    assert [
        statement
        for statement in statements
        if statement.startswith("select") and "from callback_deliveries" in statement
    ] == []


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
async def test_delivery_enqueue_once_replays_after_duplicate_key_race(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subscription = await _subscription(session)
    repo = CallbackDeliveryRepository(session)
    now = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
    winner, _created = await repo.enqueue_once(
        subscription=subscription,
        event_kind="workspace",
        event_type="workspace.state_changed",
        source_id="evt_race",
        dedupe_key="workspace:evt_race",
        workspace_id="ws_race",
        operation_id=None,
        merge_candidate_id=None,
        envelope={"event": {"type": "workspace.state_changed"}},
        now=now,
    )
    original_get = repo.get_by_dedupe_key
    lookups = 0

    async def miss_once_then_get(
        *,
        subscription_id: str,
        dedupe_key: str,
    ):
        nonlocal lookups
        lookups += 1
        if lookups == 1:
            return None
        return await original_get(
            subscription_id=subscription_id,
            dedupe_key=dedupe_key,
        )

    monkeypatch.setattr(repo, "get_by_dedupe_key", miss_once_then_get)

    replay, was_created = await repo.enqueue_once(
        subscription=subscription,
        event_kind="workspace",
        event_type="workspace.state_changed",
        source_id="evt_race",
        dedupe_key="workspace:evt_race",
        workspace_id="ws_race",
        operation_id=None,
        merge_candidate_id=None,
        envelope={"event": {"type": "workspace.state_changed"}},
        now=now,
    )

    assert was_created is False
    assert replay.id == winner.id
    assert lookups == 2


@pytest.mark.unit
async def test_delivery_enqueue_once_falls_back_without_conflict_helper(
    session: AsyncSession,
) -> None:
    subscription = await _subscription(session)
    repo = CallbackDeliveryRepository(session, dialect_name="unsupported")
    now = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)

    created, was_created = await repo.enqueue_once(
        subscription=subscription,
        event_kind="workspace",
        event_type="workspace.state_changed",
        source_id="evt_fallback",
        dedupe_key="workspace:evt_fallback",
        workspace_id="ws_fallback",
        operation_id=None,
        merge_candidate_id=None,
        envelope={"event": {"type": "workspace.state_changed"}},
        now=now,
    )

    assert was_created is True
    assert created.subscription_id == subscription.id
    assert created.envelope["delivery"]["dedupe_key"] == "workspace:evt_fallback"


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
