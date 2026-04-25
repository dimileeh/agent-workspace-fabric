"""Workspace artifact metadata endpoints."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.deps import get_db_session, require_api_token
from awf.common.config import get_settings
from awf.db.repositories import WorkspaceRepository

router = APIRouter(prefix="/v1/workspaces/{workspace_id}/artifacts", tags=["artifacts"])


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
    return WorkspaceArtifactListResponse(items=items)


async def _require_workspace(session: AsyncSession, workspace_id: str) -> None:
    if not await WorkspaceRepository(session).exists(workspace_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "NOT_FOUND", "message": f"No workspace with id {workspace_id}"},
        )


def _workspace_artifact_dir(workspace_id: str) -> Path:
    return Path(get_settings().work_dir) / "artifacts" / workspace_id


def _list_artifacts(workspace_id: str, artifact_dir: Path) -> list[WorkspaceArtifactResponse]:
    if not artifact_dir.is_dir() or artifact_dir.is_symlink():
        return []

    root = artifact_dir.resolve(strict=True)
    items: list[WorkspaceArtifactResponse] = []
    for directory, dirnames, filenames in artifact_dir.walk(follow_symlinks=False):
        dirnames.sort()
        for filename in sorted(filenames):
            candidate = directory / filename
            if candidate.is_symlink():
                continue
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if not resolved.is_file() or not resolved.is_relative_to(root):
                continue
            stat = resolved.stat()
            relative_path = candidate.relative_to(artifact_dir).as_posix()
            items.append(
                WorkspaceArtifactResponse(
                    artifact_id=_artifact_id(workspace_id, relative_path),
                    workspace_id=workspace_id,
                    name=candidate.name,
                    relative_path=relative_path,
                    path=str(resolved),
                    kind=_artifact_kind(candidate),
                    size_bytes=stat.st_size,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
                )
            )
    return sorted(items, key=lambda item: item.relative_path)


def _artifact_id(workspace_id: str, relative_path: str) -> str:
    digest = sha256(f"{workspace_id}\0{relative_path}".encode()).hexdigest()[:24]
    return f"art_{digest}"


def _artifact_kind(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    return suffix or "file"
