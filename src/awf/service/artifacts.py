"""Safe filesystem access and shared metadata builders for workspace artifacts."""

from __future__ import annotations

import asyncio
import mimetypes
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from heapq import heappop, heappush
from itertools import count
from os import stat_result
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from awf.api.schemas import WorkspaceArtifactListResponse, WorkspaceArtifactResponse
from awf.common.config import get_settings
from awf.common.logging import get_logger
from awf.db.repositories import WorkspaceRepository
from awf.service.bounded_list import BoundedListPage, paginate_bounded_iterable

_log = get_logger(__name__)

DEFAULT_ARTIFACT_LIST_LIMIT = 50
MAX_ARTIFACT_LIST_LIMIT = 500
MAX_ARTIFACT_CONTENT_BYTES = 1_048_576

# Stable filenames for the per-workspace plan + conformance report deposited
# into the served artifact dir. The console labels artifacts by these names;
# ``artifact_kind`` stays suffix-based (``md`` / ``json``).
DEPOSITED_PLAN_NAME = "plan.md"
DEPOSITED_CONFORMANCE_NAME = "conformance.json"


class ArtifactPathError(ValueError):
    """Raised when a client-supplied artifact path is not a safe relative path."""


class ArtifactNotFoundError(FileNotFoundError):
    """Raised when an artifact cannot be safely read from managed storage."""


class ArtifactOversizedError(ValueError):
    """Raised when an artifact exceeds the requested or absolute byte-size limit."""

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    """Immutable artifact identity + filesystem metadata."""

    artifact_id: str
    workspace_id: str
    name: str
    relative_path: str
    path: Path
    kind: str
    size_bytes: int
    modified_at: datetime
    content_type: str


@dataclass(frozen=True, slots=True)
class DownloadableArtifact:
    """Resolved artifact ready for stream/download with its stat snapshot."""

    workspace_id: str
    name: str
    relative_path: str
    path: Path
    size_bytes: int
    modified_at: datetime
    content_type: str
    stat_result: stat_result


async def list_workspace_artifacts_metadata(
    session: AsyncSession,
    *,
    workspace_id: str,
    work_dir: str | Path | None = None,
    limit: int = DEFAULT_ARTIFACT_LIST_LIMIT,
    cursor: str | None = None,
) -> WorkspaceArtifactListResponse | None:
    """Return safe artifact metadata for one workspace, or ``None`` if missing."""

    if not await WorkspaceRepository(session).exists(workspace_id):
        return None
    artifact_dir = _workspace_artifact_dir(workspace_id, work_dir=work_dir)
    page = await asyncio.to_thread(
        _list_artifacts_page,
        workspace_id,
        artifact_dir,
        limit,
        cursor,
    )
    return WorkspaceArtifactListResponse(
        items=page.items,
        next_cursor=page.next_cursor,
        has_more=page.has_more,
        limit=page.limit,
        cursor=page.cursor,
    )


def workspace_artifact_dir(work_dir: str | Path, workspace_id: str) -> Path:
    """Return the managed artifact root for a workspace."""
    return Path(work_dir) / "artifacts" / workspace_id


def _workspace_artifact_dir(
    workspace_id: str,
    *,
    work_dir: str | Path | None = None,
) -> Path:
    root = Path(work_dir) if work_dir is not None else Path(get_settings().work_dir)
    return workspace_artifact_dir(root, workspace_id)


def deposit_workspace_planning_artifacts(
    *,
    work_dir: str | Path,
    workspace_id: str,
    worktree_path: Path,
    plan_path: Path,
    report_path: Path,
) -> None:
    """Best-effort copy of the worktree plan + conformance report into the
    served artifact dir under stable names.

    ``plan_path`` and ``report_path`` are worktree-relative. The served artifact
    dir (``work_dir/artifacts/{workspace_id}``) is a sibling of the worktree, so
    deposits made here survive successful-workspace teardown. Each file is
    copied independently (either may be absent) and overwritten on re-run.
    A copy failure is logged and skipped — depositing an inspectable artifact
    must never fail the workspace.
    """
    artifact_dir = workspace_artifact_dir(work_dir, workspace_id)
    worktree_root = worktree_path.resolve()
    for source, dest_name in (
        (worktree_path / plan_path, DEPOSITED_PLAN_NAME),
        (worktree_path / report_path, DEPOSITED_CONFORMANCE_NAME),
    ):
        _deposit_one_planning_artifact(
            artifact_dir=artifact_dir,
            workspace_id=workspace_id,
            source=source,
            dest_name=dest_name,
            worktree_root=worktree_root,
        )


def _deposit_one_planning_artifact(
    *,
    artifact_dir: Path,
    workspace_id: str,
    source: Path,
    dest_name: str,
    worktree_root: Path,
) -> None:
    try:
        # The source lives in an agent-writable worktree, so a symlinked
        # plan/report would otherwise let ``shutil.copyfile`` dereference an
        # arbitrary host-readable file into the served artifact dir. Mirror the
        # reader's symlink rejection here: refuse a symlink source outright and
        # require the resolved target to stay inside the worktree (guarding
        # against escape via an intermediate directory symlink).
        if source.is_symlink():
            _reject_unsafe_planning_source(workspace_id, source, dest_name, "symlink")
            return
        if not source.is_file():
            return
        resolved = source.resolve(strict=True)
        if not resolved.is_relative_to(worktree_root):
            _reject_unsafe_planning_source(workspace_id, source, dest_name, "escapes_worktree")
            return
        artifact_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(resolved, artifact_dir / dest_name)
    except OSError as exc:
        _log.warning(
            "service.planning_artifact_deposit_failed",
            workspace_id=workspace_id,
            source=str(source),
            dest_name=dest_name,
            error_type=type(exc).__name__,
            errno=exc.errno,
        )


def _reject_unsafe_planning_source(
    workspace_id: str,
    source: Path,
    dest_name: str,
    reason: str,
) -> None:
    _log.warning(
        "service.planning_artifact_deposit_rejected",
        workspace_id=workspace_id,
        source=str(source),
        dest_name=dest_name,
        reason=reason,
    )


def list_artifacts(workspace_id: str, artifact_dir: Path) -> list[ArtifactMetadata]:
    """List regular, non-symlink files below a managed artifact root."""

    return list(_iter_artifacts(workspace_id, artifact_dir))


def _iter_artifacts(workspace_id: str, artifact_dir: Path) -> Iterator[ArtifactMetadata]:
    try:
        root = _resolve_artifact_root(artifact_dir)
    except ArtifactNotFoundError:
        return

    pending: list[tuple[str, int, Path, bool]] = []
    sequence = count()
    _push_artifact_directory_entries(pending, sequence, artifact_dir, artifact_dir)
    while pending:
        _, _, candidate, is_directory = heappop(pending)
        if is_directory:
            try:
                if candidate.is_symlink():
                    continue
                resolved_directory = candidate.resolve(strict=True)
                if not resolved_directory.is_relative_to(root) or not resolved_directory.is_dir():
                    continue
            except OSError:
                continue
            _push_artifact_directory_entries(pending, sequence, artifact_dir, candidate)
            continue
        try:
            if candidate.is_symlink():
                continue
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(root) or not resolved.is_file():
                continue
            stat = resolved.stat()
        except OSError:
            continue
        relative_path = candidate.relative_to(artifact_dir).as_posix()
        yield _metadata_from_stat(workspace_id, relative_path, resolved, stat)


def _list_artifacts(workspace_id: str, artifact_dir: Path) -> list[WorkspaceArtifactResponse]:
    return [_artifact_response(item) for item in list_artifacts(workspace_id, artifact_dir)]


def _list_artifacts_page(
    workspace_id: str,
    artifact_dir: Path,
    limit: int,
    cursor: str | None,
) -> BoundedListPage[WorkspaceArtifactResponse]:
    return paginate_bounded_iterable(
        (_artifact_response(item) for item in _iter_artifacts(workspace_id, artifact_dir)),
        limit=limit,
        max_limit=MAX_ARTIFACT_LIST_LIMIT,
        cursor=cursor,
    )


def _push_artifact_directory_entries(
    pending: list[tuple[str, int, Path, bool]],
    sequence: Iterator[int],
    artifact_dir: Path,
    directory: Path,
) -> None:
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for candidate in entries:
        try:
            if candidate.is_symlink():
                continue
            is_directory = candidate.is_dir()
        except OSError:
            continue
        relative_path = candidate.relative_to(artifact_dir).as_posix()
        sort_key = f"{relative_path}/" if is_directory else relative_path
        heappush(pending, (sort_key, next(sequence), candidate, is_directory))


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


def get_downloadable_artifact(
    *,
    workspace_id: str,
    artifact_dir: Path,
    relative_path: str,
) -> DownloadableArtifact:
    """Resolve a client artifact path to a regular file below the managed root."""
    parts = _safe_relative_path_parts(relative_path)
    root = _resolve_artifact_root(artifact_dir)
    candidate = _join_without_symlink_components(artifact_dir, parts)
    try:
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ArtifactNotFoundError(relative_path)
        stat = resolved.stat()
    except OSError as exc:
        raise ArtifactNotFoundError(relative_path) from exc

    return DownloadableArtifact(
        workspace_id=workspace_id,
        name=resolved.name,
        relative_path="/".join(parts),
        path=resolved,
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
        content_type=content_type_for(resolved),
        stat_result=stat,
    )


def get_workspace_artifact_content(
    *,
    workspace_id: str,
    artifact_dir: Path,
    relative_path: str,
    limit_bytes: int,
) -> tuple[str, str, int, bytes]:
    """Read validated artifact bytes and return metadata + content.

    Returns ``(name, content_type, size_bytes, content)``.
    Raises ``ArtifactPathError``, ``ArtifactNotFoundError``, or
    ``ArtifactOversizedError``.
    """
    if limit_bytes > MAX_ARTIFACT_CONTENT_BYTES:
        raise ArtifactOversizedError(
            f"limit_bytes exceeds absolute maximum {MAX_ARTIFACT_CONTENT_BYTES}",
            detail={
                "limit_bytes": limit_bytes,
                "actual_bytes": None,  # file not read yet; size is unknown
            },
        )
    artifact = get_downloadable_artifact(
        workspace_id=workspace_id,
        artifact_dir=artifact_dir,
        relative_path=relative_path,
    )
    if artifact.stat_result.st_nlink > 1:
        raise ArtifactNotFoundError(relative_path)
    if artifact.size_bytes > limit_bytes:
        raise ArtifactOversizedError(
            f"artifact size {artifact.size_bytes} bytes exceeds limit {limit_bytes}",
            detail={
                "limit_bytes": limit_bytes,
                "actual_bytes": artifact.size_bytes,
            },
        )
    try:
        with artifact.path.open("rb") as f:
            content = f.read(limit_bytes + 1)
    except OSError as exc:
        raise ArtifactNotFoundError(
            f"artifact {artifact_id(workspace_id, relative_path)} (path={relative_path}) "
            f"not readable, limit_bytes={limit_bytes}"
        ) from exc
    if len(content) > limit_bytes:
        raise ArtifactOversizedError(
            f"artifact grew during read and exceeds limit {limit_bytes}",
            detail={
                "limit_bytes": limit_bytes,
                "actual_bytes": len(content),
            },
        )
    return (artifact.name, artifact.content_type, len(content), content)


def artifact_id(workspace_id: str, relative_path: str) -> str:
    digest = sha256(f"{workspace_id}\0{relative_path}".encode()).hexdigest()[:24]
    return f"art_{digest}"


def _artifact_id(workspace_id: str, relative_path: str) -> str:
    return artifact_id(workspace_id, relative_path)


def artifact_kind(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    return suffix or "file"


def _artifact_kind(path: Path) -> str:
    return artifact_kind(path)


def content_type_for(path: Path) -> str:
    content_type, _ = mimetypes.guess_type(path.name)
    return content_type or "application/octet-stream"


def _metadata_from_stat(
    workspace_id: str,
    relative_path: str,
    resolved: Path,
    stat: stat_result,
) -> ArtifactMetadata:
    return ArtifactMetadata(
        artifact_id=artifact_id(workspace_id, relative_path),
        workspace_id=workspace_id,
        name=resolved.name,
        relative_path=relative_path,
        path=resolved,
        kind=artifact_kind(resolved),
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, UTC),
        content_type=content_type_for(resolved),
    )


def _resolve_artifact_root(artifact_dir: Path) -> Path:
    try:
        if not artifact_dir.is_dir() or artifact_dir.is_symlink():
            raise ArtifactNotFoundError(str(artifact_dir))
        root = artifact_dir.resolve(strict=True)
        if not root.is_dir():
            raise ArtifactNotFoundError(str(artifact_dir))
    except OSError as exc:
        raise ArtifactNotFoundError(str(artifact_dir)) from exc
    return root


def _safe_relative_path_parts(relative_path: str) -> tuple[str, ...]:
    if not relative_path or "\x00" in relative_path or "\\" in relative_path:
        raise ArtifactPathError(relative_path)
    if relative_path.startswith("/"):
        raise ArtifactPathError(relative_path)
    parts = tuple(relative_path.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise ArtifactPathError(relative_path)
    return parts


def _join_without_symlink_components(artifact_dir: Path, parts: tuple[str, ...]) -> Path:
    current = artifact_dir
    for part in parts:
        current = current / part
        try:
            if current.is_symlink():
                raise ArtifactNotFoundError("/".join(parts))
        except OSError as exc:
            raise ArtifactNotFoundError("/".join(parts)) from exc
    return current


def _is_symlink(path: Path) -> bool:
    try:
        return path.is_symlink()
    except OSError:
        return True
