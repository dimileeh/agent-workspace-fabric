"""Non-transition row-metadata writers (activity stamps and human-attention flags).

These helpers write out-of-band metadata on an existing workspace row without
performing a state transition: they deliberately do NOT bump ``version`` and do
not route through the state machine. They are kept as session-first free
functions so the ``WorkspaceRepository`` class stays a thin delegating shell.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.models import Workspace


async def update_activity(
    session: AsyncSession,
    workspace_id: str,
    *,
    subphase: str | None = None,
) -> None:
    """Stamp recent workspace activity and optionally update its subphase."""
    stmt = (
        update(Workspace)
        .where(Workspace.id == workspace_id)
        .values(
            last_activity_at=datetime.now(UTC),
        )
    )
    if subphase is not None:
        stmt = stmt.values(subphase=subphase)
    await session.execute(stmt)
    await session.flush()


async def set_workspace_attention(
    session: AsyncSession,
    workspace_id: str,
    *,
    reason: str,
    now: datetime,
) -> None:
    """Flag a workspace as awaiting human attention (a HUMAN_WAIT escalation).

    ``COALESCE`` keeps the episode start (``awaiting_human_since``) stable
    across repeated ``NotifyHuman`` for the same ongoing block, while the
    reason is always refreshed to the latest escalation message. This is an
    out-of-band metadata flag on a still-polling ``monitoring_pr`` row — it
    deliberately does NOT bump ``version`` (it is not a state transition).
    """
    await session.execute(
        update(Workspace)
        .where(Workspace.id == workspace_id)
        .values(
            awaiting_human_since=func.coalesce(Workspace.awaiting_human_since, now),
            awaiting_human_reason=reason,
        )
    )
    await session.flush()


async def clear_workspace_attention(session: AsyncSession, workspace_id: str) -> None:
    """Clear the awaiting-human attention flag once the monitor resumes.

    Guarded by ``awaiting_human_since IS NOT NULL`` so the per-poll clear is
    a DB-level no-op (no row churn, no spurious ``updated_at`` bump) when the
    flag is already clear.
    """
    await session.execute(
        update(Workspace)
        .where(
            Workspace.id == workspace_id,
            Workspace.awaiting_human_since.is_not(None),
        )
        .values(
            awaiting_human_since=None,
            awaiting_human_reason=None,
        )
    )
    await session.flush()
