"""Non-transition row-metadata writers (activity stamps and human-attention flags).

These helpers write out-of-band metadata on an existing workspace row without
performing a state transition: they deliberately do NOT bump ``version`` and do
not route through the state machine. They are kept as session-first free
functions so the ``WorkspaceRepository`` class stays a thin delegating shell.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.attention_events import (
    ATTENTION_CLEARED_EVENT_TYPE,
    ATTENTION_REQUIRED_EVENT_TYPE,
    monitoring_pr_attention_payload,
)
from awf.db.enums import WorkspaceStatus
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

    Episode start (``awaiting_human_since``) flips only via a guarded UPDATE
    ``WHERE awaiting_human_since IS NULL AND status = monitoring_pr``, so
    concurrent first-time escalations cannot each observe NULL and double-emit,
    and a cancel/stop/destroy that already left ``monitoring_pr`` (and cleared
    attention) cannot reopen the episode when a blocked enter UPDATE is
    re-evaluated. Reason is always refreshed to the latest escalation message
    (clamped to the column length so an unbounded operator-hint reason cannot
    abort the write). This is an out-of-band metadata flag on a still-polling
    ``monitoring_pr`` row — it deliberately does NOT bump ``version`` (it is
    not a state transition).

    Emits ``workspace.attention_required`` exactly once when the guarded enter
    UPDATE flips the row. Reason-only refreshes (and lost enter races) do not emit.
    Reason refreshes are fenced to the episode start observed when enter lost so a
    concurrent clear (or clear + new episode enter) between lost enter and refresh
    cannot orphan reason text on a cleared row or overwrite a newer episode's reason.
    """
    # Late import: ``WorkspaceRepository`` loads this module at class build time.
    from awf.db.repositories.workspace_repo import WorkspaceRepository

    repo = WorkspaceRepository(session)
    workspace = await repo.get(workspace_id)
    if workspace is None:
        return
    # synchronize_session=False keeps the identity-mapped instance from being
    # expired (async lazy-load would otherwise raise MissingGreenlet when
    # building the event payload).
    pr_url = workspace.pr_url
    clamped_reason = reason[:_AWAITING_HUMAN_REASON_MAX_LENGTH]
    # Episode start known before enter; used to fence a later reason-only refresh
    # to the same attention episode (not a replacement opened after a clear).
    observed_since = workspace.awaiting_human_since
    enter_result = await session.execute(
        update(Workspace)
        .where(
            Workspace.id == workspace_id,
            Workspace.awaiting_human_since.is_(None),
            Workspace.status == WorkspaceStatus.monitoring_pr.value,
        )
        .values(
            awaiting_human_since=now,
            awaiting_human_reason=clamped_reason,
        )
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    # Gate the event on the guarded UPDATE flip, not the pre-update identity-map
    # read: another session may have already entered, leaving rowcount 0.
    enter_rowcount = getattr(enter_result, "rowcount", 0)
    entered = enter_rowcount is not None and enter_rowcount > 0
    if entered:
        workspace.awaiting_human_since = now
        workspace.awaiting_human_reason = clamped_reason
        await repo.add_event(
            workspace,
            event_type=ATTENTION_REQUIRED_EVENT_TYPE,
            payload=monitoring_pr_attention_payload(
                reason=clamped_reason,
                pr_url=pr_url,
            ),
        )
        return

    # Already in an episode (or lost the enter race): refresh reason only so
    # episode start stays owned by the winning writer. Fence to the observed
    # episode start so a concurrent clear (or clear + re-enter) between enter
    # and refresh cannot orphan reason or overwrite a newer episode.
    if observed_since is None:
        # Stale in-memory clear while another session already entered: snapshot
        # the winner's episode start before the fenced refresh.
        workspace = await repo.get(workspace_id, populate_existing=True)
        if workspace is None:
            return
        observed_since = workspace.awaiting_human_since
        if observed_since is None:
            workspace.awaiting_human_reason = None
            return

    refresh_result = await session.execute(
        update(Workspace)
        .where(
            Workspace.id == workspace_id,
            Workspace.awaiting_human_since == observed_since,
        )
        .values(awaiting_human_reason=clamped_reason)
        .execution_options(synchronize_session=False)
    )
    await session.flush()
    refresh_rowcount = getattr(refresh_result, "rowcount", 0)
    if refresh_rowcount is not None and refresh_rowcount > 0:
        workspace.awaiting_human_reason = clamped_reason
    else:
        # Episode cleared or replaced: realign from DB (do not assume cleared).
        workspace = await repo.get(workspace_id, populate_existing=True)


async def clear_workspace_attention(session: AsyncSession, workspace_id: str) -> None:
    """Clear the awaiting-human attention flag once the monitor resumes.

    Guarded by ``awaiting_human_since IS NOT NULL`` so the per-poll clear is
    a DB-level no-op (no row churn, no spurious ``updated_at`` bump) when the
    flag is already clear.

    Emits ``workspace.attention_cleared`` exactly once when an episode ends
    (a guarded clear updates the row). A second clear while already clear is
    a no-op for both columns and events.

    Always executes the guarded UPDATE — never skip based on a cached
    ``awaiting_human_since`` read. guide/remonitor may hold an identity-mapped
    row loaded while attention was clear; another transaction can open an
    episode before this call, and trusting the stale clear would bypass the
    atomic guard and leave the persisted flag active.
    """
    from awf.db.repositories.workspace_repo import WorkspaceRepository

    repo = WorkspaceRepository(session)
    # Refresh so prior reason/pr_url are not taken from a stale identity-map
    # clear while another transaction has since opened an episode.
    workspace = await repo.get(workspace_id, populate_existing=True)
    if workspace is None:
        return
    prior_reason = workspace.awaiting_human_reason
    pr_url = workspace.pr_url
    result = await session.execute(
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
    # Gate the event on the guarded UPDATE flip, not the pre-update identity-map
    # read: another session may have already cleared, leaving rowcount 0.
    rowcount = getattr(result, "rowcount", 0)
    if rowcount is None or rowcount <= 0:
        workspace.awaiting_human_since = None
        workspace.awaiting_human_reason = None
        return
    workspace.awaiting_human_since = None
    workspace.awaiting_human_reason = None
    await repo.add_event(
        workspace,
        event_type=ATTENTION_CLEARED_EVENT_TYPE,
        payload=monitoring_pr_attention_payload(reason=prior_reason, pr_url=pr_url),
    )
