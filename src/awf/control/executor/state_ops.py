"""WorkspaceExecutor state operations.

Mechanically extracted from the original orchestrator; behavior is unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import (
    UTC,
    datetime,
)
from typing import Any

from sqlalchemy import (
    String,
    cast,
    or_,
    select,
    update,
)

from awf.common.audit import redact_audit_text
from awf.common.compose_exec import EXEC_PROCESS_CLEANUP_FAILED
from awf.control.executor.helpers import _realign_profile_from_resolved_profile_snapshot
from awf.control.executor.metadata import (
    _metadata_int,
    _metadata_number,
    _metadata_str,
)
from awf.control.executor.quality_gates import (
    _log,
)
from awf.control.executor.recovery_payloads import _get_active_recovery_payload
from awf.control.executor.status_helpers import _is_callback_terminal_status
from awf.db.enums import (
    FailureReason,
    WorkspaceStatus,
)
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.profiles.models import WorkspaceProfile
from awf.runtime.validation import ValidationCommandResult


def _resolved_profile_snapshot_from_db_value(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def _load_workspace(self: Any, workspace_id: str) -> Workspace | None:
    async with self._session_factory() as session:
        return await WorkspaceRepository(session).get(workspace_id)


async def _persist_resolved_profile_snapshot_if_missing(
    self: Any,
    *,
    workspace_id: str,
    profile: WorkspaceProfile,
) -> dict[str, Any] | None:
    """Freeze the runtime-resolved profile snapshot and return the stored snapshot."""
    snapshot = profile.model_dump(mode="json", by_alias=True)
    async with self._session_factory() as session:
        result = await session.execute(
            update(Workspace)
            .where(
                Workspace.id == workspace_id,
                or_(
                    Workspace.resolved_profile.is_(None),
                    cast(Workspace.resolved_profile, String) == "null",
                ),
            )
            .values(resolved_profile=snapshot)
            .returning(Workspace.resolved_profile)
            .execution_options(synchronize_session=False)
        )
        returned_snapshot = result.scalar_one_or_none()
        persisted_snapshot = _resolved_profile_snapshot_from_db_value(returned_snapshot)
        if returned_snapshot is not None:
            if persisted_snapshot is None:
                _log.warning(
                    "executor.resolved_profile_returning_unparseable",
                    workspace_id=workspace_id,
                    returned_type=type(returned_snapshot).__name__,
                )
            await session.commit()
            return persisted_snapshot if persisted_snapshot is not None else snapshot
        frozen_snapshot = await session.scalar(
            select(Workspace.resolved_profile).where(Workspace.id == workspace_id)
        )
        return _resolved_profile_snapshot_from_db_value(frozen_snapshot)


async def _sync_resolved_profile(
    self: Any,
    *,
    ws: Workspace,
    workspace_id: str,
    profile: WorkspaceProfile,
    planning_max_iterations_default: int = 3,
) -> WorkspaceProfile:
    """Freeze the resolved profile snapshot and align the active profile to the winner."""
    persisted_profile_snapshot = await _persist_resolved_profile_snapshot_if_missing(
        self,
        workspace_id=workspace_id,
        profile=profile,
    )
    persisted_profile = _realign_profile_from_resolved_profile_snapshot(
        ws,
        persisted_profile_snapshot,
        planning_max_iterations_default=planning_max_iterations_default,
    )
    return persisted_profile if persisted_profile is not None else profile


async def _claim_ready(
    self: Any,
    workspace_id: str,
    *,
    execution_owner_id: str | None = None,
    execution_lease_expires_at: datetime | None = None,
) -> Workspace | None:
    """Atomically transition a ready workspace to running before execution."""
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.transition_if_current(
            workspace_id,
            from_status=WorkspaceStatus.ready,
            to=WorkspaceStatus.running,
            reason_code="EXECUTOR_CLAIMED",
        )
        if ws is not None:
            ws.execution_claimed_by = execution_owner_id
            ws.execution_claim_expires_at = execution_lease_expires_at
            await session.commit()
            return ws

        current: Workspace | None = None
        if execution_owner_id is not None and execution_lease_expires_at is not None:
            current = await repo.claim_worker_restart_recovery_execution(
                workspace_id,
                owner_id=execution_owner_id,
                lease_expires_at=execution_lease_expires_at,
                claim_cutoff=datetime.now(UTC),
            )
            if current is not None:
                await session.commit()
                return current

        current = await repo.get_with_operations(workspace_id)
        if current is None:
            _log.warning("executor.skip_unknown", workspace_id=workspace_id)
            return None
        recovery = _get_active_recovery_payload(current)
        if (
            current.status == WorkspaceStatus.running.value
            and recovery is not None
            and recovery.get("source") == "worker_restart"
        ):
            _log.info(
                "executor.skip_active_execution_claim",
                workspace_id=workspace_id,
                execution_claimed_by=current.execution_claimed_by,
            )
            return None
        _log.info(
            "executor.skip_not_ready",
            workspace_id=workspace_id,
            status=current.status,
        )
        return None


async def _update_subphase(self: Any, workspace_id: str, subphase: str) -> None:
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        await repo.update_activity(workspace_id, subphase=subphase)
        await session.commit()


async def _recheck_status(
    self: Any,
    workspace_id: str,
    *,
    expected: WorkspaceStatus,
    action: str,
    reason_code: str = "EXECUTOR_STALE_STATUS",
) -> bool:
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        if ws is None:  # pragma: no cover - destroyed mid-flight
            _log.warning(
                "executor.skip_unknown",
                workspace_id=workspace_id,
                action=action,
            )
            return False
        if ws.status == expected.value:
            return True
        await self._record_stale_action_skip(
            repo,
            ws,
            action=action,
            expected=expected,
            reason_code=reason_code,
        )
        if _is_callback_terminal_status(ws.status):
            await self._finish_ignored_stale_callback_operations_in_session(
                session,
                workspace_id=workspace_id,
                callback_source="executor",
                callback_action=action,
                expected_status=expected,
                actual_status=ws.status,
            )
        await session.commit()
        return False


async def _transition_if_current(
    self: Any,
    workspace_id: str,
    *,
    from_status: WorkspaceStatus,
    to: WorkspaceStatus,
    reason: str,
    action: str,
) -> bool:
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.transition_if_current(
            workspace_id,
            from_status=from_status,
            to=to,
            reason_code=reason,
        )
        if ws is not None:
            await session.commit()
            return True

        current = await repo.get(workspace_id)
        if current is None:
            # pragma: no cover - destroyed mid-flight
            return False

        await self._record_stale_action_skip(
            repo,
            current,
            action=action,
            expected=from_status,
            reason_code="EXECUTOR_STALE_STATUS",
        )
        if _is_callback_terminal_status(current.status):
            await self._finish_ignored_stale_callback_operations_in_session(
                session,
                workspace_id=workspace_id,
                callback_source="executor",
                callback_action=action,
                expected_status=from_status,
                actual_status=current.status,
            )
        await session.commit()
        return False


async def _record_stale_action_skip(
    _self: Any,
    repo: WorkspaceRepository,
    ws: Workspace,
    *,
    action: str,
    expected: WorkspaceStatus,
    reason_code: str,
) -> None:
    _log.info(
        "executor.skip_stale_status",
        workspace_id=ws.id,
        action=action,
        expected_status=expected.value,
        status=ws.status,
    )
    if _is_callback_terminal_status(ws.status):
        await repo.record_ignored_stale_callback(
            ws,
            callback_source="executor",
            callback_action=action,
            expected_status=expected,
            reason_code=reason_code,
        )
    await repo.add_event(
        ws,
        event_type="workspace.stale_action_skipped",
        reason_code=reason_code,
        payload={
            "action": action,
            "expected_status": expected.value,
            "actual_status": ws.status,
        },
    )


async def _record_health_check_failed_event(
    self: Any,
    *,
    workspace_id: str,
    failure: ValidationCommandResult,
) -> None:
    metadata = failure.metadata if isinstance(failure.metadata, dict) else {}
    stream_ids = metadata.get("stream_ids")
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(workspace_id)
        if ws is None or ws.status != WorkspaceStatus.validating.value:
            return
        await repo.add_event(
            ws,
            event_type="workspace.health_check_failed",
            reason_code=failure.reason_code,
            payload={
                "healthcheck_name": _metadata_str(metadata, "healthcheck_name"),
                "healthcheck_kind": _metadata_str(metadata, "healthcheck_kind"),
                "target": _metadata_str(metadata, "target") or failure.command,
                "attempts": _metadata_int(metadata, "attempts"),
                "timeout_seconds": _metadata_number(metadata, "timeout_seconds"),
                "stream_ids": dict(stream_ids) if isinstance(stream_ids, dict) else {},
            },
        )
        await session.commit()


async def _mark_failed(
    self: Any,
    *,
    workspace_id: str,
    from_status: WorkspaceStatus,
    failure_reason: FailureReason,
    message: str,
    reason_code: str | None = None,
    details: Mapping[str, Any] | None = None,
    salvage: Mapping[str, Any] | None = None,
) -> None:
    async with self._session_factory() as session:
        repo = WorkspaceRepository(session)
        final_reason_code = reason_code or failure_reason.value.upper()
        safe_message = redact_audit_text(message, limit=2000)
        payload: dict[str, Any] | None = None
        if details is not None or salvage is not None:
            payload = {
                "failure_reason": failure_reason.value,
                "reason_code": final_reason_code,
                "message": safe_message,
            }
            if details is not None:
                payload["details"] = dict(details)
            if salvage is not None:
                payload["salvage"] = dict(salvage)

        ws = await repo.transition_if_current(
            workspace_id,
            from_status=from_status,
            to=WorkspaceStatus.failed,
            reason_code=final_reason_code,
            payload=payload,
        )
        if ws is None:
            ws = await repo.get(workspace_id)
            if ws is None:  # pragma: no cover
                return
            # Already moved (e.g. cancelled) — respect it.
            await self._record_stale_action_skip(
                repo,
                ws,
                action="mark_failed",
                expected=from_status,
                reason_code="EXECUTOR_MARK_FAILED_SKIPPED",
            )
            if _is_callback_terminal_status(ws.status):
                await self._finish_ignored_stale_callback_operations_in_session(
                    session,
                    workspace_id=workspace_id,
                    callback_source="executor",
                    callback_action="mark_failed",
                    expected_status=from_status,
                    actual_status=ws.status,
                )
            await session.commit()
            return

        ws.failure_reason = failure_reason.value
        ws.failure_message = safe_message
        if final_reason_code == EXEC_PROCESS_CLEANUP_FAILED:
            await repo.add_event(
                ws,
                event_type="workspace.exec_process_cleanup_failed",
                reason_code=EXEC_PROCESS_CLEANUP_FAILED,
                payload={"message": safe_message[:1000]},
            )
        await session.commit()
