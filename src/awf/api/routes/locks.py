"""Owned-path reservation and overlap-risk visibility endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as fastapi_status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.deps import get_db_session_factory
from awf.api.schemas import WorkspaceLockListResponse, WorkspaceLockResponse
from awf.db.enums import TaskClass, WorkspaceStatus
from awf.service.locks import InvalidWorkspaceLockCursorError, list_workspace_lock_page

router = APIRouter(prefix="/v1/locks", tags=["locks"])


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
    )
