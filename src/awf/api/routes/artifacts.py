"""Workspace artifact metadata endpoints."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session, require_api_token
from awf.common.config import get_settings
from awf.db.repositories import WorkspaceRepository
from awf.service.artifacts import (
    ArtifactMetadata,
    ArtifactNotFoundError,
    ArtifactPathError,
    artifact_id,
    artifact_kind,
    get_downloadable_artifact,
    list_artifacts,
    workspace_artifact_dir,
)

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/artifacts", tags=["artifacts"])

DEFAULT_ARTIFACT_LIST_LIMIT = 50


class WorkspaceArtifactResponse(BaseModel):
    artifact_id: str
    workspace_id: str
    name: str
    relative_path: str
    path: str
    kind: str
    size_bytes: int
    modified_at: datetime


class WorkspaceArtifactListResponse(BaseModel):
    items: list[WorkspaceArtifactResponse]
    next_cursor: str | None = None
    has_more: bool = False
    limit: int = DEFAULT_ARTIFACT_LIST_LIMIT
    cursor: str | None = None


@router.get(
    "",
    response_model=WorkspaceArtifactListResponse,
    dependencies=[Depends(require_api_token)],
)
async def list_workspace_artifacts(
    workspace_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceArtifactListResponse:
    await _require_workspace(session, workspace_id)
    artifact_dir = _workspace_artifact_dir(workspace_id)
    items = await asyncio.to_thread(_list_artifacts, workspace_id, artifact_dir)
    return WorkspaceArtifactListResponse(
        items=items,
        limit=DEFAULT_ARTIFACT_LIST_LIMIT,
        cursor=None,
    )


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
    if not await WorkspaceRepository(session).exists(workspace_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": f"No workspace with id {workspace_id}"},
        )


def _workspace_artifact_dir(workspace_id: str) -> Path:
    return workspace_artifact_dir(get_settings().work_dir, workspace_id)


def _list_artifacts(workspace_id: str, artifact_dir: Path) -> list[WorkspaceArtifactResponse]:
    return [_artifact_response(item) for item in list_artifacts(workspace_id, artifact_dir)]


def _artifact_response(item: ArtifactMetadata) -> WorkspaceArtifactResponse:
    return WorkspaceArtifactResponse(
        artifact_id=item.artifact_id,
        workspace_id=item.workspace_id,
        name=item.name,
        relative_path=item.relative_path,
        path=str(item.path),
        kind=item.kind,
        size_bytes=item.size_bytes,
        modified_at=item.modified_at,
    )


def _artifact_id(workspace_id: str, relative_path: str) -> str:
    return artifact_id(workspace_id, relative_path)


def _artifact_kind(path: Path) -> str:
    return artifact_kind(path)
