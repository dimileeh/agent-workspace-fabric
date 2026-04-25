"""Workspace-backed task views for operator consoles."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session
from awf.api.schemas import TaskListResponse, TaskResponse
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    workspace_status: Annotated[WorkspaceStatus | None, Query(alias="status")] = None,
    agent: Annotated[AgentRuntime | None, Query()] = None,
    repo_url: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    session: AsyncSession = Depends(get_db_session),
) -> TaskListResponse:
    rows = await WorkspaceRepository(session).list(
        status=workspace_status,
        agent=agent,
        repo_url=repo_url,
        limit=limit,
    )
    return TaskListResponse(
        items=[
            TaskResponse(
                task_id=row.task_external_id or row.id,
                workspace_id=row.id,
                title=row.task_title,
                repo_url=row.repo_url,
                base_branch=row.branch_base,
                agent=AgentRuntime(row.agent),
                status=WorkspaceStatus(row.status),
                pr_url=row.pr_url,
                failure_reason=row.failure_reason,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        ]
    )
