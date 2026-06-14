"""ControlWorker on-demand ``service_gc_requests`` consumer (#582).

These cover the worker side of the gc-delegation channel: claiming a pending
trigger row, running the *already-wired* terminal-workspace GC reaper exactly once
(and **not** the claude-base reaper separately, which would double-reap), writing
the combined report into ``result``, and the ``failed`` + reason-code path. The
reaper engine itself is covered by ``test_worker_terminal_gc_reaper.py``; here we
prove the trigger consumer drives it and persists the outcome.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.control.worker import ControlWorker, WorkerConfig
from awf.db.repositories.service_gc_request_repo import ServiceGCRequestRepository
from awf.db.session import make_session_factory

pytestmark = pytest.mark.unit

_NODE_ID = "local-service"


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


def _make_worker(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    terminal_gc_reaper: object | None,
    claude_base_reaper: object | None = None,
) -> ControlWorker:
    return ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        terminal_gc_reaper=terminal_gc_reaper,  # type: ignore[arg-type]
        claude_base_reaper=claude_base_reaper,  # type: ignore[arg-type]
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_provisions=0,
            node_id=_NODE_ID,
            # The interval kill-switch is OFF; the on-demand consumer must still run.
            terminal_workspace_gc_enabled=False,
        ),
    )


async def _seed_pending(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    node_id: str | None = _NODE_ID,
) -> str:
    now = datetime.now(UTC)
    async with session_factory() as session:
        request = await ServiceGCRequestRepository(session).create_pending(
            node_id=node_id,
            requested_at=now,
            deadline_at=now + timedelta(seconds=30),
            params={"execute": True},
        )
        await session.commit()
        return request.id


async def test_consume_runs_terminal_reaper_once_and_completes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    request_id = await _seed_pending(session_factory)
    terminal_calls = 0
    claude_calls = 0
    report = {
        "status": "succeeded",
        "reason_code": "CLEANUP_EXECUTION_SUCCEEDED",
        "deleted_path_count": 4,
        "total_estimated_bytes": 1_700_000_000,
    }

    async def _terminal_reaper() -> dict[str, object]:
        nonlocal terminal_calls
        terminal_calls += 1
        return report

    async def _claude_reaper() -> dict[str, object]:
        nonlocal claude_calls
        claude_calls += 1
        raise AssertionError("claude-base reaper must not be called separately")

    worker = _make_worker(
        session_factory,
        terminal_gc_reaper=_terminal_reaper,
        claude_base_reaper=_claude_reaper,
    )

    await worker._maybe_consume_service_gc_trigger()  # noqa: SLF001

    assert terminal_calls == 1
    assert claude_calls == 0
    async with session_factory() as session:
        finished = await ServiceGCRequestRepository(session).get(request_id)
        assert finished is not None
        assert finished.status == "completed"
        assert finished.result == report
        assert finished.error_code is None


async def test_consume_is_noop_without_reaper(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    request_id = await _seed_pending(session_factory)
    worker = _make_worker(session_factory, terminal_gc_reaper=None)

    await worker._maybe_consume_service_gc_trigger()  # noqa: SLF001

    async with session_factory() as session:
        unchanged = await ServiceGCRequestRepository(session).get(request_id)
        assert unchanged is not None
        assert unchanged.status == "pending"


async def test_consume_is_noop_with_no_pending_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    calls = 0

    async def _terminal_reaper() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {}

    worker = _make_worker(session_factory, terminal_gc_reaper=_terminal_reaper)

    await worker._maybe_consume_service_gc_trigger()  # noqa: SLF001

    assert calls == 0


async def test_consume_marks_failed_on_reaper_error(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    request_id = await _seed_pending(session_factory)

    async def _terminal_reaper() -> dict[str, object]:
        raise RuntimeError("rmtree blew up")

    worker = _make_worker(session_factory, terminal_gc_reaper=_terminal_reaper)

    await worker._maybe_consume_service_gc_trigger()  # noqa: SLF001

    async with session_factory() as session:
        failed = await ServiceGCRequestRepository(session).get(request_id)
        assert failed is not None
        assert failed.status == "failed"
        assert failed.error_code == "SERVICE_GC_WORKER_RECLAIM_FAILED"
        assert "rmtree blew up" in (failed.error_message or "")


async def test_consume_swallows_claim_failure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def _terminal_reaper() -> dict[str, object]:
        raise AssertionError("reaper must not run when the claim fails")

    worker = _make_worker(session_factory, terminal_gc_reaper=_terminal_reaper)

    async def _boom() -> str | None:
        raise RuntimeError("db down")

    worker._claim_service_gc_trigger = _boom  # type: ignore[method-assign]  # noqa: SLF001

    # A claim failure is swallowed-and-logged, never propagated (must not break dispatch).
    await worker._maybe_consume_service_gc_trigger()  # noqa: SLF001


async def test_consume_propagates_cancellation_during_claim(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _make_worker(session_factory, terminal_gc_reaper=lambda: None)

    async def _cancel() -> str | None:
        raise asyncio.CancelledError

    worker._claim_service_gc_trigger = _cancel  # type: ignore[method-assign]  # noqa: SLF001

    with pytest.raises(asyncio.CancelledError):
        await worker._maybe_consume_service_gc_trigger()  # noqa: SLF001


async def test_consume_propagates_cancellation_during_reap(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    request_id = await _seed_pending(session_factory)

    async def _terminal_reaper() -> dict[str, object]:
        raise asyncio.CancelledError

    worker = _make_worker(session_factory, terminal_gc_reaper=_terminal_reaper)

    with pytest.raises(asyncio.CancelledError):
        await worker._maybe_consume_service_gc_trigger()  # noqa: SLF001

    # The row was claimed (running) but left for retry — not marked failed.
    async with session_factory() as session:
        row = await ServiceGCRequestRepository(session).get(request_id)
        assert row is not None
        assert row.status == "running"


async def test_consume_claims_one_row_second_call_noops(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_pending(session_factory)
    calls = 0

    async def _terminal_reaper() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"status": "succeeded", "deleted_path_count": 1}

    worker = _make_worker(session_factory, terminal_gc_reaper=_terminal_reaper)

    await worker._maybe_consume_service_gc_trigger()  # noqa: SLF001
    await worker._maybe_consume_service_gc_trigger()  # noqa: SLF001

    # The single pending row is consumed once; the second cycle finds nothing.
    assert calls == 1
