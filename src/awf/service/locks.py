"""Read-only lock reservation queries for operator visibility."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.enums import TaskClass, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import ACTIVE_OWNED_PATH_CONFLICT_STATUSES


@dataclass(frozen=True)
class WorkspaceLock:
    workspace_id: str
    title: str
    agent: str
    status: str
    repo_url: str
    branch_base: str
    task_class: str | None
    owned_paths: list[str]
    pr_url: str | None
    created_at: datetime
    updated_at: datetime


async def list_workspace_locks(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repo_url: str | None = None,
    task_class: TaskClass | str | None = None,
    status: WorkspaceStatus | str | None = None,
    limit: int = 50,
) -> list[WorkspaceLock]:
    """List workspace lock reservations, newest first.

    With no explicit status, the listing mirrors the owned-path admission policy
    and shows only workspaces that still block new overlapping reservations.
    """
    async with session_factory() as session:
        return await list_workspace_locks_for_session(
            session,
            repo_url=repo_url,
            task_class=task_class,
            status=status,
            limit=limit,
        )


async def list_workspace_locks_for_session(
    session: AsyncSession,
    *,
    repo_url: str | None = None,
    task_class: TaskClass | str | None = None,
    status: WorkspaceStatus | str | None = None,
    limit: int = 50,
) -> list[WorkspaceLock]:
    status_value = _status_value(status)
    task_class_value = _task_class_value(task_class)

    stmt = select(Workspace)
    if status_value is None:
        stmt = stmt.where(Workspace.status.in_(ACTIVE_OWNED_PATH_CONFLICT_STATUSES))
    else:
        stmt = stmt.where(Workspace.status == status_value)
    if repo_url is not None:
        stmt = stmt.where(Workspace.repo_url == repo_url)
    if task_class_value is not None:
        stmt = stmt.where(Workspace.task_class == task_class_value)
    stmt = stmt.order_by(Workspace.created_at.desc(), Workspace.id.desc()).limit(limit)

    rows = list((await session.execute(stmt)).scalars())
    return [_workspace_lock(row) for row in rows]


def _workspace_lock(workspace: Workspace) -> WorkspaceLock:
    return WorkspaceLock(
        workspace_id=workspace.id,
        title=workspace.task_title,
        agent=workspace.agent,
        status=workspace.status,
        repo_url=workspace.repo_url,
        branch_base=workspace.branch_base,
        task_class=workspace.task_class,
        owned_paths=list(workspace.owned_paths),
        pr_url=workspace.pr_url,
        created_at=workspace.created_at,
        updated_at=workspace.updated_at,
    )


def _status_value(status: WorkspaceStatus | str | None) -> str | None:
    if status is None:
        return None
    return status.value if isinstance(status, WorkspaceStatus) else status


def _task_class_value(task_class: TaskClass | str | None) -> str | None:
    if task_class is None:
        return None
    return task_class.value if isinstance(task_class, TaskClass) else task_class
