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

from awf.common.attention_events import (
    ATTENTION_CLEARED_EVENT_TYPE,
    ATTENTION_REQUIRED_EVENT_TYPE,
    monitoring_pr_attention_payload,
)
from awf.db.models import Workspace

# ``Workspace.awaiting_human_reason`` is a ``String(2048)`` column. Escalation
# reasons can carry unbounded text (an operator-hint repair surfaces raw push
# stderr/``str(exc)`` as the ``NotifyHuman`` reason), and on Postgres a value
# longer than the column raises ``StringDataRightTruncation`` — which would abort
# the monitor before the human-notification path completes. Clamp at this write
# boundary, mirroring the other ``[:limit]`` clamps in the repository layer.
_AWAITING_HUMAN_REASON_MAX_LENGTH = 2048


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
    reason is always refreshed to the latest escalation message (clamped to the
    column length so an unbounded operator-hint reason cannot abort the write).
    This is an out-of-band metadata flag on a still-polling ``monitoring_pr``
    row — it deliberately does NOT bump ``version`` (it is not a state transition).

    Emits ``workspace.attention_required`` exactly once when entering an episode
    (prior ``awaiting_human_since`` was NULL). Reason-only refreshes do not emit.
    """
    # Late import: ``WorkspaceRepository`` loads this module at class build time.
    from awf.db.repositories.workspace_repo import WorkspaceRepository

    repo = WorkspaceRepository(session)
    workspace = await repo.get(workspace_id)
    if workspace is None:
        return
    # Read flip inputs before the Core UPDATE. synchronize_session=False keeps
    # the identity-mapped instance from being expired (async lazy-load would
    # otherwise raise MissingGreenlet when building the event payload).
    entering = workspace.awaiting_human_since is None
    pr_url = workspace.pr_url
    clamped_reason = reason[:_AWAITING_HUMAN_REASON_MAX_LENGTH]
    await session.execute(
        update(Workspace)
        .where(Workspace.id == workspace_id)
        .values(
            awaiting_human_since=func.coalesce(Workspace.awaiting_human_since, now),
            awaiting_human_reason=clamped_reason,
        )
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    if entering:
        workspace.awaiting_human_since = now
    workspace.awaiting_human_reason = clamped_reason
    if entering:
        await repo.add_event(
            workspace,
            event_type=ATTENTION_REQUIRED_EVENT_TYPE,
            payload=monitoring_pr_attention_payload(
                reason=clamped_reason,
                pr_url=pr_url,
            ),
        )


async def clear_workspace_attention(session: AsyncSession, workspace_id: str) -> None:
    """Clear the awaiting-human attention flag once the monitor resumes.

    Guarded by ``awaiting_human_since IS NOT NULL`` so the per-poll clear is
    a DB-level no-op (no row churn, no spurious ``updated_at`` bump) when the
    flag is already clear.

    Emits ``workspace.attention_cleared`` exactly once when an episode ends
    (a guarded clear updates the row). A second clear while already clear is
    a no-op for both columns and events.
    """
    from awf.db.repositories.workspace_repo import WorkspaceRepository

    repo = WorkspaceRepository(session)
    workspace = await repo.get(workspace_id)
    if workspace is None or workspace.awaiting_human_since is None:
        return
    prior_reason = workspace.awaiting_human_reason
    pr_url = workspace.pr_url
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
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    workspace.awaiting_human_since = None
    workspace.awaiting_human_reason = None
    await repo.add_event(
        workspace,
        event_type=ATTENTION_CLEARED_EVENT_TYPE,
        payload=monitoring_pr_attention_payload(reason=prior_reason, pr_url=pr_url),
    )
