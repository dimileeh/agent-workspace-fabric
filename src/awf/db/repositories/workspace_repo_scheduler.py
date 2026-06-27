"""Workspace repository helpers for scheduler candidate selection."""

from __future__ import annotations

import builtins
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories._scheduler import (
    _monitoring_pr_deferred_active_execution_claim_stmt,
    _schedulable_workspace_ids_stmt,
    _scheduler_scoring_time,
)
from awf.db.repositories.system_repo import WorkerHeartbeatRepository


async def list_schedulable_ids(
    session: AsyncSession,
    dialect_name: str | None,
    *,
    status: WorkspaceStatus,
    limit: int,
    exclude_ids: set[str] | None = None,
    node_id: str | None = None,
    after: Any | None = None,
    scoring_at: datetime | None = None,
    execution_claim_owner_id: str | None = None,
) -> builtins.list[str]:
    """Return schedulable workspace IDs in scheduler priority order."""
    if limit <= 0:
        return []

    scoring_time = _scheduler_scoring_time(after=after, scoring_at=scoring_at)
    candidates = await list_schedulable_candidates(
        session,
        dialect_name,
        status=status,
        limit=limit,
        exclude_ids=exclude_ids,
        node_id=node_id,
        after=after,
        scoring_at=scoring_time,
        execution_claim_owner_id=execution_claim_owner_id,
    )
    return [
        workspace.id
        for workspace in sort_schedulable_workspaces(
            candidates,
            limit,
            scoring_at=scoring_time,
        )
    ]


async def list_schedulable_workspaces(
    session: AsyncSession,
    dialect_name: str | None,
    *,
    status: WorkspaceStatus,
    limit: int,
    exclude_ids: set[str] | None = None,
    node_id: str | None = None,
    after: Any | None = None,
    scoring_at: datetime | None = None,
    execution_claim_owner_id: str | None = None,
) -> builtins.list[Workspace]:
    """Return schedulable workspaces in scheduler priority order."""
    if limit <= 0:
        return []

    scoring_time = _scheduler_scoring_time(after=after, scoring_at=scoring_at)
    candidates = await list_schedulable_candidates(
        session,
        dialect_name,
        status=status,
        limit=limit,
        exclude_ids=exclude_ids,
        node_id=node_id,
        after=after,
        scoring_at=scoring_time,
        execution_claim_owner_id=execution_claim_owner_id,
    )
    return sort_schedulable_workspaces(candidates, limit, scoring_at=scoring_time)


async def list_monitoring_pr_deferred_active_execution_claim_workspaces(
    session: AsyncSession,
    dialect_name: str | None,
    *,
    limit: int,
    claim_cutoff: datetime,
    owner_id: str,
    exclude_ids: set[str] | None = None,
    node_id: str | None = None,
    scoring_at: datetime | None = None,
) -> builtins.list[Workspace]:
    """Return monitor rows blocked by another worker's active execution claim."""
    if limit <= 0:
        return []

    scoring_time = scoring_at or claim_cutoff
    fresh_execution_claim_owner_ids = await WorkerHeartbeatRepository(
        session
    ).list_fresh_worker_ids(now=claim_cutoff)
    stmt = _monitoring_pr_deferred_active_execution_claim_stmt(
        limit=limit,
        exclude_ids=exclude_ids,
        node_id=node_id,
        scoring_at=scoring_time,
        dialect_name=dialect_name,
        claim_cutoff=claim_cutoff,
        execution_claim_owner_id=owner_id,
        fresh_execution_claim_owner_ids=fresh_execution_claim_owner_ids,
        skip_locked=dialect_name == "postgresql",
    )
    result = await session.execute(stmt)
    candidates = list(result.scalars().all())
    return sort_schedulable_workspaces(candidates, limit, scoring_at=scoring_time)


async def list_schedulable_candidates(
    session: AsyncSession,
    dialect_name: str | None,
    *,
    status: WorkspaceStatus,
    limit: int | None,
    exclude_ids: set[str] | None = None,
    node_id: str | None = None,
    after: Any | None = None,
    scoring_at: datetime,
    execution_claim_owner_id: str | None = None,
) -> builtins.list[Workspace]:
    claim_cutoff = datetime.now(UTC) if status == WorkspaceStatus.monitoring_pr else None
    fresh_execution_claim_owner_ids = (
        await WorkerHeartbeatRepository(session).list_fresh_worker_ids(now=claim_cutoff)
        if claim_cutoff is not None
        else None
    )
    stmt = _schedulable_workspace_ids_stmt(
        status=status,
        limit=limit,
        exclude_ids=exclude_ids,
        node_id=node_id,
        after=after,
        scoring_at=scoring_at,
        dialect_name=dialect_name,
        skip_locked=dialect_name == "postgresql",
        claim_cutoff=claim_cutoff,
        execution_claim_owner_id=execution_claim_owner_id,
        fresh_execution_claim_owner_ids=fresh_execution_claim_owner_ids,
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


def sort_schedulable_workspaces(
    candidates: builtins.list[Workspace],
    limit: int | None,
    *,
    scoring_at: datetime,
) -> builtins.list[Workspace]:
    from awf.service.scheduler import scheduler_score_from_workspace

    def _get_sort_key(item: tuple[Any, Workspace]) -> tuple[Any, ...]:
        score, ws = item
        return (
            -score.class_priority,
            -score.effective_score,
            ws.created_at or datetime.min,
            ws.id,
        )

    scored = sorted(
        (
            (scheduler_score_from_workspace(workspace, now=scoring_at), workspace)
            for workspace in candidates
        ),
        key=_get_sort_key,
    )
    ordered = [workspace for _score, workspace in scored]
    return ordered if limit is None else ordered[:limit]
