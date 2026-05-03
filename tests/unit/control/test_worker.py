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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import (
    ControlWorker,
    WorkerConfig,
    _ActiveExecutionCandidate,
    _candidate_claim_is_stale,
    _claim_recheck_conditions,
    _scheduler_candidate_fetch_limit,
    _stale_active_execution_failure_message,
)
from awf.db.base import Base
from awf.db.enums import FailureReason, OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    OperationRepository,
    ProviderModelCircuitBreakerRepository,
    QueueDecisionRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_engine, make_session_factory
from awf.node.git_manager import GitManager
from awf.node.provisioner import Provisioner, ProvisionerConfig
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
from awf.service.controls import WorkspaceControlService
from awf.service.scheduler import scheduler_score_from_workspace
from awf.service.workspace_runtime_health import WorkspaceRuntimeFinding


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
    session_factory: async_sessionmaker[AsyncSession],
    origin: Path,
    title: str,
    *,
    create_task_attempt: bool = False,
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
        if create_task_attempt:
            task = await TaskRepository(s).create_or_get(
                repo_url=ws.repo_url,
                base_branch=ws.branch_base,
                title=ws.task_title,
                prompt=ws.task_prompt,
                external_id=None,
                idempotency_key=None,
                task_class=ws.task_class,
                owned_paths=list(ws.owned_paths),
            )
            await TaskAttemptRepository(s).create_for_workspace(task=task, workspace=ws)
        await s.commit()
        return ws.id


async def _create_ready(
    session_factory: async_sessionmaker[AsyncSession],
    origin: Path,
    title: str,
    *,
    agent: str = "codex",
    task_policy: dict[str, object] | None = None,
    task_class: str | None = None,
    create_task_attempt: bool = False,
) -> str:
    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url=str(origin),
            branch_base="development",
            task_title=title,
            task_prompt="p",
            agent=agent,
            test_commands=[],
            task_policy=task_policy,
            task_class=task_class,
        )
        if create_task_attempt:
            task = await TaskRepository(s).create_or_get(
                repo_url=ws.repo_url,
                base_branch=ws.branch_base,
                title=ws.task_title,
                prompt=ws.task_prompt,
                external_id=None,
                idempotency_key=None,
                task_class=ws.task_class,
                owned_paths=list(ws.owned_paths),
            )
            await TaskAttemptRepository(s).create_for_workspace(task=task, workspace=ws)
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
    agent: str = "codex",
    task_policy: dict[str, object] | None = None,
    task_class: str | None = None,
    create_task_attempt: bool = False,
    pr_number: int = 123,
    with_pr_url: bool = True,
    monitor_iter_count: int = 0,
    monitor_threads_addressed: dict[str, str] | None = None,
    monitor_last_commit_sha: str | None = None,
    monitor_started_at: datetime | None = None,
) -> str:
    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url=str(origin),
            branch_base="development",
            task_title=title,
            task_prompt="p",
            agent=agent,
            test_commands=[],
            task_policy=task_policy,
            task_class=task_class,
        )
        if create_task_attempt:
            task = await TaskRepository(s).create_or_get(
                repo_url=ws.repo_url,
                base_branch=ws.branch_base,
                title=ws.task_title,
                prompt=ws.task_prompt,
                external_id=None,
                idempotency_key=None,
                task_class=ws.task_class,
                owned_paths=list(ws.owned_paths),
            )
            await TaskAttemptRepository(s).create_for_workspace(task=task, workspace=ws)
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
        if with_pr_url:
            ws.pr_url = f"https://github.com/example/repo/pull/{pr_number}"
            ws.pr_number = pr_number
        await repo.transition(ws, to=WorkspaceStatus.monitoring_pr, reason_code="SEED")
        ws.monitor_iter_count = monitor_iter_count
        ws.monitor_threads_addressed = dict(monitor_threads_addressed or {})
        ws.monitor_last_commit_sha = monitor_last_commit_sha
        if monitor_started_at is not None:
            ws.monitor_started_at = monitor_started_at
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
    task_policy: dict[str, object] | None = None,
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
            task_policy=task_policy,
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
        await self.provision_claimed(workspace_id)

    async def provision_claimed(self, workspace_id: str) -> None:
        self.calls.append(workspace_id)
        async with self._session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            assert ws is not None
            if ws.status == WorkspaceStatus.requested.value:
                await repo.transition(ws, to=WorkspaceStatus.provisioning, reason_code="TEST")
            elif ws.status != WorkspaceStatus.provisioning.value:
                return
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

    async def execute(self, workspace_id: str, **_kwargs: object) -> None:
        self.calls.append(workspace_id)

    async def resume_pr_monitor(self, workspace_id: str) -> None:
        self.resume_calls.append(workspace_id)


class _BlockingMonitorExecutor(_RecordingExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def resume_pr_monitor(self, workspace_id: str) -> None:
        self.resume_calls.append(workspace_id)
        self.started.set()
        await self.release.wait()


class _RecordingRuntimeInspector:
    def __init__(self, snapshots: dict[str | None, RuntimeSnapshot]) -> None:
        self._snapshots = snapshots
        self.calls: list[str | None] = []

    async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
        self.calls.append(compose_project_name)
        return self._snapshots[compose_project_name]


class _HealthyRuntimeInspector:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
        self.calls.append(compose_project_name)
        return RuntimeSnapshot(
            stack_state="running",
            services=[
                RuntimeService(
                    name="agent",
                    container_id="agent",
                    image="awf-agent:latest",
                    state="running",
                )
            ],
        )


class _RaisingRuntimeInspector:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls: list[str | None] = []

    async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
        self.calls.append(compose_project_name)
        raise self.exc


async def _noop_project_stop(_compose_project_name: str | None) -> None:
    return None


class _UnexpectedCleaner:
    async def cleanup(self, **_kwargs: object) -> list[str]:
        raise AssertionError("remonitor must not run workspace cleanup")


def _unexpected_cleaner_factory() -> _UnexpectedCleaner:
    return _UnexpectedCleaner()


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

    @pytest.mark.unit
    async def test_requested_race_skip_does_not_record_ordered_decision(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        requested_id = await _create_requested(
            session_factory,
            origin_repo,
            "race-requested-ordering",
            create_task_attempt=True,
        )

        class _UnexpectedProvisioner:
            async def provision(self, workspace_id: str) -> None:
                raise AssertionError(f"unexpected provision call for {workspace_id}")

            async def provision_claimed(self, workspace_id: str) -> None:
                raise AssertionError(f"unexpected provision call for {workspace_id}")

        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_UnexpectedProvisioner(),  # type: ignore[arg-type]
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=1),
        )

        async def _race_after_filter(
            workspace_ids: list[str],
            *,
            expected: WorkspaceStatus,
            action: str,
        ) -> list[str]:
            assert workspace_ids == [requested_id]
            assert expected == WorkspaceStatus.requested
            assert action == "provision"
            async with session_factory() as session:
                repo = WorkspaceRepository(session)
                ws = await repo.transition_if_current(
                    requested_id,
                    from_status=WorkspaceStatus.requested,
                    to=WorkspaceStatus.provisioning,
                    reason_code="OTHER_WORKER_CLAIMED",
                )
                assert ws is not None
                await session.commit()
            return [requested_id]

        worker._filter_current_status = _race_after_filter  # type: ignore[method-assign]

        assert await worker.run_once() == 0

        async with session_factory() as session:
            decisions = await QueueDecisionRepository(session).list_for_workspace(requested_id)

        assert decisions == []

    @pytest.mark.unit
    async def test_requested_ordered_decision_failure_prevents_provision_dispatch(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        requested_id = await _create_requested(
            session_factory,
            origin_repo,
            "record-before-provision",
            create_task_attempt=True,
        )
        provisioner = _TransitioningProvisioner(session_factory)
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=1),
        )

        async def _fail_record_ordered_decisions(
            workspace_ids: list[str],
            *,
            reason_code: str,
        ) -> None:
            assert workspace_ids == [requested_id]
            assert reason_code == "ORDERED_REQUESTED_PROVISIONING"
            raise RuntimeError("ordered decision commit failed")

        worker._record_ordered_decisions = _fail_record_ordered_decisions  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="ordered decision commit failed"):
            await worker.run_once()

        assert provisioner.calls == []


class TestRunOnceExecution:
    @pytest.mark.unit
    async def test_ready_execution_dispatches_highest_scored_workspace_first(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        low_id = await _create_ready(
            session_factory,
            origin_repo,
            "low-priority-ready",
            task_class="refactor_task",
            task_policy={"scheduler": {"base_priority": 10}},
        )
        high_id = await _create_ready(
            session_factory,
            origin_repo,
            "high-priority-ready",
            task_class="refactor_task",
            task_policy={"scheduler": {"base_priority": 80}},
        )
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=0,
                max_concurrent_executions=1,
            ),
        )

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        assert executor.calls == [high_id]
        assert low_id not in executor.calls

    @pytest.mark.unit
    async def test_ready_execution_scores_beyond_fetch_window(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        low_ids = [
            await _create_ready(
                session_factory,
                origin_repo,
                f"low-priority-ready-{index}",
                task_class="docs_task",
                task_policy={"scheduler": {"base_priority": 0}},
            )
            for index in range(_scheduler_candidate_fetch_limit(1) + 1)
        ]
        urgent_id = await _create_ready(
            session_factory,
            origin_repo,
            "urgent-ready-after-fetch-window",
            task_class="migration_task",
            task_policy={"scheduler": {"base_priority": 100, "human_boost": 5}},
        )
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=0,
                max_concurrent_executions=1,
            ),
        )

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        assert executor.calls == [urgent_id]
        assert not set(low_ids).intersection(executor.calls)

    @pytest.mark.unit
    async def test_human_boosted_ready_workspace_wins_equal_priority_dispatch(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        ordinary_id = await _create_ready(
            session_factory,
            origin_repo,
            "ordinary-ready",
            task_class="test_task",
            task_policy={"scheduler": {"base_priority": 40}},
        )
        boosted_id = await _create_ready(
            session_factory,
            origin_repo,
            "boosted-ready",
            task_class="test_task",
            task_policy={"scheduler": {"base_priority": 40, "human_boost": 5}},
        )
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=0,
                max_concurrent_executions=1,
            ),
        )

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        assert executor.calls == [boosted_id]
        assert ordinary_id not in executor.calls

    @pytest.mark.unit
    async def test_retry_bonus_does_not_outrank_materially_higher_priority_new_work(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        retry_id = await _create_ready(
            session_factory,
            origin_repo,
            "infra-retry-ready",
            task_class="refactor_task",
            task_policy={
                "scheduler": {
                    "base_priority": 40,
                    "parent_failure_reason": FailureReason.infrastructure_failure.value,
                }
            },
            create_task_attempt=True,
        )
        high_priority_id = await _create_ready(
            session_factory,
            origin_repo,
            "higher-priority-new-ready",
            task_class="refactor_task",
            task_policy={"scheduler": {"base_priority": 50}},
            create_task_attempt=True,
        )
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=0,
                max_concurrent_executions=1,
            ),
        )

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        assert executor.calls == [high_priority_id]
        async with session_factory() as session:
            retry_workspace = await WorkspaceRepository(session).get(retry_id)
            assert retry_workspace is not None
            retry_score = scheduler_score_from_workspace(retry_workspace)
            retry_decisions = await QueueDecisionRepository(session).list_for_workspace(retry_id)
            high_decisions = await QueueDecisionRepository(session).list_for_workspace(
                high_priority_id
            )

        assert retry_score.retry_bonus == 3
        assert retry_decisions == []
        assert high_decisions[0].decision == "ordered"
        assert high_decisions[0].score_summary["effective_score"] == 54

    @pytest.mark.unit
    async def test_ordered_decision_is_recorded_when_ready_workspace_dispatches(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        ready_id = await _create_ready(
            session_factory,
            origin_repo,
            "record-ordered-ready",
            task_class="refactor_task",
            task_policy={"scheduler": {"base_priority": 33}},
            create_task_attempt=True,
        )
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=0,
                max_concurrent_executions=1,
            ),
        )

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        async with session_factory() as session:
            decisions = await QueueDecisionRepository(session).list_for_workspace(ready_id)

        assert decisions[0].decision == "ordered"
        assert decisions[0].reason_code == "ORDERED_READY_EXECUTION"
        assert decisions[0].score_summary["base_priority"] == 33

    @pytest.mark.unit
    async def test_ready_ordered_decision_failure_prevents_execution_dispatch(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        ready_id = await _create_ready(
            session_factory,
            origin_repo,
            "record-before-execute",
            create_task_attempt=True,
        )
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=0,
                max_concurrent_executions=1,
            ),
        )

        async def _fail_record_ordered_decisions(
            workspace_ids: list[str],
            *,
            reason_code: str,
        ) -> None:
            if not workspace_ids:
                return
            assert workspace_ids == [ready_id]
            assert reason_code == "ORDERED_READY_EXECUTION"
            raise RuntimeError("ordered decision commit failed")

        worker._record_ordered_decisions = _fail_record_ordered_decisions  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="ordered decision commit failed"):
            await worker.run_once()
        await worker.wait_for_execution_tasks()

        assert executor.calls == []

    @pytest.mark.unit
    async def test_ordered_decisions_avoid_per_workspace_attempt_and_decision_reads(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        ready_ids = [
            await _create_ready(
                session_factory,
                origin_repo,
                f"record-ordered-ready-{index}",
                task_class="refactor_task",
                task_policy={"scheduler": {"base_priority": 10 + index}},
                create_task_attempt=True,
            )
            for index in range(2)
        ]
        task_attempt_point_selects: list[str] = []
        queue_decision_point_selects: list[str] = []

        def _capture_ordered_decision_selects(
            _conn: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            normalized = statement.upper()
            if not normalized.lstrip().startswith("SELECT"):
                return
            if (
                "FROM TASK_ATTEMPTS" in normalized
                and "WHERE TASK_ATTEMPTS.WORKSPACE_ID = " in normalized
            ):
                task_attempt_point_selects.append(statement)
            if (
                "FROM QUEUE_DECISIONS" in normalized
                and "WHERE QUEUE_DECISIONS.WORKSPACE_ID = " in normalized
            ):
                queue_decision_point_selects.append(statement)

        engine = session_factory.kw["bind"]
        event.listen(
            engine.sync_engine,
            "before_cursor_execute",
            _capture_ordered_decision_selects,
        )
        try:
            executor = _RecordingExecutor()
            worker = ControlWorker(
                session_factory=session_factory,
                provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
                executor=executor,
                runtime_inspector=_HealthyRuntimeInspector(),
                config=WorkerConfig(
                    poll_interval_seconds=0.01,
                    max_concurrent_provisions=0,
                    max_concurrent_executions=2,
                ),
            )

            async def _skip_runtime_recovery_scan() -> None:
                return None

            worker._maybe_recover_stale_active_executions = (  # type: ignore[method-assign]
                _skip_runtime_recovery_scan
            )

            assert await worker.run_once() == 2
            await worker.wait_for_execution_tasks()
        finally:
            event.remove(
                engine.sync_engine,
                "before_cursor_execute",
                _capture_ordered_decision_selects,
            )

        assert set(executor.calls) == set(ready_ids)
        assert task_attempt_point_selects == []
        assert queue_decision_point_selects == []

    @pytest.mark.unit
    async def test_provider_cooldown_defer_does_not_consume_ready_execution_limit(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        not_before = datetime.now(UTC) + timedelta(minutes=10)
        cooling_id = await _create_ready(
            session_factory,
            origin_repo,
            "cooling-ready",
            agent="gemini",
            task_class="refactor_task",
            task_policy={
                "agent_model": "gemini-2.5-pro",
                "scheduler": {"base_priority": 100},
                "provider_recovery_state": {
                    "not_before": not_before.isoformat(),
                    "action": "retry",
                },
            },
            create_task_attempt=True,
        )
        allowed_id = await _create_ready(
            session_factory,
            origin_repo,
            "allowed-ready",
            task_class="refactor_task",
            task_policy={"scheduler": {"base_priority": 10}},
            create_task_attempt=True,
        )
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=0,
                max_concurrent_executions=1,
            ),
        )

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        assert executor.calls == [allowed_id]
        async with session_factory() as session:
            deferred = await QueueDecisionRepository(session).list_for_workspace(cooling_id)

        assert deferred[0].decision == "deferred"
        assert deferred[0].reason_code == "PROVIDER_RECOVERY_NOT_BEFORE"
        assert deferred[0].score_summary["suppression"]["suppressed"] is True

    @pytest.mark.unit
    async def test_provider_cooldown_suppression_scans_past_fetch_buffer_to_fill_limit(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        not_before = datetime.now(UTC) + timedelta(minutes=10)
        for index in range(_scheduler_candidate_fetch_limit(1)):
            await _create_ready(
                session_factory,
                origin_repo,
                f"cooling-ready-{index}",
                agent="gemini",
                task_class="refactor_task",
                task_policy={
                    "agent_model": "gemini-2.5-pro",
                    "scheduler": {"base_priority": 100},
                    "provider_recovery_state": {
                        "not_before": not_before.isoformat(),
                        "action": "retry",
                    },
                },
            )
        allowed_id = await _create_ready(
            session_factory,
            origin_repo,
            "allowed-after-cooldown-buffer",
            task_class="refactor_task",
            task_policy={"scheduler": {"base_priority": 1}},
        )
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=0,
                max_concurrent_executions=1,
            ),
        )

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        assert executor.calls == [allowed_id]

    @pytest.mark.unit
    async def test_provider_recovery_filter_reuses_schedulable_workspace_rows(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        not_before = datetime.now(UTC) + timedelta(minutes=10)
        await _create_ready(
            session_factory,
            origin_repo,
            "cooling-ready",
            agent="gemini",
            task_class="refactor_task",
            task_policy={
                "agent_model": "gemini-2.5-pro",
                "scheduler": {"base_priority": 100},
                "provider_recovery_state": {
                    "not_before": not_before.isoformat(),
                    "action": "retry",
                },
            },
        )
        allowed_id = await _create_ready(
            session_factory,
            origin_repo,
            "allowed-ready",
            task_class="refactor_task",
            task_policy={"scheduler": {"base_priority": 1}},
        )
        workspace_selects: list[str] = []

        def _capture_workspace_select(
            _conn: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            normalized = statement.upper()
            if (
                normalized.lstrip().startswith("SELECT")
                and "FROM WORKSPACES" in normalized
            ):
                workspace_selects.append(statement)

        engine = session_factory.kw["bind"]
        event.listen(engine.sync_engine, "before_cursor_execute", _capture_workspace_select)
        try:
            worker = ControlWorker(
                session_factory=session_factory,
                provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
                executor=_RecordingExecutor(),
                runtime_inspector=_HealthyRuntimeInspector(),
                config=WorkerConfig(
                    poll_interval_seconds=0.01,
                    max_concurrent_provisions=0,
                    max_concurrent_executions=1,
                ),
            )

            assert await worker._list_ready(limit=1) == [allowed_id]
        finally:
            event.remove(
                engine.sync_engine,
                "before_cursor_execute",
                _capture_workspace_select,
            )

        assert len(workspace_selects) == 1

    @pytest.mark.unit
    async def test_provider_cooldown_refill_uses_cursor_without_growing_exclusions(
        self,
        monkeypatch: pytest.MonkeyPatch,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        not_before = datetime.now(UTC) + timedelta(minutes=10)
        suppressed_ids = [
            await _create_ready(
                session_factory,
                origin_repo,
                f"cooling-ready-{index}",
                agent="gemini",
                task_class="refactor_task",
                task_policy={
                    "agent_model": "gemini-2.5-pro",
                    "scheduler": {"base_priority": 100},
                    "provider_recovery_state": {
                        "not_before": not_before.isoformat(),
                        "action": "retry",
                    },
                },
            )
            for index in range(_scheduler_candidate_fetch_limit(1))
        ]
        allowed_id = await _create_ready(
            session_factory,
            origin_repo,
            "allowed-after-cooldown-buffer",
            task_class="refactor_task",
            task_policy={"scheduler": {"base_priority": 1}},
        )
        ordered_ids = [*suppressed_ids, allowed_id]
        base_exclude_ids = {"active-workspace"}
        created_at_by_id: dict[str, datetime] = {}
        base_created_at = datetime(2026, 1, 1)
        async with session_factory() as session:
            for index, workspace_id in enumerate(ordered_ids):
                created_at = base_created_at + timedelta(seconds=index)
                created_at_by_id[workspace_id] = created_at
                await session.execute(
                    update(Workspace)
                    .where(Workspace.id == workspace_id)
                    .values(created_at=created_at, updated_at=created_at)
                )
            await session.commit()
        queries: list[tuple[tuple[datetime, str] | None, set[str]]] = []

        async def _list_schedulable_workspaces(
            self: WorkspaceRepository,
            *,
            status: WorkspaceStatus,
            limit: int,
            exclude_ids: set[str] | None = None,
            after: tuple[datetime, str] | None = None,
        ) -> list[Workspace]:
            assert status == WorkspaceStatus.ready
            excluded = set(exclude_ids or set())
            queries.append((after, excluded))
            visible = [
                workspace_id for workspace_id in ordered_ids if workspace_id not in excluded
            ]
            if after is not None:
                visible = [
                    workspace_id
                    for workspace_id in visible
                    if (created_at_by_id[workspace_id], workspace_id) > after
                ]
            visible = visible[:limit]
            if not visible:
                return []
            result = await self._session.execute(
                select(Workspace).where(Workspace.id.in_(visible))
            )
            rows = {workspace.id: workspace for workspace in result.scalars()}
            return [rows[workspace_id] for workspace_id in visible]

        monkeypatch.setattr(
            WorkspaceRepository,
            "list_schedulable_workspaces",
            _list_schedulable_workspaces,
            raising=False,
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=0,
                max_concurrent_executions=1,
            ),
        )

        assert await worker._list_ready(limit=1, exclude_ids=base_exclude_ids) == [allowed_id]
        assert queries == [
            (None, base_exclude_ids),
            (
                (
                    created_at_by_id[suppressed_ids[-1]],
                    suppressed_ids[-1],
                ),
                base_exclude_ids,
            ),
        ]

    @pytest.mark.unit
    async def test_provider_model_circuit_defer_records_decision_and_fills_limit(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        suppressed_ids: list[str] = []
        for index in range(_scheduler_candidate_fetch_limit(1)):
            suppressed_ids.append(
                await _create_ready(
                    session_factory,
                    origin_repo,
                    f"circuit-ready-{index}",
                    agent="gemini",
                    task_class="refactor_task",
                    task_policy={
                        "agent_model": "gemini-2.5-pro",
                        "scheduler": {"base_priority": 100},
                    },
                    create_task_attempt=index == 0,
                )
            )
        allowed_id = await _create_ready(
            session_factory,
            origin_repo,
            "allowed-after-circuit-buffer",
            task_class="refactor_task",
            task_policy={"scheduler": {"base_priority": 1}},
            create_task_attempt=True,
        )
        async with session_factory() as session:
            await ProviderModelCircuitBreakerRepository(session).record_failure(
                provider="google",
                model="gemini-2.5-pro",
                reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
                failure_fingerprint="capacity:fingerprint",
                workspace_id=suppressed_ids[0],
                attempt_id=None,
                now=datetime.now(UTC),
                failure_threshold=1,
                cooldown_seconds=600,
            )
            await session.commit()

        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=0,
                max_concurrent_executions=1,
            ),
        )

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        assert executor.calls == [allowed_id]
        async with session_factory() as session:
            decisions = await QueueDecisionRepository(session).list_for_workspace(
                suppressed_ids[0]
            )

        assert decisions[0].decision == "deferred"
        assert decisions[0].reason_code == "PROVIDER_MODEL_CIRCUIT_OPEN"
        assert decisions[0].score_summary["suppression"]["suppressed"] is True
        assert decisions[0].score_summary["suppression"]["reason_code"] == (
            "PROVIDER_MODEL_CIRCUIT_OPEN"
        )
        assert decisions[0].score_summary["suppression"]["provider"] == "google"
        assert decisions[0].score_summary["suppression"]["model"] == "gemini-2.5-pro"
        assert isinstance(decisions[0].score_summary["suppression"]["cooldown_until"], str)

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
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=3),
        )

        dispatched = await worker.run_once()
        await worker.wait_for_execution_tasks()

        assert dispatched == 2
        assert provisioner.calls == [requested_id]
        assert set(executor.calls) == {ready_id, requested_id}

    @pytest.mark.unit
    async def test_ready_execution_skips_open_provider_model_circuit_without_event(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        ready_id = await _create_ready(
            session_factory,
            origin_repo,
            "gemini-ready",
            agent="gemini",
            task_policy={"agent_model": "gemini-2.5-pro"},
        )
        async with session_factory() as session:
            await ProviderModelCircuitBreakerRepository(session).record_failure(
                provider="google",
                model="gemini-2.5-pro",
                reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
                failure_fingerprint="capacity:fingerprint",
                workspace_id="ws_previous",
                attempt_id=None,
                now=datetime.now(UTC),
                failure_threshold=1,
                cooldown_seconds=600,
            )
            await session.commit()

        provisioner = _TransitioningProvisioner(session_factory)
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=3),
        )

        dispatched = await worker.run_once()
        await worker.wait_for_execution_tasks()

        assert dispatched == 0
        assert executor.calls == []
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get(ready_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.ready.value
            cooldown_events = [
                event
                for event in ws.events
                if event.event_type == "workspace.provider_recovery_cooldown"
            ]
        assert cooldown_events == []

    @pytest.mark.unit
    async def test_ready_execution_batches_provider_model_circuit_lookup(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        ready_ids = [
            await _create_ready(
                session_factory,
                origin_repo,
                f"gemini-ready-{index}",
                agent="gemini",
                task_policy={"agent_model": f"gemini-2.5-pro-{index}"},
            )
            for index in range(5)
        ]
        async with session_factory() as session:
            breaker_repo = ProviderModelCircuitBreakerRepository(session)
            for index, workspace_id in enumerate(ready_ids):
                await breaker_repo.record_failure(
                    provider="google",
                    model=f"gemini-2.5-pro-{index}",
                    reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
                    failure_fingerprint=f"capacity:fingerprint:{index}",
                    workspace_id=workspace_id,
                    attempt_id=None,
                    now=datetime.now(UTC),
                    failure_threshold=1,
                    cooldown_seconds=600,
                )
            await session.commit()

        breaker_selects: list[str] = []

        def _capture_breaker_select(
            _conn: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            normalized = statement.upper()
            if (
                normalized.lstrip().startswith("SELECT")
                and "FROM PROVIDER_MODEL_CIRCUIT_BREAKERS" in normalized
            ):
                breaker_selects.append(statement)

        engine = session_factory.kw["bind"]
        event.listen(engine.sync_engine, "before_cursor_execute", _capture_breaker_select)
        try:
            executor = _RecordingExecutor()
            worker = ControlWorker(
                session_factory=session_factory,
                provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
                executor=executor,
                runtime_inspector=_HealthyRuntimeInspector(),
                config=WorkerConfig(
                    poll_interval_seconds=0.01,
                    max_concurrent_provisions=1,
                    max_concurrent_executions=5,
                ),
            )

            assert await worker.run_once() == 0
            await worker.wait_for_execution_tasks()
        finally:
            event.remove(
                engine.sync_engine,
                "before_cursor_execute",
                _capture_breaker_select,
            )

        assert executor.calls == []
        assert len(breaker_selects) == 1

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

            async def execute(self, workspace_id: str, **_kwargs: object) -> None:
                self.calls.append(workspace_id)
                started.set()
                await release.wait()

        executor = _BlockingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=1,
                max_concurrent_executions=1,
            ),
        )

        assert await asyncio.wait_for(worker.run_once(), timeout=1.0) == 1
        await asyncio.wait_for(started.wait(), timeout=1.0)

        requested_id = await _create_requested(session_factory, origin_repo, "new-request")
        assert await asyncio.wait_for(worker.run_once(), timeout=1.0) == 1
        assert provisioner.calls == [requested_id]
        assert executor.calls == [ready_id]

        release.set()
        await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=1.0)

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
            runtime_inspector=_HealthyRuntimeInspector(),
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
            await asyncio.wait_for(active_task, timeout=1.0)
            worker._execution_tasks.pop("busy", None)

    @pytest.mark.unit
    async def test_schedulable_statuses_are_listed_through_repository(
        self,
        monkeypatch: pytest.MonkeyPatch,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        queries: list[tuple[WorkspaceStatus, int, set[str]]] = []

        async def _list_schedulable_workspaces(
            self: WorkspaceRepository,
            *,
            status: WorkspaceStatus,
            limit: int,
            exclude_ids: set[str] | None = None,
            after: tuple[datetime, str] | None = None,
        ) -> list[Workspace]:
            del self, after
            queries.append((status, limit, set(exclude_ids or set())))
            return []

        monkeypatch.setattr(
            WorkspaceRepository,
            "list_schedulable_workspaces",
            _list_schedulable_workspaces,
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
            (WorkspaceStatus.requested, 8, set()),
            (WorkspaceStatus.monitoring_pr, 16, set()),
            (WorkspaceStatus.ready, 16, set()),
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
            async def execute(self, workspace_id: str, **_kwargs: object) -> None:
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
            runtime_inspector=_HealthyRuntimeInspector(),
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

            async def execute(self, workspace_id: str, **_kwargs: object) -> None:
                self.calls.append(workspace_id)
                if workspace_id == first_id:
                    raise RuntimeError("boom")

        executor = _FlakyExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=_HealthyRuntimeInspector(),
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
                await self.provision_claimed(workspace_id)

            async def provision_claimed(self, workspace_id: str) -> None:
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
        await asyncio.wait_for(started.wait(), timeout=1.0)
        release.set()
        await asyncio.wait_for(asyncio.gather(*runs), timeout=1.0)

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
            async def execute(self, workspace_id: str, **_kwargs: object) -> None:
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
        inspector = _RecordingRuntimeInspector(
            {f"awf_{ready_id}": RuntimeSnapshot(stack_state="unavailable", reason="bypass")}
        )
        worker_a = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=inspector,
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
            runtime_inspector=inspector,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=1,
                max_concurrent_executions=1,
            ),
        )
        worker_a._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001
        worker_b._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001

        await asyncio.gather(worker_a.run_once(), worker_b.run_once())
        await asyncio.wait_for(started.wait(), timeout=1.0)
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
        inspector = _RecordingRuntimeInspector(
            {
                f"awf_{monitor_id}": RuntimeSnapshot(
                    stack_state="running",
                    services=[
                        RuntimeService(
                            name="agent",
                            container_id="agent",
                            image="awf-agent:latest",
                            state="running",
                        )
                    ],
                )
            }
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=inspector,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=3),
        )

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        assert executor.calls == []
        assert executor.resume_calls == [monitor_id]

    @pytest.mark.unit
    async def test_monitor_ordered_decision_failure_prevents_resume_dispatch(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "record-before-monitor-resume",
            create_task_attempt=True,
        )
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=1),
        )

        async def _fail_record_ordered_decisions(
            workspace_ids: list[str],
            *,
            reason_code: str,
        ) -> None:
            assert workspace_ids == [monitor_id]
            assert reason_code == "ORDERED_MONITOR_RESUME"
            raise RuntimeError("ordered decision commit failed")

        worker._record_ordered_decisions = _fail_record_ordered_decisions  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="ordered decision commit failed"):
            await worker.run_once()
        await worker.wait_for_execution_tasks()

        assert executor.calls == []
        assert executor.resume_calls == []

    @pytest.mark.unit
    async def test_monitoring_pr_provider_recovery_cooldown_suppresses_resume_without_event(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        not_before = datetime.now(UTC) + timedelta(minutes=10)
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "provider-cooling-monitor",
            agent="gemini",
            task_policy={
                "agent_model": "gemini-2.5-pro",
                "provider_recovery_state": {
                    "not_before": not_before.isoformat(),
                    "action": "fallback",
                },
            },
        )
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=3),
        )

        assert await worker.run_once() == 0
        await worker.wait_for_execution_tasks()

        assert executor.resume_calls == []
        async with session_factory() as session:
            workspace = await WorkspaceRepository(session).get(monitor_id)
            assert workspace is not None
            cooldown_events = [
                event
                for event in workspace.events
                if event.event_type == "workspace.provider_recovery_cooldown"
            ]
        assert cooldown_events == []

    @pytest.mark.unit
    async def test_monitoring_pr_in_place_fallback_resumes_monitor_not_feature_execution(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "provider-fallback-monitor",
            agent="codex",
            task_policy={
                "agent_model": "gpt-5.3-codex",
                "provider_recovery_state": {
                    "action": "fallback",
                    "decision_reason_code": "PROVIDER_FALLBACK_SELECTED",
                    "source_workspace_id": "ws_source",
                    "source_provider": "google",
                    "source_model": "gemini-2.5-pro",
                    "target_agent": "codex",
                    "target_provider": "openai",
                    "target_model": "gpt-5.3-codex",
                    "fallback_attempt_number": 1,
                    "retry_attempt_number": 0,
                },
            },
            pr_number=169,
            monitor_iter_count=4,
            monitor_threads_addressed={"thread-1": "fix_committed"},
            monitor_last_commit_sha="e" * 40,
        )
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=3),
        )

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        assert executor.calls == []
        assert executor.resume_calls == [monitor_id]
        async with session_factory() as session:
            requested_ids = await WorkspaceRepository(session).list_schedulable_ids(
                status=WorkspaceStatus.requested,
                limit=10,
            )
            workspace = await WorkspaceRepository(session).get(monitor_id)
            operations = await OperationRepository(session).list_all(
                workspace_id=monitor_id
            )
            events = await WorkspaceEventRepository(session).list(
                workspace_id=monitor_id,
                event_type="workspace.monitor_recovery_started",
            )

        assert requested_ids == []
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.monitoring_pr.value
        assert workspace.task_policy["provider_recovery_state"]["action"] == "fallback"
        assert len(operations) == 1
        assert operations[0].type == OperationType.remonitor.value
        assert operations[0].payload["pr_url"] == "https://github.com/example/repo/pull/169"
        assert operations[0].payload["pr_number"] == 169
        assert operations[0].payload["monitor_state"] == {
            "monitor_started_at": operations[0].payload["monitor_state"][
                "monitor_started_at"
            ],
            "monitor_iter_count": 4,
            "monitor_threads_addressed_count": 1,
            "monitor_last_commit_sha": "e" * 40,
        }
        assert len(events) == 1
        assert events[0].payload["pr_url"] == "https://github.com/example/repo/pull/169"
        assert events[0].payload["pr_number"] == 169

    @pytest.mark.unit
    async def test_fresh_worker_records_recovery_operation_when_resuming_monitoring_pr(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        monitor_started_at = datetime.now(UTC) - timedelta(minutes=20)
        monitor_threads = {"thread-1": "fix_committed", "thread-2": "defer"}
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "restart-recoverable-monitor",
            pr_number=456,
            monitor_iter_count=9,
            monitor_threads_addressed=monitor_threads,
            monitor_last_commit_sha="d" * 40,
            monitor_started_at=monitor_started_at,
        )
        executor = _RecordingExecutor()
        inspector = _RecordingRuntimeInspector(
            {
                f"awf_{monitor_id}": RuntimeSnapshot(
                    stack_state="running",
                    services=[
                        RuntimeService(
                            name="agent",
                            container_id="agent",
                            image="awf-agent:latest",
                            state="running",
                        )
                    ],
                )
            }
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=inspector,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=3),
        )

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        assert executor.calls == []
        assert executor.resume_calls == [monitor_id]
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.pr_url == "https://github.com/example/repo/pull/456"
            assert ws.pr_number == 456
            assert ws.monitor_iter_count == 9
            assert ws.monitor_threads_addressed == monitor_threads
            assert ws.monitor_last_commit_sha == "d" * 40
            assert ws.monitor_started_at is not None
            assert ws.monitor_started_at.replace(tzinfo=UTC) == monitor_started_at
            operations = await OperationRepository(s).list_all(workspace_id=monitor_id)
            events = await WorkspaceEventRepository(s).list(workspace_id=monitor_id)

        remonitor_operations = [
            operation
            for operation in operations
            if operation.type == OperationType.remonitor.value
        ]
        assert len(remonitor_operations) == 1
        operation = remonitor_operations[0]
        assert operation.status == OperationStatus.succeeded.value
        assert operation.payload is not None
        assert operation.payload["source"] == "worker_restart"
        assert operation.payload["owner"] == "control_worker"
        assert operation.payload["requested_action"] == OperationType.remonitor.value
        assert operation.payload["reason_code"] == "MONITOR_RECOVERY_AFTER_RESTART"
        assert operation.payload["pr_url"] == "https://github.com/example/repo/pull/456"
        assert operation.payload["pr_number"] == 456
        assert operation.payload["worker_id"].startswith("control-worker-")
        assert operation.payload["previous_claim"] == {
            "monitor_claimed_by": None,
            "monitor_claim_expires_at": None,
            "execution_claimed_by": None,
            "execution_claim_expires_at": None,
        }
        assert operation.payload["runtime_stranding_reason"] is None
        assert operation.result is not None
        assert operation.result["status"] == WorkspaceStatus.monitoring_pr.value

        recovery_events = [
            event
            for event in events
            if event.event_type == "workspace.monitor_recovery_started"
        ]
        assert len(recovery_events) == 1
        assert recovery_events[0].reason_code == "MONITOR_RECOVERY_AFTER_RESTART"
        assert recovery_events[0].payload is not None
        assert recovery_events[0].payload["operation_id"] == operation.id
        assert recovery_events[0].payload["monitor_state"] == {
            "monitor_started_at": monitor_started_at.isoformat(),
            "monitor_iter_count": 9,
            "monitor_threads_addressed_count": 2,
            "monitor_last_commit_sha": "d" * 40,
        }

    @pytest.mark.unit
    async def test_restart_recovery_clears_stale_execution_claim_and_records_monitor_claim_acquisition(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stale_execution_expires_at = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "monitor-with-stale-execution-claim",
            pr_number=457,
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            ws.execution_claimed_by = "dead-execution-worker"
            ws.execution_claim_expires_at = stale_execution_expires_at
            await s.commit()

        original_claim_monitoring_pr = WorkspaceRepository.claim_monitoring_pr
        claim_cutoffs: list[datetime | None] = []

        async def claim_monitoring_pr_with_cutoff_spy(
            self: WorkspaceRepository,
            workspace_id: str,
            *,
            owner_id: str,
            lease_expires_at: datetime,
            now: datetime | None = None,
            clear_stale_execution_claim_cutoff: datetime | None = None,
        ) -> bool:
            claim_cutoffs.append(clear_stale_execution_claim_cutoff)
            return await original_claim_monitoring_pr(
                self,
                workspace_id,
                owner_id=owner_id,
                lease_expires_at=lease_expires_at,
                now=now,
                clear_stale_execution_claim_cutoff=clear_stale_execution_claim_cutoff,
            )

        monkeypatch.setattr(
            WorkspaceRepository,
            "claim_monitoring_pr",
            claim_monitoring_pr_with_cutoff_spy,
        )

        executor = _BlockingMonitorExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=1,
                node_id="worker-node-a",
            ),
        )

        assert await asyncio.wait_for(worker.run_once(), timeout=1.0) == 1
        await asyncio.wait_for(executor.started.wait(), timeout=1.0)

        assert len(claim_cutoffs) == 1
        assert claim_cutoffs[0] is not None
        assert executor.calls == []
        assert executor.resume_calls == [monitor_id]
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.execution_claimed_by is None
            assert ws.execution_claim_expires_at is None
            assert ws.monitor_claimed_by == worker._worker_id
            assert ws.monitor_claim_expires_at is not None
            operations = await OperationRepository(s).list_all(workspace_id=monitor_id)
            recovery_events = await WorkspaceEventRepository(s).list(
                workspace_id=monitor_id,
                event_type="workspace.monitor_recovery_started",
            )

        remonitor_operations = [
            operation
            for operation in operations
            if operation.type == OperationType.remonitor.value
        ]
        assert len(remonitor_operations) == 1
        operation = remonitor_operations[0]
        assert operation.status == OperationStatus.running.value
        assert operation.payload is not None
        assert operation.payload["previous_claim"] == {
            "monitor_claimed_by": None,
            "monitor_claim_expires_at": None,
            "execution_claimed_by": "dead-execution-worker",
            "execution_claim_expires_at": stale_execution_expires_at.isoformat(),
        }
        assert operation.payload["claim_cleanup"]["execution_claim"] == {
            "action": "cleared_stale",
            "reason_code": "STALE_EXECUTION_CLAIM_CLEARED_DURING_MONITOR_RECOVERY",
            "previous_claimed_by": "dead-execution-worker",
            "previous_expires_at": stale_execution_expires_at.isoformat(),
        }
        assert operation.payload["claim_cleanup"]["monitor_claim"]["action"] == "acquired"
        assert operation.payload["claim_cleanup"]["monitor_claim"]["reason_code"] == (
            "MONITOR_CLAIM_ACQUIRED_DURING_MONITOR_RECOVERY"
        )
        assert operation.payload["claim_cleanup"]["monitor_claim"]["claimed_by"] == (
            worker._worker_id
        )
        assert len(recovery_events) == 1
        assert recovery_events[0].payload is not None
        assert recovery_events[0].payload["operation_id"] == operation.id
        assert recovery_events[0].payload["claim_cleanup"] == operation.payload[
            "claim_cleanup"
        ]

        executor.release.set()
        await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=1.0)

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            assert ws.execution_claimed_by is None
            assert ws.execution_claim_expires_at is None
            assert ws.monitor_claimed_by is None
            assert ws.monitor_claim_expires_at is None
            operations = await OperationRepository(s).list_all(workspace_id=monitor_id)

        remonitor_operations = [
            operation
            for operation in operations
            if operation.type == OperationType.remonitor.value
        ]
        assert len(remonitor_operations) == 1
        assert remonitor_operations[0].status == OperationStatus.succeeded.value

    @pytest.mark.unit
    async def test_restart_recovery_cleans_orphaned_execution_expiry_without_reporting_claim_clear(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        orphaned_execution_expires_at = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "monitor-with-orphaned-execution-expiry",
            pr_number=458,
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            ws.execution_claimed_by = None
            ws.execution_claim_expires_at = orphaned_execution_expires_at
            await s.commit()

        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=1,
                node_id="worker-node-a",
            ),
        )

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        assert executor.calls == []
        assert executor.resume_calls == [monitor_id]
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            assert ws.execution_claimed_by is None
            assert ws.execution_claim_expires_at is None
            operations = await OperationRepository(s).list_all(workspace_id=monitor_id)
            recovery_events = await WorkspaceEventRepository(s).list(
                workspace_id=monitor_id,
                event_type="workspace.monitor_recovery_started",
            )

        remonitor_operations = [
            operation
            for operation in operations
            if operation.type == OperationType.remonitor.value
        ]
        assert len(remonitor_operations) == 1
        operation = remonitor_operations[0]
        assert operation.payload is not None
        assert operation.payload["previous_claim"] == {
            "monitor_claimed_by": None,
            "monitor_claim_expires_at": None,
            "execution_claimed_by": None,
            "execution_claim_expires_at": orphaned_execution_expires_at.isoformat(),
        }
        assert operation.payload["claim_cleanup"]["execution_claim"] == {
            "action": "none",
            "reason_code": "NO_EXECUTION_CLAIM_DURING_MONITOR_RECOVERY",
            "previous_claimed_by": None,
            "previous_expires_at": orphaned_execution_expires_at.isoformat(),
        }
        assert len(recovery_events) == 1
        assert recovery_events[0].payload is not None
        assert recovery_events[0].payload["claim_cleanup"] == operation.payload[
            "claim_cleanup"
        ]

    @pytest.mark.unit
    async def test_restart_recovery_rechecks_execution_claim_before_stale_clear(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        stale_execution_expires_at = datetime.now(UTC) - timedelta(minutes=5)
        refreshed_execution_expires_at = datetime.now(UTC) + timedelta(minutes=10)
        refreshed_execution_owner = "fresh-execution-worker"
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "monitor-with-concurrent-execution-refresh",
            pr_number=460,
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            ws.execution_claimed_by = "dead-execution-worker"
            ws.execution_claim_expires_at = stale_execution_expires_at
            await s.commit()

        original_claim_monitoring_pr = WorkspaceRepository.claim_monitoring_pr

        async def claim_after_execution_refresh(
            self: WorkspaceRepository,
            workspace_id: str,
            *,
            owner_id: str,
            lease_expires_at: datetime,
            now: datetime | None = None,
            clear_stale_execution_claim_cutoff: datetime | None = None,
        ) -> bool:
            await self._session.execute(
                update(Workspace)
                .where(Workspace.id == workspace_id)
                .values(
                    execution_claimed_by=refreshed_execution_owner,
                    execution_claim_expires_at=refreshed_execution_expires_at,
                )
                .execution_options(synchronize_session=False)
            )
            return await original_claim_monitoring_pr(
                self,
                workspace_id,
                owner_id=owner_id,
                lease_expires_at=lease_expires_at,
                now=now,
                clear_stale_execution_claim_cutoff=clear_stale_execution_claim_cutoff,
            )

        monkeypatch.setattr(
            WorkspaceRepository,
            "claim_monitoring_pr",
            claim_after_execution_refresh,
        )

        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=1,
                node_id="worker-node-a",
            ),
        )

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        assert executor.resume_calls == [monitor_id]
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            assert ws.execution_claimed_by == refreshed_execution_owner
            assert ws.execution_claim_expires_at is not None
            assert ws.execution_claim_expires_at.replace(tzinfo=UTC) == (
                refreshed_execution_expires_at
            )
            operations = await OperationRepository(s).list_all(workspace_id=monitor_id)

        remonitor_operations = [
            operation
            for operation in operations
            if operation.type == OperationType.remonitor.value
        ]
        assert len(remonitor_operations) == 1
        assert remonitor_operations[0].payload is not None
        assert remonitor_operations[0].payload["claim_cleanup"]["execution_claim"] == {
            "action": "preserved_unexpired",
            "reason_code": "UNEXPIRED_EXECUTION_CLAIM_PRESERVED_DURING_MONITOR_RECOVERY",
            "previous_claimed_by": refreshed_execution_owner,
            "previous_expires_at": refreshed_execution_expires_at.isoformat(),
        }

    @pytest.mark.unit
    async def test_restart_recovery_preserves_unexpired_execution_claim_but_reports_it(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        execution_expires_at = datetime.now(UTC) + timedelta(minutes=10)
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "monitor-with-unexpired-execution-claim",
            pr_number=459,
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            ws.execution_claimed_by = "live-execution-worker"
            ws.execution_claim_expires_at = execution_expires_at
            await s.commit()

        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=1,
                node_id="worker-node-a",
            ),
        )

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        assert executor.calls == []
        assert executor.resume_calls == [monitor_id]
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.execution_claimed_by == "live-execution-worker"
            assert ws.execution_claim_expires_at is not None
            assert ws.execution_claim_expires_at.replace(tzinfo=UTC) == execution_expires_at
            assert ws.monitor_claimed_by is None
            assert ws.monitor_claim_expires_at is None
            operations = await OperationRepository(s).list_all(workspace_id=monitor_id)
            recovery_events = await WorkspaceEventRepository(s).list(
                workspace_id=monitor_id,
                event_type="workspace.monitor_recovery_started",
            )

        remonitor_operations = [
            operation
            for operation in operations
            if operation.type == OperationType.remonitor.value
        ]
        assert len(remonitor_operations) == 1
        assert remonitor_operations[0].payload is not None
        execution_cleanup = remonitor_operations[0].payload["claim_cleanup"][
            "execution_claim"
        ]
        assert execution_cleanup == {
            "action": "preserved_unexpired",
            "reason_code": "UNEXPIRED_EXECUTION_CLAIM_PRESERVED_DURING_MONITOR_RECOVERY",
            "previous_claimed_by": "live-execution-worker",
            "previous_expires_at": execution_expires_at.isoformat(),
        }
        assert len(recovery_events) == 1
        assert recovery_events[0].payload is not None
        assert recovery_events[0].payload["claim_cleanup"]["execution_claim"] == (
            execution_cleanup
        )

    @pytest.mark.unit
    async def test_repeated_restart_recovery_preserves_active_monitor_claim_idempotently(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        stale_execution_expires_at = datetime(2026, 4, 27, 12, 5, tzinfo=UTC)
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "monitor-idempotent-recovery",
            pr_number=458,
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            ws.execution_claimed_by = "dead-execution-worker"
            ws.execution_claim_expires_at = stale_execution_expires_at
            await s.commit()

        executor_a = _BlockingMonitorExecutor()
        worker_a = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor_a,
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=1,
                node_id="worker-node-a",
            ),
        )
        executor_b = _RecordingExecutor()
        worker_b = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor_b,
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=1,
                node_id="worker-node-b",
            ),
        )

        assert await asyncio.wait_for(worker_a.run_once(), timeout=1.0) == 1
        await asyncio.wait_for(executor_a.started.wait(), timeout=1.0)

        assert await asyncio.wait_for(worker_b.run_once(), timeout=1.0) == 0
        await worker_b.wait_for_execution_tasks()

        assert executor_b.calls == []
        assert executor_b.resume_calls == []
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            assert ws.execution_claimed_by is None
            assert ws.execution_claim_expires_at is None
            assert ws.monitor_claimed_by == worker_a._worker_id
            assert ws.monitor_claim_expires_at is not None
            operations = await OperationRepository(s).list_all(workspace_id=monitor_id)
            recovery_events = await WorkspaceEventRepository(s).list(
                workspace_id=monitor_id,
                event_type="workspace.monitor_recovery_started",
            )

        remonitor_operations = [
            operation
            for operation in operations
            if operation.type == OperationType.remonitor.value
        ]
        assert len(remonitor_operations) == 1
        assert len(recovery_events) == 1
        assert remonitor_operations[0].payload is not None
        assert remonitor_operations[0].payload["claim_cleanup"]["execution_claim"][
            "action"
        ] == "cleared_stale"

        executor_a.release.set()
        await asyncio.wait_for(worker_a.wait_for_execution_tasks(), timeout=1.0)

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            assert ws.execution_claimed_by is None
            assert ws.execution_claim_expires_at is None
            assert ws.monitor_claimed_by is None
            assert ws.monitor_claim_expires_at is None
            operations = await OperationRepository(s).list_all(workspace_id=monitor_id)

        remonitor_operations = [
            operation
            for operation in operations
            if operation.type == OperationType.remonitor.value
        ]
        assert len(remonitor_operations) == 1
        assert remonitor_operations[0].status == OperationStatus.succeeded.value

    @pytest.mark.unit
    async def test_restart_recovery_does_not_claim_rows_with_active_monitor_lease(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "active-monitor-lease",
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            ws.monitor_claimed_by = "healthy-monitor-worker"
            ws.monitor_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
            await s.commit()

        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=3),
        )

        assert await worker.run_once() == 0
        await worker.wait_for_execution_tasks()

        assert executor.calls == []
        assert executor.resume_calls == []
        async with session_factory() as s:
            operations = await OperationRepository(s).list_all(workspace_id=monitor_id)
            recovery_events = await WorkspaceEventRepository(s).list(
                workspace_id=monitor_id,
                event_type="workspace.monitor_recovery_started",
            )
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            assert ws.monitor_claimed_by == "healthy-monitor-worker"
            assert ws.monitor_claim_expires_at is not None
        assert [
            operation
            for operation in operations
            if operation.type == OperationType.remonitor.value
        ] == []
        assert recovery_events == []

    @pytest.mark.unit
    async def test_runtime_stranded_monitoring_pr_with_open_pr_records_and_resumes(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "stranded-monitor-runtime",
            pr_number=789,
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            ws.monitor_claimed_by = "dead-monitor-worker"
            ws.monitor_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await s.commit()

        inspector = _RecordingRuntimeInspector(
            {
                f"awf_{monitor_id}": RuntimeSnapshot(
                    stack_state="stopped",
                    reason="compose project has no running containers",
                    services=[],
                )
            }
        )
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=inspector,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=3,
                stale_active_execution_scan_interval_seconds=0.0,
            ),
        )

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        assert inspector.calls == [f"awf_{monitor_id}"]
        assert executor.calls == []
        assert executor.resume_calls == [monitor_id]
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.failure_reason is None
            assert ws.failure_message is None
            assert ws.monitor_claimed_by is None
            assert ws.monitor_claim_expires_at is None
            runtime_events = await WorkspaceEventRepository(s).list(
                workspace_id=monitor_id,
                event_type="workspace.runtime_stranded_detected",
            )
            operations = await OperationRepository(s).list_all(workspace_id=monitor_id)
            recovery_events = await WorkspaceEventRepository(s).list(
                workspace_id=monitor_id,
                event_type="workspace.monitor_recovery_started",
            )

        assert len(runtime_events) == 1
        assert runtime_events[0].reason_code == "STRANDED_WORKSPACE"
        assert runtime_events[0].payload is not None
        assert runtime_events[0].payload["decision"] == "remonitor_workspace"
        assert len(recovery_events) == 1
        assert recovery_events[0].payload is not None
        assert recovery_events[0].payload["runtime_stranding_reason"] == "STRANDED_WORKSPACE"
        remonitor_operations = [
            operation
            for operation in operations
            if operation.type == OperationType.remonitor.value
        ]
        assert len(remonitor_operations) == 1
        assert remonitor_operations[0].status == OperationStatus.succeeded.value
        assert remonitor_operations[0].payload is not None
        assert remonitor_operations[0].payload["runtime_stranding_reason"] == (
            "STRANDED_WORKSPACE"
        )

    @pytest.mark.unit
    async def test_runtime_inspection_unavailable_does_not_block_open_pr_remonitor(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "unavailable-runtime-monitor",
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            ws.monitor_claimed_by = "dead-monitor-worker"
            ws.monitor_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await s.commit()

        inspector = _RaisingRuntimeInspector(RuntimeError("Cannot connect to Docker"))
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=inspector,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=3,
                stale_active_execution_scan_interval_seconds=0.0,
            ),
        )

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        assert inspector.calls == [f"awf_{monitor_id}"]
        assert executor.resume_calls == [monitor_id]
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.failure_reason is None
            runtime_events = await WorkspaceEventRepository(s).list(
                workspace_id=monitor_id,
                event_type="workspace.runtime_stranded_detected",
            )
            recovery_events = await WorkspaceEventRepository(s).list(
                workspace_id=monitor_id,
                event_type="workspace.monitor_recovery_started",
            )

        assert len(runtime_events) == 1
        assert runtime_events[0].reason_code == "RUNTIME_INSPECTION_UNAVAILABLE"
        assert runtime_events[0].payload is not None
        assert runtime_events[0].payload["decision"] == "remonitor_workspace"
        assert runtime_events[0].payload["runtime"]["stack_state"] == "unavailable"
        assert len(recovery_events) == 1
        assert recovery_events[0].payload is not None
        assert recovery_events[0].payload["runtime_stranding_reason"] == (
            "RUNTIME_INSPECTION_UNAVAILABLE"
        )

    @pytest.mark.unit
    async def test_operator_remonitor_clears_active_claim_so_worker_resumes(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        monitor_id = await _create_monitoring_pr(
            session_factory, origin_repo, "claimed-monitor"
        )
        async with session_factory() as s:
            workspace = await WorkspaceRepository(s).get(monitor_id)
            assert workspace is not None
            future = datetime.now(UTC) + timedelta(hours=1)
            workspace.monitor_claimed_by = "dead-monitor-worker"
            workspace.monitor_claim_expires_at = future
            workspace.execution_claimed_by = "dead-execution-worker"
            workspace.execution_claim_expires_at = future
            await s.commit()

        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=3),
        )

        assert await worker.run_once() == 0
        assert executor.resume_calls == []

        async with session_factory() as s:
            result = await WorkspaceControlService(
                s,
                project_stopper=_noop_project_stop,
                cleaner_factory=_unexpected_cleaner_factory,
            ).remonitor_workspace(
                monitor_id,
                reason="operator recovery",
                idempotency_key="remonitor-worker-resume",
            )
            await s.commit()

        assert result.status == WorkspaceStatus.monitoring_pr

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
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=1,
                max_concurrent_executions=1,
            ),
        )

        assert await asyncio.wait_for(worker.run_once(), timeout=1.0) == 1
        await asyncio.wait_for(monitor_started.wait(), timeout=1.0)
        assert executor.resume_calls == [monitor_id]
        assert executor.calls == []

        assert await asyncio.wait_for(worker.run_once(), timeout=1.0) == 0
        assert executor.calls == []

        release_monitor.set()
        await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=1.0)

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
                node_id="worker-node-a",
            ),
        )

        assert await asyncio.wait_for(worker.run_once(), timeout=1.0) == 1
        await asyncio.wait_for(monitor_started.wait(), timeout=1.0)

        assert await asyncio.wait_for(worker.run_once(), timeout=1.0) == 0
        assert executor.resume_calls == [monitor_id]

        release_monitor.set()
        await asyncio.wait_for(worker.wait_for_execution_tasks(), timeout=1.0)

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
        await asyncio.wait_for(monitor_started.wait(), timeout=1.0)

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
                and event.reason_code == "STRANDED_WORKSPACE"
                for event in events
            )
            assert any(
                event.event_type == "workspace.runtime_stranded_detected"
                and event.reason_code == "STRANDED_WORKSPACE"
                for event in events
            )
        assert inspector.calls == [None]

    @pytest.mark.unit
    async def test_stale_running_with_missing_agent_container_fails_with_structured_reason(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "missing-agent",
            WorkspaceStatus.running,
            compose_project_name="awf_missing_agent",
        )
        inspector = _RecordingRuntimeInspector(
            {
                "awf_missing_agent": RuntimeSnapshot(
                    stack_state="running",
                    services=[
                        RuntimeService(
                            name="postgres",
                            container_id="pg",
                            image="postgres:16",
                            state="running",
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

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.failure_message is not None
            assert "AGENT_CONTAINER_MISSING" in ws.failure_message
            events = await WorkspaceEventRepository(s).list(workspace_id=workspace_id)
            runtime_events = [
                event
                for event in events
                if event.event_type == "workspace.runtime_stranded_detected"
            ]
            assert len(runtime_events) == 1
            assert runtime_events[0].reason_code == "AGENT_CONTAINER_MISSING"
            assert runtime_events[0].payload is not None
            assert runtime_events[0].payload["decision"] == "fail_workspace"
            assert runtime_events[0].payload["runtime"]["services"][0]["name"] == "postgres"

    @pytest.mark.unit
    async def test_stale_running_with_exited_agent_container_fails_with_structured_reason(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "exited-agent",
            WorkspaceStatus.running,
            compose_project_name="awf_exited_agent",
        )
        inspector = _RecordingRuntimeInspector(
            {
                "awf_exited_agent": RuntimeSnapshot(
                    stack_state="stopped",
                    services=[
                        RuntimeService(
                            name="agent",
                            container_id="agent",
                            image="awf-agent:latest",
                            state="exited",
                            status="Exited (1) 2 minutes ago",
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

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.failure_message is not None
            assert "AGENT_CONTAINER_EXITED" in ws.failure_message
            events = await WorkspaceEventRepository(s).list(workspace_id=workspace_id)
            assert any(
                event.event_type == "workspace.runtime_stranded_detected"
                and event.reason_code == "AGENT_CONTAINER_EXITED"
                for event in events
            )

    @pytest.mark.unit
    async def test_pre_pr_stranding_with_retry_policy_defers_recovery(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "retry-policy-running",
            WorkspaceStatus.running,
            compose_project_name="awf_retry_policy_running",
            task_policy={"runtime_recovery": {"stranded_workspace": "retry"}},
        )
        inspector = _RecordingRuntimeInspector(
            {
                "awf_retry_policy_running": RuntimeSnapshot(
                    stack_state="stopped",
                    services=[],
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
            assert ws.status == WorkspaceStatus.running.value
            assert ws.failure_reason is None
            assert ws.failure_message is None
            events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.runtime_stranded_detected",
            )
            assert len(events) == 1
            assert events[0].reason_code == "STRANDED_WORKSPACE"
            assert events[0].payload is not None
            assert events[0].payload["decision"] == "defer_retry_policy"
        assert inspector.calls == ["awf_retry_policy_running"]

    @pytest.mark.unit
    async def test_running_stack_with_exited_agent_and_retry_policy_defers_recovery(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "retry-policy-exited-agent",
            WorkspaceStatus.running,
            compose_project_name="awf_retry_policy_exited_agent",
            task_policy={"runtime_recovery": {"stranded_workspace": "retry"}},
        )
        inspector = _RecordingRuntimeInspector(
            {
                "awf_retry_policy_exited_agent": RuntimeSnapshot(
                    stack_state="running",
                    services=[
                        RuntimeService(
                            name="agent",
                            container_id="agent",
                            image="awf-agent:latest",
                            state="exited",
                            status="Exited (1) 2 minutes ago",
                        ),
                        RuntimeService(
                            name="postgres",
                            container_id="pg",
                            image="postgres:16",
                            state="running",
                        ),
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

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.running.value
            assert ws.failure_reason is None
            assert ws.failure_message is None
            runtime_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.runtime_stranded_detected",
            )
            assert len(runtime_events) == 1
            assert runtime_events[0].reason_code == "AGENT_CONTAINER_EXITED"
            assert runtime_events[0].payload is not None
            assert runtime_events[0].payload["decision"] == "defer_retry_policy"
            stale_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.stale_active_execution_detected",
            )
            assert stale_events == []
        assert inspector.calls == ["awf_retry_policy_exited_agent"]

    @pytest.mark.unit
    async def test_stale_validating_with_unavailable_docker_defers_recovery(
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
            assert ws.status == WorkspaceStatus.validating.value
            assert ws.failure_reason is None
            assert ws.failure_message is None
            events = await WorkspaceEventRepository(s).list(workspace_id=workspace_id)
            assert not any(event.reason_code == "STALE_ACTIVE_EXECUTION" for event in events)

    @pytest.mark.unit
    async def test_stale_active_execution_failure_marks_workspace_failed(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "stale-running-fail",
            WorkspaceStatus.running,
            compose_project_name="awf_stale_running_fail",
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=1),
        )

        await worker._fail_stale_active_execution(
            _ActiveExecutionCandidate(
                workspace_id=workspace_id,
                status=WorkspaceStatus.running,
                compose_project_name="awf_stale_running_fail",
            ),
            RuntimeSnapshot(
                stack_state="running",
                reason="worker process exited before releasing its claim",
            ),
        )

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.execution_claimed_by is None
            assert ws.execution_claim_expires_at is None
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.failure_message is not None
            assert "compose runtime state is running" in ws.failure_message
            assert "worker process exited before releasing its claim" in ws.failure_message
            events = await WorkspaceEventRepository(s).list(workspace_id=workspace_id)
            assert any(
                event.event_type == "workspace.state_changed"
                and event.reason_code == "STALE_ACTIVE_EXECUTION"
                for event in events
            )

    @pytest.mark.unit
    async def test_recoverable_runtime_stranding_skips_stale_rows_and_fresh_claims(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        status_mismatch_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "stranding-status-mismatch",
            WorkspaceStatus.running,
            compose_project_name="awf_status_mismatch",
        )
        fresh_claim_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "stranding-fresh-claim",
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(fresh_claim_id)
            assert ws is not None
            ws.monitor_claimed_by = "healthy-monitor"
            ws.monitor_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
            await s.commit()

        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=1),
        )
        finding = WorkspaceRuntimeFinding(
            workspace_id="ws",
            workspace_status=WorkspaceStatus.monitoring_pr.value,
            status="stranded",
            reason_code="STRANDED_WORKSPACE",
            decision="remonitor_workspace",
            message="runtime is stranded",
        )
        snapshot = RuntimeSnapshot(stack_state="stopped", reason="no containers")

        await worker._record_recoverable_runtime_stranding(
            _ActiveExecutionCandidate(
                workspace_id=status_mismatch_id,
                status=WorkspaceStatus.validating,
                compose_project_name="awf_status_mismatch",
            ),
            snapshot,
            finding,
        )
        await worker._record_recoverable_runtime_stranding(
            _ActiveExecutionCandidate(
                workspace_id=fresh_claim_id,
                status=WorkspaceStatus.monitoring_pr,
                compose_project_name=f"awf_{fresh_claim_id}",
                pr_url="https://github.com/example/repo/pull/123",
            ),
            snapshot,
            finding,
        )

        async with session_factory() as s:
            for workspace_id in (status_mismatch_id, fresh_claim_id):
                events = await WorkspaceEventRepository(s).list(
                    workspace_id=workspace_id,
                    event_type="workspace.runtime_stranded_detected",
                )
                assert events == []

    @pytest.mark.unit
    async def test_runtime_failure_helpers_ignore_rows_that_changed_status(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "changed-status-recovery",
            WorkspaceStatus.running,
            compose_project_name="awf_changed_status",
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=1),
        )
        snapshot = RuntimeSnapshot(stack_state="stopped", reason="no containers")
        candidate = _ActiveExecutionCandidate(
            workspace_id=workspace_id,
            status=WorkspaceStatus.validating,
            compose_project_name="awf_changed_status",
        )
        finding = WorkspaceRuntimeFinding(
            workspace_id=workspace_id,
            workspace_status=WorkspaceStatus.validating.value,
            status="stranded",
            reason_code="STRANDED_WORKSPACE",
            decision="fail_workspace",
            message="runtime is stranded",
        )

        await worker._fail_stale_active_execution(candidate, snapshot)
        await worker._fail_stranded_workspace(candidate, snapshot, finding)

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.running.value
            assert ws.failure_reason is None
            events = await WorkspaceEventRepository(s).list(workspace_id=workspace_id)
            assert not any(event.reason_code == "STALE_ACTIVE_EXECUTION" for event in events)
            assert not any(event.reason_code == "STRANDED_WORKSPACE" for event in events)

    @pytest.mark.unit
    async def test_stale_running_stack_is_evented_then_failed_on_next_scan(
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
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=1,
                stale_active_execution_scan_interval_seconds=0.0,
            ),
        )

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

        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.execution_claimed_by is None
            assert ws.execution_claim_expires_at is None
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.failure_message is not None
            assert "active execution was lost" in ws.failure_message
        assert inspector.calls == ["awf_pushing_running", "awf_pushing_running"]

    @pytest.mark.unit
    async def test_stale_active_execution_scan_is_throttled_between_intervals(
        self,
        monkeypatch: pytest.MonkeyPatch,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        current_time = 1_000.0
        monkeypatch.setattr("awf.control.worker.monotonic", lambda: current_time)
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "throttled-running",
            WorkspaceStatus.pushing,
            compose_project_name="awf_throttled_running",
        )
        inspector = _RecordingRuntimeInspector(
            {
                "awf_throttled_running": RuntimeSnapshot(
                    stack_state="running",
                    services=[
                        RuntimeService(
                            name="agent",
                            container_id="abc123",
                            image="awf-agent:latest",
                            state="running",
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
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=1,
                stale_active_execution_scan_interval_seconds=60.0,
            ),
        )

        assert await worker.run_once() == 0
        assert await worker.run_once() == 0
        assert inspector.calls == ["awf_throttled_running"]
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.pushing.value

        current_time = 1_060.0

        assert await worker.run_once() == 0
        assert inspector.calls == ["awf_throttled_running", "awf_throttled_running"]
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value

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
            await asyncio.wait_for(task, timeout=1.0)
            worker._execution_tasks.pop(workspace_id, None)

    @pytest.mark.unit
    async def test_stale_active_execution_scan_skips_unexpired_execution_claim(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "claimed-running",
            WorkspaceStatus.running,
            compose_project_name="awf_claimed_running",
            node_id="node-a",
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            ws.execution_claimed_by = "other-worker"
            ws.execution_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
            await s.commit()

        inspector = _RecordingRuntimeInspector({})
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
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.running.value
            assert ws.failure_reason is None
        assert inspector.calls == []

    @pytest.mark.unit
    async def test_stale_active_execution_scan_recovers_expired_execution_claim(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "expired-claim-running",
            WorkspaceStatus.running,
            compose_project_name="awf_expired_claim_running",
            node_id="node-a",
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            ws.execution_claimed_by = "dead-worker"
            ws.execution_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await s.commit()

        inspector = _RecordingRuntimeInspector(
            {
                "awf_expired_claim_running": RuntimeSnapshot(
                    stack_state="stopped",
                    services=[],
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
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
        assert inspector.calls == ["awf_expired_claim_running"]

    @pytest.mark.unit
    async def test_stale_active_execution_failure_rechecks_refreshed_claim(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "refreshed-claim-running",
            WorkspaceStatus.running,
            compose_project_name="awf_refreshed_claim_running",
            node_id="node-a",
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            ws.execution_claimed_by = "worker-a"
            ws.execution_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await s.commit()

        class _RefreshingInspector:
            def __init__(self) -> None:
                self.calls: list[str | None] = []

            async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
                self.calls.append(compose_project_name)
                async with session_factory() as s:
                    ws = await WorkspaceRepository(s).get(workspace_id)
                    assert ws is not None
                    ws.execution_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
                    await s.commit()
                return RuntimeSnapshot(
                    stack_state="unavailable",
                    reason="docker unavailable",
                )

        inspector = _RefreshingInspector()
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
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.running.value
            assert ws.failure_reason is None
        assert inspector.calls == ["awf_refreshed_claim_running"]

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
                    stack_state="stopped",
                    services=[],
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
    async def test_monitoring_pr_with_open_pr_records_recoverable_runtime_stranding(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "monitoring-pr",
        )
        inspector = _RecordingRuntimeInspector(
            {
                f"awf_{workspace_id}": RuntimeSnapshot(
                    stack_state="stopped",
                    services=[],
                )
            }
        )
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
            events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.runtime_stranded_detected",
            )
            assert len(events) == 1
            assert events[0].reason_code == "STRANDED_WORKSPACE"
            assert events[0].payload is not None
            assert events[0].payload["decision"] == "remonitor_workspace"
        assert inspector.calls == [f"awf_{workspace_id}"]
        assert executor.resume_calls == []

    @pytest.mark.unit
    async def test_monitoring_pr_runtime_stranding_clears_expired_claim_and_resumes(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "claimed-monitoring-pr",
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            ws.monitor_claimed_by = "dead-monitor-worker"
            ws.monitor_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await s.commit()

        inspector = _RecordingRuntimeInspector(
            {
                f"awf_{workspace_id}": RuntimeSnapshot(
                    stack_state="stopped",
                    services=[],
                )
            }
        )
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=inspector,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=1),
        )

        await worker._recover_stale_active_executions()

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.monitor_claimed_by is None
            assert ws.monitor_claim_expires_at is None
            assert ws.failure_reason is None
            events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.runtime_stranded_detected",
            )
            assert len(events) == 1
            assert events[0].reason_code == "STRANDED_WORKSPACE"
            assert events[0].payload is not None
            assert events[0].payload["decision"] == "remonitor_workspace"

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.monitor_claimed_by is None
            assert ws.monitor_claim_expires_at is None
        assert executor.resume_calls == [workspace_id]

    @pytest.mark.unit
    async def test_monitoring_pr_without_pr_url_follows_failure_path(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "monitoring-pr-without-url",
            with_pr_url=False,
        )
        inspector = _RecordingRuntimeInspector(
            {
                f"awf_{workspace_id}": RuntimeSnapshot(
                    stack_state="stopped",
                    services=[],
                )
            }
        )
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
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.failure_message is not None
            assert "STRANDED_WORKSPACE" in ws.failure_message
        assert inspector.calls == [f"awf_{workspace_id}"]

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


@pytest.mark.unit
@pytest.mark.parametrize(
    ("limit", "expected"),
    [
        (0, 0),
        (1, 4),
        (5, 20),
        (6, 22),
        (64, 80),
        (234, 250),
        (251, 251),
    ],
)
def test_scheduler_candidate_fetch_limit_documents_overfetch_edges(
    limit: int,
    expected: int,
) -> None:
    assert _scheduler_candidate_fetch_limit(limit) == expected


@pytest.mark.unit
def test_stale_execution_helper_defaults_for_non_runtime_statuses() -> None:
    now = datetime(2026, 4, 27, 23, 0, tzinfo=UTC)
    workspace = SimpleNamespace(
        execution_claimed_by="worker",
        execution_claim_expires_at=now + timedelta(minutes=5),
        monitor_claimed_by="monitor",
        monitor_claim_expires_at=now + timedelta(minutes=5),
    )

    assert _claim_recheck_conditions(WorkspaceStatus.ready) == ()
    assert _candidate_claim_is_stale(workspace, WorkspaceStatus.ready, now) is True

    message = _stale_active_execution_failure_message(
        _ActiveExecutionCandidate(
            workspace_id="ws_missing_compose",
            status=WorkspaceStatus.running,
            compose_project_name=None,
        ),
        RuntimeSnapshot(stack_state="unknown"),
    )

    assert "no compose project is persisted for the workspace" in message
