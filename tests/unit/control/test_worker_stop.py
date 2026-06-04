"""Tests for ControlWorker.run_forever — stop signalling and backoff."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import awf.control.worker.manager as worker_manager
from awf.control.worker import ControlWorker, WorkerConfig
from awf.db.models import WorkerHeartbeat
from awf.db.repositories import WorkerHeartbeatRepository
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine


@pytest.fixture
async def factory():  # type: ignore[no-untyped-def]
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_run_forever_exits_when_stop_requested(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """run_forever must terminate promptly on request_stop()."""
    provisioner = AsyncMock()
    worker = ControlWorker(
        session_factory=factory,
        provisioner=provisioner,
        config=WorkerConfig(poll_interval_seconds=0.05, max_concurrent_provisions=1),
    )

    async def _stop_after_tick() -> None:
        # Give run_forever a couple of idle ticks, then ask it to stop.
        await asyncio.sleep(0.12)
        worker.request_stop()

    # The task itself must complete within the test's 30s timeout — no hangs.
    await asyncio.gather(worker.run_forever(), _stop_after_tick())


@pytest.mark.unit
async def test_run_forever_shutdown_ignores_failed_heartbeat_task(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = ControlWorker(
        session_factory=factory,
        provisioner=AsyncMock(),
        config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=0),
    )
    run_once = AsyncMock(return_value=0)
    monkeypatch.setattr(worker, "run_once", run_once)

    async def _failed_heartbeat_loop() -> None:
        raise AssertionError("unreachable DB retry state")

    monkeypatch.setattr(worker, "_heartbeat_loop", _failed_heartbeat_loop)

    async def _stop_after_tick() -> None:
        await asyncio.sleep(0.03)
        worker.request_stop()

    await asyncio.wait_for(
        asyncio.gather(worker.run_forever(), _stop_after_tick()),
        timeout=1.0,
    )

    assert run_once.await_count >= 1


@pytest.mark.unit
async def test_run_once_records_worker_heartbeat(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    provisioner = AsyncMock()
    worker = ControlWorker(
        session_factory=factory,
        provisioner=provisioner,
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_provisions=0,
            node_id="node-a",
        ),
    )

    dispatched = await worker.run_once()

    assert dispatched == 0
    async with factory() as session:
        heartbeat = (
            await session.execute(
                select(WorkerHeartbeat).where(WorkerHeartbeat.worker_id == worker._worker_id)  # noqa: SLF001
            )
        ).scalar_one()

    assert heartbeat.node_id == "node-a"
    assert heartbeat.poll_interval_seconds == 0.01
    assert heartbeat.started_at <= heartbeat.last_heartbeat_at


@pytest.mark.unit
async def test_run_once_prunes_stale_worker_heartbeats(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    stale_at = datetime.now(UTC) - timedelta(days=2)
    fresh_at = datetime.now(UTC)
    async with factory() as session, session.begin():
        repo = WorkerHeartbeatRepository(session)
        await repo.record_heartbeat(
            worker_id="old-worker-process",
            node_id="local",
            started_at=stale_at - timedelta(minutes=1),
            last_heartbeat_at=stale_at,
            poll_interval_seconds=5.0,
        )
        await repo.record_heartbeat(
            worker_id="fresh-worker-process",
            node_id="local",
            started_at=fresh_at - timedelta(minutes=1),
            last_heartbeat_at=fresh_at,
            poll_interval_seconds=5.0,
        )

    worker = ControlWorker(
        session_factory=factory,
        provisioner=AsyncMock(),
        config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=0),
    )

    dispatched = await worker.run_once()

    async with factory() as session:
        repo = WorkerHeartbeatRepository(session)
        stale_heartbeat = await repo.get(worker_id="old-worker-process")
        fresh_heartbeat = await repo.get(worker_id="fresh-worker-process")
        current_heartbeat = await repo.get(worker_id=worker._worker_id)  # noqa: SLF001

    assert dispatched == 0
    assert stale_heartbeat is None
    assert fresh_heartbeat is not None
    assert current_heartbeat is not None


@pytest.mark.unit
async def test_prune_stale_heartbeats_failure_throttles_retry(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    current_time = 100.0
    monkeypatch.setattr(worker_manager, "monotonic", lambda: current_time, raising=False)
    worker = ControlWorker(
        session_factory=factory,
        provisioner=AsyncMock(),
        config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=0),
    )
    prune_stale_heartbeats = AsyncMock(side_effect=RuntimeError("db unavailable"))
    monkeypatch.setattr(worker, "_prune_stale_heartbeats", prune_stale_heartbeats)

    await worker._prune_stale_heartbeats_safely()  # noqa: SLF001
    await worker._prune_stale_heartbeats_safely()  # noqa: SLF001

    assert prune_stale_heartbeats.await_count == 1
    assert worker._last_heartbeat_pruned_at == current_time  # noqa: SLF001

    current_time += worker_manager._WORKER_HEARTBEAT_PRUNE_INTERVAL_SECONDS + 0.01  # noqa: SLF001
    await worker._prune_stale_heartbeats_safely()  # noqa: SLF001

    assert prune_stale_heartbeats.await_count == 2


@pytest.mark.unit
async def test_heartbeat_loop_defers_initial_write_to_run_once(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = ControlWorker(
        session_factory=factory,
        provisioner=AsyncMock(),
        config=WorkerConfig(poll_interval_seconds=0.05, max_concurrent_provisions=0),
    )
    record_heartbeat = AsyncMock()
    monkeypatch.setattr(worker, "_record_heartbeat", record_heartbeat)

    heartbeat_task = asyncio.create_task(worker._heartbeat_loop())  # noqa: SLF001
    try:
        await asyncio.sleep(0)
        assert record_heartbeat.await_count == 0
    finally:
        worker.request_stop()
        await asyncio.wait_for(heartbeat_task, timeout=1.0)


@pytest.mark.unit
async def test_heartbeat_write_failure_does_not_kill_worker() -> None:
    def _raising_factory() -> AsyncSession:
        raise RuntimeError("postgresql+asyncpg://awf:secret@db.internal:5432/awf")

    worker = ControlWorker(
        session_factory=_raising_factory,  # type: ignore[arg-type]
        provisioner=AsyncMock(),
        config=WorkerConfig(),
    )

    await worker._record_heartbeat_safely()  # noqa: SLF001


@pytest.mark.unit
async def test_record_heartbeat_safely_throttles_repeated_writes(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    current_time = 100.0
    monkeypatch.setattr(worker_manager, "monotonic", lambda: current_time, raising=False)
    record_heartbeat = AsyncMock()
    worker = ControlWorker(
        session_factory=factory,
        provisioner=AsyncMock(),
        config=WorkerConfig(poll_interval_seconds=1.0, max_concurrent_provisions=0),
    )
    monkeypatch.setattr(worker, "_record_heartbeat", record_heartbeat)

    await worker._record_heartbeat_safely()  # noqa: SLF001
    await worker._record_heartbeat_safely()  # noqa: SLF001

    assert record_heartbeat.call_count == 1

    current_time += 1.01
    await worker._record_heartbeat_safely()  # noqa: SLF001

    assert record_heartbeat.call_count == 2


@pytest.mark.unit
async def test_record_heartbeat_safely_throttles_failed_writes(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    current_time = 200.0
    monkeypatch.setattr(worker_manager, "monotonic", lambda: current_time, raising=False)
    record_heartbeat = AsyncMock(side_effect=RuntimeError("db unavailable"))
    worker = ControlWorker(
        session_factory=factory,
        provisioner=AsyncMock(),
        config=WorkerConfig(poll_interval_seconds=1.0, max_concurrent_provisions=0),
    )
    monkeypatch.setattr(worker, "_record_heartbeat", record_heartbeat)

    await worker._record_heartbeat_safely()  # noqa: SLF001
    await worker._record_heartbeat_safely()  # noqa: SLF001

    assert record_heartbeat.call_count == 1

    current_time += 1.01
    await worker._record_heartbeat_safely()  # noqa: SLF001

    assert record_heartbeat.call_count == 2


@pytest.mark.unit
async def test_wait_for_execution_tasks_refreshes_heartbeat_while_draining_once_worker(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(
        worker_manager,
        "worker_heartbeat_write_interval_seconds",
        lambda _poll_interval_seconds: 0.01,
        raising=False,
    )
    worker = ControlWorker(
        session_factory=factory,
        provisioner=AsyncMock(),
        config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=0),
    )
    record_heartbeat = AsyncMock()
    monkeypatch.setattr(worker, "_record_heartbeat", record_heartbeat)
    await worker._record_heartbeat_safely()  # noqa: SLF001

    release = asyncio.Event()

    async def _blocked_execution() -> None:
        await release.wait()

    worker._execution_tasks["ws_active"] = asyncio.create_task(_blocked_execution())  # noqa: SLF001
    wait_task = asyncio.create_task(worker.wait_for_execution_tasks())
    try:
        for _ in range(100):
            if record_heartbeat.await_count >= 2:
                break
            await asyncio.sleep(0.01)
        assert record_heartbeat.await_count >= 2
    finally:
        release.set()
        await asyncio.wait_for(wait_task, timeout=1.0)

    assert worker._execution_tasks == {}  # noqa: SLF001


@pytest.mark.unit
async def test_safely_provision_swallows_provisioner_exceptions(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """One failing provision must not abort the rest of the batch."""
    from awf.db.models import Workspace as WorkspaceModel
    from awf.db.repositories import WorkspaceRepository

    # Seed two workspaces in ``requested`` so run_once finds them.
    async with factory() as s:
        for title in ["a", "b"]:
            await WorkspaceRepository(s).create(
                repo_url="git@x:y.git",
                branch_base="development",
                task_title=title,
                task_prompt="p",
                agent="codex",
                test_commands=[],
            )
        await s.commit()

    provisioner = AsyncMock()
    provisioner.provision_claimed.side_effect = [
        RuntimeError("boom"),
        None,
    ]  # first fails, second ok

    worker = ControlWorker(
        session_factory=factory,
        provisioner=provisioner,
        config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=2),
    )

    # run_once must not raise even though one provision threw.
    dispatched = await worker.run_once()
    assert dispatched == 2
    assert provisioner.provision_claimed.call_count == 2

    # Both workspaces were looked up from the DB; assert shape.
    async with factory() as s:
        from sqlalchemy import select

        rows = (await s.execute(select(WorkspaceModel))).scalars().all()
        assert len(rows) == 2
