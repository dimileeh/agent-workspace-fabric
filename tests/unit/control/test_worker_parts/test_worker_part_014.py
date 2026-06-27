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
from sqlalchemy import update
from sqlalchemy.exc import InterfaceError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import (
    ControlWorker,
    WorkerConfig,
)
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    OperationRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
    WorkerHeartbeatRepository,
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


class TestRunOnceMonitorRecoveryPart002:
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
    async def test_restart_recovery_clears_dead_owner_execution_claim_and_records_monitor_claim_acquisition(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        unexpired_execution_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "monitor-with-dead-owner-execution-claim",
            pr_number=457,
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            ws.execution_claimed_by = "dead-execution-worker"
            ws.execution_claim_expires_at = unexpired_execution_expires_at
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
            "execution_claim_expires_at": unexpired_execution_expires_at.isoformat(),
        }
        assert operation.payload["claim_cleanup"]["execution_claim"] == {
            "action": "cleared_stale",
            "reason_code": "STALE_EXECUTION_CLAIM_CLEARED_DURING_MONITOR_RECOVERY",
            "previous_claimed_by": "dead-execution-worker",
            "previous_expires_at": unexpired_execution_expires_at.isoformat(),
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
    async def test_restart_recovery_deferred_recompute_uses_fresh_owner_context(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        unexpired_execution_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "monitor-with-dead-owner-deferred-recompute",
            pr_number=458,
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            ws.execution_claimed_by = "dead-execution-worker"
            ws.execution_claim_expires_at = unexpired_execution_expires_at
            await s.commit()

        async def claim_monitoring_pr_race_lost(
            _self: WorkspaceRepository,
            _workspace_id: str,
            *,
            owner_id: str,  # noqa: ARG001
            lease_expires_at: datetime,  # noqa: ARG001
            now: datetime | None = None,  # noqa: ARG001
            clear_stale_execution_claim_cutoff: datetime | None = None,  # noqa: ARG001
        ) -> bool:
            return False

        monkeypatch.setattr(
            WorkspaceRepository,
            "claim_monitoring_pr",
            claim_monitoring_pr_race_lost,
        )

        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=1,
                node_id="worker-node-a",
            ),
        )

        assert await worker._claim_monitoring_pr(monitor_id) is False  # noqa: SLF001

        async with session_factory() as s:
            deferred_events = await WorkspaceEventRepository(s).list(
                workspace_id=monitor_id,
                event_type="workspace.monitor_recovery_deferred",
            )

        assert deferred_events == []

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
            refresh_observed_at = now or datetime.now(UTC)
            await WorkerHeartbeatRepository(self._session).record_heartbeat(
                worker_id=refreshed_execution_owner,
                node_id="worker-node-a",
                started_at=refresh_observed_at - timedelta(minutes=1),
                last_heartbeat_at=refresh_observed_at,
                poll_interval_seconds=300.0,
            )
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

        assert await worker.run_once() == 0
        await worker.wait_for_execution_tasks()

        assert executor.resume_calls == []
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            assert ws.execution_claimed_by == refreshed_execution_owner
            assert ws.execution_claim_expires_at is not None
            assert ws.execution_claim_expires_at.replace(tzinfo=UTC) == (
                refreshed_execution_expires_at
            )
            operations = await OperationRepository(s).list_all(workspace_id=monitor_id)
            recovery_events = await WorkspaceEventRepository(s).list(
                workspace_id=monitor_id,
                event_type="workspace.monitor_recovery_started",
            )
            deferred_events = await WorkspaceEventRepository(s).list(
                workspace_id=monitor_id,
                event_type="workspace.monitor_recovery_deferred",
            )

        remonitor_operations = [
            operation for operation in operations if operation.type == OperationType.remonitor.value
        ]
        assert remonitor_operations == []
        assert recovery_events == []
        assert len(deferred_events) == 1
        assert deferred_events[0].reason_code == "MONITOR_RECOVERY_DEFERRED_ACTIVE_EXECUTION_CLAIM"
        assert deferred_events[0].payload is not None
        assert deferred_events[0].payload["execution_claim"] == {
            "action": "preserved_unexpired",
            "reason_code": "UNEXPIRED_EXECUTION_CLAIM_PRESERVED_DURING_MONITOR_RECOVERY",
            "previous_claimed_by": refreshed_execution_owner,
            "previous_expires_at": refreshed_execution_expires_at.isoformat(),
        }

    @pytest.mark.unit
    async def test_restart_recovery_defers_when_unexpired_execution_claim_has_different_owner(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        execution_expires_at = datetime.now(UTC) + timedelta(minutes=10)
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "monitor-with-unexpired-execution-claim",
            task_policy={"scheduler": {"base_priority": 100}},
            pr_number=459,
        )
        eligible_monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "eligible-monitor-after-deferred-execution-claim",
            task_policy={"scheduler": {"base_priority": 1}},
            pr_number=460,
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            ws.execution_claimed_by = "live-execution-worker"
            ws.execution_claim_expires_at = execution_expires_at
            await WorkerHeartbeatRepository(s).record_heartbeat(
                worker_id="live-execution-worker",
                node_id="worker-node-a",
                started_at=execution_expires_at - timedelta(minutes=1),
                last_heartbeat_at=datetime.now(UTC),
                poll_interval_seconds=0.01,
            )
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
        assert executor.resume_calls == [eligible_monitor_id]
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
            deferred_events = await WorkspaceEventRepository(s).list(
                workspace_id=monitor_id,
                event_type="workspace.monitor_recovery_deferred",
            )

        remonitor_operations = [
            operation for operation in operations if operation.type == OperationType.remonitor.value
        ]
        assert remonitor_operations == []
        assert recovery_events == []
        assert len(deferred_events) == 1
        assert deferred_events[0].reason_code == "MONITOR_RECOVERY_DEFERRED_ACTIVE_EXECUTION_CLAIM"
        assert deferred_events[0].payload is not None
        assert deferred_events[0].payload["execution_claim"] == {
            "action": "preserved_unexpired",
            "reason_code": "UNEXPIRED_EXECUTION_CLAIM_PRESERVED_DURING_MONITOR_RECOVERY",
            "previous_claimed_by": "live-execution-worker",
            "previous_expires_at": execution_expires_at.isoformat(),
        }

        refreshed_execution_expires_at = execution_expires_at + timedelta(minutes=5)
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            ws.execution_claim_expires_at = refreshed_execution_expires_at
            await s.commit()

        await worker.run_once()
        await worker.wait_for_execution_tasks()
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            assert ws.execution_claim_expires_at is not None
            assert ws.execution_claim_expires_at.replace(tzinfo=UTC) == (
                refreshed_execution_expires_at
            )
            deferred_events = await WorkspaceEventRepository(s).list(
                workspace_id=monitor_id,
                event_type="workspace.monitor_recovery_deferred",
            )
        assert len(deferred_events) == 1

    @pytest.mark.unit
    async def test_restart_recovery_same_owner_unexpired_execution_claim_can_resume(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        execution_expires_at = datetime.now(UTC) + timedelta(minutes=10)
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "monitor-with-same-owner-execution-claim",
            pr_number=461,
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
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            ws.execution_claimed_by = worker._worker_id
            ws.execution_claim_expires_at = execution_expires_at
            await s.commit()

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        assert executor.resume_calls == [monitor_id]
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            assert ws.execution_claimed_by == worker._worker_id
            assert ws.execution_claim_expires_at is not None
            assert ws.execution_claim_expires_at.replace(tzinfo=UTC) == execution_expires_at
            recovery_events = await WorkspaceEventRepository(s).list(
                workspace_id=monitor_id,
                event_type="workspace.monitor_recovery_started",
            )
            deferred_events = await WorkspaceEventRepository(s).list(
                workspace_id=monitor_id,
                event_type="workspace.monitor_recovery_deferred",
            )

        assert len(recovery_events) == 1
        assert deferred_events == []
