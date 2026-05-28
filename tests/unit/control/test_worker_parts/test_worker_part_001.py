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

import awf.db.repositories as repositories_module
from awf.control.worker import (
    ControlWorker,
    WorkerConfig,
)
from awf.control.worker import claims as worker_claims
from awf.control.worker import helpers as worker_helpers
from awf.control.worker import resource_broker as worker_resource_broker
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
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


class TestRunOncePart001:
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
    async def test_stale_requested_candidates_are_filtered_before_provision_slot_truncation(
        self,
    ) -> None:
        worker = ControlWorker(
            session_factory=object(),  # type: ignore[arg-type]
            provisioner=SimpleNamespace(),
            config=WorkerConfig(max_concurrent_provisions=3),
        )
        listed_ids = ["ws-stale", "ws-fresh-a", "ws-fresh-b"]
        filter_calls: list[list[str]] = []
        claimed_ids: list[str] = []
        ordered_ids: list[str] = []
        provisioned_ids: list[str] = []

        async def _noop() -> None:
            return None

        async def _requested_provision_slots() -> int:
            return 2

        async def _list_requested() -> list[str]:
            return listed_ids

        async def _filter_current_requested_status(
            workspace_ids: list[str],
            *,
            expected: WorkspaceStatus,
            action: str,
        ) -> list[str]:
            filter_calls.append(list(workspace_ids))
            assert expected == WorkspaceStatus.requested
            assert action == "provision"
            return [workspace_id for workspace_id in workspace_ids if workspace_id != "ws-stale"]

        async def _claim_requested_ids(
            workspace_ids: list[str] | None = None,
            *,
            limit: int | None = None,
        ) -> list[str]:
            assert limit is None
            assert workspace_ids == ["ws-fresh-a", "ws-fresh-b"]
            claimed_ids.extend(workspace_ids)
            return list(workspace_ids)

        async def _record_ordered_decisions(
            workspace_ids: list[str],
            *,
            reason_code: str,
        ) -> None:
            assert reason_code == "ORDERED_REQUESTED_PROVISIONING"
            ordered_ids.extend(workspace_ids)

        async def _safely_provision_claimed(workspace_id: str) -> None:
            provisioned_ids.append(workspace_id)

        worker._reconcile_stale_monitor_execution_tasks = _noop  # type: ignore[method-assign]
        worker._maybe_expire_due_secret_leases = _noop  # type: ignore[method-assign]
        worker._maybe_release_terminal_runtime = _noop  # type: ignore[method-assign]
        worker._requested_provision_slots = _requested_provision_slots  # type: ignore[method-assign]
        worker._list_requested = _list_requested  # type: ignore[method-assign]
        worker._filter_current_status = _filter_current_requested_status  # type: ignore[method-assign]
        worker._claim_requested_ids = _claim_requested_ids  # type: ignore[method-assign]
        worker._record_ordered_decisions = _record_ordered_decisions  # type: ignore[method-assign]
        worker._safely_provision_claimed = _safely_provision_claimed  # type: ignore[method-assign]

        assert await worker.run_once() == 2

        assert filter_calls == [listed_ids]
        assert claimed_ids == ["ws-fresh-a", "ws-fresh-b"]
        assert ordered_ids == ["ws-fresh-a", "ws-fresh-b"]
        assert set(provisioned_ids) == {"ws-fresh-a", "ws-fresh-b"}

    @pytest.mark.unit
    def test_allocated_reservation_signature_normalizes_float_drift(self) -> None:
        aggregate_total = worker_claims._AllocatedReservationTotals(  # noqa: SLF001
            workspace_count=2,
            steady_cpu=0.3,
            steady_memory_gb=0.6,
            peak_cpu=0.9,
            peak_memory_gb=1.2,
            disk_mb=2048,
            dind_slots=1,
        )
        accumulated_total = worker_claims._AllocatedReservationTotals()  # noqa: SLF001
        accumulated_total.add(  # noqa: SLF001
            worker_resource_broker._ReservationDemand(  # noqa: SLF001
                workspace_id="float-drift-a",
                steady_cpu=0.1,
                steady_memory_gb=0.2,
                peak_cpu=0.3,
                peak_memory_gb=0.4,
                disk_mb=1024,
                dind_slots=0,
            )
        )
        accumulated_total.add(  # noqa: SLF001
            worker_resource_broker._ReservationDemand(  # noqa: SLF001
                workspace_id="float-drift-b",
                steady_cpu=0.2,
                steady_memory_gb=0.4,
                peak_cpu=0.6,
                peak_memory_gb=0.8,
                disk_mb=1024,
                dind_slots=1,
            )
        )

        assert accumulated_total.steady_cpu != aggregate_total.steady_cpu
        assert worker_claims._allocated_reservation_signature(  # noqa: SLF001
            accumulated_total
        ) == worker_claims._allocated_reservation_signature(aggregate_total)  # noqa: SLF001

    @pytest.mark.unit
    async def test_requested_capacity_queue_signature_changes_when_created_at_advances(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        generated_ids = iter(
            [
                "ws_ffffffffffffffffffffffff",
                "ws_cccccccccccccccccccccccc",
                "ws_dddddddddddddddddddddddd",
                "ws_000000000000000000000000",
                "ws_111111111111111111111111",
            ]
        )
        monkeypatch.setattr(repositories_module, "new_workspace_id", lambda: next(generated_ids))
        initial_created_at = datetime(2026, 1, 1, tzinfo=UTC)
        anchor_updated_at = datetime(2026, 1, 5, tzinfo=UTC)
        replacement_created_at = datetime(2026, 1, 2, tzinfo=UTC)
        replacement_updated_at = datetime(2026, 1, 4, tzinfo=UTC)

        anchor_id = await _create_requested(
            session_factory,
            origin_repo,
            "signature-anchor",
            created_at=initial_created_at,
        )
        leaving_ids = [
            await _create_requested(
                session_factory,
                origin_repo,
                "signature-leaving-a",
                created_at=initial_created_at,
            ),
            await _create_requested(
                session_factory,
                origin_repo,
                "signature-leaving-b",
                created_at=initial_created_at,
            ),
        ]
        async with session_factory() as session:
            await session.execute(
                update(Workspace)
                .where(Workspace.id == anchor_id)
                .values(updated_at=anchor_updated_at)
            )
            await session.commit()

        async with session_factory() as session:
            before = await worker_claims._requested_capacity_queue_signature(  # noqa: SLF001
                session,
                node_id="local",
            )

        replacement_ids = [
            await _create_requested(
                session_factory,
                origin_repo,
                "signature-replacement-a",
                created_at=replacement_created_at,
            ),
            await _create_requested(
                session_factory,
                origin_repo,
                "signature-replacement-b",
                created_at=replacement_created_at,
            ),
        ]
        async with session_factory() as session:
            await session.execute(
                update(Workspace)
                .where(Workspace.id.in_(leaving_ids))
                .values(status=WorkspaceStatus.ready.value)
            )
            await session.execute(
                update(Workspace)
                .where(Workspace.id.in_(replacement_ids))
                .values(updated_at=replacement_updated_at)
            )
            await session.commit()

        async with session_factory() as session:
            after = await worker_claims._requested_capacity_queue_signature(  # noqa: SLF001
                session,
                node_id="local",
            )

        assert before[0] == after[0] == 3
        assert before[1] == after[1] == anchor_updated_at
        assert before[2] == initial_created_at
        assert after[2] == replacement_created_at
        assert before[3] == after[3] == anchor_id
        assert before != after

    @pytest.mark.unit
    async def test_requested_capacity_queue_signature_changes_when_composition_changes(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        generated_ids = iter(
            [
                "ws_ffffffffffffffffffffffff",
                "ws_cccccccccccccccccccccccc",
                "ws_dddddddddddddddddddddddd",
                "ws_aaaaaaaaaaaaaaaaaaaaaaaa",
                "ws_bbbbbbbbbbbbbbbbbbbbbbbb",
            ]
        )
        monkeypatch.setattr(repositories_module, "new_workspace_id", lambda: next(generated_ids))
        anchor_at = datetime(2026, 1, 5, tzinfo=UTC)
        queued_at = datetime(2026, 1, 2, tzinfo=UTC)

        anchor_id = await _create_requested(
            session_factory,
            origin_repo,
            "signature-composition-anchor",
            created_at=anchor_at,
        )
        leaving_ids = [
            await _create_requested(
                session_factory,
                origin_repo,
                "signature-composition-leaving-a",
                created_at=queued_at,
            ),
            await _create_requested(
                session_factory,
                origin_repo,
                "signature-composition-leaving-b",
                created_at=queued_at,
            ),
        ]

        async with session_factory() as session:
            before = await worker_claims._requested_capacity_queue_signature(  # noqa: SLF001
                session,
                node_id="local",
            )

        replacement_ids = [
            await _create_requested(
                session_factory,
                origin_repo,
                "signature-composition-replacement-a",
                created_at=queued_at,
            ),
            await _create_requested(
                session_factory,
                origin_repo,
                "signature-composition-replacement-b",
                created_at=queued_at,
            ),
        ]
        async with session_factory() as session:
            await session.execute(
                update(Workspace)
                .where(Workspace.id.in_(leaving_ids))
                .values(status=WorkspaceStatus.ready.value)
            )
            await session.execute(
                update(Workspace)
                .where(Workspace.id.in_(replacement_ids))
                .values(updated_at=queued_at)
            )
            await session.commit()

        async with session_factory() as session:
            after = await worker_claims._requested_capacity_queue_signature(  # noqa: SLF001
                session,
                node_id="local",
            )

        assert before[0] == after[0] == 3
        assert before[1] == after[1] == anchor_at
        assert before[2] == after[2] == anchor_at
        assert before[3] == after[3] == anchor_id
        assert before != after

    @pytest.mark.unit
    async def test_requested_capacity_queue_signature_changes_when_scheduler_policy_changes(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        queued_at = datetime(2026, 1, 1, tzinfo=UTC)
        mutable_updated_at = datetime(2026, 1, 2, tzinfo=UTC)
        anchor_updated_at = datetime(2026, 1, 5, tzinfo=UTC)

        anchor_id = await _create_requested(
            session_factory,
            origin_repo,
            "signature-policy-anchor",
            created_at=queued_at,
            task_class="docs_task",
            task_policy={"scheduler": {"base_priority": 0}},
        )
        mutable_id = await _create_requested(
            session_factory,
            origin_repo,
            "signature-policy-mutable",
            created_at=queued_at,
            task_class="docs_task",
            task_policy={"scheduler": {"base_priority": 5}},
        )
        async with session_factory() as session:
            await session.execute(
                update(Workspace)
                .where(Workspace.id == anchor_id)
                .values(updated_at=anchor_updated_at)
            )
            await session.execute(
                update(Workspace)
                .where(Workspace.id == mutable_id)
                .values(updated_at=mutable_updated_at)
            )
            await session.commit()

        async with session_factory() as session:
            before = await worker_claims._requested_capacity_queue_signature(  # noqa: SLF001
                session,
                node_id="local",
            )

        async with session_factory() as session:
            await session.execute(
                update(Workspace)
                .where(Workspace.id == mutable_id)
                .values(
                    task_policy={"scheduler": {"base_priority": 100, "human_boost": 5}},
                    updated_at=mutable_updated_at,
                )
            )
            await session.commit()

        async with session_factory() as session:
            after = await worker_claims._requested_capacity_queue_signature(  # noqa: SLF001
                session,
                node_id="local",
            )

        assert before[0] == after[0] == 2
        assert before[1] == after[1] == anchor_updated_at
        assert before[2] == after[2] == queued_at
        assert before[3] == after[3]
        assert before != after

    @pytest.mark.unit
    async def test_requested_capacity_queue_signature_changes_when_resolved_profile_changes(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        queued_at = datetime(2026, 1, 1, tzinfo=UTC)
        mutable_updated_at = datetime(2026, 1, 2, tzinfo=UTC)
        anchor_updated_at = datetime(2026, 1, 5, tzinfo=UTC)

        anchor_id = await _create_requested(
            session_factory,
            origin_repo,
            "signature-profile-anchor",
            created_at=queued_at,
        )
        mutable_id = await _create_requested(
            session_factory,
            origin_repo,
            "signature-profile-mutable",
            created_at=queued_at,
        )
        async with session_factory() as session:
            await session.execute(
                update(Workspace)
                .where(Workspace.id == anchor_id)
                .values(updated_at=anchor_updated_at)
            )
            await session.execute(
                update(Workspace)
                .where(Workspace.id == mutable_id)
                .values(
                    resolved_profile={"docker": {"mode": "host"}},
                    updated_at=mutable_updated_at,
                )
            )
            await session.commit()

        async with session_factory() as session:
            before = await worker_claims._requested_capacity_queue_signature(  # noqa: SLF001
                session,
                node_id="local",
            )

        async with session_factory() as session:
            await session.execute(
                update(Workspace)
                .where(Workspace.id == mutable_id)
                .values(
                    resolved_profile={"docker": {"mode": "dind"}},
                    updated_at=mutable_updated_at,
                )
            )
            await session.commit()

        async with session_factory() as session:
            after = await worker_claims._requested_capacity_queue_signature(  # noqa: SLF001
                session,
                node_id="local",
            )

        assert before[:4] == after[:4]
        assert before[0] == after[0] == 2
        assert before[1] == after[1] == anchor_updated_at
        assert before[2] == after[2] == queued_at
        assert before != after

    @pytest.mark.unit
    async def test_requested_capacity_queue_signature_changes_when_scheduler_frontier_changes_beyond_id_sample(
        self,
        monkeypatch: pytest.MonkeyPatch,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        monkeypatch.setattr(worker_helpers, "_REQUESTED_CAPACITY_QUEUE_SIGNATURE_LIMIT", 3)
        generated_ids = iter(
            [
                "ws_000000000000000000000000",
                "ws_000000000000000000000001",
                "ws_000000000000000000000002",
                "ws_ffffffffffffffffffffffff",
            ]
        )
        monkeypatch.setattr(repositories_module, "new_workspace_id", lambda: next(generated_ids))
        queued_at = datetime(2026, 1, 1, tzinfo=UTC)

        for index in range(3):
            await _create_requested(
                session_factory,
                origin_repo,
                f"signature-id-sample-front-{index}",
                created_at=queued_at + timedelta(seconds=index),
                task_policy={"scheduler": {"base_priority": 0}},
            )
        tail_id = await _create_requested(
            session_factory,
            origin_repo,
            "signature-priority-tail-outside-id-sample",
            created_at=queued_at + timedelta(seconds=3),
            task_policy={"scheduler": {"base_priority": 0}},
        )

        async with session_factory() as session:
            before = await worker_claims._requested_capacity_queue_signature(  # noqa: SLF001
                session,
                node_id="local",
            )

        async with session_factory() as session:
            await session.execute(
                update(Workspace)
                .where(Workspace.id == tail_id)
                .values(task_policy={"scheduler": {"base_priority": 100, "human_boost": 5}})
            )
            await session.commit()

        async with session_factory() as session:
            after = await worker_claims._requested_capacity_queue_signature(  # noqa: SLF001
                session,
                node_id="local",
            )

        assert before[0] == after[0] == 3
        assert before != after
