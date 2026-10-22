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
guided resume starts a fresh streak. When the ``recovered`` write fails, the
executor remembers the restart time, counts only later failures, and retries the
write on the next usable restart.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import Select, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.audit import redact_audit_text
from awf.control.blocked_transition import enter_blocked_for_protected_violation_in_session
from awf.control.executor.constants import (
    MONITOR_RUNTIME_RESTART_BACKOFF_SECONDS,
    MONITOR_RUNTIME_RESTART_BLOCK_TYPE,
    MONITOR_RUNTIME_RESTART_EXHAUSTED_EVENT_TYPE,
    MONITOR_RUNTIME_RESTART_EXHAUSTED_REASON_CODE,
    MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE,
    MONITOR_RUNTIME_RESTART_FAILURE_LIMIT,
    MONITOR_RUNTIME_RESTART_RECOVERED_EVENT_TYPE,
    MONITOR_RUNTIME_RESTART_RESUME_PHASE,
    MONITOR_RUNTIME_RESTART_STREAK_PAYLOAD_KEY,
    MONITOR_RUNTIME_RESTART_UNSETTLED_DEPENDENCIES_PAYLOAD_KEY,
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
_sleep = asyncio.sleep


def _now() -> datetime:
    return datetime.now(UTC)


def _unpersisted_recoveries(self: Any) -> dict[str, datetime]:
    """Usable-restart times, per workspace, whose ``recovered`` event is not yet stored."""
    recoveries: dict[str, datetime] | None = getattr(self, "_restart_streak_recovered_at", None)
    if recoveries is None:
        recoveries = {}
        self._restart_streak_recovered_at = recoveries
    return recoveries


async def _newest_streak_events(
    session: AsyncSession,
    workspace_id: str,
    since: datetime | None = None,
    *,
    limit: int = _STREAK_QUERY_LIMIT,
) -> list[tuple[str, dict[str, Any]]]:
    """Return the newest streak and boundary events, newest first, as (type, payload)."""
    rows = await session.execute(
        _newest_streak_events_query(
            WorkspaceEvent.event_type,
            WorkspaceEvent.payload,
            workspace_id=workspace_id,
            since=since,
        ).limit(limit)
    )
    return [(event_type, payload or {}) for event_type, payload in rows.tuples()]


def _newest_streak_events_query(
    *columns: Any, workspace_id: str, since: datetime | None
) -> Select[Any]:
    query = select(*columns).where(
        WorkspaceEvent.workspace_id == workspace_id,
        WorkspaceEvent.event_type.in_(_STREAK_EVENT_TYPES),
    )
    if since is not None:
        query = query.where(WorkspaceEvent.occurred_at > since)
    return query.order_by(
        WorkspaceEvent.event_order.desc().nulls_last(),
        WorkspaceEvent.occurred_at.desc(),
    )


def _open_streak(events: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Return the payloads of the leading streak failures, up to the first boundary."""
    streak: list[dict[str, Any]] = []
    for event_type, payload in events:
        if event_type != MONITOR_RUNTIME_RESTART_FAILED_EVENT_TYPE:
            break
        if not payload.get(MONITOR_RUNTIME_RESTART_STREAK_PAYLOAD_KEY):
            break
        streak.append(payload)
    return streak


async def consecutive_restart_failures(
    session: AsyncSession, workspace_id: str, since: datetime | None = None
) -> int:
    """Count the newest ``monitor_runtime_restart_failed`` events since the last boundary.

    Boundaries are a ``recovered`` or ``exhausted`` event, and a failure event
    recorded before the streak rule (no streak marker), so historic failures that
    were followed by unrecorded successful resumes never inflate a new streak.
    ``since`` is a usable restart whose ``recovered`` event could not be stored;
    failures at or before it are not counted.
    """
    return len(_open_streak(await _newest_streak_events(session, workspace_id, since)))


async def _last_streak_failure_at(
    session: AsyncSession, workspace_id: str, since: datetime | None
) -> tuple[int, datetime | None]:
    """Return the open streak length and when its newest failure was recorded."""
    failures = len(_open_streak(await _newest_streak_events(session, workspace_id, since)))
    if failures == 0:
        return 0, None
    # The open streak leads the same ordering, so its newest failure is the
    # first row; a closed streak's failure never sizes the backoff.
    last_failed_at = await session.scalar(
        _newest_streak_events_query(
            WorkspaceEvent.occurred_at, workspace_id=workspace_id, since=since
        ).limit(1)
    )
    return failures, last_failed_at


async def pace_monitor_resume_restart(self: Any, *, workspace_id: str) -> None:
    """Hold the next restart until the backoff after the last failed one has passed.

    The gap is measured from the failure event, so a worker restart in between
    neither resets nor repeats it. An unreadable event log never blocks a resume.
    """
    since = _unpersisted_recoveries(self).get(workspace_id)
    try:
        async with self._session_factory() as session:
            failures, last_failed_at = await _last_streak_failure_at(session, workspace_id, since)
    except SQLAlchemyError:
        _log.exception("executor.resume_restart_backoff_lookup_failed", workspace_id=workspace_id)
        return
    if failures == 0 or last_failed_at is None:
        return
    backoff = MONITOR_RUNTIME_RESTART_BACKOFF_SECONDS[
        min(failures, len(MONITOR_RUNTIME_RESTART_BACKOFF_SECONDS)) - 1
    ]
    remaining = backoff - (_now() - last_failed_at).total_seconds()
    if remaining <= 0:
        return
    _log.info(
        "executor.resume_restart_backoff",
        workspace_id=workspace_id,
        consecutive_failures=failures,
        backoff_seconds=backoff,
        remaining_seconds=round(remaining, 1),
    )
    await _sleep(remaining)


async def _parked_streak_open(session: AsyncSession, workspace_id: str) -> bool:
    events = await _newest_streak_events(session, workspace_id, limit=1)
    return bool(events) and events[0][0] == MONITOR_RUNTIME_RESTART_EXHAUSTED_EVENT_TYPE


async def dependencies_to_recreate_after_exhaustion(
    self: Any, *, workspace_id: str
) -> tuple[str, ...]:
    """Return the dependencies a guided retry after the park must force-recreate.

    Only the first resume after ``exhausted`` gets them: the dependencies still
    ``starting``/``unhealthy`` at the last dependency-wait timeout of the parked
    streak, which waiting on again would only time out once more. A usable
    resume whose ``recovered`` write failed closes the park all the same.
    """
    since = _unpersisted_recoveries(self).get(workspace_id)
    try:
        async with self._session_factory() as session:
            events = await _newest_streak_events(session, workspace_id, since)
    except SQLAlchemyError:
        _log.exception(
            "executor.resume_exhausted_dependencies_lookup_failed",
            workspace_id=workspace_id,
        )
        return ()
    if not events or events[0][0] != MONITOR_RUNTIME_RESTART_EXHAUSTED_EVENT_TYPE:
        return ()
    for payload in _open_streak(events[1:]):
        unsettled = payload.get(MONITOR_RUNTIME_RESTART_UNSETTLED_DEPENDENCIES_PAYLOAD_KEY)
        if unsettled:
            return tuple(sorted(str(name) for name in unsettled))
    return ()


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
            failures = await consecutive_restart_failures(
                session, workspace_id, _unpersisted_recoveries(self).get(workspace_id)
            )
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
    """Close an open failure streak or park after a usable restart; a no-op without one."""
    recoveries = _unpersisted_recoveries(self)
    try:
        async with self._session_factory() as session:
            failures = await consecutive_restart_failures(session, workspace_id)
            # A clean first resume after the park closes it too, so only that
            # resume recreates the dependencies left unsettled.
            if failures == 0 and not await _parked_streak_open(session, workspace_id):
                recoveries.pop(workspace_id, None)
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
        # Never block a resume whose runtime is usable. Later failures count
        # only after this restart, and the next usable restart retries the write.
        recoveries[workspace_id] = datetime.now(UTC)
        _log.exception(
            "executor.resume_restart_streak_reset_failed",
            workspace_id=workspace_id,
        )
        return
    recoveries.pop(workspace_id, None)
    _log.info(
        "executor.resume_restart_streak_reset",
        workspace_id=workspace_id,
        previous_consecutive_failures=failures,
    )
