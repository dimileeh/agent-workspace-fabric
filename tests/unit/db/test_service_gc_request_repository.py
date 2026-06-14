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


async def test_claim_excludes_pending_rows_past_deadline() -> None:
    # Expire-on-timeout (#590): a ``pending`` row whose ``deadline_at`` has elapsed must
    # never be claimed/run — the operator has already been told the trigger timed out, so
    # running that destructive reap later behind their back is the hazard we reject. A
    # fresh pending row (deadline in the future) and a NULL-deadline row stay claimable.
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        now = datetime.now(UTC)
        async with factory() as session:
            repo = ServiceGCRequestRepository(session)
            # Past-deadline row is the oldest, so ordering would pick it first if eligible.
            await repo.create_pending(
                node_id="node-a",
                requested_at=now - timedelta(seconds=120),
                deadline_at=now - timedelta(seconds=30),
            )
            fresh = await repo.create_pending(
                node_id="node-a",
                requested_at=now,
                deadline_at=now + timedelta(seconds=30),
            )
            fresh_id = fresh.id
            await session.commit()

        async with factory() as session:
            repo = ServiceGCRequestRepository(session)
            claimed = await repo.claim_oldest_pending(node_id="node-a", now=now)
            # The past-deadline row is skipped; only the fresh row is claimable.
            assert claimed is not None
            assert claimed.id == fresh_id


async def test_expire_stale_requests_expires_only_past_deadline_rows() -> None:
    # Expire-on-timeout (#590): a ``pending`` or ``running`` row whose ``deadline_at`` has
    # elapsed is retired to the terminal ``expired`` state (recording ``finished_at``) so
    # the reap never runs behind the operator's back and the row does not accumulate. A
    # still-fresh ``running`` row and a row without a ``deadline_at`` are left untouched.
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        now = datetime.now(UTC)
        async with factory() as session:
            repo = ServiceGCRequestRepository(session)
            stale_pending = await repo.create_pending(
                node_id="node-a",
                requested_at=now - timedelta(seconds=180),
                deadline_at=now - timedelta(seconds=60),
            )
            stale_running = await repo.create_pending(
                node_id="node-a",
                requested_at=now - timedelta(seconds=120),
                deadline_at=now - timedelta(seconds=30),
            )
            fresh = await repo.create_pending(
                node_id="node-a",
                requested_at=now,
                deadline_at=now + timedelta(seconds=30),
            )
            no_deadline = await repo.create_pending(
                node_id="node-a",
                requested_at=now - timedelta(seconds=120),
                deadline_at=None,
            )
            # ``stale_pending`` stays pending (never claimed); the rest move to ``running``
            # to mimic claimed-but-unfinished rows.
            for request in (stale_running, fresh, no_deadline):
                request.status = "running"
                request.claimed_at = now
            await session.flush()
            stale_pending_id, stale_running_id = stale_pending.id, stale_running.id
            fresh_id, no_deadline_id = fresh.id, no_deadline.id
            await session.commit()

        async with factory() as session:
            repo = ServiceGCRequestRepository(session)
            expired = await repo.expire_stale_requests(node_id="node-a", now=now)
            # Oldest first: the pending row precedes the running row by ``requested_at``.
            assert expired == [stale_pending_id, stale_running_id]
            await session.commit()

        async with factory() as session:
            repo = ServiceGCRequestRepository(session)
            for expired_id in (stale_pending_id, stale_running_id):
                retired = await repo.get(expired_id)
                assert retired is not None
                assert retired.status == "expired"
                assert retired.finished_at is not None
            assert (await repo.get(fresh_id)).status == "running"  # type: ignore[union-attr]
            assert (await repo.get(no_deadline_id)).status == "running"  # type: ignore[union-attr]


async def test_expire_stale_requests_excludes_other_node() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        now = datetime.now(UTC)
        async with factory() as session:
            repo = ServiceGCRequestRepository(session)
            other = await repo.create_pending(
                node_id="other-node",
                requested_at=now - timedelta(seconds=120),
                deadline_at=now - timedelta(seconds=30),
            )
            null_node = await repo.create_pending(
                node_id=None,
                requested_at=now - timedelta(seconds=120),
                deadline_at=now - timedelta(seconds=30),
            )
            for request in (other, null_node):
                request.status = "running"
            await session.flush()
            other_id, null_id = other.id, null_node.id
            await session.commit()

        async with factory() as session:
            repo = ServiceGCRequestRepository(session)
            # The other-node row is invisible; the NULL-node row is expirable.
            expired = await repo.expire_stale_requests(node_id="node-a", now=now)
            assert expired == [null_id]
            await session.commit()

        async with factory() as session:
            repo = ServiceGCRequestRepository(session)
            assert (await repo.get(other_id)).status == "running"  # type: ignore[union-attr]
            assert (await repo.get(null_id)).status == "expired"  # type: ignore[union-attr]


async def test_expire_stale_requests_returns_empty_when_none_stale() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        now = datetime.now(UTC)
        async with factory() as session:
            repo = ServiceGCRequestRepository(session)
            await repo.create_pending(node_id="node-a", requested_at=now, deadline_at=None)
            await session.commit()

        async with factory() as session:
            repo = ServiceGCRequestRepository(session)
            assert await repo.expire_stale_requests(node_id="node-a", now=now) == []


async def test_expire_stale_requests_on_non_postgres_dialect() -> None:
    # On a non-Postgres dialect the expire sweep runs without ``FOR UPDATE SKIP LOCKED``;
    # the plain UPDATE still executes against the test Postgres engine.
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        now = datetime.now(UTC)
        async with factory() as session:
            repo = ServiceGCRequestRepository(session)
            stale = await repo.create_pending(
                node_id="node-a",
                requested_at=now - timedelta(seconds=120),
                deadline_at=now - timedelta(seconds=30),
            )
            stale.status = "running"
            await session.flush()
            stale_id = stale.id
            await session.commit()

        async with factory() as session:
            repo = ServiceGCRequestRepository(session, dialect_name="sqlite")
            expired = await repo.expire_stale_requests(node_id="node-a", now=now)
            assert expired == [stale_id]


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
