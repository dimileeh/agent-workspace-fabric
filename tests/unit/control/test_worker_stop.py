"""Tests for ControlWorker.run_forever — stop signalling and backoff."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import awf.control.worker.manager as worker_manager
from awf.control.worker import ControlWorker, WorkerConfig
from awf.db.models import WorkerHeartbeat
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
