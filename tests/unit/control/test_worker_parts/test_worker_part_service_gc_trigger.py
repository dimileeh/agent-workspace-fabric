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
    params: dict[str, object] | None = None,
) -> str:
    now = datetime.now(UTC)
    async with session_factory() as session:
        request = await ServiceGCRequestRepository(session).create_pending(
            node_id=node_id,
            requested_at=now,
            deadline_at=now + timedelta(seconds=30),
            params=params if params is not None else {"execute": True},
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


async def test_consume_threads_operator_params_into_reaper(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The operator's stored ``min_age_hours``/``limit`` flow into the reaper (#590).

    The API resolves the run's scope (CLI ``--min-age-hours``/``--limit``) and
    persists it in ``service_gc_requests.params``; the worker reap of the
    capability-gated auth overlays + claude-base must honour that same scope rather
    than silently falling back to its server defaults.
    """
    await _seed_pending(
        session_factory,
        params={"execute": True, "min_age_hours": 1.5, "limit": 7},
    )
    captured: dict[str, object] = {}

    async def _terminal_reaper(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"status": "succeeded", "deleted_path_count": 1}

    worker = _make_worker(session_factory, terminal_gc_reaper=_terminal_reaper)

    await worker._maybe_consume_service_gc_trigger()  # noqa: SLF001

    assert captured == {"min_age_hours": 1.5, "limit": 7}


async def test_consume_omits_absent_params_so_reaper_uses_defaults(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Params lacking ``min_age_hours``/``limit`` leave the reaper on its defaults (#590)."""
    await _seed_pending(session_factory, params={"execute": True})
    captured: dict[str, object] = {}

    async def _terminal_reaper(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"status": "succeeded", "deleted_path_count": 1}

    worker = _make_worker(session_factory, terminal_gc_reaper=_terminal_reaper)

    await worker._maybe_consume_service_gc_trigger()  # noqa: SLF001

    assert captured == {}


async def test_consume_threads_status_filters_into_reaper(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Operator ``--status``/``--exclude-status`` filters flow into the reaper (#590).

    The API persists the resolved safety-scoping status filters in
    ``service_gc_requests.params``; the worker's capability-gated reap must honour the
    same scope so it never reclaims auth dirs the operator's flags excluded.
    """
    await _seed_pending(
        session_factory,
        params={
            "execute": True,
            "statuses": ["completed"],
            "exclude_statuses": ["cancelled"],
        },
    )
    captured: dict[str, object] = {}

    async def _terminal_reaper(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"status": "succeeded", "deleted_path_count": 1}

    worker = _make_worker(session_factory, terminal_gc_reaper=_terminal_reaper)

    await worker._maybe_consume_service_gc_trigger()  # noqa: SLF001

    assert captured == {"statuses": ["completed"], "exclude_statuses": ["cancelled"]}


async def test_consume_omits_empty_status_filters(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Empty ``statuses``/``exclude_statuses`` lists are not forwarded (#590).

    An empty filter list means "no scoping", so it must leave the reaper on its default
    two-pass sweep rather than being passed through as an empty include set.
    """
    await _seed_pending(
        session_factory,
        params={"execute": True, "statuses": [], "exclude_statuses": []},
    )
    captured: dict[str, object] = {}

    async def _terminal_reaper(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"status": "succeeded", "deleted_path_count": 1}

    worker = _make_worker(session_factory, terminal_gc_reaper=_terminal_reaper)

    await worker._maybe_consume_service_gc_trigger()  # noqa: SLF001

    assert captured == {}


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


async def test_consume_swallows_finish_write_failure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A result-write failure after a successful reap is swallowed, not propagated (#590).

    ``_run_claimed_service_gc_trigger`` is reached *after* a row is claimed; if the
    terminal-outcome write (``_finish_service_gc_trigger``) exhausts its retries and
    raises, the swallow-and-log contract the docstring guarantees requires the
    exception not escape ``_maybe_consume_service_gc_trigger`` — otherwise it aborts
    the whole poll cycle and skips provisioning/dispatch for that round.
    """
    await _seed_pending(session_factory)

    async def _terminal_reaper() -> dict[str, object]:
        return {"status": "succeeded", "deleted_path_count": 1}

    worker = _make_worker(session_factory, terminal_gc_reaper=_terminal_reaper)

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("result-write retries exhausted")

    worker._finish_service_gc_trigger = _boom  # type: ignore[method-assign]  # noqa: SLF001

    # Must not raise — one failed consume cannot break provisioning/dispatch.
    await worker._maybe_consume_service_gc_trigger()  # noqa: SLF001


async def test_consume_propagates_cancellation_during_finish(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``CancelledError`` from the finish path still propagates for cooperative shutdown."""
    await _seed_pending(session_factory)

    async def _terminal_reaper() -> dict[str, object]:
        return {"status": "succeeded", "deleted_path_count": 1}

    worker = _make_worker(session_factory, terminal_gc_reaper=_terminal_reaper)

    async def _cancel(*_args: object, **_kwargs: object) -> None:
        raise asyncio.CancelledError

    worker._finish_service_gc_trigger = _cancel  # type: ignore[method-assign]  # noqa: SLF001

    with pytest.raises(asyncio.CancelledError):
        await worker._maybe_consume_service_gc_trigger()  # noqa: SLF001


async def _seed_running_stale(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    node_id: str | None = _NODE_ID,
) -> str:
    """Seed a ``running`` row whose ``deadline_at`` has already elapsed (abandoned)."""
    now = datetime.now(UTC)
    async with session_factory() as session:
        request = await ServiceGCRequestRepository(session).create_pending(
            node_id=node_id,
            requested_at=now - timedelta(seconds=120),
            deadline_at=now - timedelta(seconds=30),
            params={"execute": True},
        )
        request.status = "running"
        request.claimed_at = now - timedelta(seconds=90)
        await session.flush()
        await session.commit()
        return request.id


async def test_consume_expires_stale_running_row_without_rerunning_reaper(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A ``running`` row past its ``deadline_at`` is expired, never re-run (#590, expire-on-timeout).

    A worker cancelled mid-reap leaves its claimed row ``running``; once its
    ``deadline_at`` has elapsed the operator has already been told the trigger timed out,
    so re-running that destructive reap behind their back is the hazard we reject. The
    consume path retires the row to the terminal ``expired`` state instead of re-queuing
    it; with no fresh pending row the reaper does not run at all. The periodic interval
    reaper remains the durable backstop for the disk reclaim.
    """
    request_id = await _seed_running_stale(session_factory)
    calls = 0

    async def _terminal_reaper() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"status": "succeeded", "deleted_path_count": 2}

    worker = _make_worker(session_factory, terminal_gc_reaper=_terminal_reaper)

    await worker._maybe_consume_service_gc_trigger()  # noqa: SLF001

    assert calls == 0
    async with session_factory() as session:
        finished = await ServiceGCRequestRepository(session).get(request_id)
        assert finished is not None
        assert finished.status == "expired"
        assert finished.finished_at is not None


async def test_consume_expires_pending_row_past_deadline_without_running_reaper(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A never-claimed ``pending`` row past its ``deadline_at`` is expired, never run (#590).

    If no worker claimed the request before the API's polling budget elapsed, the operator
    was already told it timed out. The consume path must retire the stale pending row to
    ``expired`` and must never claim/run it with the current clock + stored filters.
    """
    now = datetime.now(UTC)
    async with session_factory() as session:
        request = await ServiceGCRequestRepository(session).create_pending(
            node_id=_NODE_ID,
            requested_at=now - timedelta(seconds=120),
            deadline_at=now - timedelta(seconds=30),
            params={"execute": True},
        )
        request_id = request.id
        await session.commit()

    async def _terminal_reaper() -> dict[str, object]:
        raise AssertionError("reaper must not run for a timed-out pending row")

    worker = _make_worker(session_factory, terminal_gc_reaper=_terminal_reaper)

    await worker._maybe_consume_service_gc_trigger()  # noqa: SLF001

    async with session_factory() as session:
        expired = await ServiceGCRequestRepository(session).get(request_id)
        assert expired is not None
        assert expired.status == "expired"
        assert expired.finished_at is not None


async def test_consume_leaves_fresh_running_row_untouched(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A ``running`` row whose ``deadline_at`` has not elapsed is left in flight (#590)."""
    now = datetime.now(UTC)
    async with session_factory() as session:
        request = await ServiceGCRequestRepository(session).create_pending(
            node_id=_NODE_ID,
            requested_at=now,
            deadline_at=now + timedelta(seconds=30),
            params={"execute": True},
        )
        request.status = "running"
        await session.flush()
        request_id = request.id
        await session.commit()

    async def _terminal_reaper() -> dict[str, object]:
        raise AssertionError("reaper must not run for a fresh in-flight row")

    worker = _make_worker(session_factory, terminal_gc_reaper=_terminal_reaper)

    await worker._maybe_consume_service_gc_trigger()  # noqa: SLF001

    async with session_factory() as session:
        unchanged = await ServiceGCRequestRepository(session).get(request_id)
        assert unchanged is not None
        assert unchanged.status == "running"


async def test_consume_swallows_stale_expire_failure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A stale-row expire failure is swallowed and must not skip the claim below (#590)."""
    request_id = await _seed_pending(session_factory)

    async def _terminal_reaper() -> dict[str, object]:
        return {"status": "succeeded", "deleted_path_count": 1}

    worker = _make_worker(session_factory, terminal_gc_reaper=_terminal_reaper)

    async def _boom() -> None:
        raise RuntimeError("expire retries exhausted")

    worker._expire_stale_service_gc_triggers = _boom  # type: ignore[method-assign]  # noqa: SLF001

    # Must not raise, and the fresh pending row is still consumed despite the expire failure.
    await worker._maybe_consume_service_gc_trigger()  # noqa: SLF001

    async with session_factory() as session:
        finished = await ServiceGCRequestRepository(session).get(request_id)
        assert finished is not None
        assert finished.status == "completed"


async def test_consume_propagates_cancellation_during_stale_expire(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """``CancelledError`` from the expire sweep propagates for cooperative shutdown (#590)."""
    worker = _make_worker(session_factory, terminal_gc_reaper=lambda: None)

    async def _cancel() -> None:
        raise asyncio.CancelledError

    worker._expire_stale_service_gc_triggers = _cancel  # type: ignore[method-assign]  # noqa: SLF001

    with pytest.raises(asyncio.CancelledError):
        await worker._maybe_consume_service_gc_trigger()  # noqa: SLF001


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
