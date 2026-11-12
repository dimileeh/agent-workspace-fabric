"""Failure handling of the per-invocation work-slot scope (slot-release).

A lease write that fails must never pretend a slot was freed or taken, and must
never abort the agent/validation run it gates: the scope logs, keeps what the DB
says it holds, and leaves the rest to the reconciler.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import structlog.testing
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.compose_exec import ComposeExecCleanupError
from awf.control.worker import WorkerConfig, work_slot_scope
from awf.control.worker.constants import WORK_SLOT_LEASE_FENCED
from awf.control.worker.resource_broker import (
    WorkSlotAcquireOutcome,
    WorkSlotAcquireResult,
    WorkSlotLeaseRef,
    adopt_work,
    release_work,
)
from awf.control.worker.work_slot_scope import (
    InvocationWorkSlot,
    WorkSlotWakeups,
    _cleanup_unproven_in_chain,
    _run_shielded,
)
from awf.db.enums import WorkSlotLeasePhase, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine
from tests.unit.control._work_slot_support import create_slot_workspace, lease_rows

pytestmark = pytest.mark.unit

_WORKER = "control-worker-scope-failures"
_TIMEOUT = 10.0


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def _db_down(*_args: object, **_kwargs: object) -> object:
    raise OperationalError("UPDATE", {}, Exception("db down"))


def _scope(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    lease_seconds: int = 300,
    wakeups: WorkSlotWakeups | None = None,
) -> InvocationWorkSlot:
    return InvocationWorkSlot(
        session_factory=factory,
        config=WorkerConfig(
            max_concurrent_executions=1, execution_claim_lease_seconds=lease_seconds
        ),
        worker_id=_WORKER,
        workspace_id=workspace_id,
        wakeups=wakeups or WorkSlotWakeups(),
    )


def _events(logs: list[dict[str, object]]) -> set[object]:
    return {entry["event"] for entry in logs}


async def _held_rows(factory: async_sessionmaker[AsyncSession], workspace_id: str) -> list[object]:
    return [
        row
        for row in await lease_rows(factory, workspace_id=workspace_id)
        if row.phase == WorkSlotLeasePhase.held.value
    ]


async def _hold_once(scope: InvocationWorkSlot) -> None:
    async with scope, scope.hold("agent"):
        pass


def _cleanup_error() -> ComposeExecCleanupError:
    return ComposeExecCleanupError(
        invocation_id="awf_x", source="agent", label="codex", message="still alive"
    )


async def test_hold_check_failure_keeps_running_on_the_retained_lease(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    scope = _scope(session_factory, workspace_id)
    async with scope:
        async with scope.hold("agent"):
            pass
        lease = scope.lease
        with monkeypatch.context() as patch:
            patch.setattr(work_slot_scope, "renew_work", _db_down)
            patch.setattr(work_slot_scope, "acquire_work", _db_down)
            with structlog.testing.capture_logs() as logs:
                async with scope.hold("validation"):
                    assert scope.lease == lease

    assert "worker.work_slot_hold_check_failed" in _events(logs)


async def test_waiting_event_for_a_vanished_workspace_is_skipped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    scope = _scope(session_factory, "ws_vanished")
    result = WorkSlotAcquireResult(
        outcome=WorkSlotAcquireOutcome.waiting, lease=None, in_use=1, limit=1
    )

    await scope._record_waiting("agent", result)  # noqa: SLF001

    async with session_factory() as session:
        assert await WorkspaceRepository(session).get("ws_vanished") is None


async def test_waiting_event_write_failure_does_not_break_the_wait(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    waiter_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    wakeups = WorkSlotWakeups()
    holder = _scope(session_factory, holder_ws, wakeups=wakeups)
    waiter = _scope(session_factory, waiter_ws, wakeups=wakeups)
    monkeypatch.setattr(WorkspaceRepository, "add_event", _db_down)

    with structlog.testing.capture_logs() as logs:
        async with holder:
            async with holder.hold("agent"):
                pass
            task = asyncio.create_task(_hold_once(waiter))
            while "worker.work_slot_waiting_event_failed" not in _events(logs):
                await asyncio.sleep(0.02)
            await holder.release_idle()
            await asyncio.wait_for(task, timeout=_TIMEOUT)


async def test_ticket_withdraw_failure_is_logged_and_left_to_expire(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    waiter_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    holder = _scope(session_factory, holder_ws)
    waiter = _scope(session_factory, waiter_ws)
    async with holder:
        async with holder.hold("agent"):
            pass
        task = asyncio.create_task(_hold_once(waiter))
        while not await lease_rows(session_factory, workspace_id=waiter_ws):
            await asyncio.sleep(0.02)
        with monkeypatch.context() as patch:
            patch.setattr(work_slot_scope, "release_work", _db_down)
            with structlog.testing.capture_logs() as logs:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    assert "worker.work_slot_ticket_withdraw_failed" in _events(logs)
    rows = await lease_rows(session_factory, workspace_id=waiter_ws)
    assert [row.phase for row in rows] == [WorkSlotLeasePhase.waiting.value]


async def test_failed_recheck_while_waiting_withdraws_the_ticket(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A dead waiting row would stay the FIFO head and a live waiter until it
    # expires, blocking every other reacquire and all new admissions.
    holder_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    waiter_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    wakeups = WorkSlotWakeups()
    holder = _scope(session_factory, holder_ws, wakeups=wakeups)
    waiter = _scope(session_factory, waiter_ws, wakeups=wakeups)
    async with holder:
        async with holder.hold("agent"):
            pass
        task = asyncio.create_task(_hold_once(waiter))
        while not await lease_rows(session_factory, workspace_id=waiter_ws):
            await asyncio.sleep(0.02)
        monkeypatch.setattr(work_slot_scope, "acquire_work", _db_down)
        wakeups.notify()
        with pytest.raises(OperationalError):
            await asyncio.wait_for(task, timeout=_TIMEOUT)

    assert await lease_rows(session_factory, workspace_id=waiter_ws) == []


async def test_cleanup_unproven_flag_write_failure_still_keeps_the_lease(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    scope = _scope(session_factory, workspace_id)

    with structlog.testing.capture_logs() as logs, pytest.raises(ComposeExecCleanupError):
        async with scope, scope.hold("agent"):
            monkeypatch.setattr(work_slot_scope, "release_work", _db_down)
            raise _cleanup_error()

    assert "worker.work_slot_cleanup_unproven_flag_failed" in _events(logs)
    assert scope.cleanup_unproven is True
    assert len(await _held_rows(session_factory, workspace_id)) == 1


async def test_failed_cleanup_unproven_flag_write_is_retried_by_renewal(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The reconciler skips an own, unexpired, unflagged lease, and release_idle()
    # refuses an unproven one: a renewed row without the flag would pin the slot.
    workspace_id = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    scope = _scope(session_factory, workspace_id, lease_seconds=3)
    async with scope:
        with monkeypatch.context() as patch, pytest.raises(ComposeExecCleanupError):
            async with scope.hold("agent"):
                patch.setattr(work_slot_scope, "release_work", _db_down)
                raise _cleanup_error()
        (row,) = await _held_rows(session_factory, workspace_id)
        assert row.cleanup_unproven is False

        async with asyncio.timeout(_TIMEOUT):
            while not row.cleanup_unproven:
                await asyncio.sleep(0.05)
                (row,) = await _held_rows(session_factory, workspace_id)
        assert scope.cleanup_unproven is True
        assert scope.lease is not None


async def test_fresh_grant_drops_a_pending_cleanup_unproven_flag_write(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    scope = _scope(session_factory, workspace_id)
    async with scope:
        with monkeypatch.context() as patch, pytest.raises(ComposeExecCleanupError):
            async with scope.hold("agent"):
                patch.setattr(work_slot_scope, "release_work", _db_down)
                raise _cleanup_error()
        unproven = scope.lease
        assert unproven is not None
        # The unflagged row expired and the reconciler released it.
        async with session_factory() as session:
            assert await release_work(session, lease=unproven, now=datetime.now(UTC))
            await session.commit()

        async with scope.hold("agent"):
            fresh = scope.lease
            assert fresh is not None
            assert await scope._renew_once(fresh)  # noqa: SLF001
            # The fresh lease is ordinary: renewal must not flag it unproven.
            rows = await _held_rows(session_factory, workspace_id)
            assert [row.cleanup_unproven for row in rows] == [False]


async def test_cleanup_failure_on_a_lease_another_worker_took_logs_the_fence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    scope = _scope(session_factory, workspace_id)

    with structlog.testing.capture_logs() as logs, pytest.raises(ComposeExecCleanupError):
        async with scope, scope.hold("agent"):
            lease = scope.lease
            assert lease is not None
            async with session_factory() as session:
                assert await adopt_work(
                    session,
                    lease=lease,
                    holder_worker_id="control-worker-adopter",
                    now=datetime.now(UTC),
                    lease_seconds=300.0,
                )
                await session.commit()
            raise _cleanup_error()

    assert any(entry.get("reason_code") == WORK_SLOT_LEASE_FENCED for entry in logs)
    assert scope.lease is None


async def test_cleanup_failure_after_renewal_was_fenced_flags_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    scope = _scope(session_factory, workspace_id, lease_seconds=3)

    with pytest.raises(ComposeExecCleanupError):
        async with scope, scope.hold("agent"):
            lease = scope.lease
            assert lease is not None
            async with session_factory() as session:
                await adopt_work(
                    session,
                    lease=lease,
                    holder_worker_id="control-worker-adopter",
                    now=datetime.now(UTC),
                    lease_seconds=300.0,
                )
                await session.commit()
            while scope.lease is not None:
                await asyncio.sleep(0.05)
            raise _cleanup_error()

    # The adopter owns the row: this invocation neither flags nor deletes it.
    rows = await lease_rows(session_factory, workspace_id=workspace_id)
    assert [(row.holder_worker_id, row.cleanup_unproven) for row in rows] == [
        ("control-worker-adopter", False)
    ]


async def test_release_failure_keeps_the_row_and_never_wakes_waiters(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    wakeups = WorkSlotWakeups()
    scope = _scope(session_factory, workspace_id, wakeups=wakeups)
    async with scope.hold("agent"):
        pass
    released_event = wakeups.waiter()

    with monkeypatch.context() as patch:
        patch.setattr(work_slot_scope, "release_work", _db_down)
        with structlog.testing.capture_logs() as logs:
            await scope.release_idle()

    assert "worker.work_slot_release_failed" in _events(logs)
    assert not released_event.is_set()
    assert len(await _held_rows(session_factory, workspace_id)) == 1


async def test_failed_idle_release_is_retried_at_the_next_pure_wait(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    wakeups = WorkSlotWakeups()
    scope = _scope(session_factory, workspace_id, wakeups=wakeups)
    async with scope.hold("agent"):
        pass
    lease = scope.lease

    with monkeypatch.context() as patch:
        patch.setattr(work_slot_scope, "release_work", _db_down)
        await scope.release_idle()
    # The row is still held, so the scope keeps tracking it instead of
    # treating the slot as freed (which would also hide it from the reconciler).
    assert scope.lease == lease

    released_event = wakeups.waiter()
    await scope.release_idle()

    assert scope.lease is None
    assert released_event.is_set()
    assert await _held_rows(session_factory, workspace_id) == []


async def test_hold_after_a_failed_idle_release_resumes_renewal(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    scope = _scope(session_factory, workspace_id, lease_seconds=3)
    async with scope:
        async with scope.hold("agent"):
            pass
        with monkeypatch.context() as patch:
            patch.setattr(work_slot_scope, "release_work", _db_down)
            await scope.release_idle()

        async with scope.hold("agent"):
            (row,) = await _held_rows(session_factory, workspace_id)
            entry_expiry = row.expires_at
            await asyncio.sleep(1.3)
            (row,) = await _held_rows(session_factory, workspace_id)
            # The kept lease is renewed again, not left to expire mid-run.
            assert row.expires_at > entry_expiry


async def test_failed_close_release_hands_the_row_to_the_reconciler(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    scope = _scope(session_factory, workspace_id)
    async with scope.hold("agent"):
        pass

    with monkeypatch.context() as patch:
        patch.setattr(work_slot_scope, "release_work", _db_down)
        await scope.close()

    # A closed scope never retries; its untracked row expires to the reconciler.
    assert scope.lease is None
    assert len(await _held_rows(session_factory, workspace_id)) == 1


async def test_renewal_extends_the_lease_and_survives_db_errors(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    scope = _scope(session_factory, workspace_id, lease_seconds=3)
    async with scope, scope.hold("agent"):
        (row,) = await _held_rows(session_factory, workspace_id)
        first_expiry = row.expires_at
        await asyncio.sleep(1.3)
        (row,) = await _held_rows(session_factory, workspace_id)
        assert row.expires_at > first_expiry

        with monkeypatch.context() as patch:
            patch.setattr(work_slot_scope, "renew_work", _db_down)
            with structlog.testing.capture_logs() as logs:
                await asyncio.sleep(1.2)
        assert "worker.work_slot_renew_failed" in _events(logs)
        # A failed renewal never drops the lease: the run keeps its slot.
        assert scope.lease is not None


async def test_fenced_ignores_a_superseded_lease_ref(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    current = WorkSlotLeaseRef(
        invocation_id="wsl_current", workspace_id="ws_1", holder_worker_id=_WORKER, holder_epoch=2
    )
    old = WorkSlotLeaseRef(
        invocation_id="wsl_current", workspace_id="ws_1", holder_worker_id=_WORKER, holder_epoch=1
    )
    scope = InvocationWorkSlot(
        session_factory=session_factory,
        config=WorkerConfig(),
        worker_id=_WORKER,
        workspace_id="ws_1",
        wakeups=WorkSlotWakeups(),
        lease=current,
    )

    scope._fenced(old, phase="renew")  # noqa: SLF001

    assert scope.lease == current


def test_cleanup_chain_walk_terminates_on_cyclic_contexts() -> None:
    first = RuntimeError("first")
    second = ValueError("second")
    first.__context__ = second
    second.__context__ = first

    assert _cleanup_unproven_in_chain(first) is False


async def test_run_shielded_finishes_the_write_then_reraises_cancellation() -> None:
    started = asyncio.Event()
    finish = asyncio.Event()
    written: list[str] = []

    async def _write() -> None:
        started.set()
        await finish.wait()
        written.append("done")

    task = asyncio.create_task(_run_shielded(_write()))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    finish.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert written == ["done"]
