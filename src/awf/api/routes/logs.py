"""Workspace log stream endpoints."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session, require_api_token
from awf.api.responses import API_TOKEN_AUTH_ERROR_RESPONSES
from awf.api.schemas import (
    WorkspaceLogListResponse,
    WorkspaceLogReadResponse,
    WorkspaceLogStreamResponse,
)
from awf.db.repositories import WorkspaceLogStreamRepository, WorkspaceRepository
from awf.runtime.logs import read_log_chunk

router = APIRouter(
    prefix="/v1/workspaces/{workspace_id}/logs",
    tags=["logs"],
    responses=API_TOKEN_AUTH_ERROR_RESPONSES,
)


@router.get(
    "",
    response_model=WorkspaceLogListResponse,
    dependencies=[Depends(require_api_token)],
)
async def list_workspace_logs(
    workspace_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceLogListResponse:
    await _require_workspace(session, workspace_id)
    rows = await WorkspaceLogStreamRepository(session).list_for_workspace(workspace_id)
    return WorkspaceLogListResponse(
        items=[WorkspaceLogStreamResponse.model_validate(row) for row in rows],
        limit=len(rows),
        cursor=None,
    )


@router.get(
    "/{stream_id}",
    response_model=WorkspaceLogReadResponse,
    dependencies=[Depends(require_api_token)],
)
async def read_workspace_log(
    workspace_id: str,
    stream_id: str,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit_bytes: Annotated[int, Query(ge=1, le=1_048_576)] = 65_536,
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceLogReadResponse:
    await _require_workspace(session, workspace_id)
    stream = await WorkspaceLogStreamRepository(session).get(
        workspace_id=workspace_id,
        stream_id=stream_id,
    )
    if stream is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": f"No log stream {stream_id}"},
        )
    path = Path(stream.path)
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "LOG_FILE_MISSING",
                "message": f"Log file is missing for stream {stream_id}",
            },
        )
    data, next_offset, eof = await read_log_chunk(
        path=path,
        offset=offset,
        limit_bytes=limit_bytes,
    )
    return WorkspaceLogReadResponse(
        stream_id=stream_id,
        offset=offset,
        next_offset=next_offset,
        eof=eof,
        data=data,
    )


async def _require_workspace(session: AsyncSession, workspace_id: str) -> None:
    workspace = await WorkspaceRepository(session).get(workspace_id)
    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": f"No workspace with id {workspace_id}"},
        )
