"""Worker heartbeat repository behavior tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from awf.db.models import WorkerHeartbeat
from awf.db.repositories.base import _worker_heartbeat_upsert_stmt
from awf.db.repositories.system_repo import WorkerHeartbeatRepository
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine


@pytest.mark.unit
def test_worker_heartbeat_upsert_supports_postgres_only() -> None:
    stmt = _worker_heartbeat_upsert_stmt("postgresql")

    assert stmt is not None
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    set_clause = sql.split(" DO UPDATE SET ", maxsplit=1)[1].split(
        " RETURNING ",
        maxsplit=1,
    )[0]
    assert "ON CONFLICT (worker_id) DO UPDATE" in sql
    assert (
        "node_id = CASE WHEN (excluded.last_heartbeat_at >= "
        "worker_heartbeats.last_heartbeat_at) THEN excluded.node_id "
        "ELSE worker_heartbeats.node_id END"
    ) in set_clause
    assert (
        "last_heartbeat_at = greatest(worker_heartbeats.last_heartbeat_at, "
        "excluded.last_heartbeat_at)"
    ) in set_clause
    assert (
        "poll_interval_seconds = CASE WHEN (excluded.last_heartbeat_at >= "
        "worker_heartbeats.last_heartbeat_at) THEN excluded.poll_interval_seconds "
        "ELSE worker_heartbeats.poll_interval_seconds END"
    ) in set_clause
    assert (
        "updated_at = CASE WHEN (excluded.last_heartbeat_at >= "
        "worker_heartbeats.last_heartbeat_at) THEN excluded.updated_at "
        "ELSE worker_heartbeats.updated_at END"
    ) in set_clause
    assert "started_at =" not in set_clause

    assert _worker_heartbeat_upsert_stmt(None) is None


@pytest.mark.unit
async def test_record_heartbeat_handles_concurrent_first_writes() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        worker_id = "worker_concurrent_first_heartbeat"
        started_at = datetime(2026, 6, 4, 8, 0, tzinfo=UTC)
        first_heartbeat_at = started_at + timedelta(seconds=1)
        second_heartbeat_at = started_at + timedelta(seconds=2)

        async def write_heartbeat(
            *,
            node_id: str,
            last_heartbeat_at: datetime,
            poll_interval_seconds: float,
        ) -> None:
            async with factory() as session, session.begin():
                await WorkerHeartbeatRepository(session).record_heartbeat(
                    worker_id=worker_id,
                    node_id=node_id,
                    started_at=started_at,
                    last_heartbeat_at=last_heartbeat_at,
                    poll_interval_seconds=poll_interval_seconds,
                )

        await asyncio.gather(
            write_heartbeat(
                node_id="node-a",
                last_heartbeat_at=first_heartbeat_at,
                poll_interval_seconds=5.0,
            ),
            write_heartbeat(
                node_id="node-b",
                last_heartbeat_at=second_heartbeat_at,
                poll_interval_seconds=6.0,
            ),
        )

        async with factory() as session:
            heartbeat = await WorkerHeartbeatRepository(session).get(worker_id=worker_id)

    assert heartbeat is not None
    assert heartbeat.worker_id == worker_id
    assert heartbeat.started_at == started_at
    assert heartbeat.node_id in {"node-a", "node-b"}
    assert heartbeat.last_heartbeat_at in {first_heartbeat_at, second_heartbeat_at}
    assert heartbeat.poll_interval_seconds in {5.0, 6.0}


@pytest.mark.unit
async def test_fallback_record_heartbeat_returns_insert_without_redundant_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        worker_id = "worker_fallback_first_insert_no_get"
        started_at = datetime(2026, 6, 4, 8, 0, tzinfo=UTC)
        last_heartbeat_at = started_at + timedelta(seconds=1)
        original_get = WorkerHeartbeatRepository.get

        async def fail_redundant_get(
            self: WorkerHeartbeatRepository,
            *,
            worker_id: str,
        ) -> WorkerHeartbeat | None:
            raise AssertionError("fallback insert success should not issue a get()")

        monkeypatch.setattr(WorkerHeartbeatRepository, "get", fail_redundant_get)

        async with factory() as session, session.begin():
            heartbeat = await WorkerHeartbeatRepository(
                session,
                dialect_name="unsupported",
            ).record_heartbeat(
                worker_id=worker_id,
                node_id="node-a",
                started_at=started_at,
                last_heartbeat_at=last_heartbeat_at,
                poll_interval_seconds=5.0,
            )

        async with factory() as session:
            persisted = await original_get(
                WorkerHeartbeatRepository(session),
                worker_id=worker_id,
            )

    assert heartbeat.worker_id == worker_id
    assert heartbeat.node_id == "node-a"
    assert heartbeat.started_at == started_at
    assert heartbeat.last_heartbeat_at == last_heartbeat_at
    assert heartbeat.poll_interval_seconds == 5.0
    assert persisted is not None
    assert persisted.worker_id == worker_id


@pytest.mark.unit
async def test_fallback_record_heartbeat_handles_concurrent_first_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        target_worker_id = "worker_fallback_concurrent_first_heartbeat"
        started_at = datetime(2026, 6, 4, 8, 0, tzinfo=UTC)
        first_heartbeat_at = started_at + timedelta(seconds=1)
        second_heartbeat_at = started_at + timedelta(seconds=2)
        original_get = WorkerHeartbeatRepository.get
        missing_gets = 0
        release_missing_gets = asyncio.Event()

        async def synchronized_missing_get(
            self: WorkerHeartbeatRepository,
            *,
            worker_id: str,
        ) -> WorkerHeartbeat | None:
            nonlocal missing_gets
            heartbeat = await original_get(self, worker_id=worker_id)
            if worker_id == target_worker_id and heartbeat is None:
                missing_gets += 1
                if missing_gets >= 2:
                    release_missing_gets.set()
                await asyncio.wait_for(release_missing_gets.wait(), timeout=2.0)
            return heartbeat

        monkeypatch.setattr(WorkerHeartbeatRepository, "get", synchronized_missing_get)

        async def write_heartbeat(
            *,
            node_id: str,
            last_heartbeat_at: datetime,
            poll_interval_seconds: float,
        ) -> None:
            async with factory() as session, session.begin():
                await WorkerHeartbeatRepository(
                    session,
                    dialect_name="unsupported",
                ).record_heartbeat(
                    worker_id=target_worker_id,
                    node_id=node_id,
                    started_at=started_at,
                    last_heartbeat_at=last_heartbeat_at,
                    poll_interval_seconds=poll_interval_seconds,
                )

        await asyncio.gather(
            write_heartbeat(
                node_id="node-a",
                last_heartbeat_at=first_heartbeat_at,
                poll_interval_seconds=5.0,
            ),
            write_heartbeat(
                node_id="node-b",
                last_heartbeat_at=second_heartbeat_at,
                poll_interval_seconds=6.0,
            ),
        )

        async with factory() as session:
            heartbeat = await original_get(
                WorkerHeartbeatRepository(session),
                worker_id=target_worker_id,
            )

    assert heartbeat is not None
    assert heartbeat.worker_id == target_worker_id
    assert heartbeat.started_at == started_at
    assert heartbeat.node_id == "node-b"
    assert heartbeat.last_heartbeat_at == second_heartbeat_at
    assert heartbeat.poll_interval_seconds == 6.0


@pytest.mark.unit
async def test_record_heartbeat_preserves_newest_conflicting_write() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        worker_id = "worker_monotonic_heartbeat"
        started_at = datetime(2026, 6, 4, 8, 0, tzinfo=UTC)
        older_heartbeat_at = started_at + timedelta(seconds=1)
        newer_heartbeat_at = started_at + timedelta(seconds=10)

        async with factory() as session, session.begin():
            await WorkerHeartbeatRepository(session).record_heartbeat(
                worker_id=worker_id,
                node_id="node-new",
                started_at=started_at,
                last_heartbeat_at=newer_heartbeat_at,
                poll_interval_seconds=6.0,
            )

        async with factory() as session:
            first_heartbeat = await WorkerHeartbeatRepository(session).get(worker_id=worker_id)

        assert first_heartbeat is not None
        first_updated_at = first_heartbeat.updated_at

        async with factory() as session, session.begin():
            await WorkerHeartbeatRepository(session).record_heartbeat(
                worker_id=worker_id,
                node_id="node-old",
                started_at=started_at - timedelta(minutes=5),
                last_heartbeat_at=older_heartbeat_at,
                poll_interval_seconds=5.0,
            )

        async with factory() as session:
            heartbeat = await WorkerHeartbeatRepository(session).get(worker_id=worker_id)

    assert heartbeat is not None
    assert heartbeat.worker_id == worker_id
    assert heartbeat.started_at == started_at
    assert heartbeat.node_id == "node-new"
    assert heartbeat.last_heartbeat_at == newer_heartbeat_at
    assert heartbeat.poll_interval_seconds == 6.0
    assert heartbeat.updated_at == first_updated_at


@pytest.mark.unit
async def test_fallback_record_heartbeat_updates_existing_row_without_insert() -> None:
    """The manual-fallback path returns the conditional-UPDATE result directly.

    When a row already exists with an *older* ``last_heartbeat_at``, the initial
    conditional UPDATE matches and refreshes it, so ``record_heartbeat`` returns
    that updated row without ever reaching the INSERT / conflict-recovery branch.
    Pinning this keeps the ``heartbeat is not None`` early-return covered on the
    manual dialect deterministically rather than through the concurrent race.
    """
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        worker_id = "worker_fallback_update_existing_row"
        started_at = datetime(2026, 6, 4, 8, 0, tzinfo=UTC)
        older_heartbeat_at = started_at + timedelta(seconds=1)
        newer_heartbeat_at = started_at + timedelta(seconds=5)
        seeded_at = datetime(2026, 6, 4, 8, 5, tzinfo=UTC)

        async with factory() as session, session.begin():
            session.add(
                WorkerHeartbeat(
                    worker_id=worker_id,
                    node_id="node-old",
                    started_at=started_at,
                    last_heartbeat_at=older_heartbeat_at,
                    poll_interval_seconds=5.0,
                    created_at=seeded_at,
                    updated_at=seeded_at,
                )
            )

        async with factory() as session, session.begin():
            heartbeat = await WorkerHeartbeatRepository(
                session,
                dialect_name="unsupported",
            ).record_heartbeat(
                worker_id=worker_id,
                node_id="node-new",
                started_at=started_at,
                last_heartbeat_at=newer_heartbeat_at,
                poll_interval_seconds=7.0,
            )

    assert heartbeat.worker_id == worker_id
    assert heartbeat.node_id == "node-new"
    assert heartbeat.last_heartbeat_at == newer_heartbeat_at
    assert heartbeat.poll_interval_seconds == 7.0


@pytest.mark.unit
async def test_fallback_record_heartbeat_recovers_from_insert_conflict_with_newer_row() -> None:
    """The manual-fallback insert conflict path must resolve to the persisted row.

    On a dialect without native upsert support, ``record_heartbeat`` first runs a
    conditional UPDATE and, when that matches nothing, INSERTs inside a SAVEPOINT.
    If a row for the worker already exists with a *newer* ``last_heartbeat_at``
    than the incoming (older) write, the conditional UPDATE matches nothing, the
    INSERT hits the primary-key conflict, and the ``except IntegrityError``
    recovery must re-resolve and return the persisted newer row — never raise and
    never downgrade it. This pins that recovery branch deterministically instead
    of relying on the timing-sensitive concurrent-writer race above.
    """
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        worker_id = "worker_fallback_insert_conflict_newer_row"
        started_at = datetime(2026, 6, 4, 8, 0, tzinfo=UTC)
        newer_heartbeat_at = started_at + timedelta(seconds=5)
        older_heartbeat_at = started_at + timedelta(seconds=1)
        seeded_at = datetime(2026, 6, 4, 8, 5, tzinfo=UTC)

        # Seed the newer row the conflicting older write must not overwrite.
        async with factory() as session, session.begin():
            session.add(
                WorkerHeartbeat(
                    worker_id=worker_id,
                    node_id="node-newer",
                    started_at=started_at,
                    last_heartbeat_at=newer_heartbeat_at,
                    poll_interval_seconds=6.0,
                    created_at=seeded_at,
                    updated_at=seeded_at,
                )
            )

        # ``dialect_name="unsupported"`` forces the manual insert path; the older
        # heartbeat's conditional UPDATE matches nothing, so the INSERT conflicts
        # on the primary key and the fallback recovers the persisted newer row.
        async with factory() as session, session.begin():
            heartbeat = await WorkerHeartbeatRepository(
                session,
                dialect_name="unsupported",
            ).record_heartbeat(
                worker_id=worker_id,
                node_id="node-older",
                started_at=started_at,
                last_heartbeat_at=older_heartbeat_at,
                poll_interval_seconds=5.0,
            )

        async with factory() as session:
            persisted = await WorkerHeartbeatRepository(session).get(worker_id=worker_id)

    assert heartbeat.worker_id == worker_id
    assert heartbeat.node_id == "node-newer"
    assert heartbeat.last_heartbeat_at == newer_heartbeat_at
    assert heartbeat.poll_interval_seconds == 6.0
    assert persisted is not None
    assert persisted.node_id == "node-newer"
    assert persisted.last_heartbeat_at == newer_heartbeat_at


@pytest.mark.unit
async def test_prune_stale_deletes_bounded_old_rows_and_preserves_fresh() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        cutoff = datetime(2026, 6, 4, 8, 0, tzinfo=UTC)

        async with factory() as session, session.begin():
            repo = WorkerHeartbeatRepository(session)
            await repo.record_heartbeat(
                worker_id="worker-stale-a",
                node_id="node-a",
                started_at=cutoff - timedelta(days=3),
                last_heartbeat_at=cutoff - timedelta(days=2),
                poll_interval_seconds=5.0,
            )
            await repo.record_heartbeat(
                worker_id="worker-stale-b",
                node_id="node-a",
                started_at=cutoff - timedelta(days=2),
                last_heartbeat_at=cutoff - timedelta(days=1, seconds=1),
                poll_interval_seconds=5.0,
            )
            await repo.record_heartbeat(
                worker_id="worker-fresh",
                node_id="node-a",
                started_at=cutoff - timedelta(minutes=5),
                last_heartbeat_at=cutoff,
                poll_interval_seconds=5.0,
            )

        async with factory() as session, session.begin():
            first_pruned = await WorkerHeartbeatRepository(session).prune_stale(
                before=cutoff,
                limit=1,
            )

        async with factory() as session:
            worker_ids_after_first_prune = {
                row[0]
                for row in (
                    await session.execute(
                        select(WorkerHeartbeat.worker_id).order_by(WorkerHeartbeat.worker_id)
                    )
                ).all()
            }

        async with factory() as session, session.begin():
            second_pruned = await WorkerHeartbeatRepository(session).prune_stale(
                before=cutoff,
                limit=100,
            )

        async with factory() as session:
            remaining_worker_ids = {
                row[0]
                for row in (
                    await session.execute(
                        select(WorkerHeartbeat.worker_id).order_by(WorkerHeartbeat.worker_id)
                    )
                ).all()
            }

    assert first_pruned == 1
    assert worker_ids_after_first_prune == {"worker-fresh", "worker-stale-b"}
    assert second_pruned == 1
    assert remaining_worker_ids == {"worker-fresh"}


@pytest.mark.unit
async def test_prune_stale_with_nonpositive_limit_is_a_noop() -> None:
    """A non-positive prune limit deletes nothing and issues no DELETE.

    ``prune_stale`` guards on ``limit <= 0`` before building the bounded delete,
    so a caller passing ``0`` (or a negative bound) is a no-op that returns 0 and
    leaves stale rows intact — never an unbounded delete.
    """
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        cutoff = datetime(2026, 6, 4, 8, 0, tzinfo=UTC)

        async with factory() as session, session.begin():
            await WorkerHeartbeatRepository(session).record_heartbeat(
                worker_id="worker-stale-noop",
                node_id="node-a",
                started_at=cutoff - timedelta(days=3),
                last_heartbeat_at=cutoff - timedelta(days=2),
                poll_interval_seconds=5.0,
            )

        async with factory() as session, session.begin():
            repo = WorkerHeartbeatRepository(session)
            pruned_zero = await repo.prune_stale(before=cutoff, limit=0)
            pruned_negative = await repo.prune_stale(before=cutoff, limit=-5)

        async with factory() as session:
            survivor = await WorkerHeartbeatRepository(session).get(worker_id="worker-stale-noop")

    assert pruned_zero == 0
    assert pruned_negative == 0
    assert survivor is not None
