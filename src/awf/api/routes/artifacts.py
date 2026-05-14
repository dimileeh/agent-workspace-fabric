"""Workspace artifact metadata endpoints."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session, require_api_token
from awf.api.responses import API_TOKEN_AUTH_ERROR_RESPONSES
from awf.api.schemas import WorkspaceArtifactListResponse, WorkspaceArtifactResponse
from awf.db.repositories import WorkspaceRepository
from awf.service.artifacts import (
    DEFAULT_ARTIFACT_LIST_LIMIT,
    MAX_ARTIFACT_LIST_LIMIT,
    ArtifactNotFoundError,
    ArtifactPathError,
    _artifact_id,
    _artifact_kind,
    _list_artifacts,
    _workspace_artifact_dir,
    get_downloadable_artifact,
    list_workspace_artifacts_metadata,
)
from awf.service.bounded_list import InvalidBoundedListCursorError

router = APIRouter(
    prefix="/v1/workspaces/{workspace_id}/artifacts",
    tags=["artifacts"],
    responses=API_TOKEN_AUTH_ERROR_RESPONSES,
)

__all__ = [
    "DEFAULT_ARTIFACT_LIST_LIMIT",
    "WorkspaceArtifactListResponse",
    "WorkspaceArtifactResponse",
    "_artifact_id",
    "_artifact_kind",
    "_list_artifacts",
    "_require_workspace",
    "_workspace_artifact_dir",
    "download_workspace_artifact",
    "list_workspace_artifacts",
]


@router.get(
    "",
    response_model=WorkspaceArtifactListResponse,
    dependencies=[Depends(require_api_token)],
)
async def list_workspace_artifacts(
    workspace_id: str,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=MAX_ARTIFACT_LIST_LIMIT,
            description="Maximum artifact metadata records to return.",
        ),
    ] = DEFAULT_ARTIFACT_LIST_LIMIT,
    cursor: Annotated[str | None, Query(max_length=64)] = None,
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceArtifactListResponse:
    try:
        response = await list_workspace_artifacts_metadata(
            session,
            workspace_id=workspace_id,
            limit=limit,
            cursor=cursor,
        )
    except InvalidBoundedListCursorError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "INVALID_CURSOR",
                "message": "Invalid artifact list cursor.",
            },
        ) from exc
    if response is None:
        raise _workspace_not_found(workspace_id)
    return response


@router.get(
    "/download",
    dependencies=[Depends(require_api_token)],
)
async def download_workspace_artifact(
    workspace_id: str,
    path: Annotated[str, Query()],
    session: AsyncSession = Depends(get_db_session),
) -> FileResponse:
    await _require_workspace(session, workspace_id)
    artifact_dir = _workspace_artifact_dir(workspace_id)
    try:
        artifact = await asyncio.to_thread(
            get_downloadable_artifact,
            workspace_id=workspace_id,
            artifact_dir=artifact_dir,
            relative_path=path,
        )
    except ArtifactPathError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "INVALID_ARTIFACT_PATH",
                "message": "Artifact path must be a non-empty relative POSIX path.",
            },
        ) from exc
    except ArtifactNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": f"No artifact at path {path}"},
        ) from exc

    return FileResponse(
        artifact.path,
        media_type=artifact.content_type,
        filename=artifact.name,
        stat_result=artifact.stat_result,
    )


async def _require_workspace(session: AsyncSession, workspace_id: str) -> None:
    """Backward-compatible 404 helper retained for direct importers."""

    if not await WorkspaceRepository(session).exists(workspace_id):
        raise _workspace_not_found(workspace_id)


def _workspace_not_found(workspace_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error_code": "NOT_FOUND", "message": f"No workspace with id {workspace_id}"},
    )
