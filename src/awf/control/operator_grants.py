"""Shared epoch-scoped operator grant-spec loader.

WS-1 added ``_active_operator_grant_specs`` on the executor so a blocked-resume
honors operator grants. WS-2's PR monitor needs the SAME query to make its
protected-scope push checks grant-aware. To avoid a second grant model, the
query body lives here as a session-bound function both callers delegate to:

- the executor method (``control/executor/state_ops.py``) opens its session and
  calls this;
- the monitor runner method opens ``self._deps.session_factory`` and calls this.

A grant authorizes the protected gate only while it is unconsumed, unrevoked, and
scoped to the workspace's CURRENT ``block_epoch`` (a re-block bumps the epoch and
invalidates prior grants). Empty for a normal run, so threading it into every
gate caller is a no-op outside the resume path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from awf.control.quality_gates import GrantSpec
from awf.db.models import OperatorGrantAuditRecord
from awf.db.repositories import WorkspaceRepository


async def active_operator_grant_specs_in_session(
    session: Any, workspace_id: str
) -> list[GrantSpec]:
    """Return the operator grants active for the workspace's current block epoch."""
    ws = await WorkspaceRepository(session).get(workspace_id)
    if ws is None:
        return []
    rows = await session.execute(
        select(OperatorGrantAuditRecord).where(
            OperatorGrantAuditRecord.workspace_id == workspace_id,
            OperatorGrantAuditRecord.consumed_at.is_(None),
            OperatorGrantAuditRecord.revoked_at.is_(None),
            OperatorGrantAuditRecord.block_epoch == ws.block_epoch,
        )
    )
    return [
        GrantSpec(
            path=record.normalized_path,
            approve_policy_downgrade=record.approve_policy_downgrade,
        )
        for record in rows.scalars().all()
    ]


async def consume_active_operator_grants_in_session(session: Any, workspace_id: str) -> int:
    """Mark the workspace's active (current-epoch) operator grants consumed.

    Single-use semantics: once a resumed workspace clears the protected gate, a
    later DIFFERENT change to the same file must be granted again. Returns the
    number of grants consumed. Shared by the executor (pre-PR resume) and the PR
    monitor (post-PR resume) so both honor identical single-use behavior."""
    now = datetime.now(UTC)
    ws = await WorkspaceRepository(session).get(workspace_id)
    if ws is None:
        return 0
    rows = await session.execute(
        select(OperatorGrantAuditRecord).where(
            OperatorGrantAuditRecord.workspace_id == workspace_id,
            OperatorGrantAuditRecord.consumed_at.is_(None),
            OperatorGrantAuditRecord.revoked_at.is_(None),
            OperatorGrantAuditRecord.block_epoch == ws.block_epoch,
        )
    )
    consumed = 0
    for record in rows.scalars().all():
        record.consumed_at = now
        consumed += 1
    return consumed
