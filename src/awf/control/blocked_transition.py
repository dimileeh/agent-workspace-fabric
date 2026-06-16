"""Shared block-transition core for protected quality-gate pauses.

WS-1 introduced ``enter_blocked_for_protected_violation`` on the executor for the
pre-PR fail sites. WS-2 needs the SAME atomic, epoch-fenced ``-> blocked`` CAS +
``block_*`` column writes from the PR-monitor runner (``monitoring_pr -> blocked``).

Rather than add a second ``enter_blocked`` helper, the DB body lives here as a
session-bound function both callers delegate to:

- the executor method (``control/executor/state_ops.py``) calls it for the pre-PR
  origins and keeps its stale-CAS + active-recovery finalization around it
  (behavior unchanged);
- the monitor pause path calls it with ``from_status=monitoring_pr`` and an
  ``extra_payload`` carrying the preserved-commit SHA for divergence recovery.

The function performs ONLY the CAS + column writes; it does not commit and does
not run any caller-specific stale/recovery handling (those differ per caller).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.sql.elements import ColumnElement

from awf.control.quality_gates import (
    PROTECTED_QUALITY_GATE_BLOCK_TYPE,
    PROTECTED_VIOLATION_BLOCKED_REASON_CODE,
    QUALITY_GATE_POLICY_CHANGED_REASON_CODE,
    QualityGateViolation,
    quality_gate_violation_details,
)
from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository


async def enter_blocked_for_protected_violation_in_session(
    session: Any,
    repo: WorkspaceRepository,
    *,
    workspace_id: str,
    from_status: WorkspaceStatus,
    violations: Sequence[QualityGateViolation],
    resume_phase: str,
    block_type: str = PROTECTED_QUALITY_GATE_BLOCK_TYPE,
    block_reason_code: str = QUALITY_GATE_POLICY_CHANGED_REASON_CODE,
    execution_owner_id: str | None = None,
    extra_payload: Mapping[str, Any] | None = None,
) -> Workspace | None:
    """Atomically CAS a workspace into ``blocked`` and write the block columns.

    Returns the transitioned ``Workspace`` on success (NOT committed — the caller
    owns the commit and any caller-specific finalization), or ``None`` when the
    epoch-fenced CAS matched 0 rows (a stale/raced claimant); the caller then
    handles the stale path.

    ``block_epoch`` is bumped on every entry so a re-block invalidates all prior
    operator grants — the mechanism that makes a later DIFFERENT change re-block.
    ``extra_payload`` is folded into the transition event payload (e.g. the
    monitor pause records ``preserved_head_sha`` for divergence recovery).
    ``del session`` keeps the signature symmetric for callers that thread the
    open session even though the repository already closes over it.
    """
    del session
    normalized = quality_gate_violation_details(violations)
    extra_conditions: tuple[ColumnElement[bool], ...] = ()
    if execution_owner_id is not None:
        extra_conditions = (Workspace.execution_claimed_by == execution_owner_id,)
    payload: dict[str, Any] = {
        "block_type": block_type,
        "block_reason_code": block_reason_code,
        "resume_phase": resume_phase,
        "violations": normalized,
    }
    if extra_payload:
        payload.update(dict(extra_payload))
    ws = await repo.transition_if_current(
        workspace_id,
        from_status=from_status,
        to=WorkspaceStatus.blocked,
        reason_code=PROTECTED_VIOLATION_BLOCKED_REASON_CODE,
        payload=payload,
        extra_conditions=extra_conditions,
    )
    if ws is None:
        return None
    ws.block_reason_code = block_reason_code
    ws.block_type = block_type
    ws.block_violations = normalized
    ws.block_resume_phase = resume_phase
    ws.block_epoch = (ws.block_epoch or 0) + 1
    ws.blocked_at = datetime.now(UTC)
    # A re-block supersedes any pre-PR operator directive from a prior block
    # instance (grants are epoch-fenced and invalidated by the bumped epoch, but
    # the directive column is not), mirroring the WS-1 executor behavior.
    ws.pending_operator_hint = None
    return ws
