"""Workspace-backed task views for operator consoles."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session, require_api_token
from awf.api.responses import API_TOKEN_AUTH_ERROR_RESPONSES
from awf.api.schemas import TaskAttemptListResponse, TaskListResponse
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.service.tasks import build_task_attempt_list_response, build_task_list_response

router = APIRouter(
    prefix="/v1/tasks",
    tags=["tasks"],
    dependencies=[Depends(require_api_token)],
    responses=API_TOKEN_AUTH_ERROR_RESPONSES,
)


@router.get("", response_model=TaskListResponse)
async def list_tasks(
    workspace_status: Annotated[WorkspaceStatus | None, Query(alias="status")] = None,
    agent: Annotated[AgentRuntime | None, Query()] = None,
    repo_url: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    session: AsyncSession = Depends(get_db_session),
) -> TaskListResponse:
    return await build_task_list_response(
        session,
        workspace_status=workspace_status,
        agent=agent,
        repo_url=repo_url,
        limit=limit,
    )


@router.get("/{task_ref}/attempts", response_model=TaskAttemptListResponse)
async def list_task_attempts(
    task_ref: str,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    session: AsyncSession = Depends(get_db_session),
) -> TaskAttemptListResponse:
    response = await build_task_attempt_list_response(session, task_ref, limit=limit)
    if response is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": f"No task with ref {task_ref}"},
        )
    return response
