"""Workspace runtime inspection endpoint."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.deps import get_db_session_factory, require_api_token
from awf.api.responses import API_TOKEN_AUTH_ERROR_RESPONSES
from awf.api.schemas import WorkspaceRuntimeResponse
from awf.service.workspaces import RuntimeInspection, WorkspaceService

router = APIRouter(
    prefix="/v1/workspaces/{workspace_id}/runtime",
    tags=["runtime"],
    dependencies=[Depends(require_api_token)],
    responses=API_TOKEN_AUTH_ERROR_RESPONSES,
)


@router.get("", response_model=WorkspaceRuntimeResponse)
async def get_workspace_runtime(
    request: Request,
    workspace_id: str,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_db_session_factory),
) -> WorkspaceRuntimeResponse:
    runtime_inspector = cast(
        RuntimeInspection | None,
        getattr(request.app.state, "workspace_runtime_inspector", None),
    )
    result = await WorkspaceService(
        session_factory,
        runtime_inspector=runtime_inspector,
    ).get_runtime(workspace_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": f"No workspace with id {workspace_id}"},
        )
    return result
