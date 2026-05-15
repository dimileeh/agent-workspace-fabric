"""Read-only merge queue visualization endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import status as fastapi_status
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session, require_api_token
from awf.api.responses import API_TOKEN_AUTH_ERROR_RESPONSES
from awf.api.schemas import MergeQueueListResponse
from awf.db.enums import WorkspaceStatus
from awf.service.merge_queue import (
    InvalidMergeQueueCursorError,
    list_merge_queue_response,
)

router = APIRouter(
    prefix="/v1/merge-queue",
    tags=["merge-queue"],
    dependencies=[Depends(require_api_token)],
    responses=API_TOKEN_AUTH_ERROR_RESPONSES,
)

__all__ = ["list_merge_queue"]


@router.get("", response_model=MergeQueueListResponse)
async def list_merge_queue(
    repo_url: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
    base_branch: Annotated[str | None, Query(min_length=1, max_length=256)] = None,
    workspace_status: Annotated[WorkspaceStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    cursor: Annotated[str | None, Query(max_length=128)] = None,
    session: AsyncSession = Depends(get_db_session),
) -> MergeQueueListResponse:
    try:
        return await list_merge_queue_response(
            session,
            repo_url=repo_url,
            base_branch=base_branch,
            workspace_status=workspace_status,
            limit=limit,
            cursor=cursor,
        )
    except InvalidMergeQueueCursorError as exc:
        raise HTTPException(
            status_code=fastapi_status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "INVALID_CURSOR",
                "message": "Invalid merge queue cursor.",
            },
        ) from exc
