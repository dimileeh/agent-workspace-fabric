"""Workspace-backed task views for operator consoles."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session
from awf.api.schemas import TaskListResponse, TaskResponse
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.models import TaskAttempt, Workspace
from awf.db.repositories import TaskAttemptRepository, WorkspaceRepository

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    workspace_status: Annotated[WorkspaceStatus | None, Query(alias="status")] = None,
    agent: Annotated[AgentRuntime | None, Query()] = None,
    repo_url: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    session: AsyncSession = Depends(get_db_session),
) -> TaskListResponse:
    attempt_rows = await TaskAttemptRepository(session).list_latest(
        status=workspace_status,
        agent=agent,
        repo_url=repo_url,
        limit=limit,
    )
    legacy_rows = await WorkspaceRepository(session).list_without_task_attempts(
        status=workspace_status,
        agent=agent,
        repo_url=repo_url,
        limit=limit,
    )
    items = [_task_from_attempt(row) for row in attempt_rows]
    items.extend(_task_from_workspace(row) for row in legacy_rows)
    items.sort(key=lambda item: (item.created_at, item.workspace_id), reverse=True)
    return TaskListResponse(
        items=items[:limit],
    )


def _task_from_attempt(attempt: TaskAttempt) -> TaskResponse:
    workspace = attempt.workspace
    return TaskResponse(
        task_id=attempt.task.external_id or attempt.task.id,
        attempt_id=attempt.id,
        attempt_number=attempt.attempt_number,
        workspace_id=attempt.workspace_id,
        title=attempt.title,
        repo_url=attempt.repo_url,
        base_branch=attempt.base_branch,
        task_class=attempt.task_class,
        owned_paths=list(attempt.owned_paths),
        agent=AgentRuntime(attempt.agent),
        status=WorkspaceStatus(workspace.status),
        pr_url=workspace.pr_url,
        failure_reason=workspace.failure_reason,
        created_at=attempt.created_at,
        updated_at=workspace.updated_at,
    )


def _task_from_workspace(row: Workspace) -> TaskResponse:
    return TaskResponse(
        task_id=row.task_external_id or row.id,
        attempt_id=None,
        attempt_number=None,
        workspace_id=row.id,
        title=row.task_title,
        repo_url=row.repo_url,
        base_branch=row.branch_base,
        task_class=row.task_class,
        owned_paths=list(row.owned_paths),
        agent=AgentRuntime(row.agent),
        status=WorkspaceStatus(row.status),
        pr_url=row.pr_url,
        failure_reason=row.failure_reason,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )
