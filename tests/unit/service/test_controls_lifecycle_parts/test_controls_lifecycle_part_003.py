"""Focused remonitor lifecycle edge coverage."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import MergeCandidate, Workspace
from awf.db.repositories import (
    MergeCandidateRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.service.controls import WorkspaceControlService
from tests.postgres import postgres_test_session


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


@dataclass
class _RecordingStopper:
    calls: list[str | None] = field(default_factory=list)

    async def __call__(self, compose_project_name: str | None) -> None:
        self.calls.append(compose_project_name)


@dataclass
class _RecordingCleaner:
    calls: list[str] = field(default_factory=list)

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
        del (
            repo_url,
            companion_worktrees,
            compose_project_name,
            compose_file_path,
            worktree_host_path,
            remove_volumes,
            remove_worktree,
        )
        self.calls.append(workspace_id)
        return []


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
    status: WorkspaceStatus,
    title: str,
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


def _service(session: AsyncSession) -> WorkspaceControlService:
    cleaner = _RecordingCleaner()
    return WorkspaceControlService(
        session,
        project_stopper=_RecordingStopper(),
        cleaner_factory=lambda: cleaner,
    )


@pytest.mark.unit
async def test_remonitor_failed_workspace_reopens_existing_merge_candidate(
    session: AsyncSession,
) -> None:
    workspace, candidate = await _workspace_with_candidate(
        session,
        status=WorkspaceStatus.failed,
        title="failed candidate remonitor",
    )
    workspace.failure_reason = "infrastructure_failure"
    workspace.failure_message = "monitor crashed during repair"
    workspace.monitor_iter_count = 5
    candidate.status = "closed"
    candidate.close_reason = "STALE_MONITOR_FAILED"
    candidate.closed_at = datetime.now(UTC)
    candidate.merged_at = datetime.now(UTC)
    await session.flush()
    service = _service(session)

    response = await service.remonitor_workspace(
        workspace.id,
        reason="reattach candidate",
        idempotency_key="remonitor-failed-pr-candidate",
        expected_version=workspace.version,
    )
    attempt = await TaskAttemptRepository(session).get_by_workspace_id(workspace.id)
    events = await WorkspaceEventRepository(session).list(workspace_id=workspace.id, limit=20)

    assert response.status == WorkspaceStatus.monitoring_pr
    assert attempt is not None
    assert attempt.status == WorkspaceStatus.monitoring_pr.value
    assert candidate.status == "open"
    assert candidate.close_reason is None
    assert candidate.closed_at is None
    assert candidate.merged_at is None
    assert candidate.head_sha == workspace.monitor_last_commit_sha
    assert candidate.base_sha == workspace.base_commit
    reset_event = next(
        event
        for event in events
        if isinstance(event.payload, dict) and "state_reset" in event.payload
    )
    assert reset_event.payload["state_reset"] == {
        "from": WorkspaceStatus.failed.value,
        "to": WorkspaceStatus.monitoring_pr.value,
        "monitor_iter_count_reset_from": 5,
        "candidate_reopened": True,
    }
