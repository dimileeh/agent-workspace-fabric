"""Repository helper for selecting operator-cleared resumable blocked workspaces."""

from __future__ import annotations

import builtins
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from awf.db.enums import MONITOR_BLOCK_RESUME_PHASE_PREFIX, WorkspaceStatus
from awf.db.models import OperatorGrantAuditRecord, Workspace


async def list_resumable_blocked_ids(
    session: AsyncSession,
    dialect_name: str | None,
    *,
    limit: int,
    exclude_ids: set[str] | None = None,
    node_id: str | None = None,
) -> builtins.list[str]:
    """Return ``blocked`` workspace IDs an operator has cleared for resume.

    A pre-PR ``blocked`` workspace is resumable only once an operator has
    acted on it: ``guide`` either arms a non-empty directive in
    ``pending_operator_hint`` (revert/redo) or records at least one grant
    active for the workspace's CURRENT ``block_epoch`` (approve-and-keep).
    Blocked workspaces still awaiting an operator decision carry neither, so
    they are excluded here — otherwise the worker would spin them through
    ``blocked -> running -> blocked`` re-running the same gate every cycle.

    When ``node_id`` is given the result is scoped to that owning node
    (mirroring the scheduler/cleanup ``node_id == node_id OR node_id IS
    NULL`` predicate): a blocked workspace's warm compose stack/worktree is
    preserved on the node that ran it, so a worker on another node must not
    claim it and resume against resources that only exist elsewhere. NULL
    rows (legacy/unstamped) stay adoptable by any node.

    The hint branch matches the resume path's acceptance test exactly: a
    directive-less, whitespace-only, or non-string ``directive`` in
    ``pending_operator_hint`` (which ``execution_flow`` treats as absent via
    ``_optional_stripped_string`` — including non-``str`` JSON values such as
    ``true``/``123``, running neither the revert/redo nor the approve-and-keep
    branch) is NOT eligible on its own — selecting it would reproduce the very
    spin loop the non-null guard was meant to avoid.
    Oldest ``updated_at`` first for FIFO fairness across cleared workspaces.
    """
    if limit <= 0:
        return []
    active_grant_exists = (
        select(OperatorGrantAuditRecord.id)
        .where(
            OperatorGrantAuditRecord.workspace_id == Workspace.id,
            OperatorGrantAuditRecord.consumed_at.is_(None),
            OperatorGrantAuditRecord.revoked_at.is_(None),
            OperatorGrantAuditRecord.block_epoch == Workspace.block_epoch,
        )
        .exists()
    )
    if dialect_name == "postgresql":
        hint_directive: ColumnElement[Any] = Workspace.pending_operator_hint.op("->>")("directive")
        hint_reason: ColumnElement[Any] = Workspace.pending_operator_hint.op("->>")("reason")
        # ``pending_operator_hint`` is a generic ``JSON`` column (Postgres
        # ``json``, not ``jsonb``) so ``json_typeof`` — not ``jsonb_typeof`` —
        # is the matching introspection function for the ``->`` json value.
        directive_is_string: ColumnElement[Any] = (
            func.json_typeof(Workspace.pending_operator_hint.op("->")("directive")) == "string"
        )
        reason_is_string: ColumnElement[Any] = (
            func.json_typeof(Workspace.pending_operator_hint.op("->")("reason")) == "string"
        )
    else:
        hint_directive = func.json_extract(Workspace.pending_operator_hint, "$.directive")
        hint_reason = func.json_extract(Workspace.pending_operator_hint, "$.reason")
        directive_is_string = (
            func.json_type(Workspace.pending_operator_hint, "$.directive") == "text"
        )
        reason_is_string = func.json_type(Workspace.pending_operator_hint, "$.reason") == "text"
    # Mirror the resume path's ``pre_pr_operator_hint_from_payload`` exactly: it
    # rebuilds the hint only when BOTH ``reason`` and ``directive`` are non-blank
    # *strings* (a non-string value such as ``true``/``123`` coerces to non-empty
    # text but is discarded as absent), then injects the prompt only when
    # ``hint.directive`` survives. A directive paired with a missing/blank
    # ``reason`` makes that reconstruction return ``None``, so the worker would
    # resume and re-run the agent *unguided* — reproducing the very ``blocked ->
    # running -> blocked`` spin the non-null guard exists to avoid. Require both
    # fields here so eligibility matches exactly what the resume path can apply.
    actionable_hint = and_(
        directive_is_string,
        func.trim(func.coalesce(hint_directive, "")) != "",
        reason_is_string,
        func.trim(func.coalesce(hint_reason, "")) != "",
    )
    # Exclude POST-PR (monitor-origin) blocks: those are resumed back into the PR
    # monitor (``blocked -> monitoring_pr`` at guide time), NOT through the
    # executor's ``blocked -> running`` path. They are marked by a ``monitor_``
    # ``block_resume_phase`` prefix. A NULL or pre-PR (executor-phase) resume
    # phase stays eligible here. Selecting a monitor-origin row would let the
    # executor and monitor race to resume the same workspace.
    # ``_`` and ``%`` are LIKE wildcards, so a bare ``f"{PREFIX}%"`` pattern would
    # treat the literal underscore in ``monitor_`` as "any single char" and diverge
    # from the Python ``is_monitor_origin_block_resume_phase`` discriminator (a
    # literal ``startswith(MONITOR_BLOCK_RESUME_PHASE_PREFIX)``): a non-monitor phase
    # such as ``monitorX`` would match the loose pattern and be wrongly excluded from
    # executor resume. Escape the prefix's wildcard chars so the predicate matches the
    # literal prefix exactly, keeping the SQL filter and the Python check in lockstep.
    like_escape = "\\"
    escaped_prefix = (
        MONITOR_BLOCK_RESUME_PHASE_PREFIX.replace(like_escape, like_escape * 2)
        .replace("_", like_escape + "_")
        .replace("%", like_escape + "%")
    )
    not_monitor_origin = or_(
        Workspace.block_resume_phase.is_(None),
        Workspace.block_resume_phase.notlike(f"{escaped_prefix}%", escape=like_escape),
    )
    stmt = select(Workspace.id).where(
        Workspace.status == WorkspaceStatus.blocked.value,
        not_monitor_origin,
        or_(actionable_hint, active_grant_exists),
    )
    if node_id is not None:
        stmt = stmt.where(or_(Workspace.node_id == node_id, Workspace.node_id.is_(None)))
    if exclude_ids:
        stmt = stmt.where(Workspace.id.notin_(exclude_ids))
    stmt = stmt.order_by(Workspace.updated_at.asc(), Workspace.id.asc()).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())
