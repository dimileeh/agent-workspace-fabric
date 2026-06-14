"""Unit tests for the API→worker GC delegation orchestration (#582).

These cover the heartbeat fast-fail (missing/stale), and the poll loop's
completed / failed / timeout outcomes, including the ``poll_interval == 0`` sleep
branch. The end-to-end happy path through the route is covered in
``tests/unit/api/test_service_gc_worker_delegation.py``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from awf.db.repositories import ServiceGCRequestRepository, WorkerHeartbeatRepository
from awf.db.session import make_session_factory
from awf.service.gc_worker_trigger import (
    _poll_for_outcome,
    delegate_service_gc_to_worker,
)
from tests.postgres import postgres_test_engine

pytestmark = pytest.mark.unit

_NODE_ID = "local"


async def _record_heartbeat(factory, *, last_heartbeat_at: datetime) -> None:
    async with factory() as session:
        await WorkerHeartbeatRepository(session).record_heartbeat(
            worker_id="worker-1",
            node_id=_NODE_ID,
            started_at=last_heartbeat_at,
            last_heartbeat_at=last_heartbeat_at,
            poll_interval_seconds=1.0,
        )
        await session.commit()


async def test_delegate_missing_heartbeat_returns_unavailable() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        outcome = await delegate_service_gc_to_worker(factory, node_id=_NODE_ID, deadline_seconds=5)
        assert outcome.status == "unavailable"
        assert outcome.reason_code == "SERVICE_GC_WORKER_UNAVAILABLE"


async def test_delegate_stale_heartbeat_returns_unavailable() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        await _record_heartbeat(
            factory, last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=1000)
        )
        outcome = await delegate_service_gc_to_worker(factory, node_id=_NODE_ID, deadline_seconds=5)
        assert outcome.status == "unavailable"
        # No pending row was written when the heartbeat is stale.
        async with factory() as session:
            from sqlalchemy import func, select

            from awf.db.models import ServiceGCRequest

            count = (
                await session.execute(select(func.count()).select_from(ServiceGCRequest))
            ).scalar_one()
            assert count == 0


async def test_delegate_fresh_heartbeat_writes_pending_and_folds_completed() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        await _record_heartbeat(factory, last_heartbeat_at=datetime.now(UTC))

        async def _complete_written_row() -> None:
            for _ in range(500):
                async with factory() as session:
                    repo = ServiceGCRequestRepository(session)
                    claimed = await repo.claim_oldest_pending(
                        node_id=_NODE_ID, now=datetime.now(UTC)
                    )
                    if claimed is not None:
                        await repo.mark_completed(
                            request_id=claimed.id,
                            result={"deleted_path_count": 9, "total_estimated_bytes": 17},
                            now=datetime.now(UTC),
                        )
                        await session.commit()
                        return
                    await session.commit()
                await asyncio.sleep(0.01)

        completer = asyncio.create_task(_complete_written_row())
        try:
            outcome = await delegate_service_gc_to_worker(
                factory, node_id=_NODE_ID, deadline_seconds=5, poll_interval_seconds=0.02
            )
        finally:
            await completer
        assert outcome.status == "completed"
        assert outcome.deleted_path_count == 9
        assert outcome.total_estimated_bytes == 17


async def test_delegate_fresh_heartbeat_times_out_when_unconsumed() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        await _record_heartbeat(factory, last_heartbeat_at=datetime.now(UTC))
        outcome = await delegate_service_gc_to_worker(
            factory, node_id=_NODE_ID, deadline_seconds=0.1, poll_interval_seconds=0.02
        )
        assert outcome.status == "timeout"
        # A pending row was written even though it was never consumed.
        async with factory() as session:
            from sqlalchemy import func, select

            from awf.db.models import ServiceGCRequest

            count = (
                await session.execute(select(func.count()).select_from(ServiceGCRequest))
            ).scalar_one()
            assert count == 1


async def _seed_request(factory, *, status: str, result=None, error_message=None) -> str:
    now = datetime.now(UTC)
    async with factory() as session:
        repo = ServiceGCRequestRepository(session)
        request = await repo.create_pending(node_id=_NODE_ID, requested_at=now, deadline_at=None)
        request_id = request.id
        if status == "completed":
            await repo.mark_completed(request_id=request_id, result=result or {}, now=now)
        elif status == "failed":
            request.status = "failed"
            request.result = result
            request.error_message = error_message
        await session.commit()
        return request_id


async def test_poll_returns_completed_from_report() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        request_id = await _seed_request(
            factory,
            status="completed",
            result={"deleted_path_count": 7, "total_estimated_bytes": 5},
        )
        outcome = await _poll_for_outcome(
            factory, request_id=request_id, deadline_seconds=5, poll_interval_seconds=0.01
        )
        assert outcome.status == "completed"
        assert outcome.deleted_path_count == 7


async def test_poll_returns_failed_with_report() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        request_id = await _seed_request(
            factory,
            status="failed",
            result={"status": "failed"},
            error_message="rmtree blew up",
        )
        outcome = await _poll_for_outcome(
            factory, request_id=request_id, deadline_seconds=5, poll_interval_seconds=0.01
        )
        assert outcome.status == "failed"
        assert outcome.reason_code == "SERVICE_GC_WORKER_RECLAIM_FAILED"
        assert outcome.message == "rmtree blew up"
        assert outcome.report == {"status": "failed"}


async def test_poll_returns_failed_without_report_uses_default_message() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        request_id = await _seed_request(factory, status="failed")
        outcome = await _poll_for_outcome(
            factory, request_id=request_id, deadline_seconds=5, poll_interval_seconds=0.01
        )
        assert outcome.status == "failed"
        assert outcome.message == "worker gc reclaim failed"
        assert outcome.report is None


async def test_poll_times_out_when_request_stays_pending() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        request_id = await _seed_request(factory, status="pending")
        outcome = await _poll_for_outcome(
            factory, request_id=request_id, deadline_seconds=0.1, poll_interval_seconds=0.02
        )
        assert outcome.status == "timeout"
        assert outcome.reason_code == "SERVICE_GC_WORKER_DELEGATION_TIMEOUT"


async def test_poll_times_out_when_request_row_is_missing() -> None:
    # A non-existent request id -> ``_get_request`` returns None -> the loop skips
    # the status checks and times out (covers the ``request is None`` branch).
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        outcome = await _poll_for_outcome(
            factory, request_id="sgc_missing", deadline_seconds=0.05, poll_interval_seconds=0.02
        )
        assert outcome.status == "timeout"


async def test_poll_with_zero_interval_sleeps_until_complete() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        request_id = await _seed_request(factory, status="pending")

        async def _complete_soon() -> None:
            await asyncio.sleep(0.05)
            async with factory() as session:
                await ServiceGCRequestRepository(session).mark_completed(
                    request_id=request_id,
                    result={"deleted_path_count": 1},
                    now=datetime.now(UTC),
                )
                await session.commit()

        completer = asyncio.create_task(_complete_soon())
        try:
            # poll_interval_seconds=0 exercises the ``else remaining`` sleep branch.
            outcome = await _poll_for_outcome(
                factory, request_id=request_id, deadline_seconds=5, poll_interval_seconds=0
            )
        finally:
            await completer
        assert outcome.status == "completed"
        assert outcome.deleted_path_count == 1
