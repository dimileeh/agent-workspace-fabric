"""Workspace runtime inspection endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.deps import get_db_session_factory
from awf.api.schemas import WorkspaceRuntimeResponse
from awf.service.workspaces import WorkspaceService

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/runtime", tags=["runtime"])


@router.get("", response_model=WorkspaceRuntimeResponse)
async def get_workspace_runtime(
    workspace_id: str,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_db_session_factory),
) -> WorkspaceRuntimeResponse:
    result = await WorkspaceService(session_factory).get_runtime(workspace_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": f"No workspace with id {workspace_id}"},
        )
    return result
