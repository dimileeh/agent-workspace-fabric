"""Shared helpers for the protected-quality-gate ``blocked`` pause.

Both phases of the "protected-file violation pauses for an operator" feature
reuse this single source of truth:

* the PRE-PR executor pause (``control/executor/state_ops.py``), and
* the POST-PR PR-monitor pause (``runtime/pr_monitor_runner``).

Keeping the block-column writes, the ``block_epoch`` bump, and the operator-grant
lifecycle here means there is exactly ONE blocked machinery — no second blocked
status, grant model, or ``enter_blocked`` helper. Each caller owns only its own
state-machine transition (the executor blocks from running/validating/pushing,
the monitor blocks from monitoring_pr); the column semantics are identical.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.quality_gates import (
    PROTECTED_QUALITY_GATE_BLOCK_TYPE,
    QUALITY_GATE_POLICY_CHANGED_REASON_CODE,
    GrantSpec,
)
from awf.db.models import OperatorGrantAuditRecord, Workspace
from awf.db.repositories import WorkspaceRepository

SessionFactory = Callable[[], AsyncSession] | async_sessionmaker[AsyncSession]


def apply_protected_block_columns(
    ws: Workspace,
    *,
    violations_normalized: list[dict[str, Any]],
    resume_phase: str,
) -> None:
    """Stamp the block-state columns on a workspace entering ``blocked``.

    The single place that defines the protected-gate block payload semantics:
    the block type/reason, the normalized violations, the resume phase, the
    monotonic ``block_epoch`` bump (which invalidates all prior operator grants),
    the wall-clock ``blocked_at``, and the cleared ``pending_operator_hint`` (a
    re-block supersedes the directive from the prior block instance — the resume
    that produced the re-block already applied it). The CAS transition into
    ``blocked`` is the caller's responsibility because the legal ``from`` state
    differs per phase.
    """
    ws.block_reason_code = QUALITY_GATE_POLICY_CHANGED_REASON_CODE
    ws.block_type = PROTECTED_QUALITY_GATE_BLOCK_TYPE
    ws.block_violations = violations_normalized
    ws.block_resume_phase = resume_phase
    ws.block_epoch = (ws.block_epoch or 0) + 1
    ws.blocked_at = datetime.now(UTC)
    ws.pending_operator_hint = None


async def load_active_operator_grant_specs(
    session_factory: SessionFactory, workspace_id: str
) -> list[GrantSpec]:
    """Return the operator grants active for the workspace's CURRENT block epoch.

    A grant authorizes the protected gate only while it is unconsumed, unrevoked,
    and scoped to ``workspace.block_epoch`` (a re-block bumps the epoch and
    invalidates prior grants). Empty for a normal run — grant rows only exist
    after an operator resolves a ``blocked`` workspace — so threading this into
    every gate caller is a no-op outside the resume path.
    """
    async with session_factory() as session:
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


async def consume_active_operator_grants(session_factory: SessionFactory, workspace_id: str) -> int:
    """Mark the workspace's active (current-epoch) operator grants as consumed.

    Called once a resumed workspace clears the protected gate so a grant is
    strictly single-use: a later DIFFERENT change to the same file must be
    granted again. Returns the number of grants consumed.
    """
    consumed = 0
    now = datetime.now(UTC)
    async with session_factory() as session:
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
        for record in rows.scalars().all():
            record.consumed_at = now
            consumed += 1
        await session.commit()
    return consumed
