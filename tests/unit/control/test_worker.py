"""ControlWorker tests.

We use the real Provisioner against real git + SQLite to validate the full
pipeline, rather than mocking the provisioner. The worker's contract is
primarily about listing work off the DB in the right order and bounding
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
from awf.db.repositories import WorkspaceEventRepository, WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.node.git_manager import GitManager
from awf.node.provisioner import Provisioner, ProvisionerConfig
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot


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


async def _create_monitoring_pr(
    session_factory: async_sessionmaker[AsyncSession],
    origin: Path,
    title: str,
    *,
    pr_number: int = 123,
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
        ws.remote_push_branch = ws.branch_name
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.compose_file_path = f"/tmp/awf/{ws.id}/compose.yml"
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.pushing, reason_code="SEED")
        ws.pr_url = f"https://github.com/example/repo/pull/{pr_number}"
        ws.pr_number = pr_number
        await repo.transition(ws, to=WorkspaceStatus.monitoring_pr, reason_code="SEED")
        await s.commit()
        return ws.id


async def _create_active_execution(
    session_factory: async_sessionmaker[AsyncSession],
    origin: Path,
    title: str,
    status: WorkspaceStatus,
    *,
    compose_project_name: str | None = None,
    node_id: str | None = None,
    persist_compose_project: bool = True,
) -> str:
    assert status in {
        WorkspaceStatus.running,
        WorkspaceStatus.validating,
        WorkspaceStatus.pushing,
    }
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
        ws.remote_push_branch = ws.branch_name
        ws.base_commit = "a" * 40
        ws.node_id = node_id
        if persist_compose_project:
            ws.compose_project_name = (
                compose_project_name
                if compose_project_name is not None
                else f"awf_{ws.id}"
            )
        else:
            ws.compose_project_name = None
        ws.compose_file_path = f"/tmp/awf/{ws.id}/compose.yml"
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED")
        if status in {WorkspaceStatus.validating, WorkspaceStatus.pushing}:
            await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="SEED")
        if status == WorkspaceStatus.pushing:
            await repo.transition(ws, to=WorkspaceStatus.pushing, reason_code="SEED")
        await s.commit()
        return ws.id


async def _create_terminal_execution(
    session_factory: async_sessionmaker[AsyncSession],
    origin: Path,
    title: str,
    status: WorkspaceStatus,
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
        ws.remote_push_branch = ws.branch_name
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.compose_file_path = f"/tmp/awf/{ws.id}/compose.yml"
        await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="SEED")
        await repo.transition(ws, to=WorkspaceStatus.running, reason_code="SEED")
        if status == WorkspaceStatus.failed:
            ws.failure_reason = "infrastructure_failure"
            ws.failure_message = "seed failure"
            await repo.transition(ws, to=WorkspaceStatus.failed, reason_code="SEED")
        elif status == WorkspaceStatus.cancelled:
            await repo.transition(ws, to=WorkspaceStatus.cancelled, reason_code="SEED")
        else:
            await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="SEED")
            await repo.transition(ws, to=WorkspaceStatus.pushing, reason_code="SEED")
            if status == WorkspaceStatus.completed:
                await repo.transition(ws, to=WorkspaceStatus.completed, reason_code="SEED")
            else:
                assert status == WorkspaceStatus.destroyed
                await repo.transition(ws, to=WorkspaceStatus.cancelled, reason_code="SEED")
                await repo.transition(ws, to=WorkspaceStatus.destroying, reason_code="SEED")
                await repo.transition(ws, to=WorkspaceStatus.destroyed, reason_code="SEED")
        await s.commit()
        return ws.id


async def _move_to_operator_control_status(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    final_status: WorkspaceStatus,
) -> None:
    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.get(workspace_id)
        assert ws is not None
        await repo.transition(ws, to=WorkspaceStatus.cancelled, reason_code="TEST_OPERATOR")
        if final_status == WorkspaceStatus.destroyed:
            await repo.transition(ws, to=WorkspaceStatus.destroying, reason_code="TEST_OPERATOR")
            await repo.transition(ws, to=WorkspaceStatus.destroyed, reason_code="TEST_OPERATOR")
        else:
            assert final_status == WorkspaceStatus.cancelled
        await s.commit()


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
        self.resume_calls: list[str] = []

    async def execute(self, workspace_id: str) -> None:
        self.calls.append(workspace_id)

    async def resume_pr_monitor(self, workspace_id: str) -> None:
        self.resume_calls.append(workspace_id)


class _RecordingRuntimeInspector:
    def __init__(self, snapshots: dict[str | None, RuntimeSnapshot]) -> None:
        self._snapshots = snapshots
        self.calls: list[str | None] = []

    async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
        self.calls.append(compose_project_name)
        return self._snapshots[compose_project_name]


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
    async def test_execution_queries_are_limited_to_available_slots(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
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
        release = asyncio.Event()

        async def _busy() -> None:
            await release.wait()

        active_task = asyncio.create_task(_busy())
        worker._execution_tasks["busy"] = active_task

        limits: dict[str, int | None] = {}
        exclusions: dict[str, set[str]] = {}

        async def _list_monitoring_pr(
            *,
            limit: int | None = None,
            exclude_ids: set[str] | None = None,
        ) -> list[str]:
            limits["monitoring"] = limit
            exclusions["monitoring"] = set(exclude_ids or set())
            return []

        async def _list_ready(
            *,
            limit: int | None = None,
            exclude_ids: set[str] | None = None,
        ) -> list[str]:
            limits["ready"] = limit
            exclusions["ready"] = set(exclude_ids or set())
            return []

        worker._list_monitoring_pr = _list_monitoring_pr  # type: ignore[method-assign]
        worker._list_ready = _list_ready  # type: ignore[method-assign]

        try:
            assert await worker.run_once() == 0
            assert limits == {"monitoring": 2, "ready": 2}
            assert exclusions == {"monitoring": {"busy"}, "ready": {"busy"}}
        finally:
            release.set()
            await asyncio.wait_for(active_task, timeout=0.2)
            worker._execution_tasks.pop("busy", None)

    @pytest.mark.unit
    async def test_schedulable_statuses_are_listed_through_repository(
        self,
        monkeypatch: pytest.MonkeyPatch,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        queries: list[tuple[WorkspaceStatus, int, set[str]]] = []

        async def _list_schedulable_ids(
            self: WorkspaceRepository,
            *,
            status: WorkspaceStatus,
            limit: int,
            exclude_ids: set[str] | None = None,
        ) -> list[str]:
            del self
            queries.append((status, limit, set(exclude_ids or set())))
            return []

        monkeypatch.setattr(
            WorkspaceRepository,
            "list_schedulable_ids",
            _list_schedulable_ids,
            raising=False,
        )

        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=2,
                max_concurrent_executions=4,
            ),
        )

        assert await worker.run_once() == 0
        assert queries == [
            (WorkspaceStatus.requested, 2, set()),
            (WorkspaceStatus.monitoring_pr, 4, set()),
            (WorkspaceStatus.ready, 4, set()),
        ]

    @pytest.mark.unit
    async def test_monitor_query_excludes_active_ids_before_limiting(
        self,
        worker: ControlWorker,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        monitor_ids = [
            await _create_monitoring_pr(
                session_factory,
                origin_repo,
                f"needs-monitor-resume-{i}",
                pr_number=100 + i,
            )
            for i in range(3)
        ]

        assert await worker._list_monitoring_pr(
            limit=2,
            exclude_ids={monitor_ids[0]},
        ) == monitor_ids[1:]

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
    @pytest.mark.parametrize(
        "final_status", [WorkspaceStatus.cancelled, WorkspaceStatus.destroyed]
    )
    async def test_stale_ready_list_entry_is_rechecked_before_dispatch(
        self,
        final_status: WorkspaceStatus,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        ready_id = await _create_ready(session_factory, origin_repo, "stale-ready")
        await _move_to_operator_control_status(session_factory, ready_id, final_status)

        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=3),
        )

        async def _stale_ready_list(
            *,
            limit: int | None = None,
            exclude_ids: set[str] | None = None,
        ) -> list[str]:
            del limit, exclude_ids
            return [ready_id]

        worker._list_ready = _stale_ready_list  # type: ignore[method-assign]

        assert await worker.run_once() == 0
        await worker.wait_for_execution_tasks()

        assert executor.calls == []

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

    @pytest.mark.unit
    async def test_concurrent_workers_do_not_claim_same_requested_workspace(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        requested_id = await _create_requested(session_factory, origin_repo, "race-requested")
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[str] = []

        class _ClaimingProvisioner:
            async def provision(self, workspace_id: str) -> None:
                async with session_factory() as s:
                    repo = WorkspaceRepository(s)
                    ws = await repo.transition_if_current(
                        workspace_id,
                        from_status=WorkspaceStatus.requested,
                        to=WorkspaceStatus.provisioning,
                        reason_code="TEST_CLAIM",
                    )
                    if ws is None:
                        return
                    await s.commit()

                calls.append(workspace_id)
                started.set()
                await release.wait()

                async with session_factory() as s:
                    repo = WorkspaceRepository(s)
                    ws = await repo.get(workspace_id)
                    assert ws is not None
                    if ws.status != WorkspaceStatus.provisioning.value:
                        return
                    ws.branch_name = f"awf/{workspace_id}"
                    ws.base_commit = "c" * 40
                    ws.compose_project_name = f"awf_{workspace_id}"
                    ws.compose_file_path = f"/tmp/awf/{workspace_id}/compose.yml"
                    await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="TEST_READY")
                    await s.commit()

        provisioner = _ClaimingProvisioner()
        worker_a = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=1),
        )
        worker_b = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=1),
        )

        runs = [
            asyncio.create_task(worker_a.run_once()),
            asyncio.create_task(worker_b.run_once()),
        ]
        await asyncio.wait_for(started.wait(), timeout=0.2)
        release.set()
        await asyncio.wait_for(asyncio.gather(*runs), timeout=0.5)

        assert calls == [requested_id]

    @pytest.mark.unit
    async def test_concurrent_workers_do_not_claim_same_ready_workspace(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        ready_id = await _create_ready(session_factory, origin_repo, "race-ready")
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[str] = []

        class _ClaimingExecutor:
            async def execute(self, workspace_id: str) -> None:
                async with session_factory() as s:
                    repo = WorkspaceRepository(s)
                    ws = await repo.transition_if_current(
                        workspace_id,
                        from_status=WorkspaceStatus.ready,
                        to=WorkspaceStatus.running,
                        reason_code="TEST_EXECUTOR_CLAIMED",
                    )
                    if ws is None:
                        return
                    await s.commit()

                calls.append(workspace_id)
                started.set()
                await release.wait()

            async def resume_pr_monitor(self, workspace_id: str) -> None:
                raise AssertionError(f"unexpected monitor resume for {workspace_id}")

        executor = _ClaimingExecutor()
        worker_a = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=1,
                max_concurrent_executions=1,
            ),
        )
        worker_b = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=1,
                max_concurrent_executions=1,
            ),
        )

        await asyncio.gather(worker_a.run_once(), worker_b.run_once())
        await asyncio.wait_for(started.wait(), timeout=0.2)
        release.set()
        await asyncio.wait_for(
            asyncio.gather(
                worker_a.wait_for_execution_tasks(), worker_b.wait_for_execution_tasks()
            ),
            timeout=0.5,
        )

        assert calls == [ready_id]


class TestRunOnceMonitorRecovery:
    @pytest.mark.unit
    async def test_fresh_worker_resumes_monitoring_pr_workspace(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        monitor_id = await _create_monitoring_pr(
            session_factory, origin_repo, "needs-monitor-resume"
        )
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=3),
        )

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        assert executor.calls == []
        assert executor.resume_calls == [monitor_id]

    @pytest.mark.unit
    async def test_monitor_resume_and_ready_execution_share_execution_limit(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        monitor_id = await _create_monitoring_pr(
            session_factory, origin_repo, "needs-monitor-resume"
        )
        ready_id = await _create_ready(session_factory, origin_repo, "already-ready")
        monitor_started = asyncio.Event()
        release_monitor = asyncio.Event()

        class _BlockingExecutor(_RecordingExecutor):
            async def resume_pr_monitor(self, workspace_id: str) -> None:
                self.resume_calls.append(workspace_id)
                monitor_started.set()
                await release_monitor.wait()
                async with session_factory() as s:
                    repo = WorkspaceRepository(s)
                    ws = await repo.get(workspace_id)
                    assert ws is not None
                    await repo.transition(
                        ws, to=WorkspaceStatus.completed, reason_code="TEST_MONITOR_DONE"
                    )
                    await s.commit()

        executor = _BlockingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=1,
                max_concurrent_executions=1,
            ),
        )

        assert await asyncio.wait_for(worker.run_once(), timeout=0.2) == 1
        await asyncio.wait_for(monitor_started.wait(), timeout=0.2)
        assert executor.resume_calls == [monitor_id]
        assert executor.calls == []

        assert await asyncio.wait_for(worker.run_once(), timeout=0.2) == 0
        assert executor.calls == []

        release_monitor.set()
        await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=0.2)

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()
        assert executor.calls == [ready_id]

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "final_status", [WorkspaceStatus.cancelled, WorkspaceStatus.destroyed]
    )
    async def test_stale_monitoring_list_entry_is_rechecked_before_dispatch(
        self,
        final_status: WorkspaceStatus,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        monitor_id = await _create_monitoring_pr(
            session_factory, origin_repo, "stale-monitor"
        )
        await _move_to_operator_control_status(session_factory, monitor_id, final_status)

        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=3),
        )

        async def _stale_monitor_list(
            *,
            limit: int | None = None,
            exclude_ids: set[str] | None = None,
        ) -> list[str]:
            del limit, exclude_ids
            return [monitor_id]

        worker._list_monitoring_pr = _stale_monitor_list  # type: ignore[method-assign]

        assert await worker.run_once() == 0
        await worker.wait_for_execution_tasks()

        assert executor.resume_calls == []

    @pytest.mark.unit
    async def test_monitor_resume_does_not_duplicate_active_task(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        monitor_id = await _create_monitoring_pr(
            session_factory, origin_repo, "needs-monitor-resume"
        )
        monitor_started = asyncio.Event()
        release_monitor = asyncio.Event()

        class _BlockingExecutor(_RecordingExecutor):
            async def resume_pr_monitor(self, workspace_id: str) -> None:
                self.resume_calls.append(workspace_id)
                monitor_started.set()
                await release_monitor.wait()

        executor = _BlockingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=1,
                max_concurrent_executions=2,
            ),
        )

        assert await asyncio.wait_for(worker.run_once(), timeout=0.2) == 1
        await asyncio.wait_for(monitor_started.wait(), timeout=0.2)

        assert await asyncio.wait_for(worker.run_once(), timeout=0.2) == 0
        assert executor.resume_calls == [monitor_id]

        release_monitor.set()
        await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=0.2)

    @pytest.mark.unit
    async def test_concurrent_workers_do_not_claim_same_monitoring_pr_workspace(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        monitor_id = await _create_monitoring_pr(session_factory, origin_repo, "race-monitor")
        monitor_started = asyncio.Event()
        release_monitor = asyncio.Event()

        class _BlockingExecutor(_RecordingExecutor):
            async def resume_pr_monitor(self, workspace_id: str) -> None:
                self.resume_calls.append(workspace_id)
                monitor_started.set()
                await release_monitor.wait()

        executor = _BlockingExecutor()
        worker_a = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=1,
                max_concurrent_executions=1,
            ),
        )
        worker_b = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=1,
                max_concurrent_executions=1,
            ),
        )

        dispatched = await asyncio.gather(worker_a.run_once(), worker_b.run_once())
        await asyncio.wait_for(monitor_started.wait(), timeout=0.2)

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            assert ws.monitor_claimed_by is not None
            assert ws.monitor_claim_expires_at is not None

        release_monitor.set()
        await asyncio.wait_for(
            asyncio.gather(
                worker_a.wait_for_execution_tasks(), worker_b.wait_for_execution_tasks()
            ),
            timeout=0.5,
        )

        assert sorted(dispatched) == [0, 1]
        assert executor.resume_calls == [monitor_id]
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            assert ws.monitor_claimed_by is None
            assert ws.monitor_claim_expires_at is None


class TestRunOnceStaleActiveExecutionRecovery:
    @pytest.mark.unit
    async def test_stale_running_with_missing_compose_project_fails(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "stale-running",
            WorkspaceStatus.running,
            persist_compose_project=False,
        )
        inspector = _RecordingRuntimeInspector(
            {
                None: RuntimeSnapshot(
                    stack_state="unknown",
                    reason="workspace has no compose project",
                )
            }
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=1),
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.failure_message is not None
            assert "active execution was lost after a service or Docker restart" in (
                ws.failure_message
            )
            assert "preserved" in ws.failure_message
            events = await WorkspaceEventRepository(s).list(workspace_id=workspace_id)
            assert any(
                event.event_type == "workspace.state_changed"
                and event.reason_code == "STALE_ACTIVE_EXECUTION"
                for event in events
            )
        assert inspector.calls == [None]

    @pytest.mark.unit
    async def test_stale_validating_with_unavailable_docker_fails(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "stale-validating",
            WorkspaceStatus.validating,
            compose_project_name="awf_validating_unavailable",
        )
        inspector = _RecordingRuntimeInspector(
            {
                "awf_validating_unavailable": RuntimeSnapshot(
                    stack_state="unavailable",
                    reason="Cannot connect to the Docker daemon",
                )
            }
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=1),
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.failure_message is not None
            assert "Cannot connect to the Docker daemon" in ws.failure_message
            events = await WorkspaceEventRepository(s).list(workspace_id=workspace_id)
            assert any(event.reason_code == "STALE_ACTIVE_EXECUTION" for event in events)

    @pytest.mark.unit
    async def test_stale_pushing_with_running_stack_is_preserved_and_evented(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "stale-pushing",
            WorkspaceStatus.pushing,
            compose_project_name="awf_pushing_running",
        )
        inspector = _RecordingRuntimeInspector(
            {
                "awf_pushing_running": RuntimeSnapshot(
                    stack_state="running",
                    services=[
                        RuntimeService(
                            name="agent",
                            container_id="abc123",
                            image="awf-agent:latest",
                            state="running",
                            status="Up 2 minutes",
                            health="healthy",
                        )
                    ],
                )
            }
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=1),
        )

        assert await worker.run_once() == 0
        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.pushing.value
            assert ws.failure_reason is None
            events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.stale_active_execution_detected",
            )
            assert len(events) == 1
            assert events[0].reason_code == "STALE_ACTIVE_EXECUTION"
            assert events[0].payload == {
                "compose_project_name": "awf_pushing_running",
                "workspace_status": "pushing",
                "runtime": {
                    "stack_state": "running",
                    "reason": None,
                    "services": [
                        {
                            "name": "agent",
                            "container_id": "abc123",
                            "image": "awf-agent:latest",
                            "state": "running",
                            "status": "Up 2 minutes",
                            "health": "healthy",
                            "ports": [],
                            "started_at": None,
                        }
                    ],
                },
            }
        assert inspector.calls == ["awf_pushing_running", "awf_pushing_running"]

    @pytest.mark.unit
    async def test_current_in_memory_execution_task_is_not_touched(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "owned-running",
            WorkspaceStatus.running,
            compose_project_name="awf_owned_running",
        )
        inspector = _RecordingRuntimeInspector(
            {
                "awf_owned_running": RuntimeSnapshot(
                    stack_state="unavailable",
                    reason="docker unavailable",
                )
            }
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=1),
        )
        release = asyncio.Event()

        async def _busy() -> None:
            await release.wait()

        task = asyncio.create_task(_busy())
        worker._execution_tasks[workspace_id] = task
        try:
            assert await worker.run_once() == 0
            async with session_factory() as s:
                ws = await WorkspaceRepository(s).get(workspace_id)
                assert ws is not None
                assert ws.status == WorkspaceStatus.running.value
                assert ws.failure_reason is None
            assert inspector.calls == []
        finally:
            release.set()
            await asyncio.wait_for(task, timeout=0.2)
            worker._execution_tasks.pop(workspace_id, None)

    @pytest.mark.unit
    async def test_stale_active_execution_scan_is_limited_to_worker_node(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        local_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "local-running",
            WorkspaceStatus.running,
            compose_project_name="awf_local_running",
            node_id="node-a",
        )
        remote_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "remote-running",
            WorkspaceStatus.running,
            compose_project_name="awf_remote_running",
            node_id="node-b",
        )
        inspector = _RecordingRuntimeInspector(
            {
                "awf_local_running": RuntimeSnapshot(
                    stack_state="unavailable",
                    reason="docker unavailable",
                )
            }
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                node_id="node-a",
            ),
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            local_ws = await WorkspaceRepository(s).get(local_id)
            remote_ws = await WorkspaceRepository(s).get(remote_id)
            assert local_ws is not None
            assert remote_ws is not None
            assert local_ws.status == WorkspaceStatus.failed.value
            assert remote_ws.status == WorkspaceStatus.running.value
            assert remote_ws.failure_reason is None
        assert inspector.calls == ["awf_local_running"]

    @pytest.mark.unit
    async def test_monitoring_pr_is_not_touched_by_stale_active_execution_scan(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "monitoring-pr",
        )
        inspector = _RecordingRuntimeInspector({})
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=inspector,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=0),
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.failure_reason is None
        assert inspector.calls == []
        assert executor.resume_calls == []

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "status",
        [
            WorkspaceStatus.completed,
            WorkspaceStatus.failed,
            WorkspaceStatus.cancelled,
            WorkspaceStatus.destroyed,
        ],
    )
    async def test_terminal_rows_are_ignored(
        self,
        status: WorkspaceStatus,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_terminal_execution(
            session_factory,
            origin_repo,
            f"terminal-{status.value}",
            status,
        )
        inspector = _RecordingRuntimeInspector({})
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=0),
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == status.value
        assert inspector.calls == []
