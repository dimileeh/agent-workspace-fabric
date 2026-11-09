"""Real Postgres uniqueness/upsert behavior for independently scoped connections."""

from datetime import UTC, datetime, timedelta

from awf.db.repositories import ProviderModelCircuitBreakerRepository, WorkerHeartbeatRepository
from tests.postgres import postgres_test_session


async def test_independent_scopes_and_batch_queries():
    async with postgres_test_session() as session:
        repo = ProviderModelCircuitBreakerRepository(session)
        now = datetime.now(UTC)
        for scope in ["endpoint-a", "endpoint-a", "endpoint-b"]:
            await repo.record_failure(
                provider="ollama",
                model="coder",
                connection_scope=scope,
                reason_code="AGENT_PROVIDER_RATE_LIMITED",
                failure_fingerprint="test",
                workspace_id=None,
                attempt_id=None,
                now=now,
                failure_threshold=2,
                cooldown_seconds=600,
            )
        assert (
            await repo.get(provider="ollama", model="coder", connection_scope="endpoint-a")
        ).failure_count == 2
        assert (
            await repo.get(provider="ollama", model="coder", connection_scope="endpoint-b")
        ).failure_count == 1
        pairs = [
            ("ollama", "coder", "endpoint-a"),
            ("ollama", "coder", "endpoint-b"),
            ("ollama", "coder"),
        ]
        assert set(await repo.open_breakers_for_pairs(pairs=pairs, now=now)) == {pairs[0]}
        assert not await repo.is_suppressed(
            provider="ollama", model="coder", connection_scope="endpoint-b", now=now
        )
        assert await repo.open_breaker(
            provider="ollama", model="coder", connection_scope="endpoint-a", now=now
        )
        assert not await repo.is_suppressed(
            provider="ollama",
            model="coder",
            connection_scope="endpoint-a",
            now=now + timedelta(seconds=601),
        )
        assert await repo.open_breakers_for_pairs(pairs=[], now=now) == {}


async def test_worker_fleet_rejects_missing_or_older_capabilities():
    async with postgres_test_session() as session:
        repo = WorkerHeartbeatRepository(session)
        now = datetime.now(UTC)
        assert not await repo.supports_execution_contract(now=now)
        for worker, version in [("new", 1), ("old", 0)]:
            await repo.record_heartbeat(
                worker_id=worker,
                node_id="local",
                started_at=now,
                last_heartbeat_at=now,
                poll_interval_seconds=5,
                execution_contract_version=version,
            )
        assert not await repo.supports_execution_contract(now=now)
        await repo.record_heartbeat(
            worker_id="old",
            node_id="local",
            started_at=now,
            last_heartbeat_at=now + timedelta(seconds=1),
            poll_interval_seconds=5,
            execution_contract_version=1,
        )
        assert await repo.supports_execution_contract(now=now + timedelta(seconds=1))


async def test_concurrent_failures_increment_same_scope_without_losing_history():
    import asyncio

    from awf.db.session import make_session_factory
    from tests.postgres import postgres_test_engine

    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)

        async def fail(scope):
            async with factory() as session, session.begin():
                await ProviderModelCircuitBreakerRepository(session).record_failure(
                    provider="ollama",
                    model="coder",
                    connection_scope=scope,
                    reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
                    failure_fingerprint="concurrent",
                    workspace_id=None,
                    attempt_id=None,
                    now=datetime.now(UTC),
                    failure_threshold=2,
                    cooldown_seconds=60,
                )

        await asyncio.gather(fail("shared"), fail("shared"), fail("independent"))
        async with factory() as session:
            repo = ProviderModelCircuitBreakerRepository(session)
            shared = await repo.get(provider="ollama", model="coder", connection_scope="shared")
            independent = await repo.get(
                provider="ollama", model="coder", connection_scope="independent"
            )
            assert shared.failure_count == 2 and shared.state == "open"
            assert independent.failure_count == 1 and independent.state == "closed"
