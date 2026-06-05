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
from awf.db.enums import FailureReason, WorkspaceStatus
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
    CLEANUP_PARTIAL,
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


class TestRunOnceStaleActiveExecutionRecoveryPart018:
    @pytest.mark.unit
    async def test_stale_active_failure_still_applies_after_salvage_not_possible_for_orphan(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        compose_project = "awf_preserved_orphan_no_salvage"
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "preserved-orphan-no-salvage",
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

        cleaner = _RecordingRuntimeCleaner()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=_RecordingRuntimeInspector({compose_project: _live_agent_snapshot()}),
            runtime_cleaner=cleaner,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=1,
                stale_active_execution_scan_interval_seconds=0.0,
                active_execution_preservation_grace_seconds=60.0,
            ),
        )

        await worker.run_once()
        await worker.run_once()

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            not_possible_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.active_execution_salvage_not_possible",
            )
            stale_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.stale_active_execution_detected",
            )

        assert ws.status == WorkspaceStatus.failed.value
        assert len(not_possible_events) == 1
        assert not_possible_events[0].payload is not None
        assert not_possible_events[0].payload["reason_code"] == (
            "ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE"
        )
        assert not_possible_events[0].payload["decision"] == "allow_stale_active_failure"
        assert len(stale_events) == 1
        assert cleaner.calls

    @pytest.mark.unit
    async def test_salvage_not_possible_recording_serializes_concurrent_events(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        compose_project = "awf_salvage_not_possible_race"
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "salvage-not-possible-race",
            WorkspaceStatus.running,
            compose_project_name=compose_project,
        )
        preserved_at = datetime.now(UTC) - timedelta(minutes=5)
        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.get(workspace_id)
            assert ws is not None
            preserved_event = await repo.add_event(
                ws,
                event_type=PRESERVED_EXECUTION_EVENT_TYPE,
                reason_code=PRESERVED_EXECUTION_REASON_CODE,
                payload={
                    "workspace_status": WorkspaceStatus.running.value,
                    "decision": "preserve_runtime",
                },
            )
            preserved_event.occurred_at = preserved_at
            await s.commit()

        candidate = _ActiveExecutionCandidate(
            workspace_id=workspace_id,
            status=WorkspaceStatus.running,
            compose_project_name=compose_project,
        )
        both_started = asyncio.Event()
        first_checked = asyncio.Event()
        allow_first_recording = asyncio.Event()
        first_recorded = asyncio.Event()
        second_attempted_acquire = asyncio.Event()
        allow_second_check = asyncio.Event()
        second_selected_after_check = asyncio.Event()
        count_lock = asyncio.Lock()
        started_count = 0
        lock_attempt_count = 0
        check_count = 0
        selected_count = 0
        first_recording_task: asyncio.Task[Any] | None = None
        original_has_event = ControlWorker._has_current_salvage_event
        original_get_for_update = WorkspaceRepository.get_for_update

        async def _recording_get_for_update(
            self: WorkspaceRepository,
            requested_workspace_id: str,
        ) -> Workspace | None:
            nonlocal lock_attempt_count
            async with count_lock:
                lock_attempt_count += 1
                call_number = lock_attempt_count
                if call_number == 2:
                    second_attempted_acquire.set()

            return await original_get_for_update(self, requested_workspace_id)

        async def _racing_has_current_salvage_event(
            self: ControlWorker,
            session: AsyncSession,
            workspace_id: str,
            *,
            event_type: str,
            reason_code: str,
            event_floor: datetime,
            workspace_status: WorkspaceStatus,
        ) -> bool:
            nonlocal check_count, first_recording_task, selected_count
            async with count_lock:
                check_count += 1
                call_number = check_count

            if call_number == 2:
                await asyncio.wait_for(
                    allow_second_check.wait(), timeout=WORKER_TEST_TIMEOUT_SECONDS
                )
            has_event = await original_has_event(
                self,
                session,
                workspace_id,
                event_type=event_type,
                reason_code=reason_code,
                event_floor=event_floor,
                workspace_status=workspace_status,
            )
            async with count_lock:
                selected_count += 1
            if call_number == 2:
                second_selected_after_check.set()
            if call_number == 1:
                first_recording_task = asyncio.current_task()
                first_checked.set()
                assert not has_event
                await asyncio.wait_for(
                    allow_first_recording.wait(), timeout=WORKER_TEST_TIMEOUT_SECONDS
                )
            return has_event

        monkeypatch.setattr(
            WorkspaceRepository,
            "get_for_update",
            _recording_get_for_update,
        )
        monkeypatch.setattr(
            ControlWorker,
            "_has_current_salvage_event",
            _racing_has_current_salvage_event,
        )
        workers = [
            ControlWorker(
                session_factory=session_factory,
                provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
                executor=_RecordingExecutor(),
                runtime_inspector=_RecordingRuntimeInspector(
                    {compose_project: _live_agent_snapshot()}
                ),
                runtime_cleaner=_RecordingRuntimeCleaner(),
                config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=0),
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
            await worker._record_preserved_active_salvage_not_possible(  # noqa: SLF001
                candidate,
                preserved_event=preserved_event,
                reason="orphaned_committed_work",
            )
            if asyncio.current_task() is first_recording_task:
                first_recorded.set()

        tasks = [asyncio.create_task(_record_started(worker)) for worker in workers]
        try:
            await asyncio.wait_for(first_checked.wait(), timeout=WORKER_TEST_TIMEOUT_SECONDS)
            await asyncio.wait_for(
                second_attempted_acquire.wait(),
                timeout=WORKER_TEST_TIMEOUT_SECONDS,
            )
            assert not second_selected_after_check.is_set()
            allow_first_recording.set()
            await asyncio.wait_for(first_recorded.wait(), timeout=WORKER_TEST_TIMEOUT_SECONDS)
            allow_second_check.set()
            await asyncio.gather(*tasks)
        finally:
            allow_first_recording.set()
            allow_second_check.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        assert started_count == 2
        assert lock_attempt_count == 2
        assert second_selected_after_check.is_set()
        assert check_count == 2
        assert selected_count == 2
        async with session_factory() as s:
            not_possible_events = await WorkspaceEventRepository(s).list(
                workspace_id=workspace_id,
                event_type="workspace.active_execution_salvage_not_possible",
            )

        assert len(not_possible_events) == 1
        assert not_possible_events[0].payload is not None
        assert not_possible_events[0].payload["reason_code"] == (
            "ACTIVE_EXECUTION_SALVAGE_NOT_POSSIBLE"
        )

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
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            ws.execution_claimed_by = "zombie-worker"
            ws.execution_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            ws.execution_claim_epoch = 4
            await s.commit()
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
            # D3: the recovery clear bumps the fencing token so a zombie worker
            # whose owner string still matches is fenced on its next CAS write.
            assert ws.execution_claim_epoch == 5
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
