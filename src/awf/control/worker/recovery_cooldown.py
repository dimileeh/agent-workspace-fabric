"""Active salvage monitor recovery cooldowns.

Mechanically extracted from recovery.py; behavior is unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from time import monotonic
from typing import Any, cast

from sqlalchemy import select

from awf.control.worker.constants import (
    _ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_REASON_CODE,
    _ACTIVE_EXECUTION_SALVAGE_MONITOR_RESUME_COOLDOWN_EVENT_TYPE,
    _ACTIVE_EXECUTION_SALVAGE_MONITOR_RESUME_COOLDOWN_REASON_CODE,
    _ACTIVE_EXECUTION_SALVAGE_OWNER,
    _ACTIVE_EXECUTION_SALVAGE_SOURCE,
    _ACTIVE_SALVAGE_MONITOR_RECOVERY_OPERATION_ID_LIMIT,
    _ACTIVE_SALVAGE_MONITOR_RESUME_COOLDOWN_LIMIT,
)
from awf.control.worker.helpers import (
    _datetime_from_json,
    _json_datetime,
    _utc_datetime,
)
from awf.control.worker.logging import _log
from awf.db.enums import WorkspaceStatus
from awf.db.models import WorkspaceEvent
from awf.db.repositories import WorkspaceRepository


def _remember_active_salvage_monitor_recovery_operation_id(self: Any, operation_id: str) -> None:
    self._active_salvage_monitor_recovery_operation_ids.pop(operation_id, None)
    self._active_salvage_monitor_recovery_operation_ids[operation_id] = None
    while (
        len(self._active_salvage_monitor_recovery_operation_ids)
        > _ACTIVE_SALVAGE_MONITOR_RECOVERY_OPERATION_ID_LIMIT
    ):
        oldest_operation_id = next(iter(self._active_salvage_monitor_recovery_operation_ids))
        self._active_salvage_monitor_recovery_operation_ids.pop(oldest_operation_id, None)


def _forget_active_salvage_monitor_recovery_operation_id(self: Any, operation_id: str) -> None:
    self._active_salvage_monitor_recovery_operation_ids.pop(operation_id, None)


async def _active_salvage_monitor_resume_cooldown_blocks_claim(
    self: Any,
    workspace_id: str,
) -> bool:
    if self._active_salvage_monitor_resume_cooldown_active(workspace_id):
        return True
    return cast(
        bool,
        await self._persisted_active_salvage_monitor_resume_cooldown_active(workspace_id),
    )


async def _persisted_active_salvage_monitor_resume_cooldown_active(
    self: Any,
    workspace_id: str,
) -> bool:
    if max(0.0, self._config.monitor_claim_lease_seconds) <= 0:
        return False

    stmt = (
        select(WorkspaceEvent)
        .where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.event_type
            == _ACTIVE_EXECUTION_SALVAGE_MONITOR_RESUME_COOLDOWN_EVENT_TYPE,
            WorkspaceEvent.reason_code
            == _ACTIVE_EXECUTION_SALVAGE_MONITOR_RESUME_COOLDOWN_REASON_CODE,
        )
        .order_by(WorkspaceEvent.occurred_at.desc(), WorkspaceEvent.id.desc())
        .limit(1)
    )
    async with self._session_factory() as session:
        event = (await session.execute(stmt)).scalar_one_or_none()
    if event is None:
        return False

    payload = event.payload if isinstance(event.payload, Mapping) else {}
    cooldown_until = _datetime_from_json(payload.get("cooldown_until"))
    if cooldown_until is None:
        cooldown_until = _utc_datetime(event.occurred_at) + timedelta(
            seconds=max(0.0, self._config.monitor_claim_lease_seconds)
        )
    return datetime.now(UTC) < cooldown_until


async def _record_active_salvage_monitor_resume_cooldown(
    self: Any,
    workspace_id: str,
    *,
    recovery_operation_id: str,
    cooldown_until: datetime,
) -> None:
    try:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            ws = await repo.get(workspace_id)
            if ws is None or ws.status != WorkspaceStatus.monitoring_pr.value:
                return
            await repo.add_event(
                ws,
                event_type=_ACTIVE_EXECUTION_SALVAGE_MONITOR_RESUME_COOLDOWN_EVENT_TYPE,
                reason_code=_ACTIVE_EXECUTION_SALVAGE_MONITOR_RESUME_COOLDOWN_REASON_CODE,
                payload={
                    "source": _ACTIVE_EXECUTION_SALVAGE_SOURCE,
                    "owner": _ACTIVE_EXECUTION_SALVAGE_OWNER,
                    "reason_code": (_ACTIVE_EXECUTION_SALVAGE_MONITOR_RESUME_COOLDOWN_REASON_CODE),
                    "workspace_status": ws.status,
                    "operation_id": recovery_operation_id,
                    "worker_id": self._worker_id,
                    "cooldown_until": _json_datetime(cooldown_until),
                    "salvage_reason_code": (_ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED_REASON_CODE),
                },
            )
            await session.commit()
    except Exception:
        _log.exception(
            "worker.active_salvage_monitor_resume_cooldown_record_failed",
            workspace_id=workspace_id,
            operation_id=recovery_operation_id,
        )


def _remember_active_salvage_monitor_resume_cooldown(
    self: Any,
    workspace_id: str,
    cooldown_until: float,
) -> None:
    self._active_salvage_monitor_resume_cooldowns.pop(workspace_id, None)
    self._active_salvage_monitor_resume_cooldowns[workspace_id] = cooldown_until
    self._evict_expired_salvage_monitor_cooldowns()


def _evict_expired_salvage_monitor_cooldowns(self: Any) -> None:
    now = monotonic()
    expired_workspace_ids = [
        workspace_id
        for workspace_id, cooldown_until in self._active_salvage_monitor_resume_cooldowns.items()
        if cooldown_until <= now
    ]
    for workspace_id in expired_workspace_ids:
        self._active_salvage_monitor_resume_cooldowns.pop(workspace_id, None)
    while (
        len(self._active_salvage_monitor_resume_cooldowns)
        > _ACTIVE_SALVAGE_MONITOR_RESUME_COOLDOWN_LIMIT
    ):
        oldest_workspace_id = next(iter(self._active_salvage_monitor_resume_cooldowns))
        self._active_salvage_monitor_resume_cooldowns.pop(oldest_workspace_id, None)


def _active_salvage_monitor_resume_cooldown_active(self: Any, workspace_id: str) -> bool:
    self._evict_expired_salvage_monitor_cooldowns()
    cooldown_until = self._active_salvage_monitor_resume_cooldowns.get(workspace_id)
    if cooldown_until is None:
        return False
    if monotonic() < cooldown_until:
        return True
    self._active_salvage_monitor_resume_cooldowns.pop(workspace_id, None)
    return False
