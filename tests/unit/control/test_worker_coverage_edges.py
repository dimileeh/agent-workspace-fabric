"""Focused branch-coverage tests for control worker scheduling helpers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import ControlWorker, WorkerConfig, _execution_claim_is_stale
from awf.db.base import Base
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory


class _NoopProvisioner:
    async def provision(self, workspace_id: str) -> None:
        del workspace_id


class _RecordingExecutor:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.executed: list[str] = []
        self.resumed: list[str] = []

    async def execute(
        self,
        workspace_id: str,
        *,
        execution_owner_id: str | None = None,
        execution_lease_expires_at: datetime | None = None,
    ) -> None:
        del execution_owner_id, execution_lease_expires_at
        self.executed.append(workspace_id)
        if self.fail:
            raise RuntimeError("executor crashed")

    async def resume_pr_monitor(self, workspace_id: str) -> None:
        self.resumed.append(workspace_id)
        if self.fail:
            raise RuntimeError("monitor crashed")


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'worker.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


def _worker(
    factory: async_sessionmaker[AsyncSession],
    *,
    executor: _RecordingExecutor | None = None,
    max_concurrent_executions: int = 2,
) -> ControlWorker:
    return ControlWorker(
        session_factory=factory,
        provisioner=_NoopProvisioner(),  # type: ignore[arg-type]
        executor=executor,
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_executions=max_concurrent_executions,
            monitor_claim_lease_seconds=30,
            execution_claim_lease_seconds=30,
            node_id="node-1",
        ),
    )


async def _seed_status(
    factory: async_sessionmaker[AsyncSession],
    status: WorkspaceStatus,
    *,
    title: str,
) -> str:
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@example.com:repo/app.git",
            branch_base="main",
            task_title=title,
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
        ws.branch_name = f"awf/{ws.id}"
        ws.remote_push_branch = ws.branch_name
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.compose_file_path = f"/tmp/{ws.id}/compose.yml"
        if status in {
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
            WorkspaceStatus.monitoring_pr,
        }:
            await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        if status in {
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
            WorkspaceStatus.monitoring_pr,
        }:
            await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED")
        if status in {
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
            WorkspaceStatus.monitoring_pr,
        }:
            await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="SEED")
        if status in {WorkspaceStatus.pushing, WorkspaceStatus.monitoring_pr}:
            await repo.transition(ws, to=WorkspaceStatus.pushing, reason_code="SEED")
        if status == WorkspaceStatus.monitoring_pr:
            ws.pr_number = 123
            ws.pr_url = "https://github.com/example/repo/pull/123"
            await repo.transition(ws, to=WorkspaceStatus.monitoring_pr, reason_code="SEED")
        await s.commit()
        return ws.id


class _ExplodingSessionFactory:
    calls = 0

    def __call__(self) -> object:
        self.calls += 1
        raise AssertionError("session factory should not be opened for empty limits")


@pytest.mark.unit
async def test_list_by_status_returns_empty_for_non_positive_limits() -> None:
    worker = ControlWorker(
        session_factory=_ExplodingSessionFactory(),  # type: ignore[arg-type]
        provisioner=_NoopProvisioner(),  # type: ignore[arg-type]
        config=WorkerConfig(),
    )

    assert await worker._list_by_status(WorkspaceStatus.ready, limit=0) == []
    assert await worker._list_by_status(WorkspaceStatus.ready, limit=-1) == []


@pytest.mark.unit
async def test_dispatch_ready_executions_respects_limit_and_existing_tasks(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)
    existing = asyncio.create_task(asyncio.sleep(0))
    worker._execution_tasks["ws_existing"] = existing

    dispatched = worker._dispatch_ready_executions(
        ["ws_existing", "ws_new", "ws_extra"],
        limit=1,
    )
    await worker.wait_for_execution_tasks()

    assert dispatched == {"ws_new"}
    assert worker._execution_tasks == {}


@pytest.mark.unit
async def test_claim_monitoring_pr_ids_respects_limit_and_running_tasks(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    running_id = await _seed_status(factory, WorkspaceStatus.monitoring_pr, title="running")
    claimable_id = await _seed_status(factory, WorkspaceStatus.monitoring_pr, title="claimable")
    extra_id = await _seed_status(factory, WorkspaceStatus.monitoring_pr, title="extra")
    worker = _worker(factory)
    worker._execution_tasks[running_id] = asyncio.create_task(asyncio.sleep(0))

    claimed = await worker._claim_monitoring_pr_ids(
        [running_id, claimable_id, extra_id],
        limit=1,
    )
    await worker.wait_for_execution_tasks()

    assert claimed == [claimable_id]
    async with factory() as s:
        repo = WorkspaceRepository(s)
        running = await repo.get(running_id)
        claimable = await repo.get(claimable_id)
        extra = await repo.get(extra_id)
        assert running is not None and running.monitor_claimed_by is None
        assert claimable is not None and claimable.monitor_claimed_by == worker._worker_id
        assert extra is not None and extra.monitor_claimed_by is None


@pytest.mark.unit
async def test_claimed_execution_releases_claim_after_executor_exception(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _seed_status(factory, WorkspaceStatus.running, title="claimed")
    executor = _RecordingExecutor(fail=True)
    worker = _worker(factory, executor=executor)
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.get(workspace_id)
        assert ws is not None
        ws.execution_claimed_by = worker._worker_id
        ws.execution_claim_expires_at = datetime.now(UTC) + timedelta(seconds=30)
        await s.commit()

    await worker._safely_execute_claimed(workspace_id)

    assert executor.executed == [workspace_id]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.execution_claimed_by is None
        assert ws.execution_claim_expires_at is None


@pytest.mark.unit
async def test_safely_execute_and_resume_noop_without_executor(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    worker = _worker(factory)

    await worker._safely_execute("ws_missing")
    await worker._safely_resume_pr_monitor("ws_missing")

    assert worker._execution_tasks == {}


@pytest.mark.unit
def test_execution_claim_is_stale_handles_missing_and_naive_datetimes() -> None:
    cutoff = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)

    assert _execution_claim_is_stale(
        SimpleNamespace(execution_claimed_by=None, execution_claim_expires_at=cutoff),
        cutoff,
    )
    assert _execution_claim_is_stale(
        SimpleNamespace(execution_claimed_by="worker", execution_claim_expires_at=None),
        cutoff,
    )
    assert _execution_claim_is_stale(
        SimpleNamespace(
            execution_claimed_by="worker",
            execution_claim_expires_at=datetime(2026, 4, 27, 11, 59),
        ),
        cutoff,
    )
    assert not _execution_claim_is_stale(
        SimpleNamespace(
            execution_claimed_by="worker",
            execution_claim_expires_at=datetime(2026, 4, 27, 12, 1),
        ),
        cutoff,
    )
