"""Count and surface repeated monitor-resume runtime restart failures (#936).

A monitor resume whose runtime restart fails records
``workspace.monitor_runtime_restart_failed`` and returns, and the worker resumes
the still-``monitoring_pr`` row again. Without a bound a stack that cannot come
back loops forever without anyone noticing. The consecutive failures are
counted from the workspace's own event log, so a worker or control-plane
restart cannot reset the count. After ``MONITOR_RUNTIME_RESTART_FAILURE_LIMIT``
in a row the workspace is parked in ``blocked`` (monitor-origin, so ``guide``
resumes it) with ``MONITOR_RUNTIME_RESTART_EXHAUSTED``, the count and the last
redacted compose error. It is never failed. A usable restart writes a
``recovered`` boundary, and the ``exhausted`` event is a boundary too, so a
guided resume starts a fresh streak.
"""

from __future__ import annotations

from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.audit import redact_audit_text
from awf.control.blocked_transition import enter_blocked_for_protected_violation_in_session
from awf.control.executor.constants import (
    MONITOR_RUNTIME_RESTART_BLOCK_TYPE,
    MONITOR_RUNTIME_RESTART_EXHAUSTED_EVENT_TYPE,
    MONITOR_RUNTIME_RESTART_EXHAUSTED_REASON_CODE,
    MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE,
    MONITOR_RUNTIME_RESTART_FAILURE_LIMIT,
    MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE,
    MONITOR_RUNTIME_RESTART_RESUME_PHASE,
)
from awf.control.executor.quality_gates import _log
from awf.db.enums import WorkspaceStatus
from awf.db.models import WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.node.compose_manager import ComposeOperationError

_STREAK_EVENT_TYPES: Final = (
    MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE,
    MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE,
    MONITOR_RUNTIME_RESTART_EXHAUSTED_EVENT_TYPE,
)
# Caps the reported count, not the decision: it is always above the limit.
_STREAK_QUERY_LIMIT: Final[int] = 50


async def consecutive_restart_failures(session: AsyncSession, workspace_id: str) -> int:
    """Count the newest ``monitor_runtime_restart_failed`` events since the last boundary."""
    rows = await session.execute(
        select(WorkspaceEvent.event_type)
        .where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.event_type.in_(_STREAK_EVENT_TYPES),
        )
        .order_by(
            WorkspaceEvent.occurred_at.desc(),
            WorkspaceEvent.event_order.desc().nulls_last(),
        )
        .limit(_STREAK_QUERY_LIMIT)
    )
    count = 0
    for event_type in rows.scalars():
        if event_type != MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE:
            break
        count += 1
    return count


async def escalate_monitor_resume_restart_failure(
    self: Any,
    *,
    workspace_id: str,
    monitor_owner_id: str | None,
    compose_project: str,
    error: ComposeOperationError,
) -> bool:
    """Park the workspace ``blocked`` once the failure streak reaches the limit.

    Called after the failure event was recorded. Returns ``True`` when it parked.
    Below the limit the resume returns and the worker retries as before.
    """
    try:
        async with self._session_factory() as session:
            failures = await consecutive_restart_failures(session, workspace_id)
    except SQLAlchemyError:
        _log.exception(
            "executor.resume_restart_failure_count_failed",
            workspace_id=workspace_id,
            reason_code=error.reason_code,
        )
        return False
    if failures < MONITOR_RUNTIME_RESTART_FAILURE_LIMIT:
        _log.warning(
            "executor.resume_restart_failure_counted",
            workspace_id=workspace_id,
            consecutive_failures=failures,
            limit=MONITOR_RUNTIME_RESTART_FAILURE_LIMIT,
            reason_code=error.reason_code,
        )
        return False
    # The restart can outlive the monitor lease; never park a newer owner's row.
    if not await self._recheck_status(
        workspace_id,
        expected=WorkspaceStatus.monitoring_pr,
        action="resume_restart_failure_park",
        monitor_owner_id=monitor_owner_id,
    ):
        return False
    stderr = redact_audit_text(error.stderr, limit=1000)
    payload = {
        "reason_code": MONITOR_RUNTIME_RESTART_EXHAUSTED_REASON_CODE,
        "message": (
            f"monitor recovery: runtime restart failed {failures} consecutive times "
            f"({error.reason_code}): {stderr or '<no output>'}"
        )[:2000],
        "consecutive_failures": failures,
        "limit": MONITOR_RUNTIME_RESTART_FAILURE_LIMIT,
        "compose_project_name": compose_project,
        "last_error": {
            "operation": error.operation,
            "returncode": error.returncode,
            "reason_code": error.reason_code,
            "stderr": stderr,
        },
    }
    try:
        parked = await _park_blocked(
            self, workspace_id=workspace_id, monitor_owner_id=monitor_owner_id, payload=payload
        )
    except SQLAlchemyError:
        # The resume returns either way; the next failure retries the park.
        _log.exception(
            "executor.resume_restart_failure_park_failed",
            workspace_id=workspace_id,
            reason_code=MONITOR_RUNTIME_RESTART_EXHAUSTED_REASON_CODE,
        )
        return False
    if not parked:
        return False
    _log.warning(
        "executor.resume_restart_failures_parked",
        workspace_id=workspace_id,
        consecutive_failures=failures,
        reason_code=MONITOR_RUNTIME_RESTART_EXHAUSTED_REASON_CODE,
        last_error_reason_code=error.reason_code,
    )
    return True


async def _park_blocked(
    self: Any,
    *,
    workspace_id: str,
    monitor_owner_id: str | None,
    payload: dict[str, Any],
) -> bool:
    """Owner-fenced ``monitoring_pr -> blocked`` CAS plus the exhausted evidence event."""
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await enter_blocked_for_protected_violation_in_session(
            session,
            repo,
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.monitoring_pr,
            violations=(),
            resume_phase=MONITOR_RUNTIME_RESTART_RESUME_PHASE,
            block_type=MONITOR_RUNTIME_RESTART_BLOCK_TYPE,
            block_reason_code=MONITOR_RUNTIME_RESTART_EXHAUSTED_REASON_CODE,
            monitor_owner_id=monitor_owner_id,
            fence_unclaimed_monitor=True,
        )
        if ws is None:
            _log.warning(
                "executor.resume_restart_failure_park_skipped",
                workspace_id=workspace_id,
                monitor_owner_id=monitor_owner_id,
                reason_code=MONITOR_RUNTIME_RESTART_EXHAUSTED_REASON_CODE,
            )
            return False
        await repo.add_event(
            ws,
            event_type=MONITOR_RUNTIME_RESTART_EXHAUSTED_EVENT_TYPE,
            reason_code=MONITOR_RUNTIME_RESTART_EXHAUSTED_REASON_CODE,
            payload=payload,
        )
        await session.commit()
    return True


async def reset_monitor_resume_restart_streak(self: Any, *, workspace_id: str) -> None:
    """Close an open failure streak after a usable restart; a no-op without one."""
    try:
        async with self._session_factory() as session:
            failures = await consecutive_restart_failures(session, workspace_id)
            if failures == 0:
                return
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None:
                return
            await repo.add_event(
                ws,
                event_type=MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE,
                payload={"previous_consecutive_failures": failures},
            )
            await session.commit()
    except SQLAlchemyError:
        # Never block a resume whose runtime is usable; the stale streak only
        # makes a later park happen sooner, and a guide resets it.
        _log.exception(
            "executor.resume_restart_streak_reset_failed",
            workspace_id=workspace_id,
        )
        return
    _log.info(
        "executor.resume_restart_streak_reset",
        workspace_id=workspace_id,
        previous_consecutive_failures=failures,
    )
