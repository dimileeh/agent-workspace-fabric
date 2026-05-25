"""Pull request monitor operation tracking.

Mechanically extracted from the original orchestrator; behavior is unchanged.
"""

from __future__ import annotations

from collections.abc import (
    Mapping,
    Sequence,
)
from typing import Any

from awf.db.enums import (
    OperationStatus,
    OperationType,
)
from awf.db.repositories import WorkspaceRepository
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.pr_monitor import PRStatus
from awf.runtime.pr_monitor_operations import (
    MonitorOperationHandle,
    begin_monitor_state_operation,
    build_monitor_operation_payload,
    create_or_start_monitor_operation,
    finish_monitor_operation,
    monitor_operation_idempotency_key,
    record_monitor_state_operation,
)
from awf.service.provider_recovery import PROVIDER_AUTH_FAILED


async def _begin_monitor_operation(
    self: Any,
    *,
    workspace_id: str,
    operation_type: OperationType | str,
    action: str,
    requested_action: str,
    reason: str | None,
    reason_code: str,
    pr_number: int,
    status: PRStatus,
    base_branch: str,
    remote_branch: str | None,
    operation_status: OperationStatus = OperationStatus.running,
    recovery_mode: str | None = None,
    stale_reason: str | None = None,
    monitor_log: WorkspaceLogSink | None = None,
    extra_payload: Mapping[str, Any] | None = None,
    extra_identity: Sequence[object] = (),
) -> MonitorOperationHandle | None:
    log_refs = {"monitor": monitor_log.stream_id} if monitor_log is not None else None
    async with self._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        if workspace is None:  # pragma: no cover - defensive invariant
            return None
        payload = build_monitor_operation_payload(
            workspace=workspace,
            action=action,
            requested_action=requested_action,
            reason=reason,
            reason_code=reason_code,
            pr_number=pr_number,
            source_head_sha=status.head_sha,
            source_base_sha=workspace.base_commit,
            target_branch=base_branch,
            remote_branch=remote_branch,
            recovery_mode=recovery_mode,
            stale_reason=stale_reason,
            log_stream_refs=log_refs,
            extra=extra_payload,
        )
        idempotency_key = monitor_operation_idempotency_key(
            workspace_id=workspace_id,
            action=action,
            pr_number=pr_number,
            reason_code=reason_code,
            source_head_sha=status.head_sha,
            source_base_sha=workspace.base_commit,
            extra=extra_identity,
        )
        handle = await create_or_start_monitor_operation(
            session,
            workspace_id=workspace_id,
            operation_type=operation_type,
            payload=payload,
            idempotency_key=idempotency_key,
            status=operation_status,
        )
        await session.commit()
        return handle


async def _finish_monitor_operation(
    self: Any,
    handle: MonitorOperationHandle | None,
    *,
    status: OperationStatus,
    result: Mapping[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    if handle is None or not handle.should_finish:
        return
    async with self._deps.session_factory() as session:
        await finish_monitor_operation(
            session,
            operation_id=handle.operation_id,
            status=status,
            result=result,
            error_code=error_code,
            error_message=error_message,
        )
        await session.commit()


async def _finish_provider_auth_failed_operation(
    self: Any,
    handle: MonitorOperationHandle | None,
    *,
    extra_result: Mapping[str, Any] | None = None,
) -> None:
    result: dict[str, Any] = {
        "status": "failed",
        "outcome": "provider_auth_failed",
        "reason_code": PROVIDER_AUTH_FAILED,
    }
    if extra_result:
        result.update(extra_result)
    result["pushed"] = False
    await self._finish_monitor_operation(
        handle,
        status=OperationStatus.failed,
        result=result,
        error_code=PROVIDER_AUTH_FAILED,
        error_message="Provider authentication failed",
    )


async def _begin_monitor_state_operation(
    self: Any,
    *,
    workspace_id: str,
    action: str,
    requested_action: str,
    reason: str | None,
    reason_code: str,
    pr_number: int,
    status: PRStatus,
    base_branch: str,
    remote_branch: str | None,
    monitor_log: WorkspaceLogSink | None = None,
    recovery_mode: str | None = None,
    stale_reason: str | None = None,
    extra_payload: Mapping[str, Any] | None = None,
    extra_identity: Sequence[object] = (),
) -> MonitorOperationHandle | None:
    log_refs = {"monitor": monitor_log.stream_id} if monitor_log is not None else None
    async with self._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        if workspace is None:  # pragma: no cover - defensive invariant
            return None
        handle = await begin_monitor_state_operation(
            session,
            workspace=workspace,
            action=action,
            requested_action=requested_action,
            reason=reason,
            reason_code=reason_code,
            pr_number=pr_number,
            source_head_sha=status.head_sha,
            source_base_sha=workspace.base_commit,
            target_branch=base_branch,
            remote_branch=remote_branch,
            recovery_mode=recovery_mode,
            stale_reason=stale_reason,
            log_stream_refs=log_refs,
            extra=extra_payload,
            extra_identity=extra_identity,
        )
        await session.commit()
        return handle


async def _record_monitor_state_operation(
    self: Any,
    *,
    workspace_id: str,
    action: str,
    requested_action: str,
    reason: str | None,
    reason_code: str,
    pr_number: int,
    status: PRStatus,
    base_branch: str,
    remote_branch: str | None,
    result: Mapping[str, Any] | None = None,
    monitor_log: WorkspaceLogSink | None = None,
    recovery_mode: str | None = None,
    stale_reason: str | None = None,
    extra_payload: Mapping[str, Any] | None = None,
    extra_identity: Sequence[object] = (),
) -> None:
    log_refs = {"monitor": monitor_log.stream_id} if monitor_log is not None else None
    async with self._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        if workspace is None:  # pragma: no cover - defensive invariant
            return
        await record_monitor_state_operation(
            session,
            workspace=workspace,
            action=action,
            requested_action=requested_action,
            reason=reason,
            reason_code=reason_code,
            pr_number=pr_number,
            source_head_sha=status.head_sha,
            source_base_sha=workspace.base_commit,
            target_branch=base_branch,
            remote_branch=remote_branch,
            result=result,
            recovery_mode=recovery_mode,
            stale_reason=stale_reason,
            log_stream_refs=log_refs,
            extra=extra_payload,
            extra_identity=extra_identity,
        )
        await session.commit()


async def _sleep_with_monitor_state_operation(
    self: Any,
    *,
    workspace_id: str,
    action: str,
    requested_action: str,
    reason: str | None,
    reason_code: str,
    pr_number: int,
    status: PRStatus,
    base_branch: str,
    remote_branch: str | None,
    wait_seconds: float,
    monitor_log: WorkspaceLogSink | None = None,
    recovery_mode: str | None = None,
    stale_reason: str | None = None,
    extra_payload: Mapping[str, Any] | None = None,
    extra_identity: Sequence[object] = (),
) -> None:
    payload = {"wait_seconds": wait_seconds, **dict(extra_payload or {})}
    operation = await self._begin_monitor_state_operation(
        workspace_id=workspace_id,
        action=action,
        requested_action=requested_action,
        reason=reason,
        reason_code=reason_code,
        pr_number=pr_number,
        status=status,
        base_branch=base_branch,
        remote_branch=remote_branch,
        monitor_log=monitor_log,
        recovery_mode=recovery_mode,
        stale_reason=stale_reason,
        extra_payload=payload,
        extra_identity=extra_identity,
    )
    try:
        await self._deps.sleep(wait_seconds)
    except Exception as exc:
        await self._finish_monitor_operation(
            operation,
            status=OperationStatus.failed,
            result={
                "status": "failed",
                "outcome": "wait_failed",
                "reason_code": reason_code,
            },
            error_code=reason_code,
            error_message=str(exc),
        )
        raise
    await self._finish_monitor_operation(
        operation,
        status=OperationStatus.succeeded,
        result={
            "status": "succeeded",
            "outcome": "wait_elapsed",
            "slept_seconds": wait_seconds,
        },
    )
