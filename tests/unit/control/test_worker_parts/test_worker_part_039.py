"""ControlWorker tests.

We use the real Provisioner against real git + PostgreSQL to validate the full
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
from typing import Any

import pytest
from sqlalchemy.exc import InterfaceError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import (
    ControlWorker,
    WorkerConfig,
)
from awf.control.worker.types import (
    _ActiveExecutionCandidate,
)
from awf.db.enums import OperationStatus, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.node.cleanup import (
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
    scheduler_score_from_workspace,
)
from tests.postgres import postgres_test_engine

PRESERVED_EXECUTION_EVENT_TYPE = "workspace.active_execution_preserved_after_restart"
PRESERVED_EXECUTION_REASON_CODE = "ACTIVE_EXECUTION_PRESERVED_AFTER_RESTART"
PRESERVED_EXECUTION_SUBPHASE = "runtime_preserved_after_restart"
WORKER_TEST_TIMEOUT_SECONDS = 300.0


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _git_output(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


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
    agent: str = "codex",
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
    task_prompt: str = "p",
    agent: str = "codex",
    task_class: str | None = None,
    owned_paths: list[str] | None = None,
    auto_merge: bool = True,
    initial_review_grace_period_seconds: float | None = None,
    profile_ref: str | None = None,
    requested_profile: dict[str, object] | None = None,
    resolved_profile: dict[str, object] | None = None,
    task_kind: str = "feature_branch_pr",
    test_commands: list[str] | None = None,
    create_task_attempt: bool = False,
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
            task_prompt=task_prompt,
            agent=agent,
            task_class=task_class,
            owned_paths=owned_paths,
            test_commands=list(test_commands or []),
            task_policy=task_policy,
            auto_merge=auto_merge,
            initial_review_grace_period_seconds=initial_review_grace_period_seconds,
            profile_ref=profile_ref,
            requested_profile=requested_profile,
            resolved_profile=resolved_profile,
            task_kind=task_kind,
        )
        if create_task_attempt:
            task = await TaskRepository(s).create_or_get(
                repo_url=ws.repo_url,
                base_branch=ws.branch_base,
                title=ws.task_title,
                prompt=ws.task_prompt,
                external_id=ws.task_external_id,
                idempotency_key=None,
                task_class=ws.task_class,
                owned_paths=list(ws.owned_paths),
            )
            await TaskAttemptRepository(s).create_for_workspace(task=task, workspace=ws)
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

    async def provision_claimed(
        self, workspace_id: str, execution_claim_epoch: int | None = None
    ) -> None:
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

    def get_worktree_path(self, workspace_id: str) -> Path | None:
        del workspace_id
        return None


class _MissingWorktreePathProvisioner(_TransitioningProvisioner):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], worktrees_root: Path):
        super().__init__(session_factory)
        self._worktrees_root = worktrees_root

    def get_worktree_path(self, workspace_id: str) -> Path:
        return self._worktrees_root / workspace_id


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.resume_calls: list[str] = []

    async def execute(self, workspace_id: str, **_kwargs: object) -> None:
        self.calls.append(workspace_id)

    async def resume_pr_monitor(self, workspace_id: str) -> None:
        self.resume_calls.append(workspace_id)


class _BlockingExecutor(_RecordingExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, workspace_id: str, **_kwargs: object) -> None:
        self.calls.append(workspace_id)
        self.started.set()
        await self.release.wait()


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


class _TrackedSessionContext:
    def __init__(self, factory: _TrackingSessionFactory) -> None:
        self._factory = factory
        self._session = factory.base_factory()

    async def __aenter__(self) -> AsyncSession:
        session = await self._session.__aenter__()
        self._factory.active_sessions += 1
        return session

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> bool | None:
        try:
            return await self._session.__aexit__(exc_type, exc, traceback)
        finally:
            self._factory.active_sessions -= 1


class _TrackingSessionFactory:
    def __init__(self, base_factory: async_sessionmaker[AsyncSession]) -> None:
        self.base_factory = base_factory
        self.active_sessions = 0

    def __call__(self) -> _TrackedSessionContext:
        return _TrackedSessionContext(self)


class _RecordingBranchOpenPRResolver:
    def __init__(
        self,
        results_by_branch: dict[str, list[SimpleNamespace] | Exception],
    ) -> None:
        self._results_by_branch = results_by_branch
        self.calls: list[dict[str, str | None]] = []

    async def resolve(
        self,
        *,
        repo_url: str,
        branch_name: str,
        base_branch: str | None,
    ) -> list[SimpleNamespace]:
        self.calls.append(
            {
                "repo_url": repo_url,
                "branch_name": branch_name,
                "base_branch": base_branch,
            }
        )
        result = self._results_by_branch[branch_name]
        if isinstance(result, Exception):
            raise result
        return list(result)


class _RetargetedBranchOpenPRResolver:
    def __init__(
        self,
        results_by_branch: dict[str, list[SimpleNamespace]],
    ) -> None:
        self._results_by_branch = results_by_branch
        self.calls: list[dict[str, str | None]] = []

    async def resolve(
        self,
        *,
        repo_url: str,
        branch_name: str,
        base_branch: str | None,
    ) -> list[SimpleNamespace]:
        self.calls.append(
            {
                "repo_url": repo_url,
                "branch_name": branch_name,
                "base_branch": base_branch,
            }
        )
        if base_branch is not None:
            return []
        return list(self._results_by_branch[branch_name])


class _SequenceBranchOpenPRResolver:
    def __init__(
        self,
        results_by_branch: dict[str, list[list[SimpleNamespace] | Exception]],
    ) -> None:
        self._results_by_branch = results_by_branch
        self.calls: list[dict[str, str | None]] = []

    async def resolve(
        self,
        *,
        repo_url: str,
        branch_name: str,
        base_branch: str | None,
    ) -> list[SimpleNamespace]:
        self.calls.append(
            {
                "repo_url": repo_url,
                "branch_name": branch_name,
                "base_branch": base_branch,
            }
        )
        results = self._results_by_branch[branch_name]
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return list(result)


def _rewrite_branch_placeholder(value: Any, branch_name: str) -> Any:
    if isinstance(value, str):
        return branch_name if value == "BRANCH" else value
    if isinstance(value, list):
        return [_rewrite_branch_placeholder(item, branch_name) for item in value]
    if isinstance(value, dict):
        return {key: _rewrite_branch_placeholder(item, branch_name) for key, item in value.items()}
    if isinstance(value, SimpleNamespace):
        return SimpleNamespace(
            **{
                key: _rewrite_branch_placeholder(item, branch_name)
                for key, item in vars(value).items()
            }
        )
    return value


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


def _seed_workspace_worktree(
    *,
    worktrees_root: Path,
    origin: Path,
    workspace_id: str,
    branch_name: str,
    commit_change: bool,
    dirty_change: bool = False,
) -> tuple[Path, str, str]:
    worktree = worktrees_root / workspace_id
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(["clone", "-q", str(origin), str(worktree)], worktree.parent)
    _git(["config", "user.name", "T"], worktree)
    _git(["config", "user.email", "t@t"], worktree)
    _git(["checkout", "-q", "-b", branch_name], worktree)
    base_commit = _git_output(["rev-parse", "HEAD"], worktree)
    if commit_change:
        (worktree / "agent.txt").write_text(f"work from {workspace_id}\n")
        _git(["add", "agent.txt"], worktree)
        _git(["commit", "-q", "-m", "agent work"], worktree)
    if dirty_change:
        (worktree / "dirty.txt").write_text("uncommitted\n")
    head_sha = _git_output(["rev-parse", "HEAD"], worktree)
    return worktree, base_commit, head_sha


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
        "awf.control.worker.claims.OperationRepository",
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
    monkeypatch.setattr("awf.control.worker.cleanup.monotonic", lambda: current_time)
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
    monkeypatch.setattr("awf.control.worker.recovery_stale.monotonic", lambda: current_time)
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
        "awf.control.worker.cleanup.SecretLeaseService",
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
        "awf.control.worker.recovery_stale.WorkspaceRepository",
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
