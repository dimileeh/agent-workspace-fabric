"""Workspace artifact metadata endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session, require_api_token
from awf.api.schemas import WorkspaceArtifactListResponse, WorkspaceArtifactResponse
from awf.db.repositories import WorkspaceRepository
from awf.service.artifacts import (
    DEFAULT_ARTIFACT_LIST_LIMIT,
    _artifact_id,
    _artifact_kind,
    _list_artifacts,
    _workspace_artifact_dir,
    list_workspace_artifacts_metadata,
)

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/artifacts", tags=["artifacts"])

__all__ = [
    "DEFAULT_ARTIFACT_LIST_LIMIT",
    "WorkspaceArtifactListResponse",
    "WorkspaceArtifactResponse",
    "_artifact_id",
    "_artifact_kind",
    "_list_artifacts",
    "_require_workspace",
    "_workspace_artifact_dir",
    "list_workspace_artifacts",
]


@router.get(
    "",
    response_model=WorkspaceArtifactListResponse,
    dependencies=[Depends(require_api_token)],
)
async def list_workspace_artifacts(
    workspace_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceArtifactListResponse:
    response = await list_workspace_artifacts_metadata(
        session,
        workspace_id=workspace_id,
    )
    if response is None:
        raise _workspace_not_found(workspace_id)
    return response


async def _require_workspace(session: AsyncSession, workspace_id: str) -> None:
    if not await WorkspaceRepository(session).exists(workspace_id):
        raise _workspace_not_found(workspace_id)


def _workspace_not_found(workspace_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error_code": "NOT_FOUND", "message": f"No workspace with id {workspace_id}"},
    )
