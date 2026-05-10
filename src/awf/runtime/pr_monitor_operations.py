"""Durable operation helpers for PR monitor recovery actions."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.audit import redact_audit_text, redact_audit_value
from awf.db.enums import OperationStatus, OperationType
from awf.db.models import Operation, Workspace
from awf.db.repositories import OperationRepository

RETRYABLE_MONITOR_OPERATION_STATUSES = frozenset(
    {
        OperationStatus.failed.value,
        OperationStatus.cancelled.value,
    }
)
_MONITOR_REDACTED_ASSIGNMENT_RE = re.compile(
    r"\b(?:[A-Za-z][A-Za-z0-9_]*_)?"
    r"(?:TOKEN|API[_-]?KEY|ACCESS[_-]?KEY|PASSWORD|PASSWD|SECRET)"
    r"\s*[:=]\s*[\"']?\[redacted\][\"']?",
    re.IGNORECASE,
)
_MONITOR_REDACTED_BEARER_RE = re.compile(
    r"\bBearer\s+\[redacted\]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MonitorOperationHandle:
    operation_id: str
    should_finish: bool


def build_monitor_operation_payload(
    *,
    workspace: Workspace,
    action: str,
    requested_action: str,
    reason: str | None,
    reason_code: str,
    pr_number: int,
    source_head_sha: str | None,
    source_base_sha: str | None,
    target_branch: str | None,
    remote_branch: str | None,
    recovery_mode: str | None = None,
    stale_reason: str | None = None,
    log_stream_refs: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "owner": "pr_monitor",
        "source": "pr_monitor",
        "action": action,
        "requested_action": requested_action,
        "reason": reason,
        "reason_code": reason_code,
        "pr_number": pr_number,
        "pr_url": workspace.pr_url,
        "source_head_sha": source_head_sha,
        "source_base_sha": source_base_sha,
        "target_branch": target_branch,
        "remote_branch": remote_branch,
    }
    if recovery_mode is not None:
        payload["recovery_mode"] = recovery_mode
    if stale_reason is not None:
        payload["stale_reason"] = stale_reason
    if log_stream_refs:
        payload["log_stream_refs"] = dict(log_stream_refs)
    if extra:
        payload.update(dict(extra))
    return cast(dict[str, Any], redact_monitor_operation_value(_drop_none(payload)))


def monitor_operation_idempotency_key(
    *,
    workspace_id: str,
    action: str,
    pr_number: int | None,
    reason_code: str | None,
    source_head_sha: str | None,
    source_base_sha: str | None,
    extra: Sequence[object] = (),
) -> str:
    identity = {
        "workspace_id": workspace_id,
        "action": action,
        "pr_number": pr_number,
        "reason_code": reason_code,
        "source_head_sha": source_head_sha,
        "source_base_sha": source_base_sha,
        "extra": list(extra),
    }
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"pr_monitor:{action}:{digest[:48]}"


async def retryable_monitor_operation_idempotency_key(
    repo: OperationRepository,
    *,
    workspace_id: str,
    action: str,
    pr_number: int | None,
    reason_code: str | None,
    source_head_sha: str | None,
    source_base_sha: str | None,
    extra: Sequence[object] = (),
) -> str:
    """Return an idempotency key that can retry an unhandled monitor operation.

    The base key is stable for a single monitor-observed PR state, which is
    what prevents duplicate active dispatches. Once the operation has failed or
    been cancelled, the same stable key would point back at a terminal row
    forever, so retries derive their identity from the operation that triggered
    them.
    """
    retry_extra = tuple(extra)
    while True:
        idempotency_key = monitor_operation_idempotency_key(
            workspace_id=workspace_id,
            action=action,
            pr_number=pr_number,
            reason_code=reason_code,
            source_head_sha=source_head_sha,
            source_base_sha=source_base_sha,
            extra=retry_extra,
        )
        existing = await repo.get_by_idempotency_key(idempotency_key)
        if existing is None or existing.status not in RETRYABLE_MONITOR_OPERATION_STATUSES:
            return idempotency_key
        retry_marker = f"retry_after_{existing.status}_operation"
        retry_extra = (
            *tuple(extra),
            retry_marker,
            existing.id,
        )


async def create_or_start_monitor_operation(
    session: AsyncSession,
    *,
    workspace_id: str,
    operation_type: OperationType | str,
    payload: dict[str, Any],
    idempotency_key: str,
    status: OperationStatus,
) -> MonitorOperationHandle:
    repo = OperationRepository(session)
    operation, created = await repo.create_idempotent(
        workspace_id=workspace_id,
        operation_type=operation_type,
        status=status,
        payload=payload,
        idempotency_key=idempotency_key,
    )
    if (
        not created
        and operation.status == OperationStatus.pending.value
        and status == OperationStatus.running
    ):
        await repo.start(operation)
    should_finish = operation.status in {
        OperationStatus.pending.value,
        OperationStatus.running.value,
    }
    return MonitorOperationHandle(
        operation_id=operation.id,
        should_finish=should_finish,
    )


async def begin_monitor_state_operation(
    session: AsyncSession,
    *,
    workspace: Workspace,
    action: str,
    requested_action: str,
    reason: str | None,
    reason_code: str,
    pr_number: int,
    source_head_sha: str | None,
    source_base_sha: str | None,
    target_branch: str | None,
    remote_branch: str | None,
    operation_status: OperationStatus = OperationStatus.running,
    recovery_mode: str | None = None,
    stale_reason: str | None = None,
    log_stream_refs: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
    extra_identity: Sequence[object] = (),
) -> MonitorOperationHandle:
    payload = build_monitor_operation_payload(
        workspace=workspace,
        action=action,
        requested_action=requested_action,
        reason=reason,
        reason_code=reason_code,
        pr_number=pr_number,
        source_head_sha=source_head_sha,
        source_base_sha=source_base_sha,
        target_branch=target_branch,
        remote_branch=remote_branch,
        recovery_mode=recovery_mode,
        stale_reason=stale_reason,
        log_stream_refs=log_stream_refs,
        extra=extra,
    )
    idempotency_key = monitor_operation_idempotency_key(
        workspace_id=workspace.id,
        action=action,
        pr_number=pr_number,
        reason_code=reason_code,
        source_head_sha=source_head_sha,
        source_base_sha=source_base_sha,
        extra=extra_identity,
    )
    return await create_or_start_monitor_operation(
        session,
        workspace_id=workspace.id,
        operation_type=OperationType.monitor_state,
        payload=payload,
        idempotency_key=idempotency_key,
        status=operation_status,
    )


async def record_monitor_state_operation(
    session: AsyncSession,
    *,
    workspace: Workspace,
    action: str,
    requested_action: str,
    reason: str | None,
    reason_code: str,
    pr_number: int,
    source_head_sha: str | None,
    source_base_sha: str | None,
    target_branch: str | None,
    remote_branch: str | None,
    result: Mapping[str, Any] | None = None,
    recovery_mode: str | None = None,
    stale_reason: str | None = None,
    log_stream_refs: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
    extra_identity: Sequence[object] = (),
) -> Operation | None:
    handle = await begin_monitor_state_operation(
        session,
        workspace=workspace,
        action=action,
        requested_action=requested_action,
        reason=reason,
        reason_code=reason_code,
        pr_number=pr_number,
        source_head_sha=source_head_sha,
        source_base_sha=source_base_sha,
        target_branch=target_branch,
        remote_branch=remote_branch,
        recovery_mode=recovery_mode,
        stale_reason=stale_reason,
        log_stream_refs=log_stream_refs,
        extra=extra,
        extra_identity=extra_identity,
    )
    if not handle.should_finish:
        return await OperationRepository(session).get(handle.operation_id)
    return await finish_monitor_operation(
        session,
        operation_id=handle.operation_id,
        status=OperationStatus.succeeded,
        result=result,
    )


async def finish_monitor_operation(
    session: AsyncSession,
    *,
    operation_id: str,
    status: OperationStatus,
    result: Mapping[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> Operation | None:
    repo = OperationRepository(session)
    operation = await repo.get(operation_id)
    if operation is None:  # pragma: no cover - defensive cleanup race.
        return None
    if operation.status not in {
        OperationStatus.pending.value,
        OperationStatus.running.value,
    }:
        return operation
    return await repo.finish(
        operation,
        status=status,
        result=redact_monitor_operation_value(dict(result or {})),
        error_code=error_code,
        error_message=(redact_audit_text(error_message) if error_message is not None else None),
    )


def redact_monitor_operation_value(value: Any) -> Any:
    return _collapse_monitor_secret_assignments(redact_audit_value(value))


def _collapse_monitor_secret_assignments(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _collapse_monitor_secret_assignments(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_collapse_monitor_secret_assignments(item) for item in value]
    if isinstance(value, str):
        redacted = _MONITOR_REDACTED_ASSIGNMENT_RE.sub("[redacted]", value)
        return _MONITOR_REDACTED_BEARER_RE.sub("[redacted]", redacted)
    return value


def _drop_none(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}
