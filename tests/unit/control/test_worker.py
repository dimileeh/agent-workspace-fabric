"""ControlWorker tests.

We use the real Provisioner against real git + PostgreSQL to validate the full
pipeline, rather than mocking the provisioner. The worker's contract is
primarily about listing work off the DB in the right order and bounding
concurrency, so end-to-end is the most useful test.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import structlog
from sqlalchemy import event, select, update
from sqlalchemy.exc import InterfaceError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import awf.control.worker as worker_module
import awf.db.repositories as repositories_module
from awf.control.worker import (
    ControlWorker,
    WorkerConfig,
    _active_execution_preservation_claim_cleanup_payload,
    _ActiveExecutionCandidate,
    _candidate_claim_is_stale,
    _execution_claim_is_stale,
    _has_running_agent_runtime,
    _json_datetime,
    _monitor_claim_is_stale,
    _monitor_recovery_claim_cleanup_payload,
    _scheduler_candidate_cursor,
    _scheduler_candidate_fetch_limit,
    _scheduler_items_are_workspace_ids,
    _scheduler_items_are_workspaces,
    _stale_active_execution_failure_message,
    _TerminalRuntimeCandidate,
    _utc_datetime,
)
from awf.db.enums import FailureReason, OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    OperationRepository,
    ProviderModelCircuitBreakerRepository,
    QueueDecisionRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.node.cleanup import (
    CLEANUP_PARTIAL,
    COMPOSE_DOWN_SUCCEEDED,
    WorkspaceCleanupResult,
    WorkspaceCleanupStepResult,
)
from awf.node.git_manager import GitManager
from awf.node.provisioner import Provisioner, ProvisionerConfig
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
from awf.service.controls import WorkspaceControlService
from awf.service.scheduler import (
    SchedulerOrderCursor,
    scheduler_order_key,
    scheduler_score_from_workspace,
)
from awf.service.workspace_runtime_health import WorkspaceRuntimeFinding
from tests.postgres import postgres_test_engine

PRESERVED_EXECUTION_EVENT_TYPE = "workspace.active_execution_preserved_after_restart"
PRESERVED_EXECUTION_REASON_CODE = "ACTIVE_EXECUTION_PRESERVED_AFTER_RESTART"
PRESERVED_EXECUTION_SUBPHASE = "runtime_preserved_after_restart"
WORKER_TEST_TIMEOUT_SECONDS = 300.0


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


async def _pending_execution_task() -> None:
    await asyncio.Event().wait()


def _scheduler_test_scoring_time(
    *,
    after: SchedulerOrderCursor | None,
    scoring_at: datetime | None,
) -> datetime:
    if after is None:
        assert scoring_at is not None
        return scoring_at
    if scoring_at is not None:
        assert scoring_at == after.scoring_at
    return after.scoring_at


def _scheduler_order_cursor_for_workspace(
    workspace: Workspace,
    *,
    scoring_at: datetime,
) -> SchedulerOrderCursor:
    score = scheduler_score_from_workspace(workspace, now=scoring_at)
    return SchedulerOrderCursor(
        class_priority=score.class_priority,
        effective_score=score.effective_score,
        queued_at=score.queued_at,
        workspace_id=score.workspace_id,
        scoring_at=scoring_at,
    )


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
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


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
    task_policy: dict[str, object] | None = None,
    task_class: str | None = None,
    created_at: datetime | None = None,
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
            task_policy=task_policy,
            task_class=task_class,
        )
        if created_at is not None:
            ws.created_at = created_at
            ws.updated_at = created_at
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


async def _reserve_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    node_id: str = "local",
    steady_cpu: float = 1.0,
    steady_memory_gb: float = 1.0,
    peak_cpu: float = 1.0,
    peak_memory_gb: float = 1.0,
    disk_mb: int | None = None,
    dind_slots: int = 0,
) -> None:
    async with session_factory() as s:
        attempt = await TaskAttemptRepository(s).get_by_workspace_id(workspace_id)
        assert attempt is not None
        await ResourceReservationRepository(s).create(
            workspace_id=workspace_id,
            attempt_id=attempt.id,
            node_id=node_id,
            steady_cpu=steady_cpu,
            steady_memory_gb=steady_memory_gb,
            peak_cpu=peak_cpu,
            peak_memory_gb=peak_memory_gb,
            disk_mb=disk_mb,
            dind_slots=dind_slots,
            phase="workspace_lifecycle",
        )
        await s.commit()


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
                compose_project_name if compose_project_name is not None else f"awf_{ws.id}"
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


async def _seed_primary_failure_evidence(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    failure_reason: str,
    failure_message: str,
    reason_code: str,
    include_validation_run: bool = False,
) -> str | None:
    async with session_factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.get(workspace_id)
        assert ws is not None
        original_status = WorkspaceStatus(ws.status)
        if original_status == WorkspaceStatus.failed:
            await repo.transition(ws, to=WorkspaceStatus.destroying, reason_code="SEED")
        validation_run_id: str | None = None
        if include_validation_run:
            validation_repo = ValidationRunRepository(s)
            run = await validation_repo.start(
                workspace_id=workspace_id,
                attempt_id=None,
                tier=0,
                commands=[
                    {
                        "command": "uv run pytest tests/unit/test_example.py::test_failure",
                        "phase": "validation",
                    }
                ],
                base_commit="a" * 40,
                target_branch="development",
                target_head_sha="b" * 40,
                log_stream_refs={"validation": "logs/validation.log"},
                workspace_head_sha="c" * 40,
                profile_name="default",
                profile_version=1,
                profile_source=".awf/workspace.yml",
                resolved_profile_digest="d" * 64,
                environment_identity_digest="e" * 64,
                environment_identity_inputs={"python": "3.12"},
            )
            await validation_repo.finish(
                run.id,
                status="failed",
                reason_code=reason_code,
                coverage={
                    "percent": 91.5,
                    "minimum_percent": 99.0,
                    "threshold": 99.0,
                    "failing_test_node_ids": [
                        "tests/unit/test_example.py::test_failure",
                    ],
                    "failing_test_evidence": [
                        "FAILED tests/unit/test_example.py::test_failure",
                    ],
                },
            )
            validation_run_id = run.id
        ws.failure_reason = failure_reason
        ws.failure_message = failure_message
        await repo.transition(
            ws,
            to=WorkspaceStatus.failed,
            reason_code=reason_code,
            payload={
                "reason_code": reason_code,
                "message": failure_message,
                "details": {
                    "recommended_action": "fix the primary failure before retrying",
                    "recovery_strategy": "retry_after_fix",
                },
            },
        )
        if original_status in {
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
        }:
            # The worker paths under test require an active row, while the
            # causality evidence itself must come from a real failed
            # transition. Do not write a remonitor state_reset here: that would
            # deliberately start a new failure epoch and suppress the primary
            # evidence these secondary-path tests exercise.
            ws.status = original_status.value
        await s.commit()
        return validation_run_id


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


class _RecordingRuntimeCleaner:
    def __init__(self, result: WorkspaceCleanupResult | None = None) -> None:
        self.result = result or WorkspaceCleanupResult.from_steps(
            [
                WorkspaceCleanupStepResult(
                    name="compose_down",
                    status="succeeded",
                    reason_code=COMPOSE_DOWN_SUCCEEDED,
                )
            ]
        )
        self.calls: list[dict[str, object]] = []

    async def cleanup(
        self,
        *,
        workspace_id: str,
        repo_url: str,
        compose_project_name: str | None = None,
        compose_file_path: Path | None = None,
        worktree_host_path: Path | None = None,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
    ) -> WorkspaceCleanupResult:
        self.calls.append(
            {
                "workspace_id": workspace_id,
                "repo_url": repo_url,
                "compose_project_name": compose_project_name,
                "compose_file_path": compose_file_path,
                "worktree_host_path": worktree_host_path,
                "remove_volumes": remove_volumes,
                "remove_worktree": remove_worktree,
            }
        )
        return self.result


def _live_agent_snapshot(*, container_id: str = "agent") -> RuntimeSnapshot:
    return RuntimeSnapshot(
        stack_state="running",
        services=[
            RuntimeService(
                name="agent",
                container_id=container_id,
                image="awf-agent:latest",
                state="running",
                status="Up 2 minutes",
                health="healthy",
            )
        ],
    )


def _closed_connection_error() -> InterfaceError:
    return InterfaceError(
        "SELECT 1",
        {},
        RuntimeError("connection is closed"),
        connection_invalidated=True,
    )


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
    async def test_requested_capacity_gate_defers_when_allocated_capacity_full(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        active_id = await _create_ready(
            session_factory,
            origin_repo,
            "active-capacity-holder",
            create_task_attempt=True,
        )
        await _reserve_workspace(
            session_factory,
            active_id,
            steady_cpu=3.0,
            steady_memory_gb=8.0,
            peak_cpu=6.0,
            peak_memory_gb=16.0,
            dind_slots=1,
        )
        requested_id = await _create_requested(
            session_factory,
            origin_repo,
            "capacity-deferred",
            create_task_attempt=True,
        )
        await _reserve_workspace(
            session_factory,
            requested_id,
            steady_cpu=3.0,
            steady_memory_gb=8.0,
            peak_cpu=6.0,
            peak_memory_gb=16.0,
            dind_slots=1,
        )
        provisioner = _TransitioningProvisioner(session_factory)
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=2,
                local_capacity_cpu_cores=6.0,
                local_capacity_memory_gb=16.0,
                local_capacity_dind_slots=1,
            ),
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            workspace = await WorkspaceRepository(s).get(requested_id)
            assert workspace is not None
            decisions = await QueueDecisionRepository(s).list_for_workspace(requested_id)

        assert provisioner.calls == []
        assert workspace.status == WorkspaceStatus.requested.value
        assert any(decision.reason_code == "LOCAL_CAPACITY_DEFERRED" for decision in decisions)
        capacity_decision = next(
            decision for decision in decisions if decision.reason_code == "LOCAL_CAPACITY_DEFERRED"
        )
        blockers = capacity_decision.resource_summary.get("blockers")
        assert isinstance(blockers, list)
        assert {blocker["reason_code"] for blocker in blockers if isinstance(blocker, dict)} >= {
            "PEAK_CPU_CAPACITY_SATURATED",
            "PEAK_MEMORY_CAPACITY_SATURATED",
            "DIND_CAPACITY_SATURATED",
        }

    @pytest.mark.unit
    async def test_requested_capacity_gate_claims_without_prefetching_requested_ids(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        requested_id = await _create_requested(
            session_factory,
            origin_repo,
            "capacity-no-prefetch-request",
            create_task_attempt=True,
        )
        await _reserve_workspace(
            session_factory,
            requested_id,
            steady_cpu=1.0,
            steady_memory_gb=1.0,
            peak_cpu=1.0,
            peak_memory_gb=1.0,
        )
        provisioner = _TransitioningProvisioner(session_factory)
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=1,
                local_capacity_cpu_cores=2.0,
            ),
        )

        async def _unexpected_list_requested() -> list[str]:
            raise AssertionError("capacity claims should not pre-list requested IDs")

        worker._list_requested = _unexpected_list_requested  # type: ignore[method-assign]

        assert await worker.run_once() == 1

        async with session_factory() as s:
            workspace = await WorkspaceRepository(s).get(requested_id)

        assert provisioner.calls == [requested_id]
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.ready.value

    @pytest.mark.unit
    async def test_requested_capacity_gate_records_one_ordered_decision_for_defaulted_claim(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        requested_id = await _create_requested(
            session_factory,
            origin_repo,
            "capacity-defaulted-ordered-once",
            create_task_attempt=True,
        )
        provisioner = _TransitioningProvisioner(session_factory)
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=1,
                local_capacity_cpu_cores=6.0,
                local_capacity_memory_gb=16.0,
            ),
        )

        assert await worker.run_once() == 1

        async with session_factory() as s:
            workspace = await WorkspaceRepository(s).get(requested_id)
            assert workspace is not None
            decisions = await QueueDecisionRepository(s).list_for_workspace(requested_id)

        ordered_decisions = [decision for decision in decisions if decision.decision == "ordered"]
        assert provisioner.calls == [requested_id]
        assert workspace.status == WorkspaceStatus.ready.value
        assert len(ordered_decisions) == 1
        assert ordered_decisions[0].reason_code == "LOCAL_CAPACITY_RESERVATION_DEFAULTED"

    @pytest.mark.unit
    async def test_requested_capacity_gate_defers_for_unreserved_active_local_workspace(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        active_id = await _create_ready(
            session_factory,
            origin_repo,
            "unreserved-active-capacity-holder",
        )
        async with session_factory() as s:
            active = await WorkspaceRepository(s).get(active_id)
            assert active is not None
            active.node_id = "worker-node-a"
            active.resolved_profile = {"docker": {"mode": "dind"}}
            await s.commit()

        requested_id = await _create_requested(
            session_factory,
            origin_repo,
            "unreserved-active-capacity-deferred",
            create_task_attempt=True,
        )
        await _reserve_workspace(
            session_factory,
            requested_id,
            node_id="worker-node-a",
            steady_cpu=1.0,
            steady_memory_gb=1.0,
            peak_cpu=1.0,
            peak_memory_gb=1.0,
            dind_slots=1,
        )
        provisioner = _TransitioningProvisioner(session_factory)
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=1,
                node_id="worker-node-a",
                local_capacity_cpu_cores=6.0,
                local_capacity_memory_gb=16.0,
                local_capacity_dind_slots=1,
            ),
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            workspace = await WorkspaceRepository(s).get(requested_id)
            assert workspace is not None
            decisions = await QueueDecisionRepository(s).list_for_workspace(requested_id)

        assert provisioner.calls == []
        assert workspace.status == WorkspaceStatus.requested.value
        capacity_decision = next(
            decision for decision in decisions if decision.reason_code == "LOCAL_CAPACITY_DEFERRED"
        )
        allocated = capacity_decision.resource_summary["allocated"]
        assert allocated["workspace_count"] == 1
        assert allocated["peak_cpu"] == 6.0
        assert allocated["peak_memory_gb"] == 16.0
        assert allocated["dind_slots"] == 1
        blockers = capacity_decision.resource_summary["blockers"]
        assert {blocker["reason_code"] for blocker in blockers if isinstance(blocker, dict)} >= {
            "PEAK_CPU_CAPACITY_SATURATED",
            "PEAK_MEMORY_CAPACITY_SATURATED",
            "DIND_CAPACITY_SATURATED",
        }

    @pytest.mark.unit
    async def test_requested_capacity_gate_ignores_unreserved_active_workspace_on_other_node(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        active_id = await _create_ready(
            session_factory,
            origin_repo,
            "remote-unreserved-active-capacity-holder",
        )
        async with session_factory() as s:
            active = await WorkspaceRepository(s).get(active_id)
            assert active is not None
            active.node_id = "worker-node-b"
            active.resolved_profile = {"docker": {"mode": "dind"}}
            await s.commit()

        requested_id = await _create_requested(
            session_factory,
            origin_repo,
            "remote-unreserved-active-capacity-request",
            create_task_attempt=True,
        )
        await _reserve_workspace(
            session_factory,
            requested_id,
            node_id="worker-node-a",
            steady_cpu=3.0,
            steady_memory_gb=8.0,
            peak_cpu=6.0,
            peak_memory_gb=16.0,
            dind_slots=1,
        )
        provisioner = _TransitioningProvisioner(session_factory)
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=1,
                node_id="worker-node-a",
                local_capacity_cpu_cores=6.0,
                local_capacity_memory_gb=16.0,
                local_capacity_dind_slots=1,
            ),
        )

        assert await worker.run_once() == 1

        async with session_factory() as s:
            workspace = await WorkspaceRepository(s).get(requested_id)
            decisions = await QueueDecisionRepository(s).list_for_workspace(requested_id)

        assert provisioner.calls == [requested_id]
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.ready.value
        assert all(decision.reason_code != "LOCAL_CAPACITY_DEFERRED" for decision in decisions)

    @pytest.mark.unit
    async def test_requested_capacity_gate_skips_repeated_unchanged_capacity_deferral(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        active_id = await _create_ready(
            session_factory,
            origin_repo,
            "stable-capacity-holder",
            create_task_attempt=True,
        )
        await _reserve_workspace(
            session_factory,
            active_id,
            steady_cpu=3.0,
            steady_memory_gb=8.0,
            peak_cpu=6.0,
            peak_memory_gb=16.0,
            dind_slots=1,
        )
        requested_id = await _create_requested(
            session_factory,
            origin_repo,
            "stable-capacity-deferred",
            create_task_attempt=True,
        )
        await _reserve_workspace(
            session_factory,
            requested_id,
            steady_cpu=3.0,
            steady_memory_gb=8.0,
            peak_cpu=6.0,
            peak_memory_gb=16.0,
            dind_slots=1,
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=2,
                local_capacity_cpu_cores=6.0,
                local_capacity_memory_gb=16.0,
                local_capacity_dind_slots=1,
            ),
        )

        assert await worker.run_once() == 0
        assert await worker.run_once() == 0

        async with session_factory() as s:
            decisions = await QueueDecisionRepository(s).list_for_workspace(requested_id)

        deferred_decisions = [
            decision for decision in decisions if decision.reason_code == "LOCAL_CAPACITY_DEFERRED"
        ]
        assert len(deferred_decisions) == 1

    @pytest.mark.unit
    async def test_requested_capacity_gate_does_not_record_defaulted_ordered_decision_for_lost_claim(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        requested_id = await _create_requested(
            session_factory,
            origin_repo,
            "defaulted-capacity-lost-claim",
            create_task_attempt=True,
        )

        class LostClaimRepository:
            dialect_name = "postgresql"

            async def transition_if_current(
                self,
                workspace_id: str,
                *,
                from_status: WorkspaceStatus,
                to: WorkspaceStatus,
                reason_code: str,
            ) -> None:
                assert workspace_id == requested_id
                assert from_status == WorkspaceStatus.requested
                assert to == WorkspaceStatus.provisioning
                assert reason_code == "WORKER_CLAIMED"

        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=object(),  # type: ignore[arg-type]
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=1),
        )

        async with session_factory() as s:
            workspace = await WorkspaceRepository(s).get(requested_id)
            assert workspace is not None

            claimed = await worker._claim_requested_capacity_candidates(  # noqa: SLF001
                s,
                repo=LostClaimRepository(),  # type: ignore[arg-type]
                reservation_repo=ResourceReservationRepository(s),
                candidates=[workspace],
                allocated=worker_module._AllocatedReservationTotals(),  # noqa: SLF001
                claim_slots=1,
                decided_at=datetime.now(UTC),
            )
            decisions = await QueueDecisionRepository(s).list_for_workspace(requested_id)

        assert claimed == []
        assert all(
            decision.reason_code != "LOCAL_CAPACITY_RESERVATION_DEFAULTED" for decision in decisions
        )

    @pytest.mark.unit
    async def test_requested_capacity_gate_records_changed_capacity_deferral(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        active_id = await _create_ready(
            session_factory,
            origin_repo,
            "changing-capacity-holder",
            create_task_attempt=True,
        )
        await _reserve_workspace(
            session_factory,
            active_id,
            steady_cpu=0.0,
            steady_memory_gb=0.0,
            peak_cpu=6.0,
            peak_memory_gb=0.0,
        )
        requested_id = await _create_requested(
            session_factory,
            origin_repo,
            "changing-capacity-deferred",
            create_task_attempt=True,
        )
        await _reserve_workspace(
            session_factory,
            requested_id,
            steady_cpu=0.0,
            steady_memory_gb=0.0,
            peak_cpu=6.0,
            peak_memory_gb=0.0,
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=2,
                local_capacity_cpu_cores=6.0,
            ),
        )

        assert await worker.run_once() == 0
        second_active_id = await _create_ready(
            session_factory,
            origin_repo,
            "new-capacity-holder",
            create_task_attempt=True,
        )
        await _reserve_workspace(
            session_factory,
            second_active_id,
            steady_cpu=0.0,
            steady_memory_gb=0.0,
            peak_cpu=1.0,
            peak_memory_gb=0.0,
        )
        assert await worker.run_once() == 0
        third_active_id = await _create_ready(
            session_factory,
            origin_repo,
            "newer-capacity-holder",
            create_task_attempt=True,
        )
        await _reserve_workspace(
            session_factory,
            third_active_id,
            steady_cpu=0.0,
            steady_memory_gb=0.0,
            peak_cpu=1.0,
            peak_memory_gb=0.0,
        )
        assert await worker.run_once() == 0

        async with session_factory() as s:
            decisions = await QueueDecisionRepository(s).list_for_workspace(requested_id)

        deferred_decisions = [
            decision for decision in decisions if decision.reason_code == "LOCAL_CAPACITY_DEFERRED"
        ]
        assert len(deferred_decisions) == 3
        latest_blockers = deferred_decisions[0].resource_summary["blockers"]
        prior_blockers = deferred_decisions[1].resource_summary["blockers"]
        initial_blockers = deferred_decisions[2].resource_summary["blockers"]
        assert latest_blockers[0]["allocated"] == 8.0
        assert prior_blockers[0]["allocated"] == 7.0
        assert initial_blockers[0]["allocated"] == 6.0
        latest_previous = deferred_decisions[0].resource_summary["previous"]
        assert latest_previous["blockers"][0]["allocated"] == 7.0
        assert "previous" not in latest_previous

    @pytest.mark.unit
    async def test_requested_capacity_gate_ignores_allocated_capacity_on_other_nodes(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        active_id = await _create_ready(
            session_factory,
            origin_repo,
            "other-node-capacity-holder",
            create_task_attempt=True,
        )
        await _reserve_workspace(
            session_factory,
            active_id,
            node_id="worker-node-b",
            steady_cpu=3.0,
            steady_memory_gb=8.0,
            peak_cpu=6.0,
            peak_memory_gb=16.0,
            dind_slots=1,
        )
        requested_id = await _create_requested(
            session_factory,
            origin_repo,
            "local-node-capacity-request",
            create_task_attempt=True,
        )
        await _reserve_workspace(
            session_factory,
            requested_id,
            node_id="worker-node-a",
            steady_cpu=3.0,
            steady_memory_gb=8.0,
            peak_cpu=6.0,
            peak_memory_gb=16.0,
            dind_slots=1,
        )
        provisioner = _TransitioningProvisioner(session_factory)
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=1,
                node_id="worker-node-a",
                local_capacity_cpu_cores=6.0,
                local_capacity_memory_gb=16.0,
                local_capacity_dind_slots=1,
            ),
        )

        assert await worker.run_once() == 1

        async with session_factory() as s:
            workspace = await WorkspaceRepository(s).get(requested_id)
            decisions = await QueueDecisionRepository(s).list_for_workspace(requested_id)

        assert provisioner.calls == [requested_id]
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.ready.value
        assert all(decision.reason_code != "LOCAL_CAPACITY_DEFERRED" for decision in decisions)

    @pytest.mark.unit
    async def test_requested_capacity_gate_counts_local_workspace_with_mismatched_reservation_node(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        active_id = await _create_ready(
            session_factory,
            origin_repo,
            "mismatched-node-capacity-holder",
            create_task_attempt=True,
        )
        async with session_factory() as s:
            active = await WorkspaceRepository(s).get(active_id)
            assert active is not None
            active.node_id = "worker-node-a"
            await s.commit()
        await _reserve_workspace(
            session_factory,
            active_id,
            node_id="worker-node-b",
            steady_cpu=3.0,
            steady_memory_gb=8.0,
            peak_cpu=6.0,
            peak_memory_gb=16.0,
            dind_slots=1,
        )
        requested_id = await _create_requested(
            session_factory,
            origin_repo,
            "mismatched-node-capacity-request",
            create_task_attempt=True,
        )
        await _reserve_workspace(
            session_factory,
            requested_id,
            node_id="worker-node-a",
            steady_cpu=3.0,
            steady_memory_gb=8.0,
            peak_cpu=6.0,
            peak_memory_gb=16.0,
            dind_slots=1,
        )
        provisioner = _TransitioningProvisioner(session_factory)
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=1,
                node_id="worker-node-a",
                local_capacity_cpu_cores=6.0,
                local_capacity_memory_gb=16.0,
                local_capacity_dind_slots=1,
            ),
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            workspace = await WorkspaceRepository(s).get(requested_id)
            decisions = await QueueDecisionRepository(s).list_for_workspace(requested_id)

        assert provisioner.calls == []
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.requested.value
        capacity_decision = next(
            decision for decision in decisions if decision.reason_code == "LOCAL_CAPACITY_DEFERRED"
        )
        allocated = capacity_decision.resource_summary["allocated"]
        assert allocated["workspace_count"] == 1
        assert allocated["peak_cpu"] == 6.0
        assert allocated["peak_memory_gb"] == 16.0
        assert allocated["dind_slots"] == 1

    @pytest.mark.unit
    async def test_requested_capacity_gate_dispatches_oldest_satisfiable_candidate(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        now = datetime.now(UTC)
        active_id = await _create_ready(
            session_factory,
            origin_repo,
            "active-partial-capacity-holder",
            create_task_attempt=True,
        )
        await _reserve_workspace(
            session_factory,
            active_id,
            steady_cpu=2.0,
            steady_memory_gb=4.0,
            peak_cpu=4.0,
            peak_memory_gb=8.0,
        )
        blocked_id = await _create_requested(
            session_factory,
            origin_repo,
            "old-blocked-capacity-request",
            create_task_attempt=True,
            created_at=now - timedelta(minutes=10),
        )
        await _reserve_workspace(
            session_factory,
            blocked_id,
            steady_cpu=4.0,
            steady_memory_gb=8.0,
            peak_cpu=8.0,
            peak_memory_gb=16.0,
        )
        fitting_id = await _create_requested(
            session_factory,
            origin_repo,
            "younger-fitting-capacity-request",
            create_task_attempt=True,
            created_at=now - timedelta(minutes=5),
        )
        await _reserve_workspace(
            session_factory,
            fitting_id,
            steady_cpu=2.0,
            steady_memory_gb=4.0,
            peak_cpu=4.0,
            peak_memory_gb=8.0,
        )
        provisioner = _TransitioningProvisioner(session_factory)
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=1,
                local_capacity_cpu_cores=8.0,
                local_capacity_memory_gb=24.0,
            ),
        )

        assert await worker.run_once() == 1

        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            blocked = await repo.get(blocked_id)
            fitting = await repo.get(fitting_id)
            blocked_decisions = await QueueDecisionRepository(s).list_for_workspace(blocked_id)

        assert provisioner.calls == [fitting_id]
        assert blocked is not None
        assert fitting is not None
        assert blocked.status == WorkspaceStatus.requested.value
        assert fitting.status == WorkspaceStatus.ready.value
        assert any(
            decision.reason_code == "LOCAL_CAPACITY_DEFERRED" for decision in blocked_decisions
        )

    @pytest.mark.unit
    async def test_requested_capacity_gate_scans_past_blocked_candidate_window(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        now = datetime.now(UTC)
        candidate_window = _scheduler_candidate_fetch_limit(1)
        blocked_ids: list[str] = []
        for index in range(candidate_window + 1):
            blocked_id = await _create_requested(
                session_factory,
                origin_repo,
                f"blocked-capacity-window-{index}",
                create_task_attempt=True,
                created_at=now + timedelta(seconds=index),
            )
            await _reserve_workspace(
                session_factory,
                blocked_id,
                steady_cpu=0.0,
                steady_memory_gb=0.0,
                peak_cpu=2.0,
                peak_memory_gb=0.0,
            )
            blocked_ids.append(blocked_id)
        fitting_id = await _create_requested(
            session_factory,
            origin_repo,
            "fitting-after-blocked-capacity-window",
            create_task_attempt=True,
            created_at=now + timedelta(seconds=candidate_window + 1),
        )
        await _reserve_workspace(
            session_factory,
            fitting_id,
            steady_cpu=0.0,
            steady_memory_gb=0.0,
            peak_cpu=1.0,
            peak_memory_gb=0.0,
        )
        provisioner = _TransitioningProvisioner(session_factory)
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=1,
                local_capacity_cpu_cores=1.0,
            ),
        )

        assert await worker.run_once() == 1

        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            blocked = [await repo.get(workspace_id) for workspace_id in blocked_ids]
            fitting = await repo.get(fitting_id)
            blocked_decisions = {
                workspace_id: await QueueDecisionRepository(s).list_for_workspace(workspace_id)
                for workspace_id in blocked_ids
            }

        assert provisioner.calls == [fitting_id]
        assert fitting is not None
        assert fitting.status == WorkspaceStatus.ready.value
        assert all(workspace is not None for workspace in blocked)
        assert all(workspace.status == WorkspaceStatus.requested.value for workspace in blocked)
        assert all(
            any(decision.reason_code == "LOCAL_CAPACITY_UNSATISFIABLE" for decision in decisions)
            for decisions in blocked_decisions.values()
        )

    @pytest.mark.unit
    async def test_requested_capacity_gate_bounds_fully_blocked_page_scan(
        self,
        monkeypatch: pytest.MonkeyPatch,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        now = datetime.now(UTC)
        candidate_window = _scheduler_candidate_fetch_limit(1)
        scanned_page_limit = 1 + worker_module._SCHEDULER_PRIORITY_REFILL_PAGES_AFTER_FILL
        blocked_ids: list[str] = []
        for index in range(candidate_window * (scanned_page_limit + 2)):
            blocked_id = await _create_requested(
                session_factory,
                origin_repo,
                f"bounded-capacity-blocked-{index}",
                create_task_attempt=True,
                created_at=now + timedelta(seconds=index),
            )
            await _reserve_workspace(
                session_factory,
                blocked_id,
                steady_cpu=0.0,
                steady_memory_gb=0.0,
                peak_cpu=2.0,
                peak_memory_gb=0.0,
            )
            blocked_ids.append(blocked_id)

        query_cursors: list[SchedulerOrderCursor | None] = []
        page_end_cursors: list[SchedulerOrderCursor] = []
        original_list_schedulable_workspaces = WorkspaceRepository.list_schedulable_workspaces

        async def _list_schedulable_workspaces(
            self: WorkspaceRepository,
            *,
            status: WorkspaceStatus,
            limit: int,
            exclude_ids: set[str] | None = None,
            after: SchedulerOrderCursor | None = None,
            scoring_at: datetime | None = None,
        ) -> list[Workspace]:
            assert status == WorkspaceStatus.requested
            assert limit == candidate_window
            query_cursors.append(after)
            if after is not None:
                assert page_end_cursors
                assert after == page_end_cursors[-1]
            scoring_time = _scheduler_test_scoring_time(after=after, scoring_at=scoring_at)
            page = await original_list_schedulable_workspaces(
                self,
                status=status,
                limit=limit,
                exclude_ids=exclude_ids,
                after=after,
                scoring_at=scoring_time,
            )
            if page:
                page_end_cursors.append(
                    _scheduler_order_cursor_for_workspace(page[-1], scoring_at=scoring_time)
                )
            return page

        monkeypatch.setattr(
            WorkspaceRepository,
            "list_schedulable_workspaces",
            _list_schedulable_workspaces,
            raising=False,
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=1,
                local_capacity_cpu_cores=1.0,
            ),
        )

        assert await worker.run_once() == 0

        assert len(query_cursors) == scanned_page_limit
        assert query_cursors[0] is None
        assert query_cursors[1] == page_end_cursors[0]

        scanned_ids = set(blocked_ids[: candidate_window * scanned_page_limit])
        unscanned_ids = set(blocked_ids[candidate_window * scanned_page_limit :])
        async with session_factory() as s:
            decisions = {
                workspace_id: await QueueDecisionRepository(s).list_for_workspace(workspace_id)
                for workspace_id in blocked_ids
            }

        assert all(
            any(
                decision.reason_code == "LOCAL_CAPACITY_UNSATISFIABLE"
                for decision in decisions[workspace_id]
            )
            for workspace_id in scanned_ids
        )
        assert all(decisions[workspace_id] == [] for workspace_id in unscanned_ids)

    @pytest.mark.unit
    async def test_requested_capacity_gate_resumes_after_bounded_blocked_scan(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        now = datetime.now(UTC)
        candidate_window = _scheduler_candidate_fetch_limit(1)
        scanned_page_limit = 1 + worker_module._SCHEDULER_PRIORITY_REFILL_PAGES_AFTER_FILL
        blocked_ids: list[str] = []
        for index in range(candidate_window * scanned_page_limit):
            blocked_id = await _create_requested(
                session_factory,
                origin_repo,
                f"resume-capacity-blocked-{index}",
                create_task_attempt=True,
                created_at=now + timedelta(seconds=index),
            )
            await _reserve_workspace(
                session_factory,
                blocked_id,
                steady_cpu=0.0,
                steady_memory_gb=0.0,
                peak_cpu=2.0,
                peak_memory_gb=0.0,
            )
            blocked_ids.append(blocked_id)
        fitting_id = await _create_requested(
            session_factory,
            origin_repo,
            "fitting-after-bounded-blocked-scan",
            create_task_attempt=True,
            created_at=now + timedelta(seconds=len(blocked_ids)),
        )
        await _reserve_workspace(
            session_factory,
            fitting_id,
            steady_cpu=0.0,
            steady_memory_gb=0.0,
            peak_cpu=1.0,
            peak_memory_gb=0.0,
        )
        provisioner = _TransitioningProvisioner(session_factory)
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=1,
                local_capacity_cpu_cores=1.0,
            ),
        )

        assert await worker.run_once() == 0
        assert provisioner.calls == []
        assert await worker.run_once() == 1

        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            blocked = [await repo.get(workspace_id) for workspace_id in blocked_ids]
            fitting = await repo.get(fitting_id)

        assert provisioner.calls == [fitting_id]
        assert fitting is not None
        assert fitting.status == WorkspaceStatus.ready.value
        assert all(workspace is not None for workspace in blocked)
        assert all(workspace.status == WorkspaceStatus.requested.value for workspace in blocked)

    @pytest.mark.unit
    async def test_requested_capacity_gate_preserves_scheduler_priority_before_fifo(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        now = datetime.now(UTC)
        older_low_id = await _create_requested(
            session_factory,
            origin_repo,
            "older-low-priority-capacity-request",
            create_task_attempt=True,
            task_class="docs_task",
            task_policy={"scheduler": {"base_priority": 0}},
            created_at=now - timedelta(minutes=10),
        )
        await _reserve_workspace(
            session_factory,
            older_low_id,
            steady_cpu=2.0,
            steady_memory_gb=4.0,
            peak_cpu=4.0,
            peak_memory_gb=8.0,
        )
        younger_high_id = await _create_requested(
            session_factory,
            origin_repo,
            "younger-high-priority-capacity-request",
            create_task_attempt=True,
            task_class="migration_task",
            task_policy={"scheduler": {"base_priority": 100, "human_boost": 5}},
            created_at=now - timedelta(minutes=5),
        )
        await _reserve_workspace(
            session_factory,
            younger_high_id,
            steady_cpu=2.0,
            steady_memory_gb=4.0,
            peak_cpu=4.0,
            peak_memory_gb=8.0,
        )
        provisioner = _TransitioningProvisioner(session_factory)
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=1,
                local_capacity_cpu_cores=4.0,
                local_capacity_memory_gb=8.0,
            ),
        )

        assert await worker.run_once() == 1

        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            older_low = await repo.get(older_low_id)
            younger_high = await repo.get(younger_high_id)

        assert provisioner.calls == [younger_high_id]
        assert older_low is not None
        assert younger_high is not None
        assert older_low.status == WorkspaceStatus.requested.value
        assert younger_high.status == WorkspaceStatus.ready.value

    @pytest.mark.unit
    async def test_concurrent_capacity_claims_do_not_oversubscribe_requested_workspaces(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        first_id = await _create_requested(
            session_factory,
            origin_repo,
            "capacity-race-first",
            create_task_attempt=True,
        )
        second_id = await _create_requested(
            session_factory,
            origin_repo,
            "capacity-race-second",
            create_task_attempt=True,
        )
        for workspace_id in (first_id, second_id):
            await _reserve_workspace(
                session_factory,
                workspace_id,
                steady_cpu=2.0,
                steady_memory_gb=4.0,
                peak_cpu=4.0,
                peak_memory_gb=8.0,
            )

        provisioner = _TransitioningProvisioner(session_factory)
        config = WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_provisions=1,
            local_capacity_cpu_cores=4.0,
            local_capacity_memory_gb=8.0,
        )
        worker_a = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            config=config,
        )
        worker_b = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            config=config,
        )

        async def _list_first() -> list[str]:
            return [first_id]

        async def _list_second() -> list[str]:
            return [second_id]

        worker_a._list_requested = _list_first  # type: ignore[method-assign]
        worker_b._list_requested = _list_second  # type: ignore[method-assign]

        dispatched = await asyncio.gather(worker_a.run_once(), worker_b.run_once())

        assert sum(dispatched) == 1
        assert len(provisioner.calls) == 1

        async with session_factory() as s:
            statuses = {
                workspace_id: (await WorkspaceRepository(s).get(workspace_id)).status  # type: ignore[union-attr]
                for workspace_id in (first_id, second_id)
            }

        assert list(statuses.values()).count(WorkspaceStatus.ready.value) == 1
        assert list(statuses.values()).count(WorkspaceStatus.requested.value) == 1

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
    async def test_capacity_requested_path_skips_prelock_status_filter(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        requested_id = await _create_requested(
            session_factory,
            origin_repo,
            "capacity-race-requested-log",
            create_task_attempt=True,
        )

        provisioner = _TransitioningProvisioner(session_factory)
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=1,
                local_capacity_cpu_cores=8.0,
            ),
        )

        async def _unexpected_filter_current_status(
            workspace_ids: list[str],
            *,
            expected: WorkspaceStatus,
            action: str,
        ) -> list[str]:
            del workspace_ids, expected, action
            raise AssertionError("capacity claims should not run the pre-lock status filter")

        worker._filter_current_status = _unexpected_filter_current_status  # type: ignore[method-assign]

        with structlog.testing.capture_logs() as captured:
            assert await worker.run_once() == 1

        assert provisioner.calls == [requested_id]
        assert not any(event.get("event") == "worker.skip_stale_dispatch" for event in captured)

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

    @pytest.mark.unit
    async def test_requested_ordered_decision_persistent_transient_commit_failure_prevents_dispatch(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        requested_id = await _create_requested(
            session_factory,
            origin_repo,
            "record-before-provision-persistent-transient-commit",
            create_task_attempt=True,
        )
        provisioner = _TransitioningProvisioner(session_factory)
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=1),
        )

        async def _claim_without_commit(workspace_ids: list[str]) -> list[str]:
            assert workspace_ids == [requested_id]
            return workspace_ids

        async def _list_requested_without_db() -> list[str]:
            return [requested_id]

        async def _filter_current_requested_status(
            workspace_ids: list[str],
            *,
            expected: WorkspaceStatus,
            action: str,
        ) -> list[str]:
            assert workspace_ids == [requested_id]
            assert expected == WorkspaceStatus.requested
            assert action == "provision"
            return workspace_ids

        async def _skip_secret_lease_scan() -> None:
            return None

        commits = 0

        async def _fail_commit(_session: AsyncSession) -> None:
            nonlocal commits
            commits += 1
            raise InterfaceError(
                "COMMIT",
                {},
                RuntimeError("connection is closed"),
                connection_invalidated=True,
            )

        worker._list_requested = _list_requested_without_db  # type: ignore[method-assign]
        worker._filter_current_status = _filter_current_requested_status  # type: ignore[method-assign]
        worker._claim_requested_ids = _claim_without_commit  # type: ignore[method-assign]
        worker._maybe_expire_due_secret_leases = _skip_secret_lease_scan  # type: ignore[method-assign]
        monkeypatch.setattr(AsyncSession, "commit", _fail_commit)

        with pytest.raises(InterfaceError, match="connection is closed"):
            await worker.run_once()

        async with session_factory() as session:
            decisions = await QueueDecisionRepository(session).list_for_workspace(requested_id)

        assert provisioner.calls == []
        assert commits == 2
        assert decisions == []

    @pytest.mark.unit
    async def test_requested_ordered_decision_ambiguous_commit_retries_without_duplicate(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        requested_id = await _create_requested(
            session_factory,
            origin_repo,
            "record-before-provision-ambiguous-commit",
            create_task_attempt=True,
        )
        provisioner = _TransitioningProvisioner(session_factory)
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=1),
        )

        async def _list_requested_without_db() -> list[str]:
            return [requested_id]

        async def _filter_current_requested_status(
            workspace_ids: list[str],
            *,
            expected: WorkspaceStatus,
            action: str,
        ) -> list[str]:
            assert workspace_ids == [requested_id]
            assert expected == WorkspaceStatus.requested
            assert action == "provision"
            return workspace_ids

        async def _skip_secret_lease_scan() -> None:
            return None

        commits = 0
        original_commit = AsyncSession.commit

        async def _raise_after_ordered_decision_commit(session: AsyncSession) -> None:
            nonlocal commits
            commits += 1
            await original_commit(session)
            if commits == 2:
                raise InterfaceError(
                    "COMMIT",
                    {},
                    RuntimeError("connection is closed"),
                    connection_invalidated=True,
                )

        worker._list_requested = _list_requested_without_db  # type: ignore[method-assign]
        worker._filter_current_status = _filter_current_requested_status  # type: ignore[method-assign]
        worker._maybe_expire_due_secret_leases = _skip_secret_lease_scan  # type: ignore[method-assign]
        monkeypatch.setattr(AsyncSession, "commit", _raise_after_ordered_decision_commit)

        assert await worker.run_once() == 1

        async with session_factory() as session:
            decisions = await QueueDecisionRepository(session).list_for_workspace(requested_id)

        assert provisioner.calls == [requested_id]
        assert commits == 4
        assert len(decisions) == 1
        assert decisions[0].reason_code == "ORDERED_REQUESTED_PROVISIONING"

    @pytest.mark.unit
    async def test_ordered_decision_retry_dedupes_when_newer_decision_is_latest(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        requested_id = await _create_requested(
            session_factory,
            origin_repo,
            "record-before-provision-ambiguous-commit-with-newer-latest",
            create_task_attempt=True,
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=1),
        )
        original_commit = AsyncSession.commit
        raised_after_ordered_commit = False

        async def _raise_after_ordered_commit_and_insert_newer_decision(
            session: AsyncSession,
        ) -> None:
            nonlocal raised_after_ordered_commit
            await original_commit(session)
            if raised_after_ordered_commit:
                return
            raised_after_ordered_commit = True

            async with session_factory() as concurrent_session:
                attempt = await TaskAttemptRepository(concurrent_session).get_by_workspace_id(
                    requested_id
                )
                assert attempt is not None
                await QueueDecisionRepository(concurrent_session).create(
                    workspace_id=requested_id,
                    task_id=attempt.task_id,
                    attempt_id=attempt.id,
                    decision="deferred",
                    reason_code="CONCURRENT_SCHEDULER_DECISION",
                    class_priority=0,
                    computed_priority=0,
                    age_boost=0,
                    retry_bonus=0,
                    resource_summary={},
                    overlap_risk_summary={},
                    score_summary={},
                    decided_at=datetime.now(UTC) + timedelta(days=1),
                )
                await original_commit(concurrent_session)

            raise InterfaceError(
                "COMMIT",
                {},
                RuntimeError("connection is closed"),
                connection_invalidated=True,
            )

        monkeypatch.setattr(
            AsyncSession,
            "commit",
            _raise_after_ordered_commit_and_insert_newer_decision,
        )

        await worker._record_ordered_decisions(  # noqa: SLF001
            [requested_id],
            reason_code="ORDERED_REQUESTED_PROVISIONING",
        )

        async with session_factory() as session:
            decisions = await QueueDecisionRepository(session).list_for_workspace(requested_id)

        ordered_decisions = [
            decision
            for decision in decisions
            if decision.reason_code == "ORDERED_REQUESTED_PROVISIONING"
        ]
        assert raised_after_ordered_commit is True
        assert len(decisions) == 2
        assert len(ordered_decisions) == 1
        assert decisions[0].reason_code == "CONCURRENT_SCHEDULER_DECISION"

    @pytest.mark.unit
    async def test_run_once_retries_scheduler_read_after_closed_connection(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        requested_id = await _create_requested(
            session_factory,
            origin_repo,
            "closed-connection-requested",
            create_task_attempt=True,
        )
        provisioner = _TransitioningProvisioner(session_factory)
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=provisioner,  # type: ignore[arg-type]
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=1),
        )
        original = WorkspaceRepository.list_schedulable_workspaces
        failures_remaining = 1
        scheduler_read_sessions: list[AsyncSession] = []
        scheduler_read_session_ids: list[int] = []

        async def _flaky_list_schedulable_workspaces(
            self: WorkspaceRepository,
            *args: object,
            **kwargs: object,
        ) -> list[Workspace]:
            nonlocal failures_remaining
            scheduler_read_sessions.append(self._session)
            scheduler_read_session_ids.append(id(self._session))
            if failures_remaining:
                failures_remaining -= 1
                raise _closed_connection_error()
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(
            WorkspaceRepository,
            "list_schedulable_workspaces",
            _flaky_list_schedulable_workspaces,
        )

        assert await worker.run_once() == 1

        assert len(scheduler_read_sessions) == 2
        assert scheduler_read_sessions[1] is not scheduler_read_sessions[0]
        assert len(scheduler_read_session_ids) == 2
        assert scheduler_read_session_ids[1] != scheduler_read_session_ids[0]
        assert provisioner.calls == [requested_id]
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get(requested_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.ready.value

    @pytest.mark.unit
    async def test_provider_recovery_filter_retries_closed_connection(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        ready_id = await _create_ready(
            session_factory,
            origin_repo,
            "filter-outside-read-retry",
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
        failures_remaining = 1
        filter_attempts = 0
        filter_sessions: list[AsyncSession] = []
        filter_session_ids: list[int] = []
        retry_attempts: list[int] = []
        original_filter = worker._filter_provider_recovery_suppressed

        async def _flaky_filter(
            session: AsyncSession,
            workspaces: list[Workspace] | list[str],
        ) -> list[str]:
            nonlocal failures_remaining, filter_attempts
            filter_attempts += 1
            filter_sessions.append(session)
            filter_session_ids.append(id(session))
            if failures_remaining:
                failures_remaining -= 1
                raise _closed_connection_error()
            return await original_filter(session, workspaces)

        async def _record_retry(_exc: BaseException, attempt: int) -> None:
            retry_attempts.append(attempt)

        worker._filter_provider_recovery_suppressed = _flaky_filter  # type: ignore[method-assign]
        worker._log_transient_db_retry = _record_retry  # type: ignore[method-assign]

        assert await worker._list_ready(limit=1) == [ready_id]  # noqa: SLF001
        assert filter_attempts == 2
        assert len(filter_sessions) == 2
        assert filter_sessions[1] is not filter_sessions[0]
        assert len(filter_session_ids) == 2
        assert filter_session_ids[1] != filter_session_ids[0]
        assert retry_attempts == [1]

    @pytest.mark.unit
    async def test_scheduler_deferred_decisions_are_not_replayed_after_commit_failure(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        not_before = datetime.now(UTC) + timedelta(minutes=10)
        ready_id = await _create_ready(
            session_factory,
            origin_repo,
            "scheduler-commit-boundary",
            agent="gemini",
            task_class="refactor_task",
            task_policy={
                "agent_model": "gemini-2.5-pro",
                "provider_recovery_state": {
                    "not_before": not_before.isoformat(),
                    "action": "retry",
                },
            },
            create_task_attempt=True,
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
        original_commit = AsyncSession.commit
        failures_remaining = 1
        commit_attempts = 0

        async def _commit_then_closed(session: AsyncSession) -> None:
            nonlocal failures_remaining, commit_attempts
            commit_attempts += 1
            await original_commit(session)
            if failures_remaining:
                failures_remaining -= 1
                raise _closed_connection_error()

        monkeypatch.setattr(AsyncSession, "commit", _commit_then_closed)

        with pytest.raises(InterfaceError, match="connection is closed"):
            await worker._list_ready(limit=1)  # noqa: SLF001

        async with session_factory() as session:
            decisions = await QueueDecisionRepository(session).list_for_workspace(ready_id)

        assert commit_attempts == 1
        assert len(decisions) == 1
        assert decisions[0].decision == "deferred"
        assert decisions[0].reason_code == "PROVIDER_RECOVERY_NOT_BEFORE"

    @pytest.mark.unit
    async def test_provider_recovery_filter_keeps_scheduler_locks_until_decision_commit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        ready_id = await _create_ready(
            session_factory,
            origin_repo,
            "filter-keeps-scheduler-locks",
        )
        scheduler_read_session_ids: list[int] = []
        filter_session_ids: list[int] = []
        original_list = WorkspaceRepository.list_schedulable_workspaces

        async def _list_schedulable_workspaces(
            self: WorkspaceRepository,
            *,
            status: WorkspaceStatus,
            limit: int,
            exclude_ids: set[str] | None = None,
            after: SchedulerOrderCursor | None = None,
            scoring_at: datetime | None = None,
        ) -> list[Workspace]:
            del scoring_at
            scheduler_read_session_ids.append(id(self._session))
            return await original_list(
                self,
                status=status,
                limit=limit,
                exclude_ids=exclude_ids,
                after=after,
            )

        async def _filter_provider_recovery_suppressed(
            session: AsyncSession,
            workspaces: list[Workspace] | list[str],
        ) -> list[str]:
            filter_session_ids.append(id(session))
            assert not isinstance(workspaces[0], str)
            return [workspace.id for workspace in workspaces]

        monkeypatch.setattr(
            WorkspaceRepository,
            "list_schedulable_workspaces",
            _list_schedulable_workspaces,
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
        worker._filter_provider_recovery_suppressed = (  # type: ignore[method-assign]
            _filter_provider_recovery_suppressed
        )

        assert await worker._list_ready(limit=1) == [ready_id]  # noqa: SLF001

        assert len(scheduler_read_session_ids) == 1
        assert filter_session_ids == scheduler_read_session_ids


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
    async def test_monitor_claim_refresh_recomputes_lease_expiry_between_retries(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "monitor-claim-refresh-fresh-expiry",
        )
        base_time = datetime(2026, 5, 8, 12, 0, tzinfo=UTC)

        class _RetryClock:
            calls = 0

            @classmethod
            def now(cls, tz: object) -> datetime:
                assert tz is UTC
                cls.calls += 1
                return base_time + timedelta(seconds=cls.calls)

        monkeypatch.setattr("awf.control.worker.datetime", _RetryClock)
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=0,
                max_concurrent_executions=1,
                monitor_claim_lease_seconds=120,
            ),
        )
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            assert ws is not None
            ws.monitor_claimed_by = worker._worker_id
            ws.monitor_claim_expires_at = base_time
            await session.commit()

        original = WorkspaceRepository.refresh_monitoring_pr_claim
        failures_remaining = 1
        lease_expiries: list[datetime] = []
        refresh_sessions: list[AsyncSession] = []
        refresh_session_ids: list[int] = []

        async def _flaky_refresh_monitoring_pr_claim(
            self: WorkspaceRepository,
            workspace_id: str,
            *,
            owner_id: str,
            lease_expires_at: datetime,
        ) -> bool:
            nonlocal failures_remaining
            lease_expiries.append(lease_expires_at)
            refresh_sessions.append(self._session)
            refresh_session_ids.append(id(self._session))
            if failures_remaining:
                failures_remaining -= 1
                raise _closed_connection_error()
            return await original(
                self,
                workspace_id,
                owner_id=owner_id,
                lease_expires_at=lease_expires_at,
            )

        monkeypatch.setattr(
            WorkspaceRepository,
            "refresh_monitoring_pr_claim",
            _flaky_refresh_monitoring_pr_claim,
        )

        assert await worker._refresh_monitoring_pr_claim(workspace_id) is True

        assert lease_expiries == [
            base_time + timedelta(seconds=121),
            base_time + timedelta(seconds=122),
        ]
        assert len(refresh_sessions) == 2
        assert refresh_sessions[1] is not refresh_sessions[0]
        assert len(refresh_session_ids) == 2
        assert refresh_session_ids[1] != refresh_session_ids[0]
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            assert ws is not None
            assert ws.monitor_claim_expires_at is not None
            assert ws.monitor_claim_expires_at.replace(tzinfo=UTC) == lease_expiries[-1]

    @pytest.mark.unit
    async def test_execution_claim_refresh_retries_closed_connection_without_losing_owner(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace_id = await _create_ready(
            session_factory,
            origin_repo,
            "claim-refresh-closed-connection",
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
                execution_claim_lease_seconds=120,
            ),
        )
        old_expiry = datetime.now(UTC) + timedelta(seconds=5)
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            assert ws is not None
            ws.execution_claimed_by = worker._worker_id
            ws.execution_claim_expires_at = old_expiry
            await session.commit()

        original = WorkspaceRepository.refresh_execution_claim
        failures_remaining = 1
        refresh_sessions: list[AsyncSession] = []
        refresh_session_ids: list[int] = []

        async def _flaky_refresh_execution_claim(
            self: WorkspaceRepository,
            workspace_id: str,
            *,
            owner_id: str,
            lease_expires_at: datetime,
        ) -> bool:
            nonlocal failures_remaining
            refresh_sessions.append(self._session)
            refresh_session_ids.append(id(self._session))
            if failures_remaining:
                failures_remaining -= 1
                raise _closed_connection_error()
            return await original(
                self,
                workspace_id,
                owner_id=owner_id,
                lease_expires_at=lease_expires_at,
            )

        monkeypatch.setattr(
            WorkspaceRepository,
            "refresh_execution_claim",
            _flaky_refresh_execution_claim,
        )

        assert await worker._refresh_execution_claim(workspace_id) is True

        assert len(refresh_sessions) == 2
        assert refresh_sessions[1] is not refresh_sessions[0]
        assert len(refresh_session_ids) == 2
        assert refresh_session_ids[1] != refresh_session_ids[0]
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            assert ws is not None
            assert ws.execution_claimed_by == worker._worker_id
            assert ws.execution_claim_expires_at is not None
            assert ws.execution_claim_expires_at > old_expiry

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
    async def test_ready_execution_scores_beyond_priority_refill_page(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        low_ids = [
            await _create_ready(
                session_factory,
                origin_repo,
                f"refill-window-low-priority-{index}",
                task_class="docs_task",
                task_policy={"scheduler": {"base_priority": 0}},
            )
            for index in range(_scheduler_candidate_fetch_limit(1) * 2)
        ]
        urgent_id = await _create_ready(
            session_factory,
            origin_repo,
            "urgent-ready-after-refill-window",
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
    async def test_ready_refill_keeps_final_order_on_frozen_scoring_timestamp(
        self,
        monkeypatch: pytest.MonkeyPatch,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        frozen_scoring_at = datetime(2026, 1, 1, 0, 14, 59, tzinfo=UTC)
        drifted_scoring_at = frozen_scoring_at + timedelta(seconds=1)
        older_id = await _create_ready(
            session_factory,
            origin_repo,
            "age-boost-after-frozen-score",
            task_class="docs_task",
            task_policy={"scheduler": {"base_priority": 0}},
        )
        priority_id = await _create_ready(
            session_factory,
            origin_repo,
            "priority-at-frozen-score",
            task_class="docs_task",
            task_policy={"scheduler": {"base_priority": 1}},
        )
        older_created_at = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        priority_created_at = older_created_at + timedelta(minutes=1)
        async with session_factory() as session:
            await session.execute(
                update(Workspace)
                .where(Workspace.id == older_id)
                .values(created_at=older_created_at, updated_at=older_created_at)
            )
            await session.execute(
                update(Workspace)
                .where(Workspace.id == priority_id)
                .values(created_at=priority_created_at, updated_at=priority_created_at)
            )
            await session.commit()

        class DriftedDateTime(datetime):
            calls = 0

            @classmethod
            def now(cls, tz: object = None) -> datetime:
                del tz
                cls.calls += 1
                if cls.calls == 1:
                    return frozen_scoring_at
                return drifted_scoring_at

        async def _list_schedulable_workspaces(
            self: WorkspaceRepository,
            *,
            status: WorkspaceStatus,
            limit: int,
            exclude_ids: set[str] | None = None,
            after: SchedulerOrderCursor | None = None,
            scoring_at: datetime | None = None,
        ) -> list[Workspace]:
            del exclude_ids, after
            assert status == WorkspaceStatus.ready
            assert scoring_at == frozen_scoring_at
            result = await self._session.execute(
                select(Workspace).where(Workspace.id.in_([older_id, priority_id]))
            )
            rows = list(result.scalars())
            scored = sorted(
                (
                    (scheduler_score_from_workspace(workspace, now=scoring_at), workspace)
                    for workspace in rows
                ),
                key=lambda item: scheduler_order_key(item[0]),
            )
            return [workspace for _score, workspace in scored][:limit]

        async def _allow_all_scheduler_candidates(
            session: AsyncSession,
            workspaces: list[Workspace],
            *,
            limit: int,
            scoring_at: datetime,
        ) -> list[str]:
            del session, limit, scoring_at
            return [workspace.id for workspace in workspaces]

        monkeypatch.setattr(worker_module, "datetime", DriftedDateTime)
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
        worker._filter_scheduler_candidate_workspaces = (  # type: ignore[method-assign]
            _allow_all_scheduler_candidates
        )

        assert await worker._list_ready(limit=1) == [priority_id]  # noqa: SLF001

    @pytest.mark.unit
    async def test_ready_scan_stops_after_dispatch_slots_are_filled(
        self,
        monkeypatch: pytest.MonkeyPatch,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        candidate_limit = _scheduler_candidate_fetch_limit(1)
        low_ids = [
            await _create_ready(
                session_factory,
                origin_repo,
                f"bounded-scan-low-{index}",
                task_class="docs_task",
                task_policy={"scheduler": {"base_priority": 0}},
            )
            for index in range(candidate_limit)
        ]
        urgent_id = await _create_ready(
            session_factory,
            origin_repo,
            "bounded-scan-urgent",
            task_class="migration_task",
            task_policy={"scheduler": {"base_priority": 100, "human_boost": 5}},
        )
        tail_ids = [
            await _create_ready(
                session_factory,
                origin_repo,
                f"bounded-scan-tail-{index}",
                task_class="docs_task",
                task_policy={"scheduler": {"base_priority": 0}},
            )
            for index in range(candidate_limit)
        ]
        ordered_ids = [*low_ids, urgent_id, *tail_ids]
        base_created_at = datetime(2026, 1, 1, tzinfo=UTC)
        created_at_by_id: dict[str, datetime] = {}
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
        query_cursors: list[SchedulerOrderCursor | None] = []
        page_end_cursors: list[SchedulerOrderCursor] = []
        original_list_schedulable_workspaces = WorkspaceRepository.list_schedulable_workspaces

        async def _list_schedulable_workspaces(
            self: WorkspaceRepository,
            *,
            status: WorkspaceStatus,
            limit: int,
            exclude_ids: set[str] | None = None,
            after: SchedulerOrderCursor | None = None,
            scoring_at: datetime | None = None,
        ) -> list[Workspace]:
            assert status == WorkspaceStatus.ready
            assert limit == candidate_limit
            query_cursors.append(after)
            if after is not None:
                assert page_end_cursors
                assert after == page_end_cursors[-1]
            scoring_time = _scheduler_test_scoring_time(after=after, scoring_at=scoring_at)
            page = await original_list_schedulable_workspaces(
                self,
                status=status,
                limit=limit,
                exclude_ids=exclude_ids,
                after=after,
                scoring_at=scoring_time,
            )
            if page:
                page_end_cursors.append(
                    _scheduler_order_cursor_for_workspace(page[-1], scoring_at=scoring_time)
                )
            return page

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

        assert await worker._list_ready(limit=1) == [urgent_id]  # noqa: SLF001
        assert len(query_cursors) == 2
        assert query_cursors[0] is None
        assert query_cursors[1] is not None
        assert query_cursors[1] == page_end_cursors[0]
        assert query_cursors[1].queued_at == created_at_by_id[query_cursors[1].workspace_id]
        assert all(
            cursor is None or cursor.workspace_id not in tail_ids for cursor in query_cursors
        )

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
            if normalized.lstrip().startswith("SELECT") and "FROM WORKSPACES" in normalized:
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
        base_created_at = datetime(2026, 1, 1, tzinfo=UTC)
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
        queries: list[tuple[SchedulerOrderCursor | None, set[str]]] = []
        page_end_cursors: list[SchedulerOrderCursor] = []
        original_list_schedulable_workspaces = WorkspaceRepository.list_schedulable_workspaces

        async def _list_schedulable_workspaces(
            self: WorkspaceRepository,
            *,
            status: WorkspaceStatus,
            limit: int,
            exclude_ids: set[str] | None = None,
            after: SchedulerOrderCursor | None = None,
            scoring_at: datetime | None = None,
        ) -> list[Workspace]:
            assert status == WorkspaceStatus.ready
            excluded = set(exclude_ids or set())
            queries.append((after, excluded))
            if after is not None:
                assert page_end_cursors
                assert after == page_end_cursors[-1]
            scoring_time = _scheduler_test_scoring_time(after=after, scoring_at=scoring_at)
            page = await original_list_schedulable_workspaces(
                self,
                status=status,
                limit=limit,
                exclude_ids=exclude_ids,
                after=after,
                scoring_at=scoring_time,
            )
            if page:
                page_end_cursors.append(
                    _scheduler_order_cursor_for_workspace(page[-1], scoring_at=scoring_time)
                )
            return page

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
        assert len(queries) == 2
        assert queries[0] == (None, base_exclude_ids)
        assert queries[1][0] is not None
        assert queries[1][0] == page_end_cursors[0]
        assert queries[1][0].queued_at == created_at_by_id[suppressed_ids[-1]]
        assert queries[1][0].workspace_id == suppressed_ids[-1]
        assert queries[1][1] == base_exclude_ids

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
            decisions = await QueueDecisionRepository(session).list_for_workspace(suppressed_ids[0])

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

        assert await asyncio.wait_for(worker.run_once(), timeout=WORKER_TEST_TIMEOUT_SECONDS) == 1
        await asyncio.wait_for(started.wait(), timeout=WORKER_TEST_TIMEOUT_SECONDS)

        requested_id = await _create_requested(session_factory, origin_repo, "new-request")
        assert await asyncio.wait_for(worker.run_once(), timeout=WORKER_TEST_TIMEOUT_SECONDS) == 1
        assert provisioner.calls == [requested_id]
        assert executor.calls == [ready_id]

        release.set()
        await asyncio.wait_for(
            worker.wait_for_execution_tasks(), timeout=WORKER_TEST_TIMEOUT_SECONDS
        )

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
            await asyncio.wait_for(active_task, timeout=WORKER_TEST_TIMEOUT_SECONDS)
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
            after: SchedulerOrderCursor | None = None,
            scoring_at: datetime | None = None,
        ) -> list[Workspace]:
            del self, after, scoring_at
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

        assert (
            await worker._list_monitoring_pr(
                limit=2,
                exclude_ids={monitor_ids[0]},
            )
            == monitor_ids[1:]
        )

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
    @pytest.mark.parametrize("final_status", [WorkspaceStatus.cancelled, WorkspaceStatus.destroyed])
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
        await asyncio.wait_for(started.wait(), timeout=WORKER_TEST_TIMEOUT_SECONDS)
        release.set()
        await asyncio.wait_for(asyncio.gather(*runs), timeout=WORKER_TEST_TIMEOUT_SECONDS)

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
        await asyncio.wait_for(started.wait(), timeout=WORKER_TEST_TIMEOUT_SECONDS)
        release.set()
        await asyncio.wait_for(
            asyncio.gather(
                worker_a.wait_for_execution_tasks(), worker_b.wait_for_execution_tasks()
            ),
            timeout=WORKER_TEST_TIMEOUT_SECONDS,
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
    async def test_stale_active_scan_preserves_monitor_provider_retry_cooldown(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        not_before = datetime.now(UTC) + timedelta(minutes=10)
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "provider-cooling-stale-monitor",
            agent="codex",
            task_policy={
                "agent_model": "gpt-5.3-codex-spark",
                "provider_recovery_state": {
                    "not_before": not_before.isoformat(),
                    "action": "retry",
                    "decision_reason_code": "PROVIDER_RETRY_DELAYED",
                },
            },
        )
        compose_project = f"awf_{monitor_id}"
        inspector = _RecordingRuntimeInspector({compose_project: _live_agent_snapshot()})
        cleaner = _RecordingRuntimeCleaner()
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=inspector,
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                stale_active_execution_scan_interval_seconds=0.0,
            ),
        )

        assert await worker.run_once() == 0
        await worker.wait_for_execution_tasks()

        assert executor.resume_calls == []
        assert inspector.calls == []
        assert cleaner.calls == []
        async with session_factory() as session:
            workspace = await WorkspaceRepository(session).get(monitor_id)
            assert workspace is not None
            assert workspace.status == WorkspaceStatus.monitoring_pr.value
            events = await WorkspaceEventRepository(session).list(workspace_id=monitor_id)

        assert not any(event.reason_code == "STALE_ACTIVE_EXECUTION" for event in events)

    @pytest.mark.unit
    async def test_stale_active_scan_preserves_monitor_provider_no_cooldown_open_circuit(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        not_before = datetime.now(UTC) - timedelta(minutes=10)
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "provider-open-circuit-no-cooldown-stale-monitor",
            agent="codex",
            task_policy={
                "agent_model": "gpt-5.3-codex-spark",
                "provider_recovery_state": {
                    "not_before": not_before.isoformat(),
                    "action": "retry",
                    "decision_reason_code": "PROVIDER_RETRY_DELAYED",
                },
            },
        )
        async with session_factory() as session:
            breaker_repo = ProviderModelCircuitBreakerRepository(session)
            breaker = await breaker_repo.record_failure(
                provider="openai",
                model="gpt-5.3-codex-spark",
                reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
                failure_fingerprint="capacity:openai:gpt-5.3-codex-spark",
                workspace_id=monitor_id,
                attempt_id=None,
                now=datetime.now(UTC),
                failure_threshold=1,
                cooldown_seconds=600,
            )
            breaker.cooldown_until = None
            await session.commit()

        compose_project = f"awf_{monitor_id}"
        inspector = _RecordingRuntimeInspector({compose_project: _live_agent_snapshot()})
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                stale_active_execution_scan_interval_seconds=0.0,
            ),
        )

        assert await worker.run_once() == 0
        await worker.wait_for_execution_tasks()

        assert inspector.calls == []
        assert cleaner.calls == []
        async with session_factory() as session:
            workspace = await WorkspaceRepository(session).get(monitor_id)
            assert workspace is not None
            assert workspace.status == WorkspaceStatus.monitoring_pr.value
            events = await WorkspaceEventRepository(session).list(workspace_id=monitor_id)

        assert not any(event.reason_code == "STALE_ACTIVE_EXECUTION" for event in events)

    @pytest.mark.unit
    async def test_stale_active_scan_preserves_due_monitor_provider_retry_for_resume(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        not_before = datetime.now(UTC) - timedelta(minutes=10)
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "provider-expired-retry-monitor",
            agent="codex",
            task_policy={
                "agent_model": "gpt-5.3-codex-spark",
                "provider_recovery_state": {
                    "not_before": not_before.isoformat(),
                    "action": "retry",
                    "decision_reason_code": "PROVIDER_RETRY_DELAYED",
                },
            },
        )
        compose_project = f"awf_{monitor_id}"
        inspector = _RecordingRuntimeInspector({compose_project: _live_agent_snapshot()})
        cleaner = _RecordingRuntimeCleaner()
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=inspector,
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=1,
                stale_active_execution_scan_interval_seconds=0.0,
            ),
        )

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        assert executor.resume_calls == [monitor_id]
        assert inspector.calls == [compose_project]
        assert cleaner.calls == []
        async with session_factory() as session:
            workspace = await WorkspaceRepository(session).get(monitor_id)
            assert workspace is not None
            assert workspace.status == WorkspaceStatus.monitoring_pr.value
            events = await WorkspaceEventRepository(session).list(workspace_id=monitor_id)

        assert not any(event.reason_code == "STALE_ACTIVE_EXECUTION" for event in events)

    @pytest.mark.unit
    async def test_stale_active_scan_recovering_provider_retry_uses_runtime_classification_for_running_monitor(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        not_before = datetime.now(UTC) - timedelta(minutes=10)
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "provider-retry-running-monitor-unhealthy",
            agent="codex",
            task_policy={
                "agent_model": "gpt-5.3-codex-spark",
                "provider_recovery_state": {
                    "not_before": not_before.isoformat(),
                    "action": "retry",
                    "decision_reason_code": "PROVIDER_RETRY_DELAYED",
                },
            },
        )
        compose_project = f"awf_{monitor_id}"
        inspector = _RecordingRuntimeInspector(
            {
                compose_project: RuntimeSnapshot(
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
            runtime_cleaner=_RecordingRuntimeCleaner(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                stale_active_execution_scan_interval_seconds=0.0,
            ),
        )

        await worker._recover_stale_active_executions()

        async with session_factory() as session:
            workspace = await WorkspaceRepository(session).get(monitor_id)
            assert workspace is not None
            assert workspace.status == WorkspaceStatus.monitoring_pr.value
            runtime_events = await WorkspaceEventRepository(session).list(
                workspace_id=monitor_id, event_type="workspace.runtime_stranded_detected"
            )

        assert runtime_events
        assert runtime_events[0].reason_code == "AGENT_CONTAINER_MISSING"
        assert runtime_events[0].payload is not None
        assert runtime_events[0].payload["decision"] == "remonitor_workspace"
        assert inspector.calls == [compose_project]

    @pytest.mark.unit
    async def test_stale_active_scan_recovers_monitor_provider_retry_after_breaker_close(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        not_before = datetime.now(UTC) - timedelta(minutes=10)
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "provider-closed-circuit-retry-monitor",
            agent="codex",
            task_policy={
                "agent_model": "gpt-5.3-codex-spark",
                "provider_recovery_state": {
                    "not_before": not_before.isoformat(),
                    "action": "retry",
                    "decision_reason_code": "PROVIDER_RETRY_DELAYED",
                },
            },
        )
        compose_project = f"awf_{monitor_id}"
        inspector = _RecordingRuntimeInspector(
            {compose_project: RuntimeSnapshot(stack_state="stopped", services=[])}
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            runtime_cleaner=_RecordingRuntimeCleaner(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                stale_active_execution_scan_interval_seconds=0.0,
            ),
        )

        assert await worker.run_once() == 0
        assert inspector.calls == [compose_project]

        async with session_factory() as session:
            workspace = await WorkspaceRepository(session).get(monitor_id)
            assert workspace is not None
            assert workspace.status == WorkspaceStatus.monitoring_pr.value
            events = await WorkspaceEventRepository(session).list(workspace_id=monitor_id)

        assert any(
            event.event_type == "workspace.runtime_stranded_detected"
            and event.reason_code == "STRANDED_WORKSPACE"
            and event.payload is not None
            and event.payload.get("decision") == "remonitor_workspace"
            for event in events
        )

    @pytest.mark.unit
    async def test_stale_active_scan_preserves_due_monitor_provider_fallback_for_resume(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "provider-fallback-stale-monitor",
            agent="codex",
            task_policy={
                "agent_model": "gpt-5.5",
                "provider_recovery_state": {
                    "action": "fallback",
                    "decision_reason_code": "PROVIDER_FALLBACK_SELECTED",
                    "source_reason_code": "AGENT_PROVIDER_CAPACITY_EXHAUSTED",
                    "target_agent": "codex",
                    "target_provider": "openai",
                    "target_model": "gpt-5.5",
                },
            },
        )
        compose_project = f"awf_{monitor_id}"
        inspector = _RecordingRuntimeInspector({compose_project: _live_agent_snapshot()})
        cleaner = _RecordingRuntimeCleaner()
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=inspector,
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=1,
                stale_active_execution_scan_interval_seconds=0.0,
            ),
        )

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        assert executor.resume_calls == [monitor_id]
        assert inspector.calls == []
        assert cleaner.calls == []
        async with session_factory() as session:
            workspace = await WorkspaceRepository(session).get(monitor_id)
            assert workspace is not None
            assert workspace.status == WorkspaceStatus.monitoring_pr.value
            events = await WorkspaceEventRepository(session).list(workspace_id=monitor_id)

        assert not any(event.reason_code == "STALE_ACTIVE_EXECUTION" for event in events)

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
            operations = await OperationRepository(session).list_all(workspace_id=monitor_id)
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
            "monitor_started_at": operations[0].payload["monitor_state"]["monitor_started_at"],
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
            operation for operation in operations if operation.type == OperationType.remonitor.value
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
            event for event in events if event.event_type == "workspace.monitor_recovery_started"
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

        assert await asyncio.wait_for(worker.run_once(), timeout=WORKER_TEST_TIMEOUT_SECONDS) == 1
        await asyncio.wait_for(executor.started.wait(), timeout=WORKER_TEST_TIMEOUT_SECONDS)

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
            operation for operation in operations if operation.type == OperationType.remonitor.value
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
        assert recovery_events[0].payload["claim_cleanup"] == operation.payload["claim_cleanup"]

        executor.release.set()
        await asyncio.wait_for(
            worker.wait_for_execution_tasks(), timeout=WORKER_TEST_TIMEOUT_SECONDS
        )

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            assert ws.execution_claimed_by is None
            assert ws.execution_claim_expires_at is None
            assert ws.monitor_claimed_by is None
            assert ws.monitor_claim_expires_at is None
            operations = await OperationRepository(s).list_all(workspace_id=monitor_id)

        remonitor_operations = [
            operation for operation in operations if operation.type == OperationType.remonitor.value
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
            operation for operation in operations if operation.type == OperationType.remonitor.value
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
        assert recovery_events[0].payload["claim_cleanup"] == operation.payload["claim_cleanup"]

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
            operation for operation in operations if operation.type == OperationType.remonitor.value
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
            operation for operation in operations if operation.type == OperationType.remonitor.value
        ]
        assert len(remonitor_operations) == 1
        assert remonitor_operations[0].payload is not None
        execution_cleanup = remonitor_operations[0].payload["claim_cleanup"]["execution_claim"]
        assert execution_cleanup == {
            "action": "preserved_unexpired",
            "reason_code": "UNEXPIRED_EXECUTION_CLAIM_PRESERVED_DURING_MONITOR_RECOVERY",
            "previous_claimed_by": "live-execution-worker",
            "previous_expires_at": execution_expires_at.isoformat(),
        }
        assert len(recovery_events) == 1
        assert recovery_events[0].payload is not None
        assert recovery_events[0].payload["claim_cleanup"]["execution_claim"] == (execution_cleanup)

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

        assert await asyncio.wait_for(worker_a.run_once(), timeout=WORKER_TEST_TIMEOUT_SECONDS) == 1
        await asyncio.wait_for(executor_a.started.wait(), timeout=WORKER_TEST_TIMEOUT_SECONDS)

        assert await asyncio.wait_for(worker_b.run_once(), timeout=WORKER_TEST_TIMEOUT_SECONDS) == 0
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
            operation for operation in operations if operation.type == OperationType.remonitor.value
        ]
        assert len(remonitor_operations) == 1
        assert len(recovery_events) == 1
        assert remonitor_operations[0].payload is not None
        assert (
            remonitor_operations[0].payload["claim_cleanup"]["execution_claim"]["action"]
            == "cleared_stale"
        )

        executor_a.release.set()
        await asyncio.wait_for(
            worker_a.wait_for_execution_tasks(), timeout=WORKER_TEST_TIMEOUT_SECONDS
        )

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            assert ws.execution_claimed_by is None
            assert ws.execution_claim_expires_at is None
            assert ws.monitor_claimed_by is None
            assert ws.monitor_claim_expires_at is None
            operations = await OperationRepository(s).list_all(workspace_id=monitor_id)

        remonitor_operations = [
            operation for operation in operations if operation.type == OperationType.remonitor.value
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
            operation for operation in operations if operation.type == OperationType.remonitor.value
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
            operation for operation in operations if operation.type == OperationType.remonitor.value
        ]
        assert len(remonitor_operations) == 1
        assert remonitor_operations[0].status == OperationStatus.succeeded.value
        assert remonitor_operations[0].payload is not None
        assert remonitor_operations[0].payload["runtime_stranding_reason"] == ("STRANDED_WORKSPACE")

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
        monitor_id = await _create_monitoring_pr(session_factory, origin_repo, "claimed-monitor")
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

        assert await asyncio.wait_for(worker.run_once(), timeout=WORKER_TEST_TIMEOUT_SECONDS) == 1
        await asyncio.wait_for(monitor_started.wait(), timeout=WORKER_TEST_TIMEOUT_SECONDS)
        assert executor.resume_calls == [monitor_id]
        assert executor.calls == []

        assert await asyncio.wait_for(worker.run_once(), timeout=WORKER_TEST_TIMEOUT_SECONDS) == 0
        assert executor.calls == []

        release_monitor.set()
        await asyncio.wait_for(
            worker.wait_for_execution_tasks(), timeout=WORKER_TEST_TIMEOUT_SECONDS
        )

        assert await asyncio.wait_for(worker.run_once(), timeout=WORKER_TEST_TIMEOUT_SECONDS) == 1
        await asyncio.wait_for(
            worker.wait_for_execution_tasks(), timeout=WORKER_TEST_TIMEOUT_SECONDS
        )
        assert executor.calls == [ready_id]

    @pytest.mark.unit
    @pytest.mark.parametrize("final_status", [WorkspaceStatus.cancelled, WorkspaceStatus.destroyed])
    async def test_stale_monitoring_list_entry_is_rechecked_before_dispatch(
        self,
        final_status: WorkspaceStatus,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        monitor_id = await _create_monitoring_pr(session_factory, origin_repo, "stale-monitor")
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

        assert await asyncio.wait_for(worker.run_once(), timeout=WORKER_TEST_TIMEOUT_SECONDS) == 1
        await asyncio.wait_for(monitor_started.wait(), timeout=WORKER_TEST_TIMEOUT_SECONDS)

        assert await asyncio.wait_for(worker.run_once(), timeout=WORKER_TEST_TIMEOUT_SECONDS) == 0
        assert executor.resume_calls == [monitor_id]

        release_monitor.set()
        await asyncio.wait_for(
            worker.wait_for_execution_tasks(), timeout=WORKER_TEST_TIMEOUT_SECONDS
        )

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

        dispatched = await asyncio.wait_for(
            asyncio.gather(worker_a.run_once(), worker_b.run_once()),
            timeout=WORKER_TEST_TIMEOUT_SECONDS,
        )
        await asyncio.wait_for(monitor_started.wait(), timeout=WORKER_TEST_TIMEOUT_SECONDS)

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
            timeout=WORKER_TEST_TIMEOUT_SECONDS,
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
    async def test_stale_active_scan_closed_connection_does_not_terminal_fail_workspace(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "stale-scan-closed-connection",
            WorkspaceStatus.running,
            node_id="node-a",
        )
        previous_owner = "worker-before-restart"
        previous_expiry = datetime.now(UTC) - timedelta(seconds=5)
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            assert ws is not None
            ws.execution_claimed_by = previous_owner
            ws.execution_claim_expires_at = previous_expiry
            await session.commit()

        original = WorkspaceRepository.get
        failures_remaining = 1

        async def _flaky_get(
            self: WorkspaceRepository,
            workspace_id: str,
        ) -> Workspace | None:
            nonlocal failures_remaining
            if failures_remaining:
                failures_remaining -= 1
                raise _closed_connection_error()
            return await original(self, workspace_id)

        monkeypatch.setattr(WorkspaceRepository, "get", _flaky_get)
        inspector = _RecordingRuntimeInspector({f"awf_{workspace_id}": _live_agent_snapshot()})
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_provisions=0,
                max_concurrent_executions=1,
                node_id="node-a",
            ),
        )

        assert await worker.run_once() == 0

        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.running.value
            assert ws.execution_claimed_by == previous_owner
            assert ws.execution_claim_expires_at == previous_expiry
            assert ws.failure_reason is None
            events = await WorkspaceEventRepository(session).list(workspace_id=workspace_id)

        assert inspector.calls == []
        assert any(event.reason_code == "DB_CONNECTION_CLOSED" for event in events)

    @pytest.mark.unit
    async def test_stale_active_cleanup_closed_connection_text_surfaces_runtime_failure(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        class _RaisingRuntimeCleaner:
            def __init__(self) -> None:
                self.calls: list[str] = []

            async def cleanup(
                self,
                *,
                workspace_id: str,
                repo_url: str,
                compose_project_name: str | None = None,
                compose_file_path: Path | None = None,
                worktree_host_path: Path | None = None,
                remove_volumes: bool = True,
                remove_worktree: bool = True,
            ) -> WorkspaceCleanupResult:
                self.calls.append(workspace_id)
                raise RuntimeError("runtime cleanup connection is closed")

        compose_project = "awf_cleanup_closed_text"
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "cleanup-closed-text",
            WorkspaceStatus.pushing,
            compose_project_name=compose_project,
            node_id="node-a",
        )
        now = datetime.now(UTC)
        status_started_at = now - timedelta(minutes=10)
        preserved_at = now - timedelta(minutes=5)
        refresh_requested_at = now - timedelta(minutes=1)
        async with session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            assert ws is not None
            state_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.state_changed",
            )
            pushing_started = next(
                event for event in state_events if event.new_state == WorkspaceStatus.pushing.value
            )
            pushing_started.occurred_at = status_started_at
            preserved = await repo.add_event(
                ws,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
                reason_code=PRESERVED_EXECUTION_REASON_CODE,
                payload={
                    "workspace_status": WorkspaceStatus.pushing.value,
                    "decision": "preserve_runtime",
                },
            )
            preserved.occurred_at = preserved_at
            await WorkspaceControlService(
                session,
                project_stopper=_noop_project_stop,
                cleaner_factory=_unexpected_cleaner_factory,
            ).request_refresh_workspace(
                workspace_id,
                reason="operator recovery",
                idempotency_key="refresh-before-cleanup-closed-text",
            )
            refresh_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.refresh_requested",
            )
            assert refresh_events
            refresh_events[0].occurred_at = refresh_requested_at
            await repo.add_event(
                ws,
                event_type="workspace.stale_active_execution_detected",
                reason_code="STALE_ACTIVE_EXECUTION",
                payload={
                    "compose_project_name": compose_project,
                    "workspace_status": WorkspaceStatus.pushing.value,
                    "runtime": {"stack_state": "running"},
                },
            )
            await session.commit()

        inspector = _RecordingRuntimeInspector({compose_project: _live_agent_snapshot()})
        cleaner = _RaisingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            runtime_cleaner=cleaner,  # type: ignore[arg-type]
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                node_id="node-a",
            ),
        )

        with pytest.raises(RuntimeError, match="runtime cleanup connection is closed"):
            await worker._recover_stale_active_executions()  # noqa: SLF001

        assert cleaner.calls == [workspace_id]
        async with session_factory() as session:
            db_events = await WorkspaceEventRepository(session).list(
                workspace_id=workspace_id,
                event_type="workspace.db_connection_transient",
            )

        assert db_events == []

    @pytest.mark.unit
    async def test_stale_running_with_retry_provider_state_continues_recovery(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "retry-provider-state-running",
            WorkspaceStatus.running,
            compose_project_name="awf_retry_provider_state_running",
            node_id="node-a",
            task_policy={"provider_recovery_state": {"action": "retry"}},
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            ws.execution_claimed_by = "orphan-worker"
            ws.execution_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await s.commit()

        inspector = _RecordingRuntimeInspector(
            {"awf_retry_provider_state_running": _live_agent_snapshot()}
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

        await worker._recover_stale_active_executions()

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.running.value
            assert ws.execution_claimed_by is None
            assert ws.execution_claim_expires_at is None
            assert ws.subphase == PRESERVED_EXECUTION_SUBPHASE
            preserved_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            )
            stale_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.stale_active_execution_detected",
            )

        assert len(preserved_events) == 1
        assert preserved_events[0].reason_code == PRESERVED_EXECUTION_REASON_CODE
        assert stale_events == []
        assert inspector.calls == ["awf_retry_provider_state_running"]

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
    async def test_restart_recovery_preserves_live_running_agent_runtime_without_cleanup(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "preserve-live-running",
            WorkspaceStatus.running,
            compose_project_name="awf_preserve_live_running",
        )
        inspector = _RecordingRuntimeInspector(
            {"awf_preserve_live_running": _live_agent_snapshot(container_id="agent-running")}
        )
        cleaner = _RecordingRuntimeCleaner()
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=inspector,
            runtime_cleaner=cleaner,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=0),
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.running.value
            assert ws.subphase == PRESERVED_EXECUTION_SUBPHASE
            assert ws.execution_claimed_by is None
            assert ws.execution_claim_expires_at is None
            assert ws.failure_reason is None
            assert ws.failure_message is None
            stale_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.stale_active_execution_detected",
            )
            preserved_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            )
            operations = await OperationRepository(s).list_for_workspace(workspace_id)

        assert stale_events == []
        assert len(preserved_events) == 1
        preserved = preserved_events[0]
        assert preserved.reason_code == PRESERVED_EXECUTION_REASON_CODE
        assert preserved.payload is not None
        assert preserved.payload["message"].startswith("Worker restart found")
        assert preserved.payload["decision"] == "preserve_runtime"
        assert preserved.payload["requested_action"] == OperationType.refresh.value
        assert preserved.payload["workspace_status"] == WorkspaceStatus.running.value
        assert preserved.payload["previous_claim"] == {
            "monitor_claimed_by": None,
            "monitor_claim_expires_at": None,
            "execution_claimed_by": None,
            "execution_claim_expires_at": None,
        }
        assert preserved.payload["runtime"]["services"][0]["container_id"] == "agent-running"
        assert len(operations) == 1
        operation = operations[0]
        assert operation.type == OperationType.refresh.value
        assert operation.status == OperationStatus.succeeded.value
        assert operation.payload == {**preserved.payload, "operation_id": operation.id}
        assert inspector.calls == ["awf_preserve_live_running"]
        assert executor.calls == []
        assert cleaner.calls == []

    @pytest.mark.unit
    async def test_active_execution_preservation_after_restart_keeps_primary_failure_evidence(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        compose_project = "awf_preserve_primary_failure"
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "preserve-primary-failure",
            WorkspaceStatus.running,
            compose_project_name=compose_project,
        )
        validation_run_id = await _seed_primary_failure_evidence(
            session_factory,
            workspace_id,
            failure_reason=FailureReason.validation_failure.value,
            failure_message="pytest failed before worker reconnect",
            reason_code="PYTEST_TEST_FAILURE",
            include_validation_run=True,
        )
        snapshot = _live_agent_snapshot(container_id="agent-primary-failure")

        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=_RecordingRuntimeInspector({compose_project: snapshot}),
            runtime_cleaner=_RecordingRuntimeCleaner(),
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=0),
        )

        await worker._record_preserved_active_execution_after_restart(  # noqa: SLF001
            _ActiveExecutionCandidate(
                workspace_id=workspace_id,
                status=WorkspaceStatus.running,
                repo_url=str(origin_repo),
                compose_project_name=compose_project,
            ),
            snapshot,
        )

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.running.value
            assert ws.failure_reason == FailureReason.validation_failure.value
            assert ws.failure_message == "pytest failed before worker reconnect"
            validation_run = await ValidationRunRepository(s).get(validation_run_id or "")
            assert validation_run is not None
            assert validation_run.reason_code == "PYTEST_TEST_FAILURE"
            preserved_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            )

        assert len(preserved_events) == 1
        assert preserved_events[0].payload is not None
        assert preserved_events[0].payload["primary_failure"]["reason_code"] == (
            "PYTEST_TEST_FAILURE"
        )
        assert preserved_events[0].payload["primary_failure"]["validation_run"]["id"] == (
            validation_run_id
        )

    @pytest.mark.unit
    async def test_restart_recovery_fails_expired_preserved_active_execution(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        compose_project = "awf_preserve_expired"
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "preserve-expired",
            WorkspaceStatus.running,
            compose_project_name=compose_project,
        )
        preserved_at = datetime.now(UTC) - timedelta(minutes=30)
        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            assert ws is not None
            state_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.state_changed",
            )
            running_started = next(
                event for event in state_events if event.new_state == WorkspaceStatus.running.value
            )
            running_started.occurred_at = preserved_at - timedelta(minutes=1)
            preserved = await repo.add_event(
                ws,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
                reason_code=PRESERVED_EXECUTION_REASON_CODE,
                payload={
                    "workspace_status": WorkspaceStatus.running.value,
                    "decision": "preserve_runtime",
                },
            )
            preserved.occurred_at = preserved_at
            await s.commit()

        inspector = _RecordingRuntimeInspector({compose_project: _live_agent_snapshot()})
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                stale_active_execution_scan_interval_seconds=0.0,
                active_execution_preservation_grace_seconds=60.0,
            ),
        )

        assert await worker.run_once() == 0
        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            stale_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.stale_active_execution_detected",
            )
            preserved_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            )

        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == FailureReason.infrastructure_failure.value
        assert ws.failure_message is not None
        assert "active execution was lost after a service or Docker restart" in ws.failure_message
        assert "stopped the stale runtime" in ws.failure_message
        assert "without cleanup" not in ws.failure_message
        assert len(stale_events) == 1
        assert len(preserved_events) == 1
        assert cleaner.calls == [
            {
                "workspace_id": workspace_id,
                "repo_url": str(origin_repo),
                "compose_project_name": compose_project,
                "compose_file_path": Path(f"/tmp/awf/{workspace_id}/compose.yml"),
                "worktree_host_path": None,
                "remove_volumes": True,
                "remove_worktree": False,
            }
        ]

    @pytest.mark.unit
    async def test_restart_recovery_records_preservation_once_per_active_phase(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        compose_project = "awf_preserve_phase_change"
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "preserve-phase-change",
            WorkspaceStatus.running,
            compose_project_name=compose_project,
        )
        inspector = _RecordingRuntimeInspector(
            {compose_project: _live_agent_snapshot(container_id="agent-phase")}
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            runtime_cleaner=_RecordingRuntimeCleaner(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                stale_active_execution_scan_interval_seconds=0.0,
            ),
        )

        assert await worker.run_once() == 0
        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            assert ws is not None
            await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="TEST_ADVANCE")
            await s.commit()

        assert await worker.run_once() == 0

        async with session_factory() as s:
            preserved_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            )
            operations = await OperationRepository(s).list_for_workspace(workspace_id)

        assert len(preserved_events) == 2
        assert {event.payload["workspace_status"] for event in preserved_events} == {
            WorkspaceStatus.running.value,
            WorkspaceStatus.validating.value,
        }
        assert len(operations) == 2
        assert {operation.payload["workspace_status"] for operation in operations} == {
            WorkspaceStatus.running.value,
            WorkspaceStatus.validating.value,
        }
        assert inspector.calls == [compose_project, compose_project]

    @pytest.mark.unit
    async def test_restart_recovery_preservation_idempotency_is_scoped_to_fresh_execution(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        compose_project = "awf_preserve_fresh_execution"
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "preserve-fresh-execution",
            WorkspaceStatus.running,
            compose_project_name=compose_project,
        )
        old_event_time = datetime.now(UTC) - timedelta(days=1)
        new_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            assert ws is not None
            old_event = await repo.add_event(
                ws,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
                reason_code=PRESERVED_EXECUTION_REASON_CODE,
                payload={
                    "workspace_status": WorkspaceStatus.running.value,
                    "decision": "preserve_runtime",
                },
            )
            old_event.occurred_at = old_event_time
            await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="TEST_ADVANCE")
            await repo.transition(ws, to=WorkspaceStatus.monitoring_pr, reason_code="TEST_ADVANCE")
            await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="TEST_REQUEUE")
            await repo.transition(ws, to=WorkspaceStatus.running, reason_code="TEST_REEXECUTE")
            ws.execution_claimed_by = "fresh-dead-worker"
            ws.execution_claim_expires_at = new_claim_expires_at
            await s.commit()

        inspector = _RecordingRuntimeInspector(
            {compose_project: _live_agent_snapshot(container_id="agent-fresh")}
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            runtime_cleaner=_RecordingRuntimeCleaner(),
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=0),
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            preserved_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            )
            operations = await OperationRepository(s).list_for_workspace(workspace_id)

        assert ws.execution_claimed_by is None
        assert ws.execution_claim_expires_at is None
        assert len(preserved_events) == 2
        latest_preserved = preserved_events[0]
        assert latest_preserved.occurred_at > old_event_time
        assert latest_preserved.payload is not None
        assert latest_preserved.payload["previous_claim"]["execution_claimed_by"] == (
            "fresh-dead-worker"
        )
        assert latest_preserved.payload["previous_claim"]["execution_claim_expires_at"] == (
            new_claim_expires_at.isoformat()
        )
        assert len(operations) == 1
        assert operations[0].payload["runtime"]["services"][0]["container_id"] == "agent-fresh"
        assert inspector.calls == [compose_project]

    @pytest.mark.unit
    async def test_preservation_idempotency_keeps_current_event_with_unexpired_claim(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        compose_project = "awf_preserve_fresh_claim"
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "preserve-fresh-claim",
            WorkspaceStatus.running,
            compose_project_name=compose_project,
        )
        now = datetime.now(UTC)
        status_started_at = now - timedelta(minutes=5)
        preserved_at = now - timedelta(minutes=1)
        claim_expires_at = now + timedelta(minutes=5)
        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            assert ws is not None
            state_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.state_changed",
            )
            running_started = next(
                event for event in state_events if event.new_state == WorkspaceStatus.running.value
            )
            running_started.occurred_at = status_started_at
            preserved = await repo.add_event(
                ws,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
                reason_code=PRESERVED_EXECUTION_REASON_CODE,
                payload={
                    "workspace_status": WorkspaceStatus.running.value,
                    "decision": "preserve_runtime",
                },
            )
            preserved.occurred_at = preserved_at
            ws.execution_claimed_by = "live-worker"
            ws.execution_claim_expires_at = claim_expires_at
            await s.commit()

        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=_RecordingRuntimeInspector({compose_project: _live_agent_snapshot()}),
            runtime_cleaner=_RecordingRuntimeCleaner(),
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=0),
        )

        assert await worker._has_current_preserved_active_execution(  # noqa: SLF001
            _ActiveExecutionCandidate(
                workspace_id=workspace_id,
                status=WorkspaceStatus.running,
                repo_url=str(origin_repo),
                compose_project_name=compose_project,
            )
        )

    @pytest.mark.unit
    async def test_operator_refresh_from_old_cycle_does_not_block_fresh_preservation(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        compose_project = "awf_preserve_after_old_refresh"
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "preserve-after-old-refresh",
            WorkspaceStatus.running,
            compose_project_name=compose_project,
        )
        old_preservation_time = datetime.now(UTC) - timedelta(days=1)
        old_refresh_time = old_preservation_time + timedelta(minutes=5)
        new_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            assert ws is not None
            old_preserved = await repo.add_event(
                ws,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
                reason_code=PRESERVED_EXECUTION_REASON_CODE,
                payload={
                    "workspace_status": WorkspaceStatus.running.value,
                    "decision": "preserve_runtime",
                },
            )
            old_preserved.occurred_at = old_preservation_time
            old_refresh = await repo.add_event(
                ws,
                event_type="workspace.refresh_requested",
                reason_code="OPERATOR_REFRESH",
                payload={"reason": "operator recovery"},
            )
            old_refresh.occurred_at = old_refresh_time
            await repo.transition(ws, to=WorkspaceStatus.validating, reason_code="TEST_ADVANCE")
            await repo.transition(ws, to=WorkspaceStatus.monitoring_pr, reason_code="TEST_ADVANCE")
            await repo.transition(ws, to=WorkspaceStatus.ready, reason_code="TEST_REQUEUE")
            await repo.transition(ws, to=WorkspaceStatus.running, reason_code="TEST_REEXECUTE")
            ws.execution_claimed_by = "fresh-dead-worker"
            ws.execution_claim_expires_at = new_claim_expires_at
            await s.commit()

        inspector = _RecordingRuntimeInspector(
            {compose_project: _live_agent_snapshot(container_id="agent-fresh-after-refresh")}
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            runtime_cleaner=_RecordingRuntimeCleaner(),
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=0),
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            preserved_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            )
            stale_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.stale_active_execution_detected",
            )
            operations = await OperationRepository(s).list_for_workspace(workspace_id)

        assert ws.execution_claimed_by is None
        assert ws.execution_claim_expires_at is None
        assert len(preserved_events) == 2
        assert preserved_events[0].occurred_at > old_refresh_time
        assert preserved_events[0].payload is not None
        assert preserved_events[0].payload["workspace_status"] == WorkspaceStatus.running.value
        assert stale_events == []
        assert len(operations) == 1
        assert operations[0].payload["runtime"]["services"][0]["container_id"] == (
            "agent-fresh-after-refresh"
        )
        assert inspector.calls == [compose_project]

    @pytest.mark.unit
    async def test_restart_recovery_serializes_concurrent_preservation_recording(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        compose_project = "awf_preserve_race"
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "preserve-live-race",
            WorkspaceStatus.running,
            compose_project_name=compose_project,
            node_id="node-a",
        )
        candidate = _ActiveExecutionCandidate(
            workspace_id=workspace_id,
            status=WorkspaceStatus.running,
            repo_url=str(origin_repo),
            compose_project_name=compose_project,
        )
        snapshot = _live_agent_snapshot(container_id="agent-race")
        both_started = asyncio.Event()
        first_selected = asyncio.Event()
        allow_first_recording = asyncio.Event()
        second_checked = asyncio.Event()
        started_count = 0
        call_count = 0
        selected_count = 0
        count_lock = asyncio.Lock()
        original_has_event = ControlWorker._has_preserved_active_execution_event

        async def _racing_has_preserved_event(
            self: ControlWorker,
            session: AsyncSession,
            workspace_id: str,
            status: WorkspaceStatus,
            *,
            event_floor: datetime | None = None,
        ) -> bool:
            nonlocal call_count, selected_count
            async with count_lock:
                call_count += 1
                call_number = call_count
                if call_number == 2:
                    second_checked.set()

            has_event = await original_has_event(
                self,
                session,
                workspace_id,
                status,
                event_floor=event_floor,
            )
            async with count_lock:
                selected_count += 1
            if call_number == 1:
                first_selected.set()
                assert not has_event
                await asyncio.wait_for(
                    allow_first_recording.wait(), timeout=WORKER_TEST_TIMEOUT_SECONDS
                )
            return has_event

        monkeypatch.setattr(
            ControlWorker,
            "_has_preserved_active_execution_event",
            _racing_has_preserved_event,
        )
        workers = [
            ControlWorker(
                session_factory=session_factory,
                provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
                executor=_RecordingExecutor(),
                runtime_inspector=_RecordingRuntimeInspector({compose_project: snapshot}),
                runtime_cleaner=_RecordingRuntimeCleaner(),
                config=WorkerConfig(
                    poll_interval_seconds=0.01,
                    max_concurrent_executions=0,
                    node_id="node-a",
                ),
            )
            for _ in range(2)
        ]

        async def _record_started(worker: ControlWorker) -> None:
            nonlocal started_count
            async with count_lock:
                started_count += 1
                if started_count == len(workers):
                    both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=WORKER_TEST_TIMEOUT_SECONDS)
            await worker._record_preserved_active_execution_after_restart(  # noqa: SLF001
                candidate,
                snapshot,
            )

        tasks = [asyncio.create_task(_record_started(worker)) for worker in workers]
        try:
            await asyncio.wait_for(first_selected.wait(), timeout=WORKER_TEST_TIMEOUT_SECONDS)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(second_checked.wait(), timeout=0.2)
            allow_first_recording.set()
            await asyncio.gather(*tasks)
        finally:
            allow_first_recording.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        assert started_count == 2
        assert call_count == 2
        assert selected_count == 2

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            preserved_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            )
            operations = await OperationRepository(s).list_for_workspace(workspace_id)

        assert ws is not None
        assert ws.subphase == PRESERVED_EXECUTION_SUBPHASE
        assert len(preserved_events) == 1
        assert len(operations) == 1
        assert preserved_events[0].payload is not None
        assert preserved_events[0].payload["operation_id"] == operations[0].id

    @pytest.mark.unit
    async def test_preservation_recording_rechecks_operator_refresh_after_preservation_under_lock(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        compose_project = "awf_preserve_refresh_race"
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "preserve-refresh-race",
            WorkspaceStatus.pushing,
            compose_project_name=compose_project,
        )
        candidate = _ActiveExecutionCandidate(
            workspace_id=workspace_id,
            status=WorkspaceStatus.pushing,
            repo_url=str(origin_repo),
            compose_project_name=compose_project,
        )
        snapshot = _live_agent_snapshot(container_id="agent-refresh-race")
        now = datetime.now(UTC)
        status_started_at = now - timedelta(minutes=3)
        preserved_at = now - timedelta(minutes=2)
        refresh_requested_at = now - timedelta(minutes=1)
        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            assert ws is not None
            state_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.state_changed",
            )
            pushing_started = next(
                event for event in state_events if event.new_state == WorkspaceStatus.pushing.value
            )
            pushing_started.occurred_at = status_started_at
            preserved = await repo.add_event(
                ws,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
                reason_code=PRESERVED_EXECUTION_REASON_CODE,
                payload={
                    "workspace_status": WorkspaceStatus.pushing.value,
                    "decision": "preserve_runtime",
                },
            )
            preserved.occurred_at = preserved_at
            await WorkspaceControlService(
                s,
                project_stopper=_noop_project_stop,
                cleaner_factory=_unexpected_cleaner_factory,
            ).request_refresh_workspace(
                workspace_id,
                reason="operator recovery",
                idempotency_key="refresh-before-locked-preservation",
            )
            refresh_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.refresh_requested",
            )
            assert refresh_events
            refresh_events[0].occurred_at = refresh_requested_at
            await s.commit()

        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=_RecordingRuntimeInspector({compose_project: snapshot}),
            runtime_cleaner=_RecordingRuntimeCleaner(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
            ),
        )

        await worker._record_preserved_active_execution_after_restart(  # noqa: SLF001
            candidate,
            snapshot,
        )

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            preserved_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            )
            operations = await OperationRepository(s).list_for_workspace(workspace_id)

        assert ws is not None
        assert ws.subphase is None
        assert len(preserved_events) == 1
        assert [operation.type for operation in operations] == [OperationType.refresh.value]

    @pytest.mark.unit
    async def test_preservation_recording_allows_operator_refresh_before_first_preservation(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        compose_project = "awf_preserve_pre_refresh_race"
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "preserve-pre-refresh-race",
            WorkspaceStatus.pushing,
            compose_project_name=compose_project,
        )
        candidate = _ActiveExecutionCandidate(
            workspace_id=workspace_id,
            status=WorkspaceStatus.pushing,
            repo_url=str(origin_repo),
            compose_project_name=compose_project,
        )
        snapshot = _live_agent_snapshot(container_id="agent-pre-refresh-race")
        async with session_factory() as s:
            await WorkspaceControlService(
                s,
                project_stopper=_noop_project_stop,
                cleaner_factory=_unexpected_cleaner_factory,
            ).request_refresh_workspace(
                workspace_id,
                reason="operator recovery",
                idempotency_key="refresh-before-first-locked-preservation",
            )
            await s.commit()

        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=_RecordingRuntimeInspector({compose_project: snapshot}),
            runtime_cleaner=_RecordingRuntimeCleaner(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
            ),
        )

        await worker._record_preserved_active_execution_after_restart(  # noqa: SLF001
            candidate,
            snapshot,
        )

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            preserved_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            )
            operations = await OperationRepository(s).list_for_workspace(workspace_id)

        assert ws is not None
        assert ws.subphase == PRESERVED_EXECUTION_SUBPHASE
        assert len(preserved_events) == 1
        assert preserved_events[0].payload is not None
        assert preserved_events[0].payload["runtime"]["services"][0]["container_id"] == (
            "agent-pre-refresh-race"
        )
        assert (
            len(
                [
                    operation
                    for operation in operations
                    if operation.result is not None
                    and operation.result.get("reason_code") == PRESERVED_EXECUTION_REASON_CODE
                ]
            )
            == 1
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("status", [WorkspaceStatus.validating, WorkspaceStatus.pushing])
    async def test_restart_recovery_preserves_live_validating_and_pushing_runtimes(
        self,
        status: WorkspaceStatus,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        compose_project = f"awf_preserve_live_{status.value}"
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            f"preserve-live-{status.value}",
            status,
            compose_project_name=compose_project,
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            initial_version = ws.version
            initial_event_sequence = ws.event_sequence

        inspector = _RecordingRuntimeInspector({compose_project: _live_agent_snapshot()})
        cleaner = _RecordingRuntimeCleaner()
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=inspector,
            runtime_cleaner=cleaner,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=0),
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == status.value
            assert ws.subphase == PRESERVED_EXECUTION_SUBPHASE
            assert ws.version == initial_version + 1
            assert ws.failure_reason is None
            preserved_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            )
            stale_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.stale_active_execution_detected",
            )

        assert len(preserved_events) == 1
        assert preserved_events[0].event_order == initial_event_sequence + 1
        assert preserved_events[0].payload is not None
        assert preserved_events[0].payload["workspace_status"] == status.value
        assert preserved_events[0].payload["decision"] == "preserve_runtime"
        assert stale_events == []
        assert executor.calls == []
        assert cleaner.calls == []

    @pytest.mark.unit
    async def test_restart_recovery_preserves_five_live_workspaces_after_worker_restart(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        statuses = [
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
            WorkspaceStatus.running,
            WorkspaceStatus.pushing,
        ]
        workspace_ids: list[str] = []
        snapshots: dict[str | None, RuntimeSnapshot] = {}
        for index, status in enumerate(statuses):
            compose_project = f"awf_preserve_five_{index}"
            workspace_ids.append(
                await _create_active_execution(
                    session_factory,
                    origin_repo,
                    f"preserve-five-{index}",
                    status,
                    compose_project_name=compose_project,
                    node_id="node-a",
                )
            )
            snapshots[compose_project] = _live_agent_snapshot(container_id=f"agent-{index}")

        inspector = _RecordingRuntimeInspector(snapshots)
        cleaner = _RecordingRuntimeCleaner()
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=inspector,
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                node_id="node-a",
            ),
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            for workspace_id, status in zip(workspace_ids, statuses, strict=True):
                ws = await WorkspaceRepository(s).get(workspace_id)
                assert ws is not None
                assert ws.status == status.value
                assert ws.subphase == PRESERVED_EXECUTION_SUBPHASE
                assert ws.failure_reason is None
                preserved_events = await WorkspaceEventRepository(s).list(
                    workspace_id=workspace_id,
                    event_type=PRESERVED_EXECUTION_EVENT_TYPE,
                )
                stale_events = await WorkspaceEventRepository(s).list(
                    workspace_id=workspace_id,
                    event_type="workspace.stale_active_execution_detected",
                )
                operations = await OperationRepository(s).list_for_workspace(workspace_id)
                assert len(preserved_events) == 1
                assert stale_events == []
                assert len(operations) == 1
                assert operations[0].status == OperationStatus.succeeded.value

        assert set(inspector.calls) == set(snapshots)
        assert len(inspector.calls) == 5
        assert executor.calls == []
        assert cleaner.calls == []

    @pytest.mark.unit
    async def test_restart_recovery_preserves_expired_claim_with_live_container(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        expired_at = datetime.now(UTC) - timedelta(seconds=1)
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "preserve-expired-claim",
            WorkspaceStatus.running,
            compose_project_name="awf_preserve_expired_claim",
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            ws.execution_claimed_by = "dead-worker"
            ws.execution_claim_expires_at = expired_at
            await s.commit()

        inspector = _RecordingRuntimeInspector(
            {"awf_preserve_expired_claim": _live_agent_snapshot()}
        )
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            runtime_cleaner=cleaner,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=0),
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.running.value
            assert ws.execution_claimed_by is None
            assert ws.execution_claim_expires_at is None
            preserved_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            )
        assert len(preserved_events) == 1
        assert preserved_events[0].payload is not None
        assert preserved_events[0].payload["claim_cleanup"] == {
            "action": "cleared_stale",
            "reason_code": "STALE_EXECUTION_CLAIM_CLEARED_DURING_ACTIVE_EXECUTION_PRESERVATION",
            "previous_claimed_by": "dead-worker",
            "previous_expires_at": expired_at.isoformat(),
        }
        assert cleaner.calls == []

    @pytest.mark.unit
    async def test_cleanup_failure_path_does_not_target_preserved_live_runtime(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "preserved-live-idempotent",
            WorkspaceStatus.pushing,
            compose_project_name="awf_preserved_live_idempotent",
        )
        inspector = _RecordingRuntimeInspector(
            {"awf_preserved_live_idempotent": _live_agent_snapshot()}
        )
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                stale_active_execution_scan_interval_seconds=0.0,
            ),
        )

        assert await worker.run_once() == 0
        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.pushing.value
            preserved_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            )
            cleanup_failed_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.stale_active_execution_cleanup_failed",
            )
        assert len(preserved_events) == 1
        assert cleanup_failed_events == []
        assert inspector.calls == ["awf_preserved_live_idempotent"]
        assert cleaner.calls == []

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
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=1),
        )

        candidate = _ActiveExecutionCandidate(
            workspace_id=workspace_id,
            status=WorkspaceStatus.running,
            compose_project_name="awf_stale_running_fail",
            compose_file_path="/tmp/awf/ws/compose.yml",
            repo_url=str(origin_repo),
        )
        snapshot = RuntimeSnapshot(
            stack_state="running",
            reason="worker process exited before releasing its claim",
        )
        assert await worker._record_stale_active_execution_detected(candidate, snapshot)

        await worker._cleanup_and_fail_stale_active_execution(candidate, snapshot)

        assert cleaner.calls == [
            {
                "workspace_id": workspace_id,
                "repo_url": str(origin_repo),
                "compose_project_name": "awf_stale_running_fail",
                "compose_file_path": Path("/tmp/awf/ws/compose.yml"),
                "worktree_host_path": None,
                "remove_volumes": True,
                "remove_worktree": False,
            }
        ]
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
    async def test_stale_active_execution_preserves_validation_failure_and_records_secondary_stale(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "stale-running-preserve-validation",
            WorkspaceStatus.running,
            compose_project_name="awf_stale_preserve_validation",
        )
        validation_run_id = await _seed_primary_failure_evidence(
            session_factory,
            workspace_id,
            failure_reason=FailureReason.validation_failure.value,
            failure_message="pytest failed before runtime cleanup",
            reason_code="PYTEST_TEST_FAILURE",
            include_validation_run=True,
        )
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=1),
        )

        candidate = _ActiveExecutionCandidate(
            workspace_id=workspace_id,
            status=WorkspaceStatus.running,
            compose_project_name="awf_stale_preserve_validation",
            compose_file_path="/tmp/awf/ws/compose.yml",
            repo_url=str(origin_repo),
        )
        snapshot = RuntimeSnapshot(
            stack_state="running",
            reason="worker process exited after validation failed",
        )
        assert await worker._record_stale_active_execution_detected(candidate, snapshot)

        await worker._cleanup_and_fail_stale_active_execution(candidate, snapshot)

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == FailureReason.validation_failure.value
            assert ws.failure_message == "pytest failed before runtime cleanup"
            validation_run = await ValidationRunRepository(s).get(validation_run_id or "")
            assert validation_run is not None
            assert validation_run.reason_code == "PYTEST_TEST_FAILURE"
            assert validation_run.coverage is not None
            assert validation_run.coverage["percent"] == 91.5
            assert validation_run.coverage["threshold"] == 99.0
            assert validation_run.coverage["failing_test_node_ids"] == [
                "tests/unit/test_example.py::test_failure"
            ]
            state_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.state_changed",
            )

        latest_failed = next(
            event for event in state_events if event.new_state == WorkspaceStatus.failed.value
        )
        assert latest_failed.reason_code == "PYTEST_TEST_FAILURE"
        assert latest_failed.payload is not None
        assert latest_failed.payload["reason_code"] == "PYTEST_TEST_FAILURE"
        assert latest_failed.payload["primary_failure"]["validation_run"]["id"] == (
            validation_run_id
        )
        assert latest_failed.payload["primary_failure"]["validation_run"]["coverage"][
            "failing_test_node_ids"
        ] == ["tests/unit/test_example.py::test_failure"]
        assert latest_failed.payload["secondary_failure"]["reason_code"] == (
            "STALE_ACTIVE_EXECUTION"
        )
        assert latest_failed.payload["secondary_failure"]["runtime"]["stack_state"] == "running"
        assert latest_failed.payload["secondary_failures"][-1]["reason_code"] == (
            "STALE_ACTIVE_EXECUTION"
        )

    @pytest.mark.unit
    async def test_stale_active_execution_cleanup_failure_keeps_row_active(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "stale-running-cleanup-fail",
            WorkspaceStatus.running,
            compose_project_name="awf_stale_cleanup_fail",
        )
        cleaner = _RecordingRuntimeCleaner(
            WorkspaceCleanupResult(
                status="partial",
                reason_code=CLEANUP_PARTIAL,
                steps=(
                    WorkspaceCleanupStepResult(
                        name="compose_down",
                        status="failed",
                        reason_code="DOCKER_UNAVAILABLE",
                        error="cannot connect to docker",
                    ),
                ),
            )
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=1),
        )

        candidate = _ActiveExecutionCandidate(
            workspace_id=workspace_id,
            status=WorkspaceStatus.running,
            compose_project_name="awf_stale_cleanup_fail",
            repo_url=str(origin_repo),
        )
        snapshot = RuntimeSnapshot(stack_state="running", reason="lost worker task")
        assert await worker._record_stale_active_execution_detected(candidate, snapshot)

        await worker._cleanup_and_fail_stale_active_execution(candidate, snapshot)

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.running.value
            assert ws.failure_reason is None
            assert ws.execution_claimed_by is None
            cleanup_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.stale_active_execution_cleanup_failed",
            )
            assert len(cleanup_events) == 1
            assert cleanup_events[0].reason_code == "STALE_ACTIVE_EXECUTION_CLEANUP_FAILED"
            assert cleanup_events[0].payload["cleanup"]["reason_code"] == CLEANUP_PARTIAL

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
            repo_url=str(origin_repo),
        )
        finding = WorkspaceRuntimeFinding(
            workspace_id=workspace_id,
            workspace_status=WorkspaceStatus.validating.value,
            status="stranded",
            reason_code="STRANDED_WORKSPACE",
            decision="fail_workspace",
            message="runtime is stranded",
        )

        await worker._cleanup_and_fail_stale_active_execution(candidate, snapshot)
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
    async def test_runtime_stranding_preserves_provider_auth_primary_failure(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "stranded-preserve-auth",
            WorkspaceStatus.validating,
            compose_project_name="awf_stranded_preserve_auth",
        )
        await _seed_primary_failure_evidence(
            session_factory,
            workspace_id,
            failure_reason=FailureReason.agent_failure.value,
            failure_message="provider auth failed before runtime stranding",
            reason_code="AGENT_AUTH_FAILED",
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=1),
        )
        candidate = _ActiveExecutionCandidate(
            workspace_id=workspace_id,
            status=WorkspaceStatus.validating,
            compose_project_name="awf_stranded_preserve_auth",
            repo_url=str(origin_repo),
        )
        snapshot = RuntimeSnapshot(stack_state="stopped", reason="no containers")
        finding = WorkspaceRuntimeFinding(
            workspace_id=workspace_id,
            workspace_status=WorkspaceStatus.validating.value,
            status="stranded",
            reason_code="STRANDED_WORKSPACE",
            decision="fail_workspace",
            message="runtime is stranded",
        )

        await worker._fail_stranded_workspace(candidate, snapshot, finding)  # noqa: SLF001

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == FailureReason.agent_failure.value
            assert ws.failure_message == "provider auth failed before runtime stranding"
            state_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.state_changed",
            )
            stranded_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.runtime_stranded_detected",
            )

        latest_failed = next(
            event for event in state_events if event.new_state == WorkspaceStatus.failed.value
        )
        assert latest_failed.reason_code == "AGENT_AUTH_FAILED"
        assert latest_failed.payload is not None
        assert latest_failed.payload["primary_failure"]["reason_code"] == "AGENT_AUTH_FAILED"
        assert latest_failed.payload["secondary_failure"]["reason_code"] == "STRANDED_WORKSPACE"
        assert latest_failed.payload["secondary_failures"][-1]["reason_code"] == (
            "STRANDED_WORKSPACE"
        )
        assert len(stranded_events) == 1
        assert stranded_events[0].payload is not None
        assert stranded_events[0].payload["primary_failure"]["reason_code"] == ("AGENT_AUTH_FAILED")

    @pytest.mark.unit
    async def test_live_running_stack_is_preserved_and_not_failed_on_next_scan(
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
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            runtime_cleaner=cleaner,
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
            assert ws.subphase == PRESERVED_EXECUTION_SUBPHASE
            assert ws.failure_reason is None
            stale_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.stale_active_execution_detected",
            )
            preserved_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            )
            operations = await OperationRepository(s).list_for_workspace(workspace_id)
            assert stale_events == []
            assert len(preserved_events) == 1
            assert preserved_events[0].reason_code == PRESERVED_EXECUTION_REASON_CODE
            assert preserved_events[0].payload is not None
            assert preserved_events[0].payload["runtime"]["services"][0]["container_id"] == "abc123"
            assert len(operations) == 1
            assert operations[0].type == OperationType.refresh.value
            assert operations[0].status == OperationStatus.succeeded.value

        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.pushing.value
            assert ws.execution_claimed_by is None
            assert ws.execution_claim_expires_at is None
            assert ws.failure_reason is None
            preserved_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            )
            assert len(preserved_events) == 1
        assert inspector.calls == ["awf_pushing_running"]
        assert cleaner.calls == []

    @pytest.mark.unit
    async def test_preserved_runtime_exit_before_operator_recovery_is_not_failed_by_scan(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        compose_project = "awf_preserved_then_exited"
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "preserved-then-exited",
            WorkspaceStatus.pushing,
            compose_project_name=compose_project,
        )
        inspector = _RecordingRuntimeInspector(
            {compose_project: _live_agent_snapshot(container_id="agent-preserved")}
        )
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                stale_active_execution_scan_interval_seconds=0.0,
            ),
        )

        assert await worker.run_once() == 0

        inspector._snapshots[compose_project] = RuntimeSnapshot(  # noqa: SLF001
            stack_state="stopped",
            services=[
                RuntimeService(
                    name="agent",
                    container_id="agent-preserved",
                    image="awf-agent:latest",
                    state="exited",
                    status="Exited (0) 1 minute ago",
                )
            ],
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.pushing.value
            assert ws.subphase == PRESERVED_EXECUTION_SUBPHASE
            assert ws.failure_reason is None
            assert ws.failure_message is None
            preserved_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            )
            stranded_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.runtime_stranded_detected",
            )
            stale_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.stale_active_execution_detected",
            )

        assert len(preserved_events) == 1
        assert stranded_events == []
        assert stale_events == []
        assert inspector.calls == [compose_project]
        assert cleaner.calls == []

    @pytest.mark.unit
    async def test_operator_refresh_unblocks_preserved_runtime_recovery_scan(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        compose_project = "awf_preserved_refresh_exited"
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "preserved-refresh-exited",
            WorkspaceStatus.pushing,
            compose_project_name=compose_project,
        )
        inspector = _RecordingRuntimeInspector(
            {compose_project: _live_agent_snapshot(container_id="agent-preserved-refresh")}
        )
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                stale_active_execution_scan_interval_seconds=0.0,
            ),
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            await WorkspaceControlService(
                s,
                project_stopper=_noop_project_stop,
                cleaner_factory=_unexpected_cleaner_factory,
            ).request_refresh_workspace(
                workspace_id,
                reason="operator recovery",
                idempotency_key="refresh-preserved-runtime",
            )
            await s.commit()

        inspector._snapshots[compose_project] = RuntimeSnapshot(  # noqa: SLF001
            stack_state="stopped",
            services=[
                RuntimeService(
                    name="agent",
                    container_id="agent-preserved-refresh",
                    image="awf-agent:latest",
                    state="exited",
                    status="Exited (0) 1 minute ago",
                )
            ],
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            preserved_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            )
            stranded_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.runtime_stranded_detected",
            )

        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == FailureReason.infrastructure_failure.value
        assert len(preserved_events) == 1
        assert len(stranded_events) == 1
        assert stranded_events[0].reason_code == "AGENT_CONTAINER_EXITED"
        assert inspector.calls == [compose_project, compose_project]
        assert cleaner.calls == []

    @pytest.mark.unit
    async def test_operator_refresh_before_first_preservation_preserves_live_runtime(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        compose_project = "awf_refresh_before_preservation"
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "refresh-before-preservation",
            WorkspaceStatus.pushing,
            compose_project_name=compose_project,
        )
        async with session_factory() as s:
            await WorkspaceControlService(
                s,
                project_stopper=_noop_project_stop,
                cleaner_factory=_unexpected_cleaner_factory,
            ).request_refresh_workspace(
                workspace_id,
                reason="operator recovery",
                idempotency_key="refresh-before-preservation",
            )
            await s.commit()

        inspector = _RecordingRuntimeInspector(
            {compose_project: _live_agent_snapshot(container_id="agent-refresh-before-preserve")}
        )
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                stale_active_execution_scan_interval_seconds=0.0,
            ),
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            preserved_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            )
            stale_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.stale_active_execution_detected",
            )

        assert ws.status == WorkspaceStatus.pushing.value
        assert ws.subphase == PRESERVED_EXECUTION_SUBPHASE
        assert ws.failure_reason is None
        assert ws.failure_message is None
        assert len(preserved_events) == 1
        assert stale_events == []
        assert cleaner.calls == []
        assert inspector.calls == [compose_project]

    @pytest.mark.unit
    async def test_operator_refresh_before_stale_lease_expiry_allows_first_preservation(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        compose_project = "awf_refresh_before_lease_expiry"
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "refresh-before-lease-expiry",
            WorkspaceStatus.pushing,
            compose_project_name=compose_project,
        )
        now = datetime.now(UTC)
        status_started_at = now - timedelta(minutes=10)
        refresh_requested_at = now - timedelta(minutes=2)
        lease_expires_at = now - timedelta(minutes=1)
        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            assert ws is not None
            ws.execution_claimed_by = "old-worker"
            ws.execution_claim_expires_at = lease_expires_at
            state_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.state_changed",
            )
            pushing_started = next(
                event for event in state_events if event.new_state == WorkspaceStatus.pushing.value
            )
            pushing_started.occurred_at = status_started_at
            await WorkspaceControlService(
                s,
                project_stopper=_noop_project_stop,
                cleaner_factory=_unexpected_cleaner_factory,
            ).request_refresh_workspace(
                workspace_id,
                reason="operator recovery",
                idempotency_key="refresh-before-lease-expiry",
            )
            refresh_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.refresh_requested",
            )
            assert refresh_events
            refresh_events[0].occurred_at = refresh_requested_at
            await s.commit()

        inspector = _RecordingRuntimeInspector(
            {compose_project: _live_agent_snapshot(container_id="agent-refresh-before-expiry")}
        )
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                stale_active_execution_scan_interval_seconds=0.0,
            ),
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            preserved_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            )
            stale_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.stale_active_execution_detected",
            )

        assert ws.status == WorkspaceStatus.pushing.value
        assert ws.subphase == PRESERVED_EXECUTION_SUBPHASE
        assert ws.failure_reason is None
        assert ws.failure_message is None
        assert len(preserved_events) == 1
        assert preserved_events[0].payload is not None
        assert preserved_events[0].payload["runtime"]["services"][0]["container_id"] == (
            "agent-refresh-before-expiry"
        )
        assert stale_events == []
        assert cleaner.calls == []
        assert inspector.calls == [compose_project]

    @pytest.mark.unit
    async def test_operator_refresh_of_live_preserved_runtime_does_not_represerve(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        compose_project = "awf_preserved_refresh_live"
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "preserved-refresh-live",
            WorkspaceStatus.pushing,
            compose_project_name=compose_project,
        )
        inspector = _RecordingRuntimeInspector(
            {compose_project: _live_agent_snapshot(container_id="agent-preserved-refresh-live")}
        )
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                stale_active_execution_scan_interval_seconds=0.0,
            ),
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            await WorkspaceControlService(
                s,
                project_stopper=_noop_project_stop,
                cleaner_factory=_unexpected_cleaner_factory,
            ).request_refresh_workspace(
                workspace_id,
                reason="operator recovery",
                idempotency_key="refresh-preserved-live-runtime",
            )
            await s.commit()

        assert await worker.run_once() == 0
        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            preserved_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            )
            stale_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.stale_active_execution_detected",
            )

        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == FailureReason.infrastructure_failure.value
        assert len(preserved_events) == 1
        assert len(stale_events) == 1
        assert cleaner.calls == [
            {
                "workspace_id": workspace_id,
                "repo_url": str(origin_repo),
                "compose_project_name": compose_project,
                "compose_file_path": Path(f"/tmp/awf/{workspace_id}/compose.yml"),
                "worktree_host_path": None,
                "remove_volumes": True,
                "remove_worktree": False,
            }
        ]
        assert inspector.calls == [compose_project, compose_project, compose_project]

    @pytest.mark.unit
    async def test_operator_refresh_ignores_stale_cleanup_evidence_before_refresh(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        compose_project = "awf_preserved_refresh_stale_floor"
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "preserved-refresh-stale-floor",
            WorkspaceStatus.pushing,
            compose_project_name=compose_project,
        )
        now = datetime.now(UTC)
        status_started_at = now - timedelta(minutes=10)
        old_preservation_at = now - timedelta(minutes=9)
        old_stale_at = now - timedelta(minutes=8)
        refresh_requested_at = now - timedelta(minutes=1)
        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            assert ws is not None
            state_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.state_changed",
            )
            pushing_started = next(
                event for event in state_events if event.new_state == WorkspaceStatus.pushing.value
            )
            pushing_started.occurred_at = status_started_at
            preserved = await repo.add_event(
                ws,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
                reason_code=PRESERVED_EXECUTION_REASON_CODE,
                payload={
                    "workspace_status": WorkspaceStatus.pushing.value,
                    "decision": "preserve_runtime",
                },
            )
            preserved.occurred_at = old_preservation_at
            stale = await repo.add_event(
                ws,
                event_type="workspace.stale_active_execution_detected",
                reason_code="STALE_ACTIVE_EXECUTION",
                payload={
                    "compose_project_name": compose_project,
                    "workspace_status": WorkspaceStatus.pushing.value,
                    "runtime": {"stack_state": "running"},
                },
            )
            stale.occurred_at = old_stale_at
            await WorkspaceControlService(
                s,
                project_stopper=_noop_project_stop,
                cleaner_factory=_unexpected_cleaner_factory,
            ).request_refresh_workspace(
                workspace_id,
                reason="operator recovery",
                idempotency_key="refresh-stale-floor",
            )
            refresh_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.refresh_requested",
            )
            assert refresh_events
            refresh_events[0].occurred_at = refresh_requested_at
            await s.commit()

        inspector = _RecordingRuntimeInspector(
            {compose_project: _live_agent_snapshot(container_id="agent-refresh-stale-floor")}
        )
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                stale_active_execution_scan_interval_seconds=0.0,
            ),
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            stale_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.stale_active_execution_detected",
            )

        fresh_stale_events = [
            event for event in stale_events if event.occurred_at >= refresh_requested_at
        ]
        assert ws.status == WorkspaceStatus.pushing.value
        assert ws.failure_reason is None
        assert len(stale_events) == 2
        assert len(fresh_stale_events) == 1
        assert cleaner.calls == []
        assert inspector.calls == [compose_project]

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
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=1,
                stale_active_execution_scan_interval_seconds=60.0,
            ),
        )

        assert await worker.run_once() == 0
        assert worker._next_stale_active_execution_scan_at == 1_060.0  # noqa: SLF001
        assert await worker.run_once() == 0
        assert worker._next_stale_active_execution_scan_at == 1_060.0  # noqa: SLF001
        assert inspector.calls == ["awf_throttled_running"]
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.pushing.value

        current_time = 1_060.0

        assert await worker.run_once() == 0
        assert worker._next_stale_active_execution_scan_at == 1_120.0  # noqa: SLF001
        assert inspector.calls == ["awf_throttled_running"]
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.pushing.value
            assert ws.subphase == PRESERVED_EXECUTION_SUBPHASE
            assert ws.failure_reason is None
            preserved_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            )
            assert len(preserved_events) == 1
        assert cleaner.calls == []

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
            await asyncio.wait_for(task, timeout=WORKER_TEST_TIMEOUT_SECONDS)
            worker._execution_tasks.pop(workspace_id, None)

    @pytest.mark.unit
    async def test_stale_active_execution_scan_skips_unexpired_exited_claim_failure(
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

        inspector = _RecordingRuntimeInspector(
            {
                "awf_claimed_running": RuntimeSnapshot(
                    stack_state="stopped",
                    services=[
                        RuntimeService(
                            name="agent",
                            container_id="agent-claimed",
                            image="awf-agent:latest",
                            state="exited",
                            status="Exited (0) 1 minute ago",
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
            preserved_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            )
            stranded_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.runtime_stranded_detected",
            )
        assert preserved_events == []
        assert stranded_events == []
        assert inspector.calls == []

    @pytest.mark.unit
    async def test_restart_recovery_defers_live_runtime_until_unexpired_claim_expires(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        compose_project = "awf_unexpired_claim_live_runtime"
        claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "unexpired-claim-live-runtime",
            WorkspaceStatus.running,
            compose_project_name=compose_project,
            node_id="node-a",
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            ws.execution_claimed_by = "previous-worker"
            ws.execution_claim_expires_at = claim_expires_at
            await s.commit()

        inspector = _RecordingRuntimeInspector(
            {compose_project: _live_agent_snapshot(container_id="agent-unexpired")}
        )
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                stale_active_execution_scan_interval_seconds=0.0,
                node_id="node-a",
            ),
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            preserved_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            )
            stranded_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.runtime_stranded_detected",
            )
            stale_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.stale_active_execution_detected",
            )

        assert ws.status == WorkspaceStatus.running.value
        assert ws.subphase != PRESERVED_EXECUTION_SUBPHASE
        assert ws.execution_claimed_by == "previous-worker"
        assert ws.execution_claim_expires_at is not None
        assert ws.execution_claim_expires_at.replace(tzinfo=UTC) == claim_expires_at
        assert ws.failure_reason is None
        assert ws.failure_message is None
        assert preserved_events == []
        assert stranded_events == []
        assert stale_events == []
        assert inspector.calls == []
        assert cleaner.calls == []

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            expired_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            ws.execution_claim_expires_at = expired_claim_expires_at
            await s.commit()

        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            preserved_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            )
            stranded_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.runtime_stranded_detected",
            )
            stale_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.stale_active_execution_detected",
            )

        assert ws.status == WorkspaceStatus.running.value
        assert ws.subphase == PRESERVED_EXECUTION_SUBPHASE
        assert ws.execution_claimed_by is None
        assert ws.execution_claim_expires_at is None
        assert ws.failure_reason is None
        assert ws.failure_message is None
        assert len(preserved_events) == 1
        assert preserved_events[0].payload is not None
        assert preserved_events[0].payload["claim_cleanup"] == {
            "action": "cleared_stale",
            "reason_code": ("STALE_EXECUTION_CLAIM_CLEARED_DURING_ACTIVE_EXECUTION_PRESERVATION"),
            "previous_claimed_by": "previous-worker",
            "previous_expires_at": expired_claim_expires_at.isoformat(),
        }
        assert stranded_events == []
        assert stale_events == []
        assert inspector.calls == [compose_project]
        assert cleaner.calls == []

    @pytest.mark.unit
    async def test_restart_recovery_preservation_rechecks_refreshed_execution_claim(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        compose_project = "awf_refreshed_live_claim_runtime"
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "refreshed-live-claim-runtime",
            WorkspaceStatus.running,
            compose_project_name=compose_project,
            node_id="node-a",
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            ws.execution_claimed_by = "maybe-dead-worker"
            ws.execution_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await s.commit()

        refreshed_expires_at = datetime.now(UTC) + timedelta(minutes=5)

        class _RefreshingLiveRuntimeInspector:
            def __init__(self) -> None:
                self.calls: list[str | None] = []

            async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
                self.calls.append(compose_project_name)
                async with session_factory() as s:
                    ws = await WorkspaceRepository(s).get(workspace_id)
                    assert ws is not None
                    ws.execution_claimed_by = "live-worker"
                    ws.execution_claim_expires_at = refreshed_expires_at
                    await s.commit()
                return _live_agent_snapshot(container_id="agent-refreshed-live")

        inspector = _RefreshingLiveRuntimeInspector()
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                stale_active_execution_scan_interval_seconds=0.0,
                node_id="node-a",
            ),
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            preserved_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
            )
            stale_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.stale_active_execution_detected",
            )

        assert ws.status == WorkspaceStatus.running.value
        assert ws.subphase != PRESERVED_EXECUTION_SUBPHASE
        assert ws.execution_claimed_by == "live-worker"
        assert ws.execution_claim_expires_at is not None
        assert ws.execution_claim_expires_at.replace(tzinfo=UTC) == refreshed_expires_at
        assert ws.failure_reason is None
        assert preserved_events == []
        assert stale_events == []
        assert inspector.calls == [compose_project]
        assert cleaner.calls == []

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
    async def test_stale_active_execution_failure_transition_rechecks_refreshed_claim(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace_id = "ws_refreshed_during_failure"
        stale_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        refreshed_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        workspace = SimpleNamespace(
            id=workspace_id,
            status=WorkspaceStatus.running.value,
            execution_claimed_by="worker-a",
            execution_claim_expires_at=stale_expires_at,
            failure_reason=None,
            failure_message=None,
        )

        class RecordingSession:
            committed = False

            async def __aenter__(self) -> RecordingSession:
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            async def commit(self) -> None:
                self.committed = True

        class RecordingWorkspaceRepository:
            transition_calls = 0
            transition_if_current_calls: list[dict[str, object]] = []

            def __init__(self, session: RecordingSession) -> None:
                assert session is recording_session

            async def get_for_update(self, requested_workspace_id: str) -> SimpleNamespace:
                assert requested_workspace_id == workspace_id
                return workspace

            async def transition(
                self,
                transitioned_workspace: SimpleNamespace,
                *,
                to: WorkspaceStatus,
                reason_code: str,
                payload: dict[str, object] | None = None,
            ) -> SimpleNamespace:
                del reason_code, payload
                self.__class__.transition_calls += 1
                transitioned_workspace.status = to.value
                return transitioned_workspace

            async def transition_if_current(
                self,
                requested_workspace_id: str,
                *,
                from_status: WorkspaceStatus,
                to: WorkspaceStatus,
                reason_code: str,
                payload: dict[str, object] | None = None,
                extra_conditions: tuple[object, ...] = (),
            ) -> None:
                self.__class__.transition_if_current_calls.append(
                    {
                        "workspace_id": requested_workspace_id,
                        "from_status": from_status,
                        "to": to,
                        "reason_code": reason_code,
                        "payload": payload,
                        "extra_conditions_count": len(extra_conditions),
                    }
                )

        async def _refresh_claim_during_failure_causality_load(
            session: AsyncSession,
            loaded_workspace: Workspace,
        ) -> None:
            del session
            loaded_workspace.execution_claim_expires_at = refreshed_expires_at

        recording_session = RecordingSession()
        monkeypatch.setattr(
            worker_module,
            "load_failure_causality_snapshot",
            _refresh_claim_during_failure_causality_load,
        )
        monkeypatch.setattr(
            worker_module,
            "WorkspaceRepository",
            RecordingWorkspaceRepository,
        )
        worker = ControlWorker(
            session_factory=lambda: recording_session,  # type: ignore[arg-type]
            provisioner=object(),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                node_id="node-a",
            ),
        )

        await worker._fail_stale_active_execution(  # noqa: SLF001
            _ActiveExecutionCandidate(
                workspace_id=workspace_id,
                status=WorkspaceStatus.running,
                compose_project_name="awf_refreshed_during_failure",
            ),
            RuntimeSnapshot(stack_state="stopped", reason="worker restarted"),
        )

        assert RecordingWorkspaceRepository.transition_calls == 0
        assert RecordingWorkspaceRepository.transition_if_current_calls == [
            {
                "workspace_id": workspace_id,
                "from_status": WorkspaceStatus.running,
                "to": WorkspaceStatus.failed,
                "reason_code": "STALE_ACTIVE_EXECUTION",
                "payload": None,
                "extra_conditions_count": 1,
            }
        ]
        assert recording_session.committed is False
        assert workspace.status == WorkspaceStatus.running.value
        assert workspace.failure_reason is None
        assert workspace.execution_claimed_by == "worker-a"
        assert workspace.execution_claim_expires_at == refreshed_expires_at

    @pytest.mark.unit
    async def test_runtime_stranding_failure_transition_rechecks_refreshed_claim(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        workspace_id = "ws_stranding_refreshed_during_failure"
        stale_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        refreshed_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        workspace = SimpleNamespace(
            id=workspace_id,
            status=WorkspaceStatus.running.value,
            execution_claimed_by="worker-a",
            execution_claim_expires_at=stale_expires_at,
            monitor_claimed_by=None,
            monitor_claim_expires_at=None,
            failure_reason=None,
            failure_message=None,
        )

        class RecordingSession:
            committed = False

            async def __aenter__(self) -> RecordingSession:
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            async def commit(self) -> None:
                self.committed = True

        class RecordingWorkspaceRepository:
            transition_calls = 0
            transition_if_current_calls: list[dict[str, object]] = []
            event_calls = 0

            def __init__(self, session: RecordingSession) -> None:
                assert session is recording_session

            async def get_for_update(self, requested_workspace_id: str) -> SimpleNamespace:
                assert requested_workspace_id == workspace_id
                return workspace

            async def transition(
                self,
                transitioned_workspace: SimpleNamespace,
                *,
                to: WorkspaceStatus,
                reason_code: str,
                payload: dict[str, object] | None = None,
            ) -> SimpleNamespace:
                del reason_code, payload
                self.__class__.transition_calls += 1
                transitioned_workspace.status = to.value
                return transitioned_workspace

            async def transition_if_current(
                self,
                requested_workspace_id: str,
                *,
                from_status: WorkspaceStatus,
                to: WorkspaceStatus,
                reason_code: str,
                payload: dict[str, object] | None = None,
                extra_conditions: tuple[object, ...] = (),
            ) -> None:
                self.__class__.transition_if_current_calls.append(
                    {
                        "workspace_id": requested_workspace_id,
                        "from_status": from_status,
                        "to": to,
                        "reason_code": reason_code,
                        "payload": payload,
                        "extra_conditions_count": len(extra_conditions),
                    }
                )

            async def add_event(self, *_args: object, **_kwargs: object) -> None:
                self.__class__.event_calls += 1

        async def _refresh_claim_during_failure_causality_load(
            session: AsyncSession,
            loaded_workspace: Workspace,
        ) -> None:
            del session
            loaded_workspace.execution_claim_expires_at = refreshed_expires_at

        recording_session = RecordingSession()
        monkeypatch.setattr(
            worker_module,
            "load_failure_causality_snapshot",
            _refresh_claim_during_failure_causality_load,
        )
        monkeypatch.setattr(
            worker_module,
            "WorkspaceRepository",
            RecordingWorkspaceRepository,
        )
        worker = ControlWorker(
            session_factory=lambda: recording_session,  # type: ignore[arg-type]
            provisioner=object(),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                node_id="node-a",
            ),
        )
        finding = WorkspaceRuntimeFinding(
            workspace_id=workspace_id,
            workspace_status=WorkspaceStatus.running.value,
            status="stranded",
            reason_code="STRANDED_WORKSPACE",
            decision="fail_workspace",
            message="runtime is stranded",
        )

        await worker._fail_stranded_workspace(  # noqa: SLF001
            _ActiveExecutionCandidate(
                workspace_id=workspace_id,
                status=WorkspaceStatus.running,
                compose_project_name="awf_refreshed_runtime_stranding",
            ),
            RuntimeSnapshot(stack_state="stopped", reason="worker restarted"),
            finding,
        )

        assert RecordingWorkspaceRepository.transition_calls == 0
        assert RecordingWorkspaceRepository.transition_if_current_calls == [
            {
                "workspace_id": workspace_id,
                "from_status": WorkspaceStatus.running,
                "to": WorkspaceStatus.failed,
                "reason_code": "STRANDED_WORKSPACE",
                "payload": None,
                "extra_conditions_count": 1,
            }
        ]
        assert RecordingWorkspaceRepository.event_calls == 0
        assert recording_session.committed is False
        assert workspace.status == WorkspaceStatus.running.value
        assert workspace.failure_reason is None
        assert workspace.execution_claimed_by == "worker-a"
        assert workspace.execution_claim_expires_at == refreshed_expires_at

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


@pytest.mark.unit
def test_scheduler_candidate_cursor_handles_empty_and_uses_last_page_row() -> None:
    scoring_at = datetime(2026, 5, 2, 12, 2, tzinfo=UTC)
    first = SimpleNamespace(id="ws_b", created_at=datetime(2026, 5, 2, 12, 0, tzinfo=UTC))
    second = SimpleNamespace(id="ws_a", created_at=datetime(2026, 5, 2, 12, 1, tzinfo=UTC))
    third = SimpleNamespace(id="ws_c", created_at=datetime(2026, 5, 2, 12, 1, tzinfo=UTC))

    assert _scheduler_candidate_cursor([], scoring_at=scoring_at) is None
    assert _scheduler_candidate_cursor([first, second, third], scoring_at=scoring_at) == (
        SchedulerOrderCursor(
            class_priority=0,
            effective_score=0,
            queued_at=third.created_at,
            workspace_id="ws_c",
            scoring_at=scoring_at,
        )
    )


@pytest.mark.unit
def test_scheduler_candidate_cursor_uses_sql_age_boost_domain() -> None:
    scoring_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    workspace = SimpleNamespace(
        id="ws_aged",
        task_class="docs_task",
        task_policy={"scheduler": {"base_priority": 20}},
        created_at=scoring_at - timedelta(hours=2),
    )
    score = scheduler_score_from_workspace(workspace, now=scoring_at)

    assert score.age_boost > 0
    assert _scheduler_candidate_cursor(
        [workspace],
        scoring_at=scoring_at,
        dialect_name="postgresql",
    ) == SchedulerOrderCursor(
        class_priority=score.class_priority,
        effective_score=score.effective_score,
        queued_at=workspace.created_at,
        workspace_id=workspace.id,
        scoring_at=scoring_at,
    )
    assert _scheduler_candidate_cursor(
        [workspace],
        scoring_at=scoring_at,
        dialect_name="unsupported",
    ) == SchedulerOrderCursor(
        class_priority=score.class_priority,
        effective_score=score.effective_score - score.age_boost,
        queued_at=workspace.created_at,
        workspace_id=workspace.id,
        scoring_at=scoring_at,
    )


@pytest.mark.unit
def test_scheduler_candidate_cursor_uses_repository_sql_age_boost_dialect_contract() -> None:
    assert (
        worker_module.SCHEDULER_SQL_AGE_BOOST_DIALECTS
        is repositories_module.SCHEDULER_SQL_AGE_BOOST_DIALECTS
    )


@pytest.mark.unit
async def test_scheduler_page_filter_limit_uses_remaining_dispatch_slots(
    worker: ControlWorker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate_limit = _scheduler_candidate_fetch_limit(2)
    scoring_at = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    pages = [
        [
            SimpleNamespace(
                id=f"ws_page_{page_index}_{workspace_index}",
                task_class="docs_task",
                task_policy={},
                created_at=scoring_at
                + timedelta(seconds=(page_index * candidate_limit) + workspace_index),
            )
            for workspace_index in range(candidate_limit)
        ]
        for page_index in range(2)
    ]
    filter_limits: list[int] = []

    async def _list_schedulable_workspaces(
        self: WorkspaceRepository,
        *,
        status: WorkspaceStatus,
        limit: int,
        exclude_ids: set[str] | None = None,
        after: SchedulerOrderCursor | None = None,
        scoring_at: datetime | None = None,
    ) -> list[Workspace]:
        del self, exclude_ids, after, scoring_at
        assert status == WorkspaceStatus.ready
        assert limit == candidate_limit
        return pages.pop(0) if pages else []

    async def _return_one_candidate(
        session: AsyncSession,
        workspaces: list[Workspace],
        *,
        limit: int,
        scoring_at: datetime,
    ) -> list[str]:
        del session, scoring_at
        filter_limits.append(limit)
        return [workspaces[0].id]

    monkeypatch.setattr(
        WorkspaceRepository,
        "list_schedulable_workspaces",
        _list_schedulable_workspaces,
        raising=False,
    )
    worker._filter_scheduler_candidate_workspaces = (  # type: ignore[method-assign]
        _return_one_candidate
    )

    listed = await worker._list_scheduler_dispatchable_ids_from_pages(  # noqa: SLF001
        SimpleNamespace(info={}),  # type: ignore[arg-type]
        status=WorkspaceStatus.ready,
        limit=2,
    )

    assert filter_limits == [2, 1]
    assert listed == ["ws_page_0_0", "ws_page_1_0"]


@pytest.mark.unit
async def test_scheduler_candidate_filter_requires_scoring_timestamp(
    worker: ControlWorker,
) -> None:
    with pytest.raises(TypeError, match="scoring_at"):
        await worker._filter_scheduler_candidate_workspaces(  # noqa: SLF001
            SimpleNamespace(info={}),  # type: ignore[arg-type]
            [],
            limit=1,
        )


@pytest.mark.unit
def test_scheduler_item_type_guards_reject_empty_and_mixed_lists() -> None:
    workspace = object.__new__(Workspace)

    assert _scheduler_items_are_workspace_ids([]) is False
    assert _scheduler_items_are_workspaces([]) is False
    assert _scheduler_items_are_workspace_ids(["ws_1", "ws_2"]) is True
    assert _scheduler_items_are_workspace_ids(["ws_1", workspace]) is False
    assert _scheduler_items_are_workspaces([workspace]) is True
    assert _scheduler_items_are_workspaces([workspace, "ws_1"]) is False


@pytest.mark.unit
def test_monitor_recovery_claim_payload_derives_execution_cleanup_when_omitted() -> None:
    now = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    workspace = SimpleNamespace(
        execution_claimed_by="dead-worker",
        execution_claim_expires_at=now - timedelta(seconds=1),
    )

    payload = _monitor_recovery_claim_cleanup_payload(
        workspace,
        claim_cutoff=now,
        monitor_claimed_by="control-worker",
        monitor_claim_expires_at=now + timedelta(minutes=5),
    )

    assert payload["execution_claim"]["action"] == "cleared_stale"
    assert payload["monitor_claim"] == {
        "action": "acquired",
        "reason_code": "MONITOR_CLAIM_ACQUIRED_DURING_MONITOR_RECOVERY",
        "claimed_by": "control-worker",
        "expires_at": "2026-05-02T12:05:00+00:00",
    }


@pytest.mark.unit
def test_worker_helper_branches_normalize_naive_datetimes() -> None:
    cutoff = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)
    stale_execution = SimpleNamespace(
        execution_claimed_by="worker",
        execution_claim_expires_at=datetime(2026, 5, 2, 11, 59),
    )
    stale_monitor = SimpleNamespace(
        monitor_claimed_by="monitor",
        monitor_claim_expires_at=datetime(2026, 5, 2, 11, 59),
    )
    fresh_execution = SimpleNamespace(
        execution_claimed_by="worker",
        execution_claim_expires_at=cutoff + timedelta(minutes=5),
    )

    assert _execution_claim_is_stale(stale_execution, cutoff) is True
    assert _monitor_claim_is_stale(stale_monitor, cutoff) is True
    assert _has_running_agent_runtime(RuntimeSnapshot(stack_state="exited")) is False
    assert _json_datetime(datetime(2026, 5, 2, 12, 0)) == "2026-05-02T12:00:00+00:00"
    assert _utc_datetime(datetime(2026, 5, 2, 12, 0)) == cutoff
    assert (
        _active_execution_preservation_claim_cleanup_payload(
            fresh_execution,
            claim_cutoff=cutoff,
        )["action"]
        == "preserved_unexpired"
    )


@pytest.mark.unit
async def test_list_by_status_uses_repository_alias_for_non_scheduler_statuses(
    worker: ControlWorker,
) -> None:
    assert (
        await worker._list_by_status(  # noqa: SLF001
            WorkspaceStatus.failed,
            limit=10,
        )
        == []
    )


@pytest.mark.unit
async def test_provider_recovery_filter_short_circuits_empty_input(
    worker: ControlWorker,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        assert await worker._filter_provider_recovery_suppressed(session, []) == []  # noqa: SLF001


@pytest.mark.unit
async def test_run_forever_stops_after_idle_iteration_and_list_pending_alias(
    worker: ControlWorker,
) -> None:
    assert await worker._list_pending() == []  # noqa: SLF001

    run_once_calls = 0

    async def _run_once() -> int:
        nonlocal run_once_calls
        run_once_calls += 1
        worker.request_stop()
        return 0

    worker.run_once = _run_once  # type: ignore[method-assign]

    await worker.run_forever()

    assert run_once_calls == 1


@pytest.mark.unit
async def test_scheduler_filter_handles_string_ids_and_missing_rows(
    worker: ControlWorker,
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    ready_id = await _create_ready(
        session_factory,
        origin_repo,
        "gemini-ready-without-open-circuit",
        agent="gemini",
        task_policy={"agent_model": "gemini-2.5-pro"},
    )

    async with session_factory() as session:
        allowed = await worker._filter_provider_recovery_suppressed(  # noqa: SLF001
            session,
            [ready_id, "missing-workspace"],
        )

    assert allowed == [ready_id]


@pytest.mark.unit
async def test_scheduler_candidate_filter_short_circuits_empty_page(
    worker: ControlWorker,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        assert (
            await worker._filter_scheduler_candidate_workspaces(  # noqa: SLF001
                session,
                [],
                limit=10,
                scoring_at=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
            )
            == []
        )


@pytest.mark.unit
async def test_db_connection_closed_event_skips_stale_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RecordingSession:
        committed = False
        closed = False

        async def commit(self) -> None:
            self.committed = True

        async def rollback(self) -> None:
            raise AssertionError("stale event skip should not roll back")

        async def close(self) -> None:
            self.closed = True

    class StaleWorkspaceRepository:
        def __init__(self, session: RecordingSession) -> None:
            assert session is recording_session

        async def get(self, workspace_id: str) -> SimpleNamespace:
            assert workspace_id == "ws_stale_event"
            return SimpleNamespace(status=WorkspaceStatus.ready.value)

        async def add_event(self, *_args: object, **_kwargs: object) -> object:
            raise AssertionError("stale workspace should not receive an event")

    recording_session = RecordingSession()
    worker = ControlWorker(
        session_factory=lambda: recording_session,  # type: ignore[arg-type]
        provisioner=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    monkeypatch.setattr(
        "awf.control.worker.WorkspaceRepository",
        StaleWorkspaceRepository,
    )

    await worker._record_db_connection_closed_event(  # noqa: SLF001
        _ActiveExecutionCandidate(
            workspace_id="ws_stale_event",
            status=WorkspaceStatus.running,
            compose_project_name="awf_ws_stale_event",
        ),
        _closed_connection_error(),
    )

    assert recording_session.committed is True
    assert recording_session.closed is True


@pytest.mark.unit
async def test_dispatch_helpers_respect_limits_and_existing_tasks(
    worker: ControlWorker,
) -> None:
    existing_task = asyncio.create_task(_pending_execution_task())
    try:
        worker._execution_tasks["existing"] = existing_task  # noqa: SLF001

        assert worker._dispatchable_execution_ids(["new"], limit=0) == []  # noqa: SLF001
        assert (
            worker._dispatchable_execution_ids(["existing", "new"], limit=2)  # noqa: SLF001
            == ["new"]
        )
        assert worker._dispatch_ready_executions(["new"], limit=0) == set()  # noqa: SLF001
        assert (
            worker._dispatch_ready_executions(["existing"], limit=1)  # noqa: SLF001
            == set()
        )
        assert worker._dispatch_monitor_resumes(["new"], limit=0) == set()  # noqa: SLF001
        assert (
            worker._dispatch_monitor_resumes(["existing"], limit=1)  # noqa: SLF001
            == set()
        )
    finally:
        existing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await existing_task
        worker._execution_tasks.clear()  # noqa: SLF001


@pytest.mark.unit
async def test_safe_worker_paths_swallow_runtime_failures(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    class RaisingProvisioner:
        async def provision_claimed(self, workspace_id: str) -> None:
            assert workspace_id == "ws_provision"
            raise RuntimeError("provision failed")

    class RaisingExecutor(_RecordingExecutor):
        async def execute(self, workspace_id: str, **_kwargs: object) -> None:
            assert workspace_id == "ws_execute"
            raise RuntimeError("execute failed")

    class RaisingMonitorExecutor(_RecordingExecutor):
        async def resume_pr_monitor(self, workspace_id: str) -> None:
            assert workspace_id == "ws_monitor"
            raise RuntimeError("resume failed")

    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=RaisingProvisioner(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    finish_calls: list[dict[str, object]] = []

    async def _finish_monitor_recovery_operation(
        workspace_id: str,
        **kwargs: object,
    ) -> None:
        finish_calls.append({"workspace_id": workspace_id, **kwargs})

    worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )

    await worker._safely_provision_claimed("ws_provision")  # noqa: SLF001
    await worker._safely_resume_pr_monitor(  # noqa: SLF001
        "ws_monitor",
        recovery_operation_id="op_no_executor",
    )

    assert finish_calls == [
        {
            "workspace_id": "ws_monitor",
            "operation_id": "op_no_executor",
            "status": OperationStatus.failed,
            "error_code": "MONITOR_RECOVERY_NO_EXECUTOR",
            "error_message": "Worker has no executor configured.",
        }
    ]

    execute_worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=RaisingExecutor(),
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    await execute_worker._safely_execute("ws_execute")  # noqa: SLF001

    raising_worker = ControlWorker(
        session_factory=session_factory,
        provisioner=object(),  # type: ignore[arg-type]
        executor=RaisingMonitorExecutor(),
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    finish_calls.clear()
    raising_worker._finish_monitor_recovery_operation = (  # type: ignore[method-assign]
        _finish_monitor_recovery_operation
    )

    await raising_worker._safely_resume_pr_monitor(  # noqa: SLF001
        "ws_monitor",
        recovery_operation_id="op_resume_failed",
    )

    assert finish_calls[0]["workspace_id"] == "ws_monitor"
    assert finish_calls[0]["operation_id"] == "op_resume_failed"
    assert finish_calls[0]["status"] == OperationStatus.failed
    assert finish_calls[0]["error_code"] == "MONITOR_RECOVERY_FAILED"
    assert "resume failed" in str(finish_calls[0]["error_message"])


@pytest.mark.unit
async def test_claim_monitoring_pr_ids_respects_limit_and_existing_tasks(
    worker: ControlWorker,
) -> None:
    claim_calls: list[str] = []

    async def _claim_monitoring_pr(workspace_id: str) -> bool:
        claim_calls.append(workspace_id)
        return True

    worker._claim_monitoring_pr = _claim_monitoring_pr  # type: ignore[method-assign]  # noqa: SLF001

    assert (
        await worker._claim_monitoring_pr_ids(["first", "second"], limit=1)  # noqa: SLF001
        == ["first"]
    )
    assert claim_calls == ["first"]

    existing_task = asyncio.create_task(_pending_execution_task())
    try:
        worker._execution_tasks["existing"] = existing_task  # noqa: SLF001
        claim_calls.clear()

        assert (
            await worker._claim_monitoring_pr_ids(["existing", "next"], limit=2)  # noqa: SLF001
            == ["next"]
        )
        assert claim_calls == ["next"]
    finally:
        existing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await existing_task
        worker._execution_tasks.clear()  # noqa: SLF001


@pytest.mark.unit
async def test_finish_monitor_recovery_operation_handles_missing_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EmptyOperationSession:
        entered = False
        exited = False

        async def __aenter__(self) -> EmptyOperationSession:
            self.entered = True
            return self

        async def __aexit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            self.exited = True

    class EmptyOperationRepository:
        def __init__(self, session: EmptyOperationSession) -> None:
            assert session is empty_session

        async def get(self, operation_id: str) -> None:
            assert operation_id == "missing-op"
            return

    empty_session = EmptyOperationSession()
    worker = ControlWorker(
        session_factory=lambda: empty_session,  # type: ignore[arg-type]
        provisioner=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )

    await worker._finish_monitor_recovery_operation(  # noqa: SLF001
        "ws_monitor",
        operation_id=None,
        status=OperationStatus.succeeded,
    )
    assert empty_session.entered is False

    monkeypatch.setattr(
        "awf.control.worker.OperationRepository",
        EmptyOperationRepository,
    )
    await worker._finish_monitor_recovery_operation(  # noqa: SLF001
        "ws_monitor",
        operation_id="missing-op",
        status=OperationStatus.succeeded,
    )
    assert empty_session.entered is True
    assert empty_session.exited is True

    class RaisingSession:
        entered = False

        async def __aenter__(self) -> None:
            self.entered = True
            raise RuntimeError("session failed")

        async def __aexit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            raise AssertionError("enter failure should skip exit")

    raising_session = RaisingSession()
    failing_worker = ControlWorker(
        session_factory=lambda: raising_session,  # type: ignore[arg-type]
        provisioner=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )

    await failing_worker._finish_monitor_recovery_operation(  # noqa: SLF001
        "ws_monitor",
        operation_id="op-session-failed",
        status=OperationStatus.failed,
    )
    assert raising_session.entered is True


@pytest.mark.unit
async def test_secret_lease_expiration_scan_surfaces_expiration_failures(
    worker: ControlWorker,
) -> None:
    async def _raise_expiration_failure() -> None:
        raise RuntimeError("lease expiration failed")

    worker._next_secret_lease_expiration_scan_at = 0.0  # noqa: SLF001
    worker._expire_due_secret_leases = _raise_expiration_failure  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="lease expiration failed"):
        await worker._maybe_expire_due_secret_leases()  # noqa: SLF001


@pytest.mark.unit
async def test_secret_lease_expiration_scan_skips_transient_closed_connection(
    worker: ControlWorker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_time = 1_000.0
    monkeypatch.setattr("awf.control.worker.monotonic", lambda: current_time)
    expiration_attempts = 0

    async def _raise_expiration_failure() -> None:
        nonlocal expiration_attempts
        expiration_attempts += 1
        raise _closed_connection_error()

    worker._next_secret_lease_expiration_scan_at = 0.0  # noqa: SLF001
    worker._expire_due_secret_leases = _raise_expiration_failure  # type: ignore[method-assign]
    scan_interval = max(
        0.0,
        worker._config.secret_lease_expiration_scan_interval_seconds,  # noqa: SLF001
    )

    await worker._maybe_expire_due_secret_leases()  # noqa: SLF001

    expected_next_scan_at = current_time + scan_interval
    actual_next_scan_at = worker._next_secret_lease_expiration_scan_at  # noqa: SLF001
    assert actual_next_scan_at == expected_next_scan_at

    await worker._maybe_expire_due_secret_leases()  # noqa: SLF001

    assert expiration_attempts == 1


@pytest.mark.unit
async def test_stale_active_execution_scan_skips_transient_closed_connection(
    worker: ControlWorker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_time = 1_000.0
    monkeypatch.setattr("awf.control.worker.monotonic", lambda: current_time)
    recovery_attempts = 0

    async def _raise_recovery_failure() -> None:
        nonlocal recovery_attempts
        recovery_attempts += 1
        raise _closed_connection_error()

    worker._next_stale_active_execution_scan_at = 0.0  # noqa: SLF001
    worker._recover_stale_active_executions = _raise_recovery_failure  # type: ignore[method-assign]
    scan_interval = max(
        0.0,
        worker._config.stale_active_execution_scan_interval_seconds,  # noqa: SLF001
    )

    await worker._maybe_recover_stale_active_executions()  # noqa: SLF001

    expected_next_scan_at = current_time + scan_interval
    actual_next_scan_at = worker._next_stale_active_execution_scan_at  # noqa: SLF001
    assert actual_next_scan_at == expected_next_scan_at

    await worker._maybe_recover_stale_active_executions()  # noqa: SLF001

    assert recovery_attempts == 1


@pytest.mark.unit
async def test_expire_due_secret_leases_preserves_commit_error_when_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CloseError(Exception):
        pass

    class FailingCommitSession:
        invalidated = False
        closed = False

        async def __aenter__(self) -> FailingCommitSession:
            return self

        async def __aexit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            await self.close()

        async def commit(self) -> None:
            raise commit_error

        async def invalidate(self) -> None:
            self.invalidated = True

        async def rollback(self) -> None:
            raise AssertionError("transient commit failure should invalidate session")

        async def close(self) -> None:
            self.closed = True
            raise CloseError("close failed")

    class EmptySecretLeaseService:
        def __init__(self, session: FailingCommitSession) -> None:
            assert session is failing_session

        async def expire_due_secret_leases(self) -> list[object]:
            return []

    commit_error = _closed_connection_error()
    failing_session = FailingCommitSession()
    worker = ControlWorker(
        session_factory=lambda: failing_session,  # type: ignore[arg-type]
        provisioner=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    monkeypatch.setattr(
        "awf.control.worker.SecretLeaseService",
        EmptySecretLeaseService,
    )

    with pytest.raises(InterfaceError) as exc_info:
        await worker._expire_due_secret_leases()  # noqa: SLF001

    assert exc_info.value is commit_error
    assert failing_session.invalidated is True
    assert failing_session.closed is True


@pytest.mark.unit
async def test_db_connection_closed_event_rolls_back_when_event_write_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingEventSession:
        rolled_back = False
        closed = False

        async def __aenter__(self) -> FailingEventSession:
            return self

        async def __aexit__(
            self,
            _exc_type: object,
            _exc: object,
            _tb: object,
        ) -> None:
            await self.close()

        async def commit(self) -> None:
            raise AssertionError("commit should not run after event write failure")

        async def rollback(self) -> None:
            self.rolled_back = True

        async def close(self) -> None:
            self.closed = True

    class FailingEventRepository:
        def __init__(self, session: FailingEventSession) -> None:
            assert session is failing_session

        async def get(self, workspace_id: str) -> SimpleNamespace:
            assert workspace_id == "ws_event_failure"
            return SimpleNamespace(status=WorkspaceStatus.running.value)

        async def add_event(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("event write failed")

    failing_session = FailingEventSession()
    worker = ControlWorker(
        session_factory=lambda: failing_session,  # type: ignore[arg-type]
        provisioner=object(),  # type: ignore[arg-type]
        config=WorkerConfig(poll_interval_seconds=0.01),
    )
    monkeypatch.setattr(
        "awf.control.worker.WorkspaceRepository",
        FailingEventRepository,
    )

    await worker._record_db_connection_closed_event(  # noqa: SLF001
        _ActiveExecutionCandidate(
            workspace_id="ws_event_failure",
            status=WorkspaceStatus.running,
            compose_project_name="awf_ws_event_failure",
        ),
        _closed_connection_error(),
    )

    assert failing_session.rolled_back is True
    assert failing_session.closed is True


@pytest.mark.unit
async def test_stale_active_execution_check_preserves_unexpired_execution_claim(
    worker: ControlWorker,
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    workspace_id = await _create_active_execution(
        session_factory,
        origin_repo,
        "fresh-execution-claim",
        WorkspaceStatus.running,
    )
    async with session_factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        ws.execution_claimed_by = "live-worker"
        ws.execution_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        await session.commit()

    assert not await worker._stale_active_execution_can_fail(  # noqa: SLF001
        _ActiveExecutionCandidate(
            workspace_id=workspace_id,
            status=WorkspaceStatus.running,
            compose_project_name=f"awf_{workspace_id}",
            repo_url=str(origin_repo),
        )
    )


@pytest.mark.unit
async def test_stale_active_execution_check_ignores_stale_event_before_refresh(
    worker: ControlWorker,
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    workspace_id = await _create_active_execution(
        session_factory,
        origin_repo,
        "stale-event-before-refresh",
        WorkspaceStatus.running,
    )
    now = datetime.now(UTC)
    status_started_at = now - timedelta(minutes=10)
    old_stale_at = now - timedelta(minutes=8)
    refresh_requested_at = now - timedelta(minutes=1)
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        assert ws is not None
        state_events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.state_changed",
        )
        running_started = next(
            event for event in state_events if event.new_state == WorkspaceStatus.running.value
        )
        running_started.occurred_at = status_started_at
        stale = await repo.add_event(
            ws,
            event_type="workspace.stale_active_execution_detected",
            reason_code="STALE_ACTIVE_EXECUTION",
            payload={
                "compose_project_name": f"awf_{workspace_id}",
                "workspace_status": WorkspaceStatus.running.value,
                "runtime": {"stack_state": "running"},
            },
        )
        stale.occurred_at = old_stale_at
        await WorkspaceControlService(
            session,
            project_stopper=_noop_project_stop,
            cleaner_factory=_unexpected_cleaner_factory,
        ).request_refresh_workspace(
            workspace_id,
            reason="operator recovery",
            idempotency_key="refresh-before-stale-check",
        )
        refresh_events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.refresh_requested",
        )
        assert refresh_events
        refresh_events[0].occurred_at = refresh_requested_at
        await session.commit()

    assert not await worker._stale_active_execution_can_fail(  # noqa: SLF001
        _ActiveExecutionCandidate(
            workspace_id=workspace_id,
            status=WorkspaceStatus.running,
            compose_project_name=f"awf_{workspace_id}",
            repo_url=str(origin_repo),
        )
    )


@pytest.mark.unit
async def test_cleanup_failure_event_preserves_unexpired_execution_claim(
    worker: ControlWorker,
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    workspace_id = await _create_active_execution(
        session_factory,
        origin_repo,
        "cleanup-failure-fresh-claim",
        WorkspaceStatus.running,
    )
    async with session_factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        ws.execution_claimed_by = "live-worker"
        ws.execution_claim_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        await session.commit()

    await worker._record_stale_active_execution_cleanup_failed(  # noqa: SLF001
        _ActiveExecutionCandidate(
            workspace_id=workspace_id,
            status=WorkspaceStatus.running,
            compose_project_name=f"awf_{workspace_id}",
            repo_url=str(origin_repo),
        ),
        RuntimeSnapshot(stack_state="running"),
        cleanup=None,
        message="cleanup should be skipped while claim is live",
    )

    async with session_factory() as session:
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.stale_active_execution_cleanup_failed",
        )

    assert events == []


@pytest.mark.unit
async def test_cleanup_failure_event_skips_status_mismatch(
    worker: ControlWorker,
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    workspace_id = await _create_active_execution(
        session_factory,
        origin_repo,
        "cleanup-failure-status-mismatch",
        WorkspaceStatus.running,
    )

    await worker._record_stale_active_execution_cleanup_failed(  # noqa: SLF001
        _ActiveExecutionCandidate(
            workspace_id=workspace_id,
            status=WorkspaceStatus.validating,
            compose_project_name=f"awf_{workspace_id}",
            repo_url=str(origin_repo),
        ),
        RuntimeSnapshot(stack_state="running"),
        cleanup=None,
        message="cleanup event should be skipped after status changes",
    )

    async with session_factory() as session:
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.stale_active_execution_cleanup_failed",
        )

    assert events == []


@pytest.mark.unit
async def test_missing_monitoring_pr_workspace_cannot_be_claimed(
    worker: ControlWorker,
) -> None:
    assert await worker._claim_monitoring_pr("ws_missing") is False  # noqa: SLF001


class TestTerminalRuntimeRelease:
    @pytest.mark.unit
    async def test_release_stops_terminal_failed_workspace_runtime_preserving_volumes_and_worktree(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_terminal_execution(
            session_factory,
            origin_repo,
            "terminal-release-failed",
            WorkspaceStatus.failed,
        )
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
            ),
        )

        await worker._release_terminal_runtime_resources()  # noqa: SLF001

        assert cleaner.calls == [
            {
                "workspace_id": workspace_id,
                "repo_url": str(origin_repo),
                "compose_project_name": f"awf_{workspace_id}",
                "compose_file_path": Path(f"/tmp/awf/{workspace_id}/compose.yml"),
                "worktree_host_path": None,
                "remove_volumes": False,
                "remove_worktree": False,
            }
        ]
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.failure_message == "seed failure"
            released_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.terminal_runtime_released",
            )
            assert len(released_events) == 1
            assert released_events[0].reason_code == "TERMINAL_RUNTIME_RELEASED"
            assert released_events[0].payload is not None
            assert released_events[0].payload["cleanup"]["status"] == "succeeded"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "status",
        [
            WorkspaceStatus.failed,
            WorkspaceStatus.cancelled,
            WorkspaceStatus.completed,
            WorkspaceStatus.destroyed,
        ],
    )
    async def test_release_runs_for_all_terminal_states(
        self,
        status: WorkspaceStatus,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_terminal_execution(
            session_factory,
            origin_repo,
            f"terminal-release-{status.value}",
            status,
        )
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
            ),
        )

        await worker._release_terminal_runtime_resources()  # noqa: SLF001

        assert len(cleaner.calls) == 1
        assert cleaner.calls[0]["workspace_id"] == workspace_id
        assert cleaner.calls[0]["remove_volumes"] is False
        assert cleaner.calls[0]["remove_worktree"] is False
        async with session_factory() as s:
            released_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.terminal_runtime_released",
            )
        assert len(released_events) == 1

    @pytest.mark.unit
    async def test_release_is_idempotent_with_repeat_calls(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_terminal_execution(
            session_factory,
            origin_repo,
            "terminal-release-idempotent",
            WorkspaceStatus.failed,
        )
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
            ),
        )

        await worker._release_terminal_runtime_resources()  # noqa: SLF001
        await worker._release_terminal_runtime_resources()  # noqa: SLF001
        await worker._release_terminal_runtime_resources()  # noqa: SLF001

        assert len(cleaner.calls) == 1
        async with session_factory() as s:
            released_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.terminal_runtime_released",
            )
        assert len(released_events) == 1

    @pytest.mark.unit
    async def test_release_records_failure_event_and_preserves_original_failure_reason(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_terminal_execution(
            session_factory,
            origin_repo,
            "terminal-release-cleanup-fail",
            WorkspaceStatus.failed,
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            ws.failure_reason = "agent_failure"
            ws.failure_message = "original agent diagnostics"
            await s.commit()
        cleaner = _RecordingRuntimeCleaner(
            WorkspaceCleanupResult(
                status="partial",
                reason_code=CLEANUP_PARTIAL,
                steps=(
                    WorkspaceCleanupStepResult(
                        name="compose_down",
                        status="failed",
                        reason_code="DOCKER_UNAVAILABLE",
                        error="cannot connect to docker",
                    ),
                ),
            )
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
            ),
        )

        await worker._release_terminal_runtime_resources()  # noqa: SLF001

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "agent_failure"
            assert ws.failure_message == "original agent diagnostics"
            release_failure_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.terminal_runtime_release_failed",
            )
            release_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.terminal_runtime_released",
            )
        assert len(release_failure_events) == 1
        assert release_failure_events[0].reason_code == "TERMINAL_RUNTIME_RELEASE_FAILED"
        assert release_failure_events[0].payload is not None
        assert release_failure_events[0].payload["cleanup"]["reason_code"] == CLEANUP_PARTIAL
        assert release_events == []

    @pytest.mark.unit
    async def test_terminal_runtime_release_failure_preserves_validation_provenance_details(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_terminal_execution(
            session_factory,
            origin_repo,
            "terminal-release-preserve-validation",
            WorkspaceStatus.failed,
        )
        validation_run_id = await _seed_primary_failure_evidence(
            session_factory,
            workspace_id,
            failure_reason=FailureReason.validation_failure.value,
            failure_message="pytest failed before terminal release cleanup",
            reason_code="PYTEST_TEST_FAILURE",
            include_validation_run=True,
        )
        cleaner = _RecordingRuntimeCleaner(
            WorkspaceCleanupResult(
                status="partial",
                reason_code=CLEANUP_PARTIAL,
                steps=(
                    WorkspaceCleanupStepResult(
                        name="compose_down",
                        status="failed",
                        reason_code="DOCKER_UNAVAILABLE",
                        error="cannot connect to docker",
                    ),
                ),
            )
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
            ),
        )

        await worker._record_terminal_runtime_release_failed(  # noqa: SLF001
            _TerminalRuntimeCandidate(
                workspace_id=workspace_id,
                status=WorkspaceStatus.failed,
                repo_url=str(origin_repo),
                compose_project_name=f"awf_{workspace_id}",
                compose_file_path=f"/tmp/awf/{workspace_id}/compose.yml",
            ),
            cleanup=cleaner.result,
            message="cleanup failed after validation failure",
        )

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == FailureReason.validation_failure.value
            assert ws.failure_message == "pytest failed before terminal release cleanup"
            validation_run = await ValidationRunRepository(s).get(validation_run_id or "")
            assert validation_run is not None
            assert validation_run.coverage is not None
            assert validation_run.coverage["failing_test_node_ids"] == [
                "tests/unit/test_example.py::test_failure"
            ]
            release_failure_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.terminal_runtime_release_failed",
            )

        assert len(release_failure_events) == 1
        assert release_failure_events[0].payload is not None
        assert release_failure_events[0].payload["cleanup"]["reason_code"] == CLEANUP_PARTIAL
        assert release_failure_events[0].payload["primary_failure"]["reason_code"] == (
            "PYTEST_TEST_FAILURE"
        )
        assert release_failure_events[0].payload["primary_failure"]["validation_run"]["id"] == (
            validation_run_id
        )

    @pytest.mark.unit
    async def test_release_does_not_record_failure_event_when_success_event_already_exists(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_terminal_execution(
            session_factory,
            origin_repo,
            "terminal-release-success-then-failure",
            WorkspaceStatus.failed,
        )
        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            assert ws is not None
            await repo.add_event(
                ws,
                event_type="workspace.terminal_runtime_released",
                reason_code="TERMINAL_RUNTIME_RELEASED",
                payload={
                    "compose_project_name": ws.compose_project_name,
                    "workspace_status": ws.status,
                    "cleanup": {"status": "succeeded"},
                },
            )
            await s.commit()
        cleaner = _RecordingRuntimeCleaner(
            WorkspaceCleanupResult(
                status="partial",
                reason_code=CLEANUP_PARTIAL,
                steps=(
                    WorkspaceCleanupStepResult(
                        name="compose_down",
                        status="failed",
                        reason_code="DOCKER_UNAVAILABLE",
                        error="cannot connect to docker",
                    ),
                ),
            )
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
            ),
        )

        await worker._record_terminal_runtime_release_failed(  # noqa: SLF001
            _TerminalRuntimeCandidate(
                workspace_id=workspace_id,
                status=WorkspaceStatus.failed,
                repo_url=str(origin_repo),
                compose_project_name=f"awf_{workspace_id}",
                compose_file_path=f"/tmp/awf/{workspace_id}/compose.yml",
            ),
            cleanup=cleaner.result,
            message="should not be recorded",
        )

        async with session_factory() as s:
            release_failure_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.terminal_runtime_release_failed",
            )
            release_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.terminal_runtime_released",
            )
        assert release_failure_events == []
        assert len(release_events) == 1

    @pytest.mark.unit
    async def test_release_does_not_record_duplicate_failure_event_on_repeated_failure(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_terminal_execution(
            session_factory,
            origin_repo,
            "terminal-release-repeated-failure",
            WorkspaceStatus.failed,
        )
        cleaner = _RecordingRuntimeCleaner(
            WorkspaceCleanupResult(
                status="partial",
                reason_code=CLEANUP_PARTIAL,
                steps=(
                    WorkspaceCleanupStepResult(
                        name="compose_down",
                        status="failed",
                        reason_code="DOCKER_UNAVAILABLE",
                        error="cannot connect to docker",
                    ),
                ),
            )
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
            ),
        )

        await worker._release_terminal_runtime_resources()  # noqa: SLF001
        await worker._release_terminal_runtime_resources()  # noqa: SLF001
        await worker._release_terminal_runtime_resources()  # noqa: SLF001

        assert len(cleaner.calls) == 3
        async with session_factory() as s:
            release_failure_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.terminal_runtime_release_failed",
            )
            release_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.terminal_runtime_released",
            )
        assert len(release_failure_events) == 1
        assert release_failure_events[0].reason_code == "TERMINAL_RUNTIME_RELEASE_FAILED"
        assert release_events == []

    @pytest.mark.unit
    async def test_release_rotates_past_persistent_failures_to_drain_backlog(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_ids: list[str] = []
        for i in range(3):
            workspace_ids.append(
                await _create_terminal_execution(
                    session_factory,
                    origin_repo,
                    f"terminal-release-rotation-{i}",
                    WorkspaceStatus.failed,
                )
            )
        cleaner = _RecordingRuntimeCleaner(
            WorkspaceCleanupResult(
                status="partial",
                reason_code=CLEANUP_PARTIAL,
                steps=(
                    WorkspaceCleanupStepResult(
                        name="compose_down",
                        status="failed",
                        reason_code="DOCKER_UNAVAILABLE",
                        error="cannot connect to docker",
                    ),
                ),
            )
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
                terminal_runtime_release_max_per_scan=2,
            ),
        )

        await worker._release_terminal_runtime_resources()  # noqa: SLF001
        first_scan_ids = {call["workspace_id"] for call in cleaner.calls}
        assert len(first_scan_ids) == 2
        assert workspace_ids[2] not in first_scan_ids

        await worker._release_terminal_runtime_resources()  # noqa: SLF001
        all_scanned_ids = {call["workspace_id"] for call in cleaner.calls}
        assert all_scanned_ids == set(workspace_ids), (
            "second scan must rotate past failed candidates so the third "
            "workspace receives a cleanup attempt"
        )

    @pytest.mark.unit
    async def test_release_retry_marker_does_not_advance_updated_at(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        # Workspace.updated_at is the retention cutoff used by service/gc.py and
        # service/orphan_resources.py. Persistently-failing terminal releases
        # MUST NOT keep advancing it on every retry, or volumes/worktrees for
        # those workspaces would never age out. The rotation marker
        # (terminal_release_retry_at) is bumped instead so the candidate scan
        # still rotates past stuck rows.
        workspace_id = await _create_terminal_execution(
            session_factory,
            origin_repo,
            "terminal-release-retry-marker",
            WorkspaceStatus.failed,
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            initial_updated_at = ws.updated_at
            assert ws.terminal_release_retry_at is None
        cleaner = _RecordingRuntimeCleaner(
            WorkspaceCleanupResult(
                status="partial",
                reason_code=CLEANUP_PARTIAL,
                steps=(
                    WorkspaceCleanupStepResult(
                        name="compose_down",
                        status="failed",
                        reason_code="DOCKER_UNAVAILABLE",
                        error="cannot connect to docker",
                    ),
                ),
            )
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
            ),
        )

        await worker._release_terminal_runtime_resources()  # noqa: SLF001
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            first_retry_marker = ws.terminal_release_retry_at
            assert first_retry_marker is not None
            assert ws.updated_at == initial_updated_at, (
                "updated_at must not advance on a failed release retry — it is "
                "the retention cutoff used by GC and orphan retention paths"
            )

        await worker._release_terminal_runtime_resources()  # noqa: SLF001
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.terminal_release_retry_at is not None
            assert ws.terminal_release_retry_at >= first_retry_marker, (
                "retry marker must advance on each retry so the candidate scan "
                "rotates past persistently-failing rows"
            )
            assert ws.updated_at == initial_updated_at, (
                "updated_at must remain pinned across repeated failed retries "
                "so the retention timer keeps ticking"
            )

    @pytest.mark.unit
    async def test_release_runs_for_legacy_workspace_with_only_compose_file_path(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_terminal_execution(
            session_factory,
            origin_repo,
            "terminal-release-no-compose-project",
            WorkspaceStatus.failed,
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            ws.compose_project_name = None
            await s.commit()
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
            ),
        )

        await worker._release_terminal_runtime_resources()  # noqa: SLF001

        assert cleaner.calls == [
            {
                "workspace_id": workspace_id,
                "repo_url": str(origin_repo),
                "compose_project_name": None,
                "compose_file_path": Path(f"/tmp/awf/{workspace_id}/compose.yml"),
                "worktree_host_path": None,
                "remove_volumes": False,
                "remove_worktree": False,
            }
        ]
        async with session_factory() as s:
            released_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.terminal_runtime_released",
            )
        assert len(released_events) == 1

    @pytest.mark.unit
    async def test_release_runs_for_legacy_workspace_with_no_persisted_runtime_state(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_terminal_execution(
            session_factory,
            origin_repo,
            "terminal-release-no-runtime-signal",
            WorkspaceStatus.failed,
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            ws.compose_project_name = None
            ws.compose_file_path = None
            await s.commit()
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
            ),
        )

        await worker._release_terminal_runtime_resources()  # noqa: SLF001

        assert cleaner.calls == [
            {
                "workspace_id": workspace_id,
                "repo_url": str(origin_repo),
                "compose_project_name": None,
                "compose_file_path": None,
                "worktree_host_path": None,
                "remove_volumes": False,
                "remove_worktree": False,
            }
        ]
        async with session_factory() as s:
            released_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.terminal_runtime_released",
            )
        assert len(released_events) == 1

    @pytest.mark.unit
    async def test_release_runs_for_failed_provisioning_with_null_node_id(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        # ``Provisioner._mark_failed`` now stamps ``node_id`` on the failure
        # path, but legacy rows persisted before that fix may still have NULL
        # ``node_id``. The sweep must still pick those legacy rows up so a
        # single-node deployment can tear down the leaked ``awf_<workspace_id>``
        # project; multi-node deployments need an ownership claim before this
        # path is safe (see worker.py comment on the NULL ``node_id`` branch).
        workspace_id = await _create_terminal_execution(
            session_factory,
            origin_repo,
            "terminal-release-null-node-id",
            WorkspaceStatus.failed,
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            ws.node_id = None
            await s.commit()
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
                node_id="worker-node-a",
            ),
        )

        await worker._release_terminal_runtime_resources()  # noqa: SLF001

        assert len(cleaner.calls) == 1
        assert cleaner.calls[0]["workspace_id"] == workspace_id
        async with session_factory() as s:
            released_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.terminal_runtime_released",
            )
        assert len(released_events) == 1

    @pytest.mark.unit
    async def test_release_skips_terminal_workspace_owned_by_other_node(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_terminal_execution(
            session_factory,
            origin_repo,
            "terminal-release-other-node",
            WorkspaceStatus.failed,
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            ws.node_id = "worker-node-b"
            await s.commit()
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
                node_id="worker-node-a",
            ),
        )

        await worker._release_terminal_runtime_resources()  # noqa: SLF001

        assert cleaner.calls == []
        async with session_factory() as s:
            released_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.terminal_runtime_released",
            )
        assert released_events == []

    @pytest.mark.unit
    async def test_release_runs_for_destroyed_workspace_with_leaked_runtime(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_terminal_execution(
            session_factory,
            origin_repo,
            "terminal-release-destroyed",
            WorkspaceStatus.destroyed,
        )
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
            ),
        )

        await worker._release_terminal_runtime_resources()  # noqa: SLF001

        assert len(cleaner.calls) == 1
        assert cleaner.calls[0]["workspace_id"] == workspace_id
        assert cleaner.calls[0]["remove_volumes"] is False
        assert cleaner.calls[0]["remove_worktree"] is False
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.destroyed.value
            events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.terminal_runtime_released",
            )
        assert len(events) == 1

    @pytest.mark.unit
    async def test_release_does_not_run_when_runtime_cleaner_not_configured(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_terminal_execution(
            session_factory,
            origin_repo,
            "terminal-release-no-cleaner",
            WorkspaceStatus.failed,
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=None,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
            ),
        )

        await worker._release_terminal_runtime_resources()  # noqa: SLF001

        async with session_factory() as s:
            events = await WorkspaceEventRepository(s).list(workspace_id=workspace_id)
            release_events = [
                event
                for event in events
                if event.event_type
                in {
                    "workspace.terminal_runtime_released",
                    "workspace.terminal_runtime_release_failed",
                }
            ]
        assert release_events == []

    @pytest.mark.unit
    async def test_release_scan_skips_transient_closed_connection(
        self,
        worker: ControlWorker,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        current_time = 1_000.0
        monkeypatch.setattr("awf.control.worker.monotonic", lambda: current_time)
        release_attempts = 0

        async def _raise_release_failure() -> None:
            nonlocal release_attempts
            release_attempts += 1
            raise _closed_connection_error()

        worker._next_terminal_runtime_release_scan_at = 0.0  # noqa: SLF001
        worker._release_terminal_runtime_resources = _raise_release_failure  # type: ignore[method-assign]
        scan_interval = max(
            0.0,
            worker._config.terminal_runtime_release_scan_interval_seconds,  # noqa: SLF001
        )

        await worker._maybe_release_terminal_runtime()  # noqa: SLF001

        expected_next_scan_at = current_time + scan_interval
        actual_next_scan_at = worker._next_terminal_runtime_release_scan_at  # noqa: SLF001
        assert actual_next_scan_at == expected_next_scan_at

        await worker._maybe_release_terminal_runtime()  # noqa: SLF001

        assert release_attempts == 1

    @pytest.mark.unit
    async def test_release_does_not_hold_db_lock_during_cleanup_io(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_terminal_execution(
            session_factory,
            origin_repo,
            "terminal-release-no-lock-held",
            WorkspaceStatus.failed,
        )
        cleaner_blocked = asyncio.Event()
        cleaner_released = asyncio.Event()
        second_read_done = asyncio.Event()

        class _BlockingCleaner:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []
                self.result = WorkspaceCleanupResult.from_steps(
                    [
                        WorkspaceCleanupStepResult(
                            name="compose_down",
                            status="succeeded",
                            reason_code=COMPOSE_DOWN_SUCCEEDED,
                        )
                    ]
                )

            async def cleanup(self, **kwargs: object) -> WorkspaceCleanupResult:
                self.calls.append(kwargs)
                cleaner_blocked.set()
                await cleaner_released.wait()
                return self.result

        cleaner = _BlockingCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,  # type: ignore[arg-type]
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
            ),
        )

        release_task = asyncio.create_task(
            worker._release_terminal_runtime_resources()  # noqa: SLF001
        )
        try:
            await asyncio.wait_for(cleaner_blocked.wait(), timeout=10.0)

            async def _read_during_io() -> None:
                async with session_factory() as session:
                    locked = await session.execute(
                        select(Workspace)
                        .where(Workspace.id == workspace_id)
                        .with_for_update(nowait=True)
                    )
                    ws = locked.scalar_one()
                    assert ws.status == WorkspaceStatus.failed.value
                second_read_done.set()

            await asyncio.wait_for(_read_during_io(), timeout=10.0)
            assert second_read_done.is_set()
        finally:
            cleaner_released.set()
            await release_task

        async with session_factory() as s:
            events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.terminal_runtime_released",
            )
        assert len(events) == 1

    @pytest.mark.unit
    async def test_record_release_skips_when_workspace_row_already_locked(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_terminal_execution(
            session_factory,
            origin_repo,
            "terminal-release-row-locked",
            WorkspaceStatus.failed,
        )
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
            ),
        )
        candidate = _TerminalRuntimeCandidate(
            workspace_id=workspace_id,
            status=WorkspaceStatus.failed,
            repo_url=str(origin_repo),
            compose_project_name=f"awf_{workspace_id}",
            compose_file_path=f"/tmp/awf/{workspace_id}/compose.yml",
        )

        async with session_factory() as locking_session:
            await locking_session.execute(
                select(Workspace).where(Workspace.id == workspace_id).with_for_update()
            )
            await worker._record_terminal_runtime_released(  # noqa: SLF001
                candidate,
                cleaner.result,
            )
            async with session_factory() as s:
                release_events = await WorkspaceEventRepository(s).list(
                    workspace_id=workspace_id,
                    event_type="workspace.terminal_runtime_released",
                )
            assert release_events == []

        await worker._record_terminal_runtime_released(  # noqa: SLF001
            candidate,
            cleaner.result,
        )
        async with session_factory() as s:
            release_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.terminal_runtime_released",
            )
        assert len(release_events) == 1

    @pytest.mark.unit
    async def test_record_release_failed_skips_when_workspace_row_already_locked(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_terminal_execution(
            session_factory,
            origin_repo,
            "terminal-release-failed-row-locked",
            WorkspaceStatus.failed,
        )
        cleaner = _RecordingRuntimeCleaner(
            WorkspaceCleanupResult(
                status="partial",
                reason_code=CLEANUP_PARTIAL,
                steps=(
                    WorkspaceCleanupStepResult(
                        name="compose_down",
                        status="failed",
                        reason_code="DOCKER_UNAVAILABLE",
                        error="cannot connect to docker",
                    ),
                ),
            )
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
            ),
        )
        candidate = _TerminalRuntimeCandidate(
            workspace_id=workspace_id,
            status=WorkspaceStatus.failed,
            repo_url=str(origin_repo),
            compose_project_name=f"awf_{workspace_id}",
            compose_file_path=f"/tmp/awf/{workspace_id}/compose.yml",
        )

        async with session_factory() as locking_session:
            await locking_session.execute(
                select(Workspace).where(Workspace.id == workspace_id).with_for_update()
            )
            await worker._record_terminal_runtime_release_failed(  # noqa: SLF001
                candidate,
                cleanup=cleaner.result,
                message="cleanup failed",
            )
            async with session_factory() as s:
                failure_events = await WorkspaceEventRepository(s).list(
                    workspace_id=workspace_id,
                    event_type="workspace.terminal_runtime_release_failed",
                )
            assert failure_events == []

        await worker._record_terminal_runtime_release_failed(  # noqa: SLF001
            candidate,
            cleanup=cleaner.result,
            message="cleanup failed",
        )
        async with session_factory() as s:
            failure_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.terminal_runtime_release_failed",
            )
        assert len(failure_events) == 1

    @pytest.mark.unit
    async def test_release_cancellation_during_cleanup_leaves_no_success_event(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_terminal_execution(
            session_factory,
            origin_repo,
            "terminal-release-cancellation",
            WorkspaceStatus.failed,
        )
        cleaner_blocked = asyncio.Event()

        class _BlockingCleaner:
            async def cleanup(self, **_kwargs: object) -> WorkspaceCleanupResult:
                cleaner_blocked.set()
                await asyncio.Event().wait()
                raise AssertionError("blocked cleaner should never complete")

        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=_BlockingCleaner(),  # type: ignore[arg-type]
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
            ),
        )

        release_task = asyncio.create_task(
            worker._release_terminal_runtime_resources()  # noqa: SLF001
        )
        await asyncio.wait_for(cleaner_blocked.wait(), timeout=10.0)
        release_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await release_task

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.failure_message == "seed failure"
            release_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.terminal_runtime_released",
            )
            failure_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.terminal_runtime_release_failed",
            )
        assert release_events == []
        assert failure_events == []

    @pytest.mark.unit
    async def test_release_skips_active_workspaces(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "release-skip-active",
            WorkspaceStatus.running,
            compose_project_name="awf_active_release",
        )
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
            ),
        )

        await worker._release_terminal_runtime_resources()  # noqa: SLF001

        assert cleaner.calls == []
        async with session_factory() as s:
            events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.terminal_runtime_released",
            )
        assert events == []

    @pytest.mark.unit
    async def test_release_bounds_work_per_scan_and_drains_backlog_across_scans(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_ids: list[str] = []
        for i in range(5):
            workspace_ids.append(
                await _create_terminal_execution(
                    session_factory,
                    origin_repo,
                    f"terminal-release-batch-{i}",
                    WorkspaceStatus.failed,
                )
            )
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
                terminal_runtime_release_max_per_scan=2,
            ),
        )

        await worker._release_terminal_runtime_resources()  # noqa: SLF001
        assert len(cleaner.calls) == 2

        await worker._release_terminal_runtime_resources()  # noqa: SLF001
        assert len(cleaner.calls) == 4

        await worker._release_terminal_runtime_resources()  # noqa: SLF001
        assert len(cleaner.calls) == 5

        cleaned_workspace_ids = {call["workspace_id"] for call in cleaner.calls}
        assert cleaned_workspace_ids == set(workspace_ids)

        async with session_factory() as s:
            repo = WorkspaceEventRepository(s)
            released_counts = [
                len(
                    await repo.list(
                        workspace_id=ws_id,
                        event_type="workspace.terminal_runtime_released",
                    )
                )
                for ws_id in workspace_ids
            ]
        assert released_counts == [1, 1, 1, 1, 1]

    @pytest.mark.unit
    async def test_release_continues_batch_when_per_candidate_recording_raises(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_ids: list[str] = []
        for i in range(3):
            workspace_ids.append(
                await _create_terminal_execution(
                    session_factory,
                    origin_repo,
                    f"terminal-release-per-candidate-error-{i}",
                    WorkspaceStatus.failed,
                )
            )
        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                terminal_runtime_release_scan_interval_seconds=0.0,
            ),
        )

        failing_workspace_id = workspace_ids[1]
        original_record = worker._record_terminal_runtime_released  # noqa: SLF001

        async def _record_with_one_failure(
            candidate: _TerminalRuntimeCandidate,
            cleanup: WorkspaceCleanupResult,
        ) -> None:
            if candidate.workspace_id == failing_workspace_id:
                raise RuntimeError("simulated event recording failure")
            await original_record(candidate, cleanup)

        worker._record_terminal_runtime_released = _record_with_one_failure  # type: ignore[method-assign]  # noqa: SLF001

        with pytest.raises(RuntimeError, match="simulated event recording failure"):
            await worker._release_terminal_runtime_resources()  # noqa: SLF001

        cleaned_workspace_ids = [call["workspace_id"] for call in cleaner.calls]
        assert set(cleaned_workspace_ids) == set(workspace_ids)
        async with session_factory() as s:
            repo = WorkspaceEventRepository(s)
            released_counts = {
                ws_id: len(
                    await repo.list(
                        workspace_id=ws_id,
                        event_type="workspace.terminal_runtime_released",
                    )
                )
                for ws_id in workspace_ids
            }
        assert released_counts[failing_workspace_id] == 0
        for ws_id, count in released_counts.items():
            if ws_id == failing_workspace_id:
                continue
            assert count == 1
