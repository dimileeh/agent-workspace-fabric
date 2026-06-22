"""Idempotency-key lookups, replay-key sampling, and PR-adoption history helpers.

Session-first free functions that back the thin ``WorkspaceRepository`` wrapper
methods. They read workspace rows keyed by idempotency identity and acquire the
PostgreSQL advisory lock that serializes idempotency decisions.
"""

from __future__ import annotations

import builtins

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from awf.db.models import Workspace
from awf.db.repositories.base import (
    DEFAULT_IDEMPOTENCY_REPLAY_KEY_LIMIT,
    _matches_pr_adoption_identity,
    _workspace_idempotency_advisory_lock_key,
)


async def get_by_idempotency_key(session: AsyncSession, key: str) -> Workspace | None:
    """Return a workspace created for an idempotency key."""
    stmt = (
        select(Workspace)
        .where(Workspace.idempotency_key == key)
        .options(selectinload(Workspace.task_attempt))
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def has_idempotency_key(session: AsyncSession, key: str) -> bool:
    """Return whether an idempotency key already has a workspace."""
    stmt = select(Workspace.id).where(Workspace.idempotency_key == key).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def list_idempotency_replay_keys(
    session: AsyncSession,
    *,
    limit: int = DEFAULT_IDEMPOTENCY_REPLAY_KEY_LIMIT,
) -> builtins.list[str]:
    """Return a bounded replay-key sample for non-request-path cache support.

    Fresh request admission paths use exact-key probes instead of this helper
    so over-limit requests cannot trigger broad replay-key warmups.
    """
    if limit <= 0:
        return []
    stmt = (
        select(Workspace.idempotency_key)
        .where(Workspace.idempotency_key.is_not(None))
        .order_by(Workspace.created_at.asc(), Workspace.id.asc())
        .limit(limit)
    )
    return [key for key in (await session.execute(stmt)).scalars().all() if key]


async def list_idempotency_key_family(
    session: AsyncSession,
    logical_key: str,
) -> builtins.list[str]:
    """Return the logical idempotency key and any generation-suffixed keys."""
    from awf.db.utils import escape_like_pattern as _escape_like_pattern

    generation_pattern = f"{_escape_like_pattern(logical_key)}:g%"
    stmt = (
        select(Workspace.idempotency_key)
        .where(
            or_(
                Workspace.idempotency_key == logical_key,
                Workspace.idempotency_key.like(generation_pattern, escape="\\"),
            )
        )
        .order_by(Workspace.idempotency_key.asc())
    )
    keys = (await session.execute(stmt)).scalars().all()
    return [key for key in keys if key is not None]


async def list_pr_adoption_history(
    session: AsyncSession,
    *,
    task_external_id: str,
    idempotency_key: str,
    task_kind: str,
    repo_slug: str,
    pr_number: int,
) -> builtins.list[Workspace]:
    """List workspaces that represent adoption history for one repo/PR."""
    adoption_repo_slug = Workspace.task_policy["pr_adoption"]["repo_slug"].as_string()
    stmt = (
        select(Workspace)
        .options(selectinload(Workspace.task_attempt))
        .where(
            or_(
                Workspace.task_external_id == task_external_id,
                Workspace.idempotency_key == idempotency_key,
                and_(
                    Workspace.task_kind == task_kind,
                    Workspace.pr_number == pr_number,
                    func.lower(adoption_repo_slug) == repo_slug.lower(),
                ),
            )
        )
        .order_by(Workspace.created_at.asc(), Workspace.id.asc())
    )
    candidates = list((await session.execute(stmt)).scalars())
    return [
        workspace
        for workspace in candidates
        if _matches_pr_adoption_identity(
            workspace,
            task_external_id=task_external_id,
            idempotency_key=idempotency_key,
            task_kind=task_kind,
            repo_slug=repo_slug,
            pr_number=pr_number,
        )
    ]


async def acquire_idempotency_key_lock(session: AsyncSession, key: str) -> None:
    """Serialize workspace idempotency decisions with a PostgreSQL advisory lock."""
    lock_key = _workspace_idempotency_advisory_lock_key(key)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": lock_key},
    )
