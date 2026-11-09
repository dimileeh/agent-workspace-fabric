"""Per-invocation work-slot scope (slot-release, plan §6.2 B).

``InvocationWorkSlot`` is the async context manager the worker injects into agent
and validation runs. It owns no admission logic, count, lock or queue: it calls
the broker, waits on the worker's in-process release event (with a ~10 s DB
re-check fallback), keeps a lease across hold exits until the workspace first
waits, and retains a lease whose ``awf-exec`` cleanup was never proven.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import structlog.testing
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.compose_exec import ComposeExecCleanupError
from awf.common.ids import new_work_slot_invocation_id
from awf.common.work_slot import MONITOR_WAITING_FOR_WORK_SLOT_REASON_CODE
from awf.control.worker import WorkerConfig, work_slot_scope
from awf.control.worker.constants import (
    WORK_SLOT_CLEANUP_UNPROVEN_RETAINED,
    WORK_SLOT_LEASE_FENCED,
)
from awf.control.worker.resource_broker import (
    WorkSlotLeaseRef,
    acquire_work,
    adopt_work,
    release_work,
)
from awf.control.worker.work_slot_scope import (
    WORK_SLOT_WAITING_EVENT_TYPE,
    InvocationWorkSlot,
    WorkSlotWakeups,
)
from awf.db.enums import WorkSlotLeasePhase, WorkspaceStatus
from awf.db.models import WorkSlotLease
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine
from tests.unit.control._work_slot_support import (
    create_slot_workspace,
    lease_rows,
    workspace_events,
)

pytestmark = pytest.mark.unit

_WORKER = "control-worker-scope"
_OTHER_WORKER = "control-worker-other"
_TIMEOUT = 10.0


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _scope(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    wakeups: WorkSlotWakeups,
    config: WorkerConfig | None = None,
    retain: bool = True,
    admission_open: object = None,
    lease: WorkSlotLeaseRef | None = None,
    invocation_id: str | None = None,
) -> InvocationWorkSlot:
    return InvocationWorkSlot(
        session_factory=factory,
        config=config or WorkerConfig(max_concurrent_executions=1),
        worker_id=_WORKER,
        workspace_id=workspace_id,
        wakeups=wakeups,
        admission_open=admission_open or (lambda: True),  # type: ignore[arg-type]
        retain=retain,
        lease=lease,
        invocation_id=invocation_id,
    )


async def _held(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> list[WorkSlotLease]:
    return [
        row
        for row in await lease_rows(factory, workspace_id=workspace_id)
        if row.phase in {WorkSlotLeasePhase.held.value, WorkSlotLeasePhase.adopted.value}
    ]


async def _phases(
    factory: async_sessionmaker[AsyncSession], workspace_id: str, expected: set[str]
) -> bool:
    return expected <= {row.phase for row in await lease_rows(factory, workspace_id=workspace_id)}


async def _wait_until(predicate: object, *, timeout: float = _TIMEOUT) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        result = predicate()  # type: ignore[operator]
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached before timeout")
        await asyncio.sleep(0.02)


async def _hold_once(scope: InvocationWorkSlot) -> str:
    async with scope, scope.hold("agent") as handle:
        return handle.invocation_id


async def _wait_hold_release(scope: InvocationWorkSlot) -> None:
    async with scope.hold("agent"):
        pass
    await scope.release_idle()


async def test_nested_hold_acquires_once_and_is_retained_until_release_idle(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    calls: list[str] = []
    original = work_slot_scope.acquire_work

    async def _spy(*args: object, **kwargs: object) -> object:
        calls.append(str(kwargs["invocation_id"]))
        return await original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(work_slot_scope, "acquire_work", _spy)
    scope = _scope(session_factory, workspace_id, wakeups=WorkSlotWakeups())

    async with scope:
        async with scope.hold("agent") as outer, scope.hold("validation") as inner:
            assert inner.invocation_id == outer.invocation_id == scope.invocation_id
        # Retained across the hold exit so a fix cycle keeps one lease.
        assert [row.invocation_id for row in await _held(session_factory, workspace_id)] == [
            scope.invocation_id
        ]
        async with scope.hold("agent"):
            pass
        await scope.release_idle()
        assert await _held(session_factory, workspace_id) == []
        assert scope.lease is None

    assert calls == [scope.invocation_id]


async def test_unretained_scope_releases_at_the_outer_hold_exit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    scope = _scope(session_factory, workspace_id, wakeups=WorkSlotWakeups(), retain=False)

    async with scope:
        async with scope.hold("agent"):
            async with scope.hold("validation"):
                assert len(await _held(session_factory, workspace_id)) == 1
            assert len(await _held(session_factory, workspace_id)) == 1
        assert await _held(session_factory, workspace_id) == []


async def test_ambient_claim_lease_makes_hooks_nested_noops(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await create_slot_workspace(session_factory)
    async with session_factory() as session:
        claim = await acquire_work(
            session,
            workspace_id=workspace_id,
            invocation_id=new_work_slot_invocation_id(),
            holder_worker_id=_WORKER,
            config=WorkerConfig(max_concurrent_executions=1),
            now=datetime.now(UTC),
            lease_seconds=300.0,
            admission="new",
        )
        await session.commit()
    claim_lease = claim.lease
    assert claim_lease is not None

    async def _unexpected(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the claim lease must cover the first runs")

    monkeypatch.setattr(work_slot_scope, "acquire_work", _unexpected)
    scope = _scope(session_factory, workspace_id, wakeups=WorkSlotWakeups(), lease=claim_lease)
    async with scope, scope.hold("agent") as handle:
        assert handle.invocation_id == claim_lease.invocation_id
    # Scope close releases the claim lease.
    assert await _held(session_factory, workspace_id) == []


async def test_fresh_invocation_for_the_same_workspace_is_refused_a_share(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    wakeups = WorkSlotWakeups()
    config = WorkerConfig(max_concurrent_executions=3)
    first = _scope(session_factory, workspace_id, wakeups=wakeups, config=config)
    second = _scope(session_factory, workspace_id, wakeups=wakeups, config=config)

    async with first:
        async with first.hold("agent"):
            pass
        task = asyncio.create_task(_hold_once(second))
        await _wait_until(
            lambda: _phases(session_factory, workspace_id, {WorkSlotLeasePhase.waiting.value})
        )
        # A free slot exists, but the workspace's lease belongs to ``first``.
        await asyncio.sleep(0.1)
        assert not task.done()
        await first.release_idle()
        second_invocation = await asyncio.wait_for(task, timeout=_TIMEOUT)

    assert second_invocation == second.invocation_id != first.invocation_id


async def test_waiter_wakes_promptly_on_in_process_release(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With the fallback far away, only the in-process release event can wake it.
    monkeypatch.setattr(work_slot_scope, "_WORK_SLOT_WAIT_RECHECK_SECONDS", 120.0)
    holder_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    waiter_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    wakeups = WorkSlotWakeups()
    holder = _scope(session_factory, holder_ws, wakeups=wakeups)
    waiter = _scope(session_factory, waiter_ws, wakeups=wakeups)
    async with holder:
        async with holder.hold("agent"):
            pass
        task = asyncio.create_task(_hold_once(waiter))
        await _wait_until(
            lambda: _phases(session_factory, waiter_ws, {WorkSlotLeasePhase.waiting.value})
        )
        await holder.release_idle()
        await asyncio.wait_for(task, timeout=_TIMEOUT)


async def test_waiter_rechecks_the_db_when_another_worker_releases(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(work_slot_scope, "_WORK_SLOT_WAIT_RECHECK_SECONDS", 0.05)
    holder_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    waiter_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    # Separate wakeups: the release happens in "another worker" process.
    holder = _scope(session_factory, holder_ws, wakeups=WorkSlotWakeups())
    waiter = _scope(session_factory, waiter_ws, wakeups=WorkSlotWakeups())
    async with holder:
        async with holder.hold("agent"):
            pass
        task = asyncio.create_task(_hold_once(waiter))
        await _wait_until(
            lambda: _phases(session_factory, waiter_ws, {WorkSlotLeasePhase.waiting.value})
        )
        lease = holder.lease
        assert lease is not None
        async with session_factory() as session:
            assert await release_work(session, lease=lease, now=datetime.now(UTC))
            await session.commit()
        await asyncio.wait_for(task, timeout=_TIMEOUT)


async def test_one_waiting_event_per_wait_episode(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(work_slot_scope, "_WORK_SLOT_WAIT_RECHECK_SECONDS", 0.02)
    holder_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    waiter_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.monitoring_pr)
    wakeups = WorkSlotWakeups()
    holder = _scope(session_factory, holder_ws, wakeups=wakeups)
    waiter = _scope(session_factory, waiter_ws, wakeups=wakeups)

    async def _episode() -> None:
        async with holder.hold("agent"):
            pass
        task = asyncio.create_task(_wait_hold_release(waiter))
        await _wait_until(
            lambda: _phases(session_factory, waiter_ws, {WorkSlotLeasePhase.waiting.value})
        )
        # Several fallback re-checks happen while the waiter stays queued.
        await asyncio.sleep(0.2)
        await holder.release_idle()
        await asyncio.wait_for(task, timeout=_TIMEOUT)

    async with holder, waiter:
        await _episode()
        events = await workspace_events(
            session_factory, waiter_ws, event_type=WORK_SLOT_WAITING_EVENT_TYPE
        )
        assert len(events) == 1
        assert events[0].reason_code == MONITOR_WAITING_FOR_WORK_SLOT_REASON_CODE
        assert events[0].payload["phase"] == "agent"
        assert events[0].payload["limit"] == 1
        await _episode()

    events = await workspace_events(
        session_factory, waiter_ws, event_type=WORK_SLOT_WAITING_EVENT_TYPE
    )
    assert len(events) == 2


def _cleanup_error() -> ComposeExecCleanupError:
    return ComposeExecCleanupError(
        invocation_id="awf_unproven",
        source="agent",
        label="codex",
        message="tagged processes still alive",
    )


def _raise_direct() -> None:
    raise _cleanup_error()


def _raise_as_cause() -> None:
    raise RuntimeError("agent run failed") from _cleanup_error()


def _raise_masked_cancellation() -> None:
    # The adapter's finalize step can replace a propagating cleanup failure with
    # a (tagged) CancelledError; the cleanup failure survives as ``__context__``.
    try:
        raise _cleanup_error()
    finally:
        raise asyncio.CancelledError()  # noqa: B012


@pytest.mark.parametrize("raiser", [_raise_direct, _raise_as_cause, _raise_masked_cancellation])
async def test_cleanup_unproven_failure_retains_the_lease(
    session_factory: async_sessionmaker[AsyncSession],
    raiser: object,
) -> None:
    workspace_id = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    scope = _scope(session_factory, workspace_id, wakeups=WorkSlotWakeups())

    with (
        structlog.testing.capture_logs() as logs,
        pytest.raises((ComposeExecCleanupError, RuntimeError, asyncio.CancelledError)),
    ):
        async with scope, scope.hold("agent"):
            raiser()  # type: ignore[operator]

    rows = await _held(session_factory, workspace_id)
    assert len(rows) == 1
    assert rows[0].cleanup_unproven is True
    assert rows[0].invocation_id == scope.invocation_id
    assert scope.cleanup_unproven is True
    assert any(entry.get("reason_code") == WORK_SLOT_CLEANUP_UNPROVEN_RETAINED for entry in logs)
    # release_idle never frees an unproven lease either.
    await scope.release_idle()
    assert len(await _held(session_factory, workspace_id)) == 1


async def test_ordinary_failure_still_releases_the_lease(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    scope = _scope(session_factory, workspace_id, wakeups=WorkSlotWakeups(), retain=False)

    with pytest.raises(RuntimeError):
        async with scope, scope.hold("agent"):
            raise RuntimeError("agent failed")

    assert await lease_rows(session_factory, workspace_id=workspace_id) == []


async def test_fenced_renewal_logs_and_drops_the_lease_without_deleting(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    # 3 s leases renew every second (max(1, lease/3)).
    config = WorkerConfig(max_concurrent_executions=1, execution_claim_lease_seconds=3)
    scope = _scope(session_factory, workspace_id, wakeups=WorkSlotWakeups(), config=config)

    with structlog.testing.capture_logs() as logs:
        async with scope, scope.hold("agent"):
            lease = scope.lease
            assert lease is not None
            async with session_factory() as session:
                adopted = await adopt_work(
                    session,
                    lease=lease,
                    holder_worker_id=_OTHER_WORKER,
                    now=datetime.now(UTC),
                    lease_seconds=300.0,
                )
                await session.commit()
            assert adopted is not None
            await _wait_until(lambda: scope.lease is None, timeout=5.0)

    assert any(entry.get("reason_code") == WORK_SLOT_LEASE_FENCED for entry in logs)
    rows = await _held(session_factory, workspace_id)
    assert [row.holder_worker_id for row in rows] == [_OTHER_WORKER]


async def test_cancellation_while_waiting_deletes_the_waiting_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    holder_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    waiter_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    wakeups = WorkSlotWakeups()
    holder = _scope(session_factory, holder_ws, wakeups=wakeups)
    waiter = _scope(session_factory, waiter_ws, wakeups=wakeups)
    async with holder:
        async with holder.hold("agent"):
            pass
        task = asyncio.create_task(_hold_once(waiter))
        await _wait_until(
            lambda: _phases(session_factory, waiter_ws, {WorkSlotLeasePhase.waiting.value})
        )
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert await lease_rows(session_factory, workspace_id=waiter_ws) == []


async def test_closed_admission_queues_the_ticket_until_reopened(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    wakeups = WorkSlotWakeups()
    admission = {"open": False}
    scope = _scope(
        session_factory,
        workspace_id,
        wakeups=wakeups,
        admission_open=lambda: admission["open"],
    )
    task = asyncio.create_task(_hold_once(scope))
    await _wait_until(
        lambda: _phases(session_factory, workspace_id, {WorkSlotLeasePhase.waiting.value})
    )
    await asyncio.sleep(0.1)
    assert not task.done()

    admission["open"] = True
    wakeups.notify()

    await asyncio.wait_for(task, timeout=_TIMEOUT)


async def test_invocation_owned_by_another_worker_rotates_to_a_fresh_invocation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    stolen = new_work_slot_invocation_id()
    async with session_factory() as session:
        await acquire_work(
            session,
            workspace_id=workspace_id,
            invocation_id=stolen,
            holder_worker_id=_OTHER_WORKER,
            config=WorkerConfig(max_concurrent_executions=1),
            # An expired ticket: the other worker stopped re-checking, so it no
            # longer blocks the FIFO head.
            now=datetime.now(UTC) - timedelta(seconds=600),
            lease_seconds=300.0,
            admission="reacquire",
            allow_grant=False,
        )
        await session.commit()
    scope = _scope(session_factory, workspace_id, wakeups=WorkSlotWakeups(), invocation_id=stolen)

    invocation_id = await asyncio.wait_for(_hold_once(scope), timeout=_TIMEOUT)

    assert invocation_id != stolen


async def test_hold_reacquires_when_the_retained_lease_was_taken_away(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    scope = _scope(session_factory, workspace_id, wakeups=WorkSlotWakeups())
    async with scope:
        async with scope.hold("agent"):
            pass
        lease = scope.lease
        assert lease is not None
        # The reconciler (or an adopter) released it behind the scope's back.
        async with session_factory() as session:
            assert await release_work(session, lease=lease, now=datetime.now(UTC))
            await session.commit()
        async with scope.hold("validation"):
            rows = await _held(session_factory, workspace_id)
            assert [row.invocation_id for row in rows] == [scope.invocation_id]


async def test_fresh_lease_after_reconciled_cleanup_unproven_lease_is_released_idle(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    scope = _scope(session_factory, workspace_id, wakeups=WorkSlotWakeups())
    async with scope:
        with pytest.raises(ComposeExecCleanupError):
            async with scope.hold("agent"):
                _raise_direct()
        unproven = scope.lease
        assert unproven is not None
        assert scope.cleanup_unproven is True
        # The reconciler proved no awf-exec remains and released the flagged row.
        async with session_factory() as session:
            assert await release_work(session, lease=unproven, now=datetime.now(UTC))
            await session.commit()

        # Agent-service recovery retries within the same scope on a fresh lease.
        async with scope.hold("agent"):
            assert scope.cleanup_unproven is False
            rows = await _held(session_factory, workspace_id)
            assert [row.cleanup_unproven for row in rows] == [False]

        await scope.release_idle()
        assert scope.lease is None
        assert await lease_rows(session_factory, workspace_id=workspace_id) == []


async def test_retry_never_runs_on_a_still_flagged_cleanup_unproven_lease(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The reconciler does not track a scope's unproven lease: it may release it
    # once the old awf-exec is gone, before a retry on it spawns its own.
    workspace_id = await create_slot_workspace(session_factory, status=WorkspaceStatus.running)
    wakeups = WorkSlotWakeups()
    scope = _scope(session_factory, workspace_id, wakeups=wakeups)

    async def _retry() -> tuple[str, list[bool]]:
        async with scope.hold("agent") as handle:
            rows = await _held(session_factory, workspace_id)
            return handle.invocation_id, [row.cleanup_unproven for row in rows]

    async with scope:
        with pytest.raises(ComposeExecCleanupError):
            async with scope.hold("agent"):
                _raise_direct()
        unproven = scope.lease
        assert unproven is not None

        retry = asyncio.create_task(_retry())
        await _wait_until(
            lambda: _phases(session_factory, workspace_id, {WorkSlotLeasePhase.waiting.value})
        )
        assert not retry.done()
        assert scope.lease is None
        rows = await _held(session_factory, workspace_id)
        assert [(row.invocation_id, row.cleanup_unproven) for row in rows] == [
            (unproven.invocation_id, True)
        ]

        # The reconciler proved no awf-exec remains, released the row and woke waiters.
        async with session_factory() as session:
            assert await release_work(session, lease=unproven, now=datetime.now(UTC))
            await session.commit()
        wakeups.notify()

        invocation_id, flags = await asyncio.wait_for(retry, timeout=_TIMEOUT)
        assert invocation_id != unproven.invocation_id
        assert flags == [False]
        assert scope.cleanup_unproven is False


async def test_detach_parks_the_lease_and_abandon_hands_it_to_the_reconciler(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    parked_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.ready)
    wedged_ws = await create_slot_workspace(session_factory, status=WorkspaceStatus.ready)
    config = WorkerConfig(max_concurrent_executions=2)

    parked = _scope(session_factory, parked_ws, wakeups=WorkSlotWakeups(), config=config)
    async with parked:
        async with parked.hold("provision"):
            pass
        detached = parked.detach()
    assert detached is not None
    assert parked.lease is None
    rows = await _held(session_factory, parked_ws)
    assert [row.invocation_id for row in rows] == [detached.invocation_id]

    wedged = _scope(session_factory, wedged_ws, wakeups=WorkSlotWakeups(), config=config)
    async with wedged, wedged.hold("agent"):
        await wedged.abandon_to_reconciler()
    rows = await _held(session_factory, wedged_ws)
    assert len(rows) == 1
    assert rows[0].cleanup_unproven is True
    # Abandoning or detaching without a lease is a no-op.
    idle = _scope(session_factory, wedged_ws, wakeups=WorkSlotWakeups(), config=config)
    await idle.abandon_to_reconciler()
    assert idle.detach() is None
