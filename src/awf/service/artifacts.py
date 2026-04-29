"""Shared workspace artifact metadata builders for REST and MCP."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.schemas import WorkspaceArtifactListResponse, WorkspaceArtifactResponse
from awf.common.config import get_settings
from awf.db.repositories import WorkspaceRepository

DEFAULT_ARTIFACT_LIST_LIMIT = 50


async def list_workspace_artifacts_metadata(
    session: AsyncSession,
    *,
    workspace_id: str,
    work_dir: str | Path | None = None,
) -> WorkspaceArtifactListResponse | None:
    """Return safe artifact metadata for one workspace, or ``None`` if missing."""

    if not await WorkspaceRepository(session).exists(workspace_id):
        return None
    artifact_dir = _workspace_artifact_dir(workspace_id, work_dir=work_dir)
    items = await asyncio.to_thread(_list_artifacts, workspace_id, artifact_dir)
    return WorkspaceArtifactListResponse(
        items=items,
        limit=DEFAULT_ARTIFACT_LIST_LIMIT,
        cursor=None,
    )


def _workspace_artifact_dir(
    workspace_id: str,
    *,
    work_dir: str | Path | None = None,
) -> Path:
    root = Path(work_dir) if work_dir is not None else Path(get_settings().work_dir)
    return root / "artifacts" / workspace_id


def _list_artifacts(workspace_id: str, artifact_dir: Path) -> list[WorkspaceArtifactResponse]:
    try:
        if not artifact_dir.is_dir() or artifact_dir.is_symlink():
            return []
        root = artifact_dir.resolve(strict=True)
    except OSError:
        return []

    items: list[WorkspaceArtifactResponse] = []
    for directory, dirnames, filenames in artifact_dir.walk(follow_symlinks=False):
        dirnames.sort()
        for filename in sorted(filenames):
            candidate = directory / filename
            try:
                if candidate.is_symlink():
                    continue
                resolved = candidate.resolve(strict=True)
                if not resolved.is_file() or not resolved.is_relative_to(root):
                    continue
                stat = resolved.stat()
            except OSError:
                continue
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
