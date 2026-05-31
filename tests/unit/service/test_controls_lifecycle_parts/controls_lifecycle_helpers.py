"""Shared fixtures for workspace control lifecycle tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import MergeCandidate, Operation, Workspace, WorkspaceEvent
from awf.db.repositories import (
    MergeCandidateRepository,
    OperationRepository,
    SecretLeaseIssue,
    SecretLeaseRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.service.controls import WorkspaceControlService, WorkspaceStackStopError


@dataclass
class RecordingStopper:
    calls: list[str | None] = field(default_factory=list)

    async def __call__(self, compose_project_name: str | None) -> None:
        self.calls.append(compose_project_name)


@dataclass
class FailingStopper:
    calls: list[str | None] = field(default_factory=list)

    async def __call__(self, compose_project_name: str | None) -> None:
        self.calls.append(compose_project_name)
        raise WorkspaceStackStopError(
            operation="stop",
            returncode=17,
            stdout="",
            stderr="compose stop denied",
        )


@dataclass
class CleanupCall:
    workspace_id: str
    repo_url: str
    compose_project_name: str | None
    compose_file_path: Path | None
    worktree_host_path: Path | None
    remove_volumes: bool
    remove_worktree: bool


@dataclass
class RecordingCleaner:
    failures: list[str] = field(default_factory=list)
    calls: list[CleanupCall] = field(default_factory=list)

    async def cleanup(
        self,
        *,
        workspace_id: str,
        repo_url: str,
        companion_worktrees: tuple[tuple[str, str], ...] = (),
        compose_project_name: str | None = None,
        compose_file_path: Path | None = None,
        worktree_host_path: Path | None = None,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
    ) -> list[str]:
        _ = companion_worktrees
        self.calls.append(
            CleanupCall(
                workspace_id=workspace_id,
                repo_url=repo_url,
                compose_project_name=compose_project_name,
                compose_file_path=compose_file_path,
                worktree_host_path=worktree_host_path,
                remove_volumes=remove_volumes,
                remove_worktree=remove_worktree,
            )
        )
        return list(self.failures)


@dataclass
class StaleCallbackCleaner(RecordingCleaner):
    session: AsyncSession | None = None
    final_status: WorkspaceStatus = WorkspaceStatus.cancelled

    async def cleanup(
        self,
        *,
        workspace_id: str,
        repo_url: str,
        companion_worktrees: tuple[tuple[str, str], ...] = (),
        compose_project_name: str | None = None,
        compose_file_path: Path | None = None,
        worktree_host_path: Path | None = None,
        remove_volumes: bool = True,
        remove_worktree: bool = True,
    ) -> list[str]:
        result = await super().cleanup(
            workspace_id=workspace_id,
            repo_url=repo_url,
            companion_worktrees=companion_worktrees,
            compose_project_name=compose_project_name,
            compose_file_path=compose_file_path,
            worktree_host_path=worktree_host_path,
            remove_volumes=remove_volumes,
            remove_worktree=remove_worktree,
        )
        assert self.session is not None
        await self.session.execute(
            sa_update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(
                status=self.final_status.value,
                failure_reason=(
                    "operator_failure" if self.final_status == WorkspaceStatus.failed else None
                ),
                failure_message=(
                    "operator moved workspace"
                    if self.final_status == WorkspaceStatus.failed
                    else None
                ),
            )
            .execution_options(synchronize_session=False)
        )
        await self.session.flush()
        return result


async def _workspace(
    session: AsyncSession,
    *,
    status: WorkspaceStatus,
    title: str = "control lifecycle",
) -> Workspace:
    workspace = await WorkspaceRepository(session).create(
        repo_url="git@github.com:example/control-lifecycle.git",
        branch_base="development",
        task_title=title,
        task_prompt="Exercise control lifecycle behavior.",
        agent=AgentRuntime.codex.value,
        test_commands=["pytest -q"],
    )
    workspace.status = status.value
    workspace.compose_project_name = f"awf_{workspace.id}"
    workspace.compose_file_path = f"/tmp/{workspace.id}/compose.yml"
    await session.flush()
    return workspace


async def _workspace_with_candidate(
    session: AsyncSession,
    *,
    status: WorkspaceStatus = WorkspaceStatus.monitoring_pr,
    title: str = "rebase lifecycle",
) -> tuple[Workspace, MergeCandidate]:
    workspace = await _workspace(session, status=status, title=title)
    workspace.branch_name = f"awf/{workspace.id}"
    workspace.remote_push_branch = workspace.branch_name
    workspace.base_commit = "a" * 40
    workspace.monitor_last_commit_sha = "h" * 40
    workspace.pr_url = "https://github.com/example/control-lifecycle/pull/50"
    workspace.pr_number = 50
    task = await TaskRepository(session).create_or_get(
        repo_url=workspace.repo_url,
        base_branch=workspace.branch_base,
        title=workspace.task_title,
        prompt=workspace.task_prompt,
        external_id=workspace.task_external_id,
        idempotency_key=None,
        task_class=workspace.task_class,
        owned_paths=list(workspace.owned_paths),
    )
    attempt = await TaskAttemptRepository(session).create_for_workspace(
        task=task,
        workspace=workspace,
    )
    candidate = await MergeCandidateRepository(session).create_or_update_open_for_attempt(
        task=task,
        attempt=attempt,
        workspace=workspace,
        head_sha=workspace.monitor_last_commit_sha,
        base_sha=workspace.base_commit,
    )
    await session.flush()
    return workspace, candidate


def _service(
    session: AsyncSession,
    *,
    stopper: RecordingStopper | None = None,
    cleaner: RecordingCleaner | None = None,
) -> tuple[WorkspaceControlService, RecordingStopper, RecordingCleaner]:
    stopper = stopper or RecordingStopper()
    cleaner = cleaner or RecordingCleaner()
    return (
        WorkspaceControlService(
            session,
            project_stopper=stopper,
            cleaner_factory=lambda: cleaner,
        ),
        stopper,
        cleaner,
    )


async def _issue_control_secret_lease(
    session: AsyncSession,
    workspace: Workspace,
    *,
    now: datetime | None = None,
) -> None:
    issued_at = now or datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
    await SecretLeaseRepository(session).issue_declared_leases(
        workspace,
        leases=[
            SecretLeaseIssue(
                secret_name="api-token",
                kind="env",
                target="API_TOKEN",
                mode="ro",
                required=True,
                provider="env",
                ref_digest="sha256:" + "d" * 64,
                expires_at=issued_at + timedelta(hours=1),
                issue_metadata={"profile": "control-lifecycle", "declaration_index": 0},
            )
        ],
        now=issued_at,
    )


async def _operations(session: AsyncSession, workspace_id: str) -> list[Operation]:
    return await OperationRepository(session).list_for_workspace(workspace_id, limit=20)


async def _events(session: AsyncSession, workspace_id: str) -> list[WorkspaceEvent]:
    return await WorkspaceEventRepository(session).list(workspace_id=workspace_id, limit=20)


__all__ = [
    "CleanupCall",
    "FailingStopper",
    "RecordingCleaner",
    "RecordingStopper",
    "StaleCallbackCleaner",
    "_events",
    "_issue_control_secret_lease",
    "_operations",
    "_service",
    "_workspace",
    "_workspace_with_candidate",
]
