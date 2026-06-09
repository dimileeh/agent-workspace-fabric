"""Owned-path reservation and overlap-risk visibility endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as fastapi_status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.deps import get_db_session_factory, require_api_token
from awf.api.responses import API_TOKEN_AUTH_ERROR_RESPONSES
from awf.api.schemas import (
    WorkspaceLockListResponse,
    WorkspaceLockResponse,
    WorkspaceOverlapGraphResponse,
)
from awf.db.enums import TaskClass, WorkspaceStatus
from awf.service.locks import InvalidWorkspaceLockCursorError, list_workspace_lock_page
from awf.service.overlap_graph import OverlapGraphQueueState, build_workspace_overlap_graph

router = APIRouter(
    prefix="/v1/locks",
    tags=["locks"],
    dependencies=[Depends(require_api_token)],
    responses=API_TOKEN_AUTH_ERROR_RESPONSES,
)


@router.get("/overlap-graph", response_model=WorkspaceOverlapGraphResponse)
async def get_overlap_graph(
    repo_url: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
    base_branch: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    task_class: Annotated[TaskClass | None, Query()] = None,
    queue_state: Annotated[OverlapGraphQueueState, Query()] = "all",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_db_session_factory),
) -> WorkspaceOverlapGraphResponse:
    graph = await build_workspace_overlap_graph(
        session_factory,
        repo_url=repo_url,
        base_branch=base_branch,
        task_class=task_class,
        queue_state=queue_state,
        limit=limit,
    )
    return WorkspaceOverlapGraphResponse.model_validate(graph)


@router.get("", response_model=WorkspaceLockListResponse)
async def list_locks(
    repo_url: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
    task_class: Annotated[TaskClass | None, Query()] = None,
    workspace_status: Annotated[WorkspaceStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_db_session_factory),
) -> WorkspaceLockListResponse:
    try:
        page = await list_workspace_lock_page(
            session_factory,
            repo_url=repo_url,
            task_class=task_class,
            status=workspace_status,
            limit=limit,
            cursor=cursor,
        )
    except InvalidWorkspaceLockCursorError as exc:
        raise HTTPException(
            status_code=fastapi_status.HTTP_400_BAD_REQUEST,
            detail={"error_code": "INVALID_CURSOR", "message": "Invalid lock list cursor."},
        ) from exc
    return WorkspaceLockListResponse(
        items=[WorkspaceLockResponse.model_validate(row) for row in page.items],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
        limit=limit,
        cursor=cursor,
    )
