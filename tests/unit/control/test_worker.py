"""ControlWorker tests.

We use the real Provisioner against real git + SQLite to validate the full
pipeline, rather than mocking the provisioner. The worker's contract is
primarily about claiming work off the DB in the right order and bounding
concurrency, so end-to-end is the most useful test.
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import ControlWorker, WorkerConfig
from awf.db.base import Base
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.node.git_manager import GitManager
from awf.node.provisioner import Provisioner, ProvisionerConfig


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(["init", "-q", "-b", "development"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["config", "user.email", "t@t"], repo)
    (repo / "README.md").write_text("hello\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "init"], repo)
    return repo


@pytest.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "awf-test.db"
    engine = make_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


@pytest.fixture
def worker(session_factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> ControlWorker:
    git = GitManager(tmp_path / "awf-work")
    prov = Provisioner(
        session_factory=session_factory,
        git=git,
        config=ProvisionerConfig(node_id="test-node-01"),
    )
    return ControlWorker(
        session_factory=session_factory,
        provisioner=prov,
        config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=3),
    )


async def _create_requested(
    session_factory: async_sessionmaker[AsyncSession], origin: Path, title: str
) -> str:
    async with session_factory() as s:
        ws = await WorkspaceRepository(s).create(
            repo_url=str(origin),
            branch_base="development",
            task_title=title,
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        await s.commit()
        return ws.id


async def _create_ready(
    session_factory: async_sessionmaker[AsyncSession], origin: Path, title: str
) -> str:
    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url=str(origin),
            branch_base="development",
            task_title=title,
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="SEED")
        ws.branch_name = f"awf/{ws.id}"
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.compose_file_path = f"/tmp/awf/{ws.id}/compose.yml"
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        await s.commit()
        return ws.id


class _TransitioningProvisioner:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self.calls: list[str] = []

    async def provision(self, workspace_id: str) -> None:
        self.calls.append(workspace_id)
        async with self._session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            assert ws is not None
            await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="TEST")
            ws.branch_name = f"awf/{workspace_id}"
            ws.base_commit = "b" * 40
            ws.compose_project_name = f"awf_{workspace_id}"
            ws.compose_file_path = f"/tmp/awf/{workspace_id}/compose.yml"
            await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="TEST_READY")
            await s.commit()


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, workspace_id: str) -> None:
        self.calls.append(workspace_id)


class TestRunOnce:
    @pytest.mark.unit
    async def test_returns_zero_when_no_pending(self, worker: ControlWorker) -> None:
        assert await worker.run_once() == 0

    @pytest.mark.unit
    async def test_dispatches_pending_workspaces(
        self,
        worker: ControlWorker,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        ids = [await _create_requested(session_factory, origin_repo, f"task-{i}") for i in range(3)]

        dispatched = await worker.run_once()
        assert dispatched == 3

        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            for ws_id in ids:
                ws = await repo.get(ws_id)
                assert ws is not None
                assert ws.status == WorkspaceStatus.ready.value

    @pytest.mark.unit
    async def test_respects_max_concurrent_bound(
        self,
        worker: ControlWorker,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        # 5 workspaces requested; worker has max_concurrent=3 so should batch.
        for i in range(5):
            await _create_requested(session_factory, origin_repo, f"task-{i}")

        dispatched = await worker.run_once()
        assert dispatched == 3  # bounded by config

        # Drain the rest.
        dispatched = await worker.run_once()
        assert dispatched == 2

        async with session_factory() as s:
            from sqlalchemy import func, select

            from awf.db.models import Workspace

            count = await s.scalar(
                select(func.count(Workspace.id)).where(
                    Workspace.status == WorkspaceStatus.ready.value
                )
            )
            assert count == 5


class TestRunOnceExecution:
    @pytest.mark.unit
    async def test_dispatches_requested_provisioning_then_ready_execution_work(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        ready_id = await _create_ready(session_factory, origin_repo, "already-ready")
        requested_id = await _create_requested(session_factory, origin_repo, "new-request")
        provisioner = _TransitioningProvisioner(session_factory)
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            executor=executor,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=3),
        )

        dispatched = await worker.run_once()
        await worker.wait_for_execution_tasks()

        assert dispatched == 2
        assert provisioner.calls == [requested_id]
        assert set(executor.calls) == {ready_id, requested_id}

    @pytest.mark.unit
    async def test_freshly_provisioned_workspace_is_not_counted_twice(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        requested_id = await _create_requested(session_factory, origin_repo, "new-request")
        provisioner = _TransitioningProvisioner(session_factory)
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            executor=executor,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=3),
        )

        dispatched = await worker.run_once()
        await worker.wait_for_execution_tasks()

        assert dispatched == 1
        assert provisioner.calls == [requested_id]
        assert executor.calls == [requested_id]

    @pytest.mark.unit
    async def test_ready_execution_does_not_block_future_poll_batches(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        ready_id = await _create_ready(session_factory, origin_repo, "already-ready")
        started = asyncio.Event()
        release = asyncio.Event()
        provisioner = _TransitioningProvisioner(session_factory)

        class _BlockingExecutor:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def execute(self, workspace_id: str) -> None:
                self.calls.append(workspace_id)
                started.set()
                await release.wait()

        executor = _BlockingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            executor=executor,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=1,
                max_concurrent_executions=1,
            ),
        )

        assert await asyncio.wait_for(worker.run_once(), timeout=0.2) == 1
        await asyncio.wait_for(started.wait(), timeout=0.2)

        requested_id = await _create_requested(session_factory, origin_repo, "new-request")
        assert await asyncio.wait_for(worker.run_once(), timeout=0.2) == 1
        assert provisioner.calls == [requested_id]
        assert executor.calls == [ready_id]

        release.set()
        await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=0.2)

    @pytest.mark.unit
    async def test_execution_limit_is_independent_from_provisioning_limit(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        ready_ids = [
            await _create_ready(session_factory, origin_repo, f"ready-{i}") for i in range(4)
        ]
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=1,
                max_concurrent_executions=3,
            ),
        )

        assert await worker.run_once() == 3
        await worker.wait_for_execution_tasks()

        assert set(executor.calls) == set(ready_ids[:3])

    @pytest.mark.unit
    async def test_ready_workspace_is_noop_when_no_executor_is_wired(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        ready_id = await _create_ready(session_factory, origin_repo, "already-ready")
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=3),
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(ready_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.ready.value

    @pytest.mark.unit
    async def test_ready_execution_race_skip_does_not_mark_workspace_failed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        ready_id = await _create_ready(session_factory, origin_repo, "race")

        class _CancellingExecutor:
            async def execute(self, workspace_id: str) -> None:
                async with session_factory() as s:
                    repo = WorkspaceRepository(s)
                    ws = await repo.get(workspace_id)
                    assert ws is not None
                    await repo.transition(ws, to=WorkspaceStatus.cancelled, reason_code="RACE")
                    await s.commit()

        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_CancellingExecutor(),
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=3),
        )

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(ready_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.cancelled.value
            assert ws.failure_reason is None

    @pytest.mark.unit
    async def test_bad_ready_execution_does_not_abort_other_ready_workspaces(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        first_id = await _create_ready(session_factory, origin_repo, "bad")
        second_id = await _create_ready(session_factory, origin_repo, "good")

        class _FlakyExecutor:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def execute(self, workspace_id: str) -> None:
                self.calls.append(workspace_id)
                if workspace_id == first_id:
                    raise RuntimeError("boom")

        executor = _FlakyExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=3),
        )

        assert await worker.run_once() == 2
        await worker.wait_for_execution_tasks()
        assert set(executor.calls) == {first_id, second_id}
