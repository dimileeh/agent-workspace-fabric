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
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.exc import InterfaceError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import awf.db.repositories as repositories_module
from awf.control.worker import (
    ControlWorker,
    WorkerConfig,
)
from awf.control.worker import claims as worker_claims
from awf.control.worker import resource_broker as worker_resource_broker
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


class TestRunOncePart004:
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
                allocated=worker_claims._AllocatedReservationTotals(),  # noqa: SLF001
                claim_slots=1,
                decided_at=datetime.now(UTC),
            )
            decisions = await QueueDecisionRepository(s).list_for_workspace(requested_id)

        assert claimed == []
        assert all(
            decision.reason_code != "LOCAL_CAPACITY_RESERVATION_DEFAULTED" for decision in decisions
        )

    @pytest.mark.unit
    async def test_requested_capacity_gate_dedupes_allocated_only_capacity_deferral_changes(
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
        assert len(deferred_decisions) == 1
        blockers = deferred_decisions[0].resource_summary["blockers"]
        assert blockers[0]["allocated"] == 6.0
        assert blockers[0]["after"] == 12.0
        assert "previous" not in deferred_decisions[0].resource_summary

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
    async def test_requested_capacity_gate_skips_null_node_workspace_with_null_node_reservation(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        active_id = await _create_ready(
            session_factory,
            origin_repo,
            "null-node-reservation-capacity-holder",
            create_task_attempt=True,
        )
        async with session_factory() as s:
            active = await WorkspaceRepository(s).get(active_id)
            assert active is not None
            active.node_id = None
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

        async with session_factory() as s:
            allocated = await worker_claims._allocated_totals_for_capacity_gate(  # noqa: SLF001
                s,
                reservation_repo=ResourceReservationRepository(s),
                config=WorkerConfig(node_id="worker-node-a"),
            )

        assert allocated.workspace_count == 0
        assert allocated.peak_cpu == 0.0
        assert allocated.peak_memory_gb == 0.0
        assert allocated.dind_slots == 0

    @pytest.mark.unit
    async def test_capacity_gate_uses_scheduler_allocation_scope_and_unreserved_defaults(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        unreserved_id = await _create_ready(
            session_factory,
            origin_repo,
            "unreserved-scheduler-scope-capacity-holder",
        )
        async with session_factory() as s:
            unreserved = await WorkspaceRepository(s).get(unreserved_id)
            assert unreserved is not None
            unreserved.node_id = "worker-node-a"
            await s.commit()

        class _SchedulerScopeReservationRepository:
            def __init__(self) -> None:
                self.calls: list[tuple[tuple[str, ...], str]] = []

            async def active_latest_totals_for_scheduler_allocation_scope(
                self,
                *,
                statuses: tuple[str, ...],
                node_id: str,
            ) -> dict[str, float | int]:
                self.calls.append((statuses, node_id))
                return {
                    "workspace_count": 2,
                    "steady_cpu": 3.0,
                    "steady_memory_gb": 8.0,
                    "peak_cpu": 6.0,
                    "peak_memory_gb": 16.0,
                    "disk_mb": 256,
                    "dind_slots": 1,
                }

            async def active_latest_totals(self, **_kwargs: object) -> dict[str, float | int]:
                raise AssertionError("capacity gate must use scheduler allocation scope")

            async def active_latest_by_workspace_ids(
                self,
                _workspace_ids: object,
            ) -> dict[str, object]:
                raise AssertionError("capacity gate must not fetch mismatched reservations")

        reservation_repo = _SchedulerScopeReservationRepository()
        async with session_factory() as s:
            allocated = await worker_claims._allocated_totals_for_capacity_gate(  # noqa: SLF001
                s,
                reservation_repo=reservation_repo,  # type: ignore[arg-type]
                config=WorkerConfig(
                    node_id="worker-node-a",
                    workspace_steady_cpu=4.0,
                    workspace_steady_memory_gb=5.0,
                    workspace_peak_cpu=6.0,
                    workspace_peak_memory_gb=7.0,
                ),
            )

        assert reservation_repo.calls == [
            (repositories_module.ALLOCATED_RESOURCE_RESERVATION_STATUSES, "worker-node-a")
        ]
        assert allocated.workspace_count == 3
        assert allocated.steady_cpu == 7.0
        assert allocated.steady_memory_gb == 13.0
        assert allocated.peak_cpu == 12.0
        assert allocated.peak_memory_gb == 23.0
        assert allocated.disk_mb == 256
        assert allocated.dind_slots == 1

    @pytest.mark.unit
    async def test_capacity_gate_unreserved_defaults_use_deduplicated_join(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        reserved_id = await _create_ready(
            session_factory,
            origin_repo,
            "reserved-join-capacity-holder",
            create_task_attempt=True,
        )
        unreserved_id = await _create_ready(
            session_factory,
            origin_repo,
            "unreserved-join-capacity-holder",
        )
        async with session_factory() as s:
            repo = WorkspaceRepository(s)
            reserved = await repo.get(reserved_id)
            unreserved = await repo.get(unreserved_id)
            assert reserved is not None
            assert unreserved is not None
            reserved.node_id = "worker-node-a"
            unreserved.node_id = "worker-node-a"
            await s.commit()
        await _reserve_workspace(
            session_factory,
            reserved_id,
            node_id="worker-node-b",
            steady_cpu=3.0,
            steady_memory_gb=8.0,
            peak_cpu=6.0,
            peak_memory_gb=16.0,
            dind_slots=1,
        )
        statements: list[str] = []

        def _capture_select(
            _conn: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            normalized = " ".join(statement.lower().split())
            if normalized.startswith("select") and " from workspaces " in normalized:
                statements.append(normalized)

        engine = session_factory.kw["bind"]
        event.listen(engine.sync_engine, "before_cursor_execute", _capture_select)
        try:
            async with session_factory() as s:
                unreserved_allocated = worker_claims._AllocatedReservationTotals()  # noqa: SLF001
                await worker_resource_broker._add_unreserved_active_workspace_defaults(  # noqa: SLF001
                    s,
                    allocated=unreserved_allocated,
                    config=WorkerConfig(),
                    node_id="worker-node-a",
                )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _capture_select)

        assert unreserved_allocated.workspace_count == 1
        unreserved_select = next(
            statement for statement in statements if "workspaces.resolved_profile" in statement
        )
        assert " exists " not in unreserved_select
        assert (
            "left outer join (select distinct resource_reservations.workspace_id"
            in unreserved_select
        )
        assert "anon_1.workspace_id is null" in unreserved_select
        assert "order by workspaces.id" not in unreserved_select
