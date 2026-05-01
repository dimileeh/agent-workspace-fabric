"""Provider/model circuit breaker persistence tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.base import Base
from awf.db.repositories import ProviderModelCircuitBreakerRepository
from awf.db.session import make_engine, make_session_factory


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = make_session_factory(engine)
    async with factory() as s:
        yield s

    await engine.dispose()


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
