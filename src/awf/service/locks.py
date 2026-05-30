"""Read-only owned-path reservation and overlap-risk queries."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.owned_paths import interworkspace_owned_paths
from awf.db.enums import TaskClass, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    ACTIVE_OWNED_PATH_OVERLAP_STATUSES,
    owned_paths_overlap,
)


@dataclass(frozen=True)
class WorkspaceLockOverlapRisk:
    overlapping_workspace_id: str
    overlapping_owned_path: str
    owned_path: str


@dataclass(frozen=True)
class WorkspaceLock:
    workspace_id: str
    title: str
    agent: str
    status: str
    repo_url: str
    branch_base: str
    task_class: str | None
    owned_paths: tuple[str, ...]
    pr_url: str | None
    created_at: datetime
    updated_at: datetime
    overlap_risks: tuple[WorkspaceLockOverlapRisk, ...] = field(
        default_factory=tuple,
        hash=False,
    )


@dataclass(frozen=True)
class WorkspaceLockPage:
    items: list[WorkspaceLock]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True)
class _DecodedCursor:
    created_at: datetime
    workspace_id: str


@dataclass(frozen=True)
class _OverlapWorkspace:
    workspace_id: str
    repo_url: str
    branch_base: str
    owned_paths: tuple[str, ...]


class InvalidWorkspaceLockCursorError(ValueError):
    """Raised when a workspace lock pagination cursor cannot be decoded."""


async def list_workspace_locks(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repo_url: str | None = None,
    task_class: TaskClass | str | None = None,
    status: WorkspaceStatus | str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> list[WorkspaceLock]:
    """List workspace owned-path reservations, newest first.

    With no explicit status, the listing shows active owned-path reservations
    so operators can see coordination hints and overlap risk.
    """
    page = await list_workspace_lock_page(
        session_factory,
        repo_url=repo_url,
        task_class=task_class,
        status=status,
        limit=limit,
        cursor=cursor,
    )
    return page.items


async def list_workspace_lock_page(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repo_url: str | None = None,
    task_class: TaskClass | str | None = None,
    status: WorkspaceStatus | str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> WorkspaceLockPage:
    """List one page of workspace owned-path reservations, newest first."""
    async with session_factory() as session:
        return await list_workspace_lock_page_for_session(
            session,
            repo_url=repo_url,
            task_class=task_class,
            status=status,
            limit=limit,
            cursor=cursor,
        )


async def list_workspace_locks_for_session(
    session: AsyncSession,
    *,
    repo_url: str | None = None,
    task_class: TaskClass | str | None = None,
    status: WorkspaceStatus | str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> list[WorkspaceLock]:
    page = await list_workspace_lock_page_for_session(
        session,
        repo_url=repo_url,
        task_class=task_class,
        status=status,
        limit=limit,
        cursor=cursor,
    )
    return page.items


async def list_workspace_lock_page_for_session(
    session: AsyncSession,
    *,
    repo_url: str | None = None,
    task_class: TaskClass | str | None = None,
    status: WorkspaceStatus | str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> WorkspaceLockPage:
    status_value = _status_value(status)
    task_class_value = _task_class_value(task_class)
    decoded_cursor = _decode_cursor(cursor)

    stmt = select(Workspace)
    if status_value is None:
        stmt = stmt.where(Workspace.status.in_(ACTIVE_OWNED_PATH_OVERLAP_STATUSES))
    else:
        stmt = stmt.where(Workspace.status == status_value)
    if repo_url is not None:
        stmt = stmt.where(Workspace.repo_url == repo_url)
    if task_class_value is not None:
        stmt = stmt.where(Workspace.task_class == task_class_value)
    if decoded_cursor is not None:
        stmt = stmt.where(
            or_(
                Workspace.created_at < decoded_cursor.created_at,
                and_(
                    Workspace.created_at == decoded_cursor.created_at,
                    Workspace.id < decoded_cursor.workspace_id,
                ),
            )
        )
    stmt = stmt.order_by(Workspace.created_at.desc(), Workspace.id.desc()).limit(limit + 1)

    rows = (await session.execute(stmt)).scalars().all()
    page_rows = list(rows[:limit])
    has_more = len(rows) > limit
    overlap_candidates = await _active_overlap_candidates(session, page_rows)
    overlap_risks_by_workspace = await _workspace_overlap_risks_for_page(
        page_rows,
        overlap_candidates=overlap_candidates,
    )
    return WorkspaceLockPage(
        items=[
            _workspace_lock(
                row,
                overlap_risks=overlap_risks_by_workspace.get(row.id, ()),
            )
            for row in page_rows
        ],
        next_cursor=_encode_cursor(page_rows[-1]) if has_more and page_rows else None,
        has_more=has_more,
    )


async def _active_overlap_candidates(
    session: AsyncSession,
    page_rows: list[Workspace],
) -> list[Workspace]:
    repo_bases = {(row.repo_url, row.branch_base) for row in page_rows}
    if not repo_bases:
        return []

    stmt = select(Workspace).where(
        Workspace.status.in_(ACTIVE_OWNED_PATH_OVERLAP_STATUSES),
        or_(
            *[
                and_(
                    Workspace.repo_url == repo_url,
                    Workspace.branch_base == branch_base,
                )
                for repo_url, branch_base in sorted(repo_bases)
            ]
        ),
    )
    stmt = stmt.order_by(Workspace.created_at.asc(), Workspace.id.asc())
    return list((await session.execute(stmt)).scalars())


def _workspace_lock(
    workspace: Workspace,
    *,
    overlap_risks: tuple[WorkspaceLockOverlapRisk, ...],
) -> WorkspaceLock:
    return WorkspaceLock(
        workspace_id=workspace.id,
        title=workspace.task_title,
        agent=workspace.agent,
        status=workspace.status,
        repo_url=workspace.repo_url,
        branch_base=workspace.branch_base,
        task_class=workspace.task_class,
        owned_paths=tuple(workspace.owned_paths),
        overlap_risks=overlap_risks,
        pr_url=workspace.pr_url,
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
    )


async def _workspace_overlap_risks_for_page(
    page_rows: list[Workspace],
    *,
    overlap_candidates: list[Workspace],
) -> dict[str, tuple[WorkspaceLockOverlapRisk, ...]]:
    if not page_rows or not overlap_candidates:
        return {}
    return await asyncio.to_thread(
        _workspace_overlap_risks_by_id,
        tuple(_overlap_workspace(row) for row in page_rows),
        tuple(_overlap_workspace(candidate) for candidate in overlap_candidates),
    )


def _overlap_workspace(workspace: Workspace) -> _OverlapWorkspace:
    return _OverlapWorkspace(
        workspace_id=workspace.id,
        repo_url=workspace.repo_url,
        branch_base=workspace.branch_base,
        owned_paths=tuple(workspace.owned_paths),
    )


def _workspace_overlap_risks_by_id(
    workspaces: tuple[_OverlapWorkspace, ...],
    overlap_candidates: tuple[_OverlapWorkspace, ...],
) -> dict[str, tuple[WorkspaceLockOverlapRisk, ...]]:
    """Group advisory overlap risks by workspace using inter-workspace paths."""
    candidates_by_repo_base: dict[
        tuple[str, str],
        list[tuple[_OverlapWorkspace, tuple[str, ...]]],
    ] = {}
    for candidate in overlap_candidates:
        key = (candidate.repo_url, candidate.branch_base)
        candidates_by_repo_base.setdefault(key, []).append(
            (candidate, interworkspace_owned_paths(candidate.owned_paths))
        )

    risks_by_workspace: dict[str, tuple[WorkspaceLockOverlapRisk, ...]] = {}
    for workspace in workspaces:
        risks: list[WorkspaceLockOverlapRisk] = []
        owned_paths = interworkspace_owned_paths(workspace.owned_paths)
        key = (workspace.repo_url, workspace.branch_base)
        candidate_entries = candidates_by_repo_base.get(key)
        if candidate_entries is None:
            continue
        for other, overlapping_owned_paths in candidate_entries:
            if other.workspace_id == workspace.workspace_id:
                continue
            for overlapping_owned_path in overlapping_owned_paths:
                for owned_path in owned_paths:
                    if owned_paths_overlap(overlapping_owned_path, owned_path):
                        risks.append(
                            WorkspaceLockOverlapRisk(
                                overlapping_workspace_id=other.workspace_id,
                                overlapping_owned_path=overlapping_owned_path,
                                owned_path=owned_path,
                            )
                        )
        if risks:
            risks_by_workspace[workspace.workspace_id] = tuple(risks)
    return risks_by_workspace


def _status_value(status: WorkspaceStatus | str | None) -> str | None:
    if status is None:
        return None
    return status.value if isinstance(status, WorkspaceStatus) else status


def _task_class_value(task_class: TaskClass | str | None) -> str | None:
    if task_class is None:
        return None
    return task_class.value if isinstance(task_class, TaskClass) else task_class


def _encode_cursor(workspace: Workspace) -> str:
    payload = {
        "created_at": workspace.created_at.isoformat(),
        "workspace_id": workspace.id,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return encoded.decode("ascii")


def _decode_cursor(cursor: str | None) -> _DecodedCursor | None:
    if cursor is None:
        return None
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(decoded.decode("utf-8"))
        created_at = datetime.fromisoformat(payload["created_at"])
        workspace_id = payload["workspace_id"]
    except (
        binascii.Error,
        KeyError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise InvalidWorkspaceLockCursorError("Invalid workspace lock cursor") from exc
    if not isinstance(workspace_id, str) or workspace_id == "":
        raise InvalidWorkspaceLockCursorError("Invalid workspace lock cursor")
    return _DecodedCursor(created_at=created_at, workspace_id=workspace_id)
