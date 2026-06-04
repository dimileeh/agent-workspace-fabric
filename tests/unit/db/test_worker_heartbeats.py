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
    assert "node_id = excluded.node_id" in set_clause
    assert "last_heartbeat_at = excluded.last_heartbeat_at" in set_clause
    assert "poll_interval_seconds = excluded.poll_interval_seconds" in set_clause
    assert "updated_at = excluded.updated_at" in set_clause
    assert "started_at =" not in set_clause

    assert _worker_heartbeat_upsert_stmt(None) is None


@pytest.mark.unit
async def test_record_heartbeat_handles_concurrent_first_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        worker_id = "worker_concurrent_first_heartbeat"
        started_at = datetime(2026, 6, 4, 8, 0, tzinfo=UTC)
        first_heartbeat_at = started_at + timedelta(seconds=1)
        second_heartbeat_at = started_at + timedelta(seconds=2)
        arrivals = 0
        arrivals_lock = asyncio.Lock()
        both_read_missing = asyncio.Event()
        original_get = WorkerHeartbeatRepository.get

        async def synchronized_get(
            self: WorkerHeartbeatRepository,
            *,
            worker_id: str,
        ) -> WorkerHeartbeat | None:
            nonlocal arrivals
            heartbeat = await original_get(self, worker_id=worker_id)
            async with arrivals_lock:
                arrivals += 1
                if arrivals == 2:
                    both_read_missing.set()
            await asyncio.wait_for(both_read_missing.wait(), timeout=2)
            return heartbeat

        monkeypatch.setattr(WorkerHeartbeatRepository, "get", synchronized_get)

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
            heartbeat = await original_get(
                WorkerHeartbeatRepository(session),
                worker_id=worker_id,
            )

    assert heartbeat is not None
    assert heartbeat.worker_id == worker_id
    assert heartbeat.started_at == started_at
    assert heartbeat.node_id in {"node-a", "node-b"}
    assert heartbeat.last_heartbeat_at in {first_heartbeat_at, second_heartbeat_at}
    assert heartbeat.poll_interval_seconds in {5.0, 6.0}


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
