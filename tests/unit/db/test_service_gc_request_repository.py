"""``ServiceGCRequestRepository`` behavior tests (#582).

Covers the create → claim → complete/fail round-trip plus the
``SELECT ... FOR UPDATE SKIP LOCKED`` claim semantics that keep a concurrent
worker (or the interval reaper racing the same row) from double-claiming.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from awf.db.repositories.service_gc_request_repo import ServiceGCRequestRepository
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine

pytestmark = pytest.mark.unit


async def test_create_claim_complete_round_trip() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        now = datetime.now(UTC)
        async with factory() as session:
            repo = ServiceGCRequestRepository(session)
            created = await repo.create_pending(
                node_id="node-a",
                requested_at=now,
                deadline_at=now + timedelta(seconds=30),
                params={"execute": True},
            )
            request_id = created.id
            await session.commit()

        async with factory() as session:
            repo = ServiceGCRequestRepository(session)
            claimed = await repo.claim_oldest_pending(node_id="node-a", now=now)
            assert claimed is not None
            assert claimed.id == request_id
            assert claimed.status == "running"
            assert claimed.claimed_at is not None
            await session.commit()

        async with factory() as session:
            repo = ServiceGCRequestRepository(session)
            completed = await repo.mark_completed(
                request_id=request_id,
                result={"deleted_path_count": 4, "total_estimated_bytes": 99},
                now=now + timedelta(seconds=2),
            )
            assert completed is not None
            assert completed.status == "completed"
            assert completed.result == {"deleted_path_count": 4, "total_estimated_bytes": 99}
            assert completed.finished_at is not None
            await session.commit()

        async with factory() as session:
            repo = ServiceGCRequestRepository(session)
            fetched = await repo.get(request_id)
            assert fetched is not None
            assert fetched.status == "completed"


async def test_mark_failed_records_reason_code() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        now = datetime.now(UTC)
        async with factory() as session:
            repo = ServiceGCRequestRepository(session)
            created = await repo.create_pending(
                node_id="node-a",
                requested_at=now,
                deadline_at=None,
            )
            request_id = created.id
            await session.commit()

        async with factory() as session:
            repo = ServiceGCRequestRepository(session)
            failed = await repo.mark_failed(
                request_id=request_id,
                error_code="SERVICE_GC_WORKER_RECLAIM_FAILED",
                error_message="boom",
                now=now,
            )
            assert failed is not None
            assert failed.status == "failed"
            assert failed.error_code == "SERVICE_GC_WORKER_RECLAIM_FAILED"
            assert failed.error_message == "boom"


async def test_claim_oldest_pending_orders_by_requested_at() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        base = datetime.now(UTC)
        async with factory() as session:
            repo = ServiceGCRequestRepository(session)
            older = await repo.create_pending(node_id="node-a", requested_at=base, deadline_at=None)
            await repo.create_pending(
                node_id="node-a", requested_at=base + timedelta(seconds=5), deadline_at=None
            )
            older_id = older.id
            await session.commit()

        async with factory() as session:
            repo = ServiceGCRequestRepository(session)
            claimed = await repo.claim_oldest_pending(node_id="node-a", now=base)
            assert claimed is not None
            assert claimed.id == older_id


async def test_claim_skips_row_locked_by_concurrent_worker() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        now = datetime.now(UTC)
        async with factory() as session:
            repo = ServiceGCRequestRepository(session)
            await repo.create_pending(node_id="node-a", requested_at=now, deadline_at=None)
            await session.commit()

        # First worker holds the row lock in an open transaction.
        session_a = factory()
        repo_a = ServiceGCRequestRepository(session_a)
        claimed_a = await repo_a.claim_oldest_pending(node_id="node-a", now=now)
        assert claimed_a is not None

        # A second worker must SKIP LOCKED past the in-flight claim, not block.
        async with factory() as session_b:
            repo_b = ServiceGCRequestRepository(session_b)
            claimed_b = await repo_b.claim_oldest_pending(node_id="node-a", now=now)
            assert claimed_b is None

        await session_a.rollback()
        await session_a.close()


async def test_claim_includes_null_node_rows_excludes_other_node() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        now = datetime.now(UTC)
        async with factory() as session:
            repo = ServiceGCRequestRepository(session)
            await repo.create_pending(node_id="other-node", requested_at=now, deadline_at=None)
            null_row = await repo.create_pending(
                node_id=None, requested_at=now + timedelta(seconds=1), deadline_at=None
            )
            null_id = null_row.id
            await session.commit()

        async with factory() as session:
            repo = ServiceGCRequestRepository(session)
            claimed = await repo.claim_oldest_pending(node_id="node-a", now=now)
            assert claimed is not None
            # The other-node row is invisible; the NULL-node row is claimable.
            assert claimed.id == null_id
            assert claimed.node_id == "node-a"


async def test_claim_without_for_update_on_non_postgres_dialect() -> None:
    # On a non-Postgres dialect the claim runs without ``FOR UPDATE SKIP LOCKED``
    # (sqlite has no such clause). Drive the non-postgres branch by overriding the
    # repo dialect; the plain SELECT still executes on the test Postgres engine.
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        now = datetime.now(UTC)
        async with factory() as session:
            await ServiceGCRequestRepository(session).create_pending(
                node_id="node-a", requested_at=now, deadline_at=None
            )
            await session.commit()

        async with factory() as session:
            repo = ServiceGCRequestRepository(session, dialect_name="sqlite")
            claimed = await repo.claim_oldest_pending(node_id="node-a", now=now)
            assert claimed is not None
            assert claimed.status == "running"


async def test_mutators_return_none_for_missing_request() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        now = datetime.now(UTC)
        async with factory() as session:
            repo = ServiceGCRequestRepository(session)
            assert await repo.get("sgc_missing") is None
            assert await repo.mark_completed(request_id="sgc_missing", result={}, now=now) is None
            assert (
                await repo.mark_failed(
                    request_id="sgc_missing",
                    error_code="X",
                    error_message="y",
                    now=now,
                )
                is None
            )
            assert await repo.claim_oldest_pending(node_id="node-a", now=now) is None
