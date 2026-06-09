"""ControlWorker tests.

We use the real Provisioner against real git + PostgreSQL to validate the full
pipeline, rather than mocking the provisioner. The worker's contract is
primarily about listing work off the DB in the right order and bounding
concurrency, so end-to-end is the most useful test.
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import InterfaceError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import (
    ControlWorker,
    WorkerConfig,
)
from awf.control.worker import claims as worker_claims
from awf.control.worker import helpers as worker_helpers
from awf.control.worker import recovery_stale as worker_recovery_stale
from awf.control.worker import resource_broker as worker_resource_broker
from awf.control.worker import scheduler_methods as worker_scheduler_methods
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    QueueDecisionRepository,
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


class TestRunOncePart002:
    @pytest.mark.unit
    async def test_requested_capacity_queue_signature_sqlite_reads_queue_once(
        self,
    ) -> None:
        first_created_at = datetime(2026, 1, 1, tzinfo=UTC)
        first_updated_at = datetime(2026, 1, 4, tzinfo=UTC)
        second_created_at = datetime(2026, 1, 2, tzinfo=UTC)
        second_updated_at = datetime(2026, 1, 3, tzinfo=UTC)
        rows = [
            (
                "ws_alpha",
                first_updated_at,
                first_created_at,
                "docs_task",
                "codex",
                {"scheduler": {"base_priority": 10}},
                {"docker": {"mode": "host"}},
            ),
            (
                "ws_zulu",
                second_updated_at,
                second_created_at,
                "test_task",
                "gemini",
                {"scheduler": {"base_priority": 20}},
                {"docker": {"mode": "dind"}},
            ),
        ]

        class SingleReadResult:
            def __iter__(self) -> object:
                return iter(rows)

            def one(self) -> tuple[int, datetime, datetime, str]:
                return (
                    len(rows),
                    max(row[1] for row in rows),
                    max(row[2] for row in rows),
                    max(row[0] for row in rows),
                )

        class SingleReadSession:
            def __init__(self) -> None:
                self.execute_calls = 0

            def get_bind(self) -> SimpleNamespace:
                return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

            async def execute(self, _stmt: object) -> SingleReadResult:
                self.execute_calls += 1
                if self.execute_calls > 1:
                    raise AssertionError("queue signature fallback must read one snapshot")
                return SingleReadResult()

        session = SingleReadSession()

        signature = await worker_claims._requested_capacity_queue_signature(  # noqa: SLF001
            session,  # type: ignore[arg-type]
            node_id="local",
        )

        digest = hashlib.sha256()
        for (
            workspace_id,
            _updated_at,
            created_at,
            task_class,
            agent,
            task_policy,
            resolved_profile,
        ) in rows:
            digest.update(
                worker_helpers._requested_capacity_queue_digest_payload(  # noqa: SLF001
                    workspace_id=workspace_id,
                    created_at=created_at,
                    task_class=task_class,
                    agent=agent,
                    task_policy=task_policy,
                    resolved_profile=resolved_profile,
                ).encode("utf-8")
            )
            digest.update(b"\0")
        assert session.execute_calls == 1
        assert signature == (
            2,
            first_updated_at,
            second_created_at,
            "ws_zulu",
            digest.hexdigest(),
        )

    @pytest.mark.unit
    async def test_requested_capacity_queue_signature_sqlite_bounds_snapshot_scan(
        self,
    ) -> None:
        class EmptyReadResult:
            def __iter__(self) -> object:
                return iter(())

        class BoundedReadSession:
            def __init__(self) -> None:
                self.compiled_statement = ""

            def get_bind(self) -> SimpleNamespace:
                return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

            async def execute(self, stmt: object) -> EmptyReadResult:
                self.compiled_statement = str(
                    stmt.compile(compile_kwargs={"literal_binds": True})  # type: ignore[attr-defined]
                )
                return EmptyReadResult()

        session = BoundedReadSession()

        signature = await worker_claims._requested_capacity_queue_signature(  # noqa: SLF001
            session,  # type: ignore[arg-type]
            node_id="local",
        )

        assert signature == (0, None, None, None, hashlib.sha256().hexdigest())
        assert "LIMIT 500" in session.compiled_statement

    @pytest.mark.unit
    async def test_requested_capacity_queue_signature_postgres_null_digest_uses_empty_string(
        self,
    ) -> None:
        class AggregateResult:
            def one(self) -> tuple[int, None, None, None, None]:
                return (0, None, None, None, None)

        class AggregateSession:
            def get_bind(self) -> SimpleNamespace:
                return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

            async def execute(self, _stmt: object) -> AggregateResult:
                return AggregateResult()

        signature = await worker_claims._requested_capacity_queue_signature(  # noqa: SLF001
            AggregateSession(),  # type: ignore[arg-type]
            node_id="local",
        )

        assert signature == (0, None, None, None, "")

    @pytest.mark.unit
    async def test_requested_capacity_queue_signature_postgres_bounds_aggregate_scan(
        self,
    ) -> None:
        class AggregateResult:
            def one(self) -> tuple[int, None, None, None, str]:
                return (0, None, None, None, "")

        class AggregateSession:
            def __init__(self) -> None:
                self.compiled_statement = ""

            def get_bind(self) -> SimpleNamespace:
                return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

            async def execute(self, stmt: object) -> AggregateResult:
                self.compiled_statement = str(
                    stmt.compile(  # type: ignore[attr-defined]
                        dialect=postgresql.dialect(),
                        compile_kwargs={"literal_binds": True},
                    )
                )
                return AggregateResult()

        session = AggregateSession()

        signature = await worker_claims._requested_capacity_queue_signature(  # noqa: SLF001
            session,  # type: ignore[arg-type]
            node_id="local",
        )

        assert signature == (0, None, None, None, "")
        assert "FROM (SELECT" in session.compiled_statement
        assert "LIMIT 500" in session.compiled_statement

    @pytest.mark.unit
    async def test_claim_requested_ids_short_circuits_without_database(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        disabled_worker = ControlWorker(
            session_factory=object(),  # type: ignore[arg-type]
            provisioner=SimpleNamespace(),
            config=WorkerConfig(max_concurrent_provisions=0),
        )

        assert await disabled_worker._claim_requested_ids(["ws-disabled"]) == []  # noqa: SLF001

        worker = ControlWorker(
            session_factory=object(),  # type: ignore[arg-type]
            provisioner=SimpleNamespace(),
            config=WorkerConfig(max_concurrent_provisions=1),
        )
        claim_calls: list[str] = []

        async def claim_requested(workspace_id: str) -> bool:
            claim_calls.append(workspace_id)
            return True

        monkeypatch.setattr(worker, "_claim_requested_for_provisioning", claim_requested)

        assert await worker._claim_requested_ids([]) == []  # noqa: SLF001
        assert await worker._claim_requested_ids(None) == []  # noqa: SLF001
        assert await worker._claim_requested_ids(["ws-first", "ws-second"]) == [  # noqa: SLF001
            "ws-first"
        ]
        assert claim_calls == ["ws-first"]

    @pytest.mark.unit
    async def test_capacity_claim_empty_queue_returns_empty_result(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class EmptyWorkspaceRepository:
            dialect_name = "postgresql"

            async def list_schedulable_workspaces(self, **_kwargs: object) -> list[Workspace]:
                return []

        async def allocated_totals(
            _session: object,
            *,
            reservation_repo: object,
            config: WorkerConfig,
        ) -> worker_claims._AllocatedReservationTotals:  # noqa: SLF001
            return worker_claims._AllocatedReservationTotals()  # noqa: SLF001

        async def queue_signature(
            _session: object,
            *,
            node_id: str,
            scoring_at: datetime | None,
        ) -> worker_claims._RequestedCapacityQueueSignature:  # noqa: SLF001
            assert scoring_at is not None
            return (0, None, None, None, "")

        monkeypatch.setattr(
            worker_claims, "WorkspaceRepository", lambda _session: EmptyWorkspaceRepository()
        )
        monkeypatch.setattr(
            worker_claims, "ResourceReservationRepository", lambda _session: object()
        )
        monkeypatch.setattr(worker_claims, "_allocated_totals_for_capacity_gate", allocated_totals)
        monkeypatch.setattr(worker_claims, "_requested_capacity_queue_signature", queue_signature)

        worker = ControlWorker(
            session_factory=object(),  # type: ignore[arg-type]
            provisioner=SimpleNamespace(),
            config=WorkerConfig(max_concurrent_provisions=2, local_capacity_cpu_cores=4.0),
        )

        result = await worker._claim_requested_ids_with_capacity(  # noqa: SLF001
            object(),  # type: ignore[arg-type]
            resume_after=None,
            resume_allocated_signature=None,
            resume_requested_queue_signature=None,
            resume_provider_suppression_expires_at=None,
        )

        assert result == worker_claims._RequestedCapacityClaimResult(workspace_ids=[])  # noqa: SLF001

    @pytest.mark.unit
    async def test_capacity_private_short_circuit_helpers(self) -> None:
        retry_state_key = worker_recovery_stale.PROVIDER_RECOVERY_STATE_KEY
        unsupported_action = worker_helpers._ActiveExecutionCandidate(  # noqa: SLF001
            workspace_id="ws-unsupported-action",
            status=WorkspaceStatus.monitoring_pr,
            compose_project_name=None,
            task_policy={retry_state_key: {"action": "pause"}},
        )
        agentless_retry = worker_helpers._ActiveExecutionCandidate(  # noqa: SLF001
            workspace_id="ws-agentless-retry",
            status=WorkspaceStatus.monitoring_pr,
            compose_project_name=None,
            task_policy={retry_state_key: {"action": "retry"}},
        )
        providerless_retry = worker_helpers._ActiveExecutionCandidate(  # noqa: SLF001
            workspace_id="ws-providerless-retry",
            status=WorkspaceStatus.monitoring_pr,
            compose_project_name=None,
            agent="unknown-agent",
            task_policy={retry_state_key: {"action": "retry", "model": "gpt-5"}},
        )

        assert not await worker_recovery_stale._monitor_provider_recovery_resume_pending(  # noqa: SLF001
            object(),  # type: ignore[arg-type]
            unsupported_action,
        )
        assert not await worker_recovery_stale._monitor_provider_recovery_resume_pending(  # noqa: SLF001
            object(),  # type: ignore[arg-type]
            agentless_retry,
        )
        assert not await worker_recovery_stale._monitor_provider_recovery_resume_pending(  # noqa: SLF001
            object(),  # type: ignore[arg-type]
            providerless_retry,
        )

        assert (
            await worker_scheduler_methods._existing_ordered_queue_decision_keys(  # noqa: SLF001
                object(),  # type: ignore[arg-type]
                [],
                reason_code="ORDERED",
                decided_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
            == set()
        )

    @pytest.mark.unit
    async def test_capacity_lock_skips_non_postgres_sessions(self) -> None:
        class SqliteSession:
            def get_bind(self) -> SimpleNamespace:
                return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

            async def execute(self, *_args: object, **_kwargs: object) -> None:
                raise AssertionError("non-postgres capacity lock should not execute SQL")

        await worker_claims._acquire_local_capacity_scheduler_lock(  # noqa: SLF001
            SqliteSession(),  # type: ignore[arg-type]
            node_id="local",
        )

    @pytest.mark.unit
    def test_capacity_decision_signature_helpers_reject_mismatches(self) -> None:
        blocker = worker_resource_broker.LocalCapacityBlocker(
            dimension="steady_cpu",
            reason_code="STEADY_CPU_CAPACITY_SATURATED",
            limit=4.0,
            allocated=4.0,
            requested=1.0,
            after=5.0,
            unsatisfiable=False,
        )
        stored = SimpleNamespace(
            attempt_id="attempt-a",
            decision=worker_claims.QUEUE_DECISION_DEFERRED,
            reason_code="CAPACITY",
            resource_summary={
                "blockers": [worker_resource_broker._capacity_blocker_payload(blocker)]
            },  # noqa: SLF001
        )
        allocation_changed = worker_resource_broker.LocalCapacityBlocker(
            dimension="steady_cpu",
            reason_code="STEADY_CPU_CAPACITY_SATURATED",
            limit=4.0,
            allocated=7.0,
            requested=1.0,
            after=8.0,
            unsatisfiable=False,
        )
        request_changed = worker_resource_broker.LocalCapacityBlocker(
            dimension="steady_cpu",
            reason_code="STEADY_CPU_CAPACITY_SATURATED",
            limit=4.0,
            allocated=4.0,
            requested=2.0,
            after=6.0,
            unsatisfiable=False,
        )
        mismatched = SimpleNamespace(
            attempt_id="attempt-a",
            decision=worker_claims.QUEUE_DECISION_ORDERED,
            reason_code="DIFFERENT",
            resource_summary={
                "blockers": [worker_resource_broker._capacity_blocker_payload(blocker)]
            },  # noqa: SLF001
        )
        malformed = SimpleNamespace(
            attempt_id="attempt-a",
            decision=worker_claims.QUEUE_DECISION_DEFERRED,
            reason_code="CAPACITY",
            resource_summary={"blockers": "not-a-list"},
        )

        assert not worker_resource_broker._capacity_deferred_decision_matches(  # noqa: SLF001
            mismatched,
            attempt_id="attempt-a",
            reason_code="CAPACITY",
            blockers=[blocker],
        )
        assert worker_resource_broker._capacity_deferred_decision_matches(  # noqa: SLF001
            stored,
            attempt_id="attempt-a",
            reason_code="CAPACITY",
            blockers=[allocation_changed],
        )
        assert not worker_resource_broker._capacity_deferred_decision_matches(  # noqa: SLF001
            stored,
            attempt_id="attempt-a",
            reason_code="CAPACITY",
            blockers=[request_changed],
        )
        assert not worker_resource_broker._capacity_deferred_decision_matches(  # noqa: SLF001
            malformed,
            attempt_id="attempt-a",
            reason_code="CAPACITY",
            blockers=[blocker],
        )
        assert (
            worker_resource_broker._capacity_blocker_signatures_from_summary(  # noqa: SLF001
                {"blockers": [object()]}
            )
            is None
        )

    @pytest.mark.unit
    def test_earliest_future_datetime_ignores_past_candidate(self) -> None:
        now = datetime(2026, 1, 1, 12, tzinfo=UTC)
        current = now + timedelta(hours=2)

        assert (
            worker_claims._earliest_future_datetime(  # noqa: SLF001
                current,
                now - timedelta(seconds=1),
                now=now,
            )
            == current
        )

    @pytest.mark.unit
    async def test_stale_active_execution_scan_reraises_non_transient_errors(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        worker = ControlWorker(
            session_factory=object(),  # type: ignore[arg-type]
            provisioner=SimpleNamespace(),
            executor=SimpleNamespace(),
            config=WorkerConfig(),
        )

        async def fail_scan() -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(worker, "_recover_stale_active_executions", fail_scan)

        with pytest.raises(RuntimeError, match="boom"):
            await worker._maybe_recover_stale_active_executions()  # noqa: SLF001

    @pytest.mark.unit
    async def test_requested_capacity_age_boost_short_circuits_empty_windows(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        class NoSqlSession:
            async def execute(self, *_args: object, **_kwargs: object) -> None:
                raise AssertionError("age boost check should not query without windows")

        now = datetime(2026, 1, 1, 12, tzinfo=UTC)
        assert not await worker_claims._requested_capacity_age_boost_changed(  # noqa: SLF001
            NoSqlSession(),  # type: ignore[arg-type]
            node_id="local",
            since=now,
            now=now,
        )

        monkeypatch.setattr(worker_helpers, "AGE_BOOST_MAX", 0)
        assert not await worker_claims._requested_capacity_age_boost_changed(  # noqa: SLF001
            NoSqlSession(),  # type: ignore[arg-type]
            node_id="local",
            since=now - timedelta(minutes=1),
            now=now,
        )

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
