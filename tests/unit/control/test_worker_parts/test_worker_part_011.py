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
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from sqlalchemy import update
from sqlalchemy.exc import InterfaceError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import (
    ControlWorker,
    WorkerConfig,
)
from awf.control.worker import dispatch_methods as worker_dispatch_methods
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    OperationRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
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


class TestRunOnceExecutionPart004:
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

            async def provision_claimed(
                self, workspace_id: str, execution_claim_epoch: int | None = None
            ) -> None:
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

    @pytest.mark.unit
    async def test_stale_monitor_moved_to_ready_is_cancelled_and_frees_slot(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        monitor_id = await _create_monitoring_pr(session_factory, origin_repo, "wedged-monitor")
        executor = _BlockingMonitorExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=1,
            ),
        )
        worker._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001

        monitor_task: asyncio.Task[None] | None = None
        try:
            # First cycle dispatches and blocks the monitor resume, occupying the slot.
            assert (
                await asyncio.wait_for(worker.run_once(), timeout=WORKER_TEST_TIMEOUT_SECONDS) == 1
            )
            await asyncio.wait_for(executor.started.wait(), timeout=WORKER_TEST_TIMEOUT_SECONDS)
            assert worker._available_execution_slots() == 0  # noqa: SLF001
            assert (
                worker._execution_task_kinds[monitor_id]  # noqa: SLF001
                is worker_dispatch_methods._ExecutionTaskKind.MONITOR_RESUME
            )
            monitor_task = worker._execution_tasks[monitor_id]  # noqa: SLF001

            # The workspace leaves monitoring_pr; a fresh ready workspace appears.
            async with session_factory() as s:
                await s.execute(
                    update(Workspace)
                    .where(Workspace.id == monitor_id)
                    .values(status=WorkspaceStatus.ready.value)
                )
                await s.commit()
            await _create_ready(session_factory, origin_repo, "ready-after-wedge")

            with structlog.testing.capture_logs() as captured:
                dispatched = await asyncio.wait_for(
                    worker.run_once(), timeout=WORKER_TEST_TIMEOUT_SECONDS
                )

            assert any(
                event.get("event") == "worker.stale_monitor_execution_task_cancelled"
                and event.get("log_level") == "warning"
                and event.get("workspace_id") == monitor_id
                for event in captured
            )
            # The stale monitor task was cancelled and is no longer tracked as a monitor
            # (its freed slot may be immediately reused by a ready execution this cycle).
            assert monitor_task.cancelling() > 0
            assert (
                worker._execution_task_kinds.get(monitor_id)  # noqa: SLF001
                is not worker_dispatch_methods._ExecutionTaskKind.MONITOR_RESUME
            )
            # A ready execution dispatched in the same cycle that freed the slot.
            assert dispatched >= 1
            assert (
                worker_dispatch_methods._ExecutionTaskKind.READY  # noqa: SLF001
                in worker._execution_task_kinds.values()  # noqa: SLF001
            )
        finally:
            executor.release.set()
            if monitor_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait_for(monitor_task, timeout=WORKER_TEST_TIMEOUT_SECONDS)
            await asyncio.wait_for(
                worker.wait_for_execution_tasks(), timeout=WORKER_TEST_TIMEOUT_SECONDS
            )

    @pytest.mark.unit
    async def test_stale_monitor_stays_tracked_as_draining_until_stopped(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        # cancel() is cooperative: a wedged monitor coroutine can keep running
        # after reconcile cancels it. The slot must free for OTHER workspaces,
        # but the cancelled task must stay tracked so a fresh dispatch for the
        # SAME workspace is blocked until it truly stops.
        monitor_id = await _create_monitoring_pr(session_factory, origin_repo, "wedged-monitor")
        executor = _BlockingMonitorExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=1,
            ),
        )
        worker._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001

        monitor_task: asyncio.Task[None] | None = None
        try:
            assert (
                await asyncio.wait_for(worker.run_once(), timeout=WORKER_TEST_TIMEOUT_SECONDS) == 1
            )
            await asyncio.wait_for(executor.started.wait(), timeout=WORKER_TEST_TIMEOUT_SECONDS)
            monitor_task = worker._execution_tasks[monitor_id]  # noqa: SLF001
            assert worker._available_execution_slots() == 0  # noqa: SLF001

            # The workspace leaves monitoring_pr, so the resume is now stale.
            async with session_factory() as s:
                await s.execute(
                    update(Workspace)
                    .where(Workspace.id == monitor_id)
                    .values(status=WorkspaceStatus.ready.value)
                )
                await s.commit()

            # Drive reconcile directly (not via run_once): there is no await
            # between cancel() and its return, so we observe the task mid-drain
            # before the cooperative cancellation can run to completion.
            with structlog.testing.capture_logs() as captured:
                await worker._reconcile_stale_monitor_execution_tasks()  # noqa: SLF001

            assert any(
                event.get("event") == "worker.stale_monitor_execution_task_cancelled"
                and event.get("workspace_id") == monitor_id
                for event in captured
            )
            # Cancellation was requested but the coroutine has not stopped yet.
            assert monitor_task.cancelling() > 0
            assert not monitor_task.done()
            # The task stays tracked under its workspace_id, reclassified as draining.
            assert worker._execution_tasks[monitor_id] is monitor_task  # noqa: SLF001
            assert (
                worker._execution_task_kinds[monitor_id]  # noqa: SLF001
                is worker_dispatch_methods._ExecutionTaskKind.MONITOR_DRAINING
            )
            # Same-workspace dispatch stays blocked while it drains ...
            assert worker._dispatchable_execution_ids([monitor_id], limit=1) == []  # noqa: SLF001
            # ... yet the slot is excluded from accounting, so it frees for others.
            assert worker._available_execution_slots() == 1  # noqa: SLF001

            # A second reconcile pass leaves the already-draining task alone:
            # no re-cancel and no duplicate warning.
            with structlog.testing.capture_logs() as second_pass:
                await worker._reconcile_stale_monitor_execution_tasks()  # noqa: SLF001
            assert not any(
                event.get("event") == "worker.stale_monitor_execution_task_cancelled"
                for event in second_pass
            )
            assert (
                worker._execution_task_kinds[monitor_id]  # noqa: SLF001
                is worker_dispatch_methods._ExecutionTaskKind.MONITOR_DRAINING
            )
        finally:
            executor.release.set()
            if monitor_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait_for(monitor_task, timeout=WORKER_TEST_TIMEOUT_SECONDS)
            await asyncio.wait_for(
                worker.wait_for_execution_tasks(), timeout=WORKER_TEST_TIMEOUT_SECONDS
            )

    @pytest.mark.unit
    async def test_stale_monitor_cancellation_finalizes_recovery_operation(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        # CancelledError is a BaseException, so a stale-monitor reconcile cancel
        # skips the resume coroutine's Exception handler. Without an explicit
        # CancelledError finalizer the remonitor operation would stay stuck in
        # running while the caller's finally drops the recovery handle, leaving
        # nothing able to finish it. The resume must mark it cancelled instead.
        monitor_id = await _create_monitoring_pr(session_factory, origin_repo, "cancelled-monitor")
        executor = _BlockingMonitorExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=1,
            ),
        )
        worker._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001

        monitor_task: asyncio.Task[None] | None = None
        try:
            assert (
                await asyncio.wait_for(worker.run_once(), timeout=WORKER_TEST_TIMEOUT_SECONDS) == 1
            )
            await asyncio.wait_for(executor.started.wait(), timeout=WORKER_TEST_TIMEOUT_SECONDS)
            monitor_task = worker._execution_tasks[monitor_id]  # noqa: SLF001

            # Claiming + dispatching the monitor records a running remonitor op.
            async with session_factory() as s:
                operations = await OperationRepository(s).list_all(workspace_id=monitor_id)
            remonitor_operations = [
                operation
                for operation in operations
                if operation.type == OperationType.remonitor.value
            ]
            assert len(remonitor_operations) == 1
            operation_id = remonitor_operations[0].id
            assert remonitor_operations[0].status == OperationStatus.running.value

            # The workspace leaves monitoring_pr, so the resume is now stale and
            # reconcile cancels it.
            async with session_factory() as s:
                await s.execute(
                    update(Workspace)
                    .where(Workspace.id == monitor_id)
                    .values(status=WorkspaceStatus.ready.value)
                )
                await s.commit()
            await worker._reconcile_stale_monitor_execution_tasks()  # noqa: SLF001
            assert monitor_task.cancelling() > 0

            # Drain the cancelled resume coroutine to completion.
            executor.release.set()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(monitor_task, timeout=WORKER_TEST_TIMEOUT_SECONDS)

            # The remonitor op is finalized as cancelled, not stuck in running.
            async with session_factory() as s:
                finalized = await OperationRepository(s).get(operation_id)
            assert finalized is not None
            assert finalized.status == OperationStatus.cancelled.value
            assert finalized.error_code == "MONITOR_RECOVERY_CANCELLED"
            # The recovery handle is dropped only after the op is finalized.
            assert monitor_id not in worker._monitor_recovery_operation_ids  # noqa: SLF001
        finally:
            executor.release.set()
            if monitor_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait_for(monitor_task, timeout=WORKER_TEST_TIMEOUT_SECONDS)
            await asyncio.wait_for(
                worker.wait_for_execution_tasks(), timeout=WORKER_TEST_TIMEOUT_SECONDS
            )

    @pytest.mark.unit
    async def test_stale_monitor_cancellation_finalizes_despite_second_cancel(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        # The CancelledError finalize is itself a cancellable DB write. If a
        # second cancellation (e.g. worker shutdown cancelling outstanding tasks)
        # lands mid-write, the remonitor op must still reach cancelled rather than
        # stay stuck in running once the caller's finally drops the recovery
        # handle. The finalize is shielded, so it runs to completion across the
        # second cancel.
        monitor_id = await _create_monitoring_pr(
            session_factory, origin_repo, "double-cancel-monitor"
        )
        executor = _BlockingMonitorExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=1,
            ),
        )
        worker._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001

        monitor_task: asyncio.Task[None] | None = None
        try:
            assert (
                await asyncio.wait_for(worker.run_once(), timeout=WORKER_TEST_TIMEOUT_SECONDS) == 1
            )
            await asyncio.wait_for(executor.started.wait(), timeout=WORKER_TEST_TIMEOUT_SECONDS)
            monitor_task = worker._execution_tasks[monitor_id]  # noqa: SLF001

            async with session_factory() as s:
                operations = await OperationRepository(s).list_all(workspace_id=monitor_id)
            remonitor_operations = [
                operation
                for operation in operations
                if operation.type == OperationType.remonitor.value
            ]
            assert len(remonitor_operations) == 1
            operation_id = remonitor_operations[0].id
            assert remonitor_operations[0].status == OperationStatus.running.value

            # Inject a second cancellation the instant the finalize DB write
            # begins, reproducing a shutdown cancel arriving mid-write. Without
            # the shield this would abort the write and orphan the op in running.
            original_finish = worker._finish_monitor_recovery_operation  # noqa: SLF001
            second_cancel_injected = False

            async def _finish_with_second_cancel(*args: Any, **kwargs: Any) -> None:
                nonlocal second_cancel_injected
                if not second_cancel_injected and monitor_task is not None:
                    second_cancel_injected = True
                    monitor_task.cancel()
                await original_finish(*args, **kwargs)

            worker._finish_monitor_recovery_operation = _finish_with_second_cancel  # type: ignore[method-assign]  # noqa: SLF001

            # Workspace leaves monitoring_pr, so the resume is stale and reconcile
            # cancels it (the first cancellation).
            async with session_factory() as s:
                await s.execute(
                    update(Workspace)
                    .where(Workspace.id == monitor_id)
                    .values(status=WorkspaceStatus.ready.value)
                )
                await s.commit()
            await worker._reconcile_stale_monitor_execution_tasks()  # noqa: SLF001
            assert monitor_task.cancelling() > 0

            executor.release.set()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(monitor_task, timeout=WORKER_TEST_TIMEOUT_SECONDS)

            # The second cancel fired during finalize, but the shielded write
            # still completed: the op is cancelled, not stuck in running.
            assert second_cancel_injected
            async with session_factory() as s:
                finalized = await OperationRepository(s).get(operation_id)
            assert finalized is not None
            assert finalized.status == OperationStatus.cancelled.value
            assert finalized.error_code == "MONITOR_RECOVERY_CANCELLED"
            assert monitor_id not in worker._monitor_recovery_operation_ids  # noqa: SLF001
        finally:
            executor.release.set()
            if monitor_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait_for(monitor_task, timeout=WORKER_TEST_TIMEOUT_SECONDS)
            await asyncio.wait_for(
                worker.wait_for_execution_tasks(), timeout=WORKER_TEST_TIMEOUT_SECONDS
            )
