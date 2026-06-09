"""Provider/model circuit breaker persistence tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.repositories import ProviderModelCircuitBreakerRepository
from tests.postgres import postgres_test_session


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


@pytest.mark.unit
async def test_repeated_capacity_failures_open_provider_model_circuit(
    session: AsyncSession,
) -> None:
    repo = ProviderModelCircuitBreakerRepository(session)
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    first = await repo.record_failure(
        provider="google",
        model="gemini-2.5-pro",
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        failure_fingerprint="capacity:fingerprint",
        workspace_id="ws_first",
        attempt_id="att_first",
        now=now,
        failure_threshold=2,
        cooldown_seconds=600,
    )
    assert first.state == "closed"

    second = await repo.record_failure(
        provider="google",
        model="gemini-2.5-pro",
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        failure_fingerprint="capacity:fingerprint",
        workspace_id="ws_second",
        attempt_id="att_second",
        now=now + timedelta(seconds=10),
        failure_threshold=2,
        cooldown_seconds=600,
    )

    assert second.state == "open"
    assert second.failure_count == 2
    assert second.cooldown_until == now + timedelta(seconds=610)
    assert await repo.is_suppressed(
        provider="google",
        model="gemini-2.5-pro",
        now=now + timedelta(seconds=30),
    )
    assert not await repo.is_suppressed(
        provider="google",
        model="gemini-flash",
        now=now + timedelta(seconds=30),
    )


@pytest.mark.unit
async def test_record_failure_reuses_row_after_stale_create_miss(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = ProviderModelCircuitBreakerRepository(session)
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    existing = await repo.record_failure(
        provider="google",
        model="gemini-2.5-pro",
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        failure_fingerprint="capacity:first",
        workspace_id="ws_first",
        attempt_id="att_first",
        now=now,
        failure_threshold=2,
        cooldown_seconds=600,
    )

    original_get = repo.get
    calls = 0

    async def stale_first_get(
        *,
        provider: str,
        model: str,
    ):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return await original_get(provider=provider, model=model)

    monkeypatch.setattr(repo, "get", stale_first_get)

    breaker = await repo.record_failure(
        provider="google",
        model="gemini-2.5-pro",
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        failure_fingerprint="capacity:second",
        workspace_id="ws_second",
        attempt_id="att_second",
        now=now + timedelta(seconds=10),
        failure_threshold=2,
        cooldown_seconds=600,
    )

    assert breaker.id == existing.id
    assert breaker.failure_count == 2
    assert breaker.state == "open"
    assert breaker.last_failure_fingerprint == "capacity:second"


@pytest.mark.unit
async def test_record_failure_errors_when_created_breaker_cannot_be_reloaded(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = ProviderModelCircuitBreakerRepository(session)

    async def always_missing(
        *,
        provider: str,
        model: str,
    ):
        return None

    monkeypatch.setattr(repo, "get", always_missing)

    with pytest.raises(RuntimeError, match="insert did not return a row"):
        await repo.record_failure(
            provider="google",
            model="gemini-2.5-pro",
            reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
            failure_fingerprint="capacity:fingerprint",
            workspace_id="ws_first",
            attempt_id="att_first",
            now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
            failure_threshold=2,
            cooldown_seconds=600,
        )


@pytest.mark.unit
async def test_record_failure_uses_orm_create_when_insert_helper_is_unavailable(
    session: AsyncSession,
) -> None:
    repo = ProviderModelCircuitBreakerRepository(session, dialect_name="sqlite")

    breaker = await repo.record_failure(
        provider="google",
        model="gemini-2.5-pro",
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        failure_fingerprint="capacity:fingerprint",
        workspace_id="ws_first",
        attempt_id="att_first",
        now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC),
        failure_threshold=1,
        cooldown_seconds=600,
    )

    assert breaker.provider == "google"
    assert breaker.model == "gemini-2.5-pro"
    assert breaker.state == "open"


@pytest.mark.unit
async def test_provider_model_circuit_expires_deterministically(
    session: AsyncSession,
) -> None:
    repo = ProviderModelCircuitBreakerRepository(session)
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    await repo.record_failure(
        provider="google",
        model="gemini-2.5-pro",
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        failure_fingerprint="capacity:fingerprint",
        workspace_id="ws_first",
        attempt_id=None,
        now=now,
        failure_threshold=1,
        cooldown_seconds=300,
    )

    assert await repo.is_suppressed(
        provider="google",
        model="gemini-2.5-pro",
        now=now + timedelta(seconds=299),
    )
    assert not await repo.is_suppressed(
        provider="google",
        model="gemini-2.5-pro",
        now=now + timedelta(seconds=300),
    )
    breaker = await repo.get(provider="google", model="gemini-2.5-pro")
    assert breaker is not None
    assert breaker.state == "closed"
    assert breaker.failure_count == 0


@pytest.mark.unit
async def test_provider_model_circuit_open_queries_close_expired_rows(
    session: AsyncSession,
) -> None:
    repo = ProviderModelCircuitBreakerRepository(session)
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    breaker = await repo.record_failure(
        provider=" google ",
        model=" gemini-2.5-pro ",
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        failure_fingerprint="capacity:first",
        workspace_id="ws_first",
        attempt_id=None,
        now=now,
        failure_threshold=1,
        cooldown_seconds=10,
    )
    assert breaker.state == "open"

    refreshed = await repo.record_failure(
        provider="google",
        model="gemini-2.5-pro",
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        failure_fingerprint="capacity:second",
        workspace_id="ws_second",
        attempt_id="att_second",
        now=now + timedelta(seconds=11),
        failure_threshold=2,
        cooldown_seconds=10,
    )
    assert refreshed.id == breaker.id
    assert refreshed.state == "closed"
    assert refreshed.failure_count == 1
    assert refreshed.opened_at is None
    assert refreshed.cooldown_until is None

    open_breaker = await repo.record_failure(
        provider="anthropic",
        model="claude-sonnet",
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        failure_fingerprint="capacity:anthropic",
        workspace_id="ws_anthropic",
        attempt_id=None,
        now=now,
        failure_threshold=1,
        cooldown_seconds=10,
    )
    assert (
        await repo.open_breaker(
            provider="anthropic",
            model="missing",
            now=now,
        )
        is None
    )
    assert (
        await repo.open_breaker(
            provider="anthropic",
            model="claude-sonnet",
            now=now + timedelta(seconds=5),
        )
        == open_breaker
    )
    assert (
        await repo.open_breaker(
            provider="anthropic",
            model="claude-sonnet",
            now=now + timedelta(seconds=10),
        )
        is None
    )

    await repo.record_failure(
        provider="openai",
        model="gpt-5.5",
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        failure_fingerprint="capacity:openai",
        workspace_id="ws_openai",
        attempt_id=None,
        now=now,
        failure_threshold=1,
        cooldown_seconds=10,
    )
    assert await repo.open_breakers_for_pairs(pairs=[("", "gpt-5.5")], now=now) == {}
    assert (
        await repo.open_breakers_for_pairs(
            pairs=[("openai", "gpt-5.5")],
            now=now + timedelta(seconds=10),
        )
        == {}
    )

    await repo.record_failure(
        provider="ollama",
        model="llama3",
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        failure_fingerprint="capacity:ollama",
        workspace_id="ws_ollama",
        attempt_id=None,
        now=now,
        failure_threshold=1,
        cooldown_seconds=10,
    )
    active = await repo.record_failure(
        provider="google",
        model="gemini-flash",
        reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
        failure_fingerprint="capacity:flash",
        workspace_id="ws_flash",
        attempt_id=None,
        now=now,
        failure_threshold=1,
        cooldown_seconds=30,
    )

    assert await repo.list_open(now=now + timedelta(seconds=10)) == [active]
