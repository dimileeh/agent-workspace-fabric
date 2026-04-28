"""Durable operation helpers for PR monitor recovery actions."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import OperationStatus, OperationType
from awf.db.models import Operation, Workspace
from awf.db.repositories import OperationRepository

_SENSITIVE_KEY_RE = re.compile(
    r"(authorization|bearer|password|passwd|secret|token|api[_-]?key|access[_-]?key)",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"Bearer\s+\S+", re.IGNORECASE),
    re.compile(r"github_pat_[A-Za-z0-9_]+"),
    re.compile(r"ghp_[A-Za-z0-9_]+"),
    re.compile(r"sk-[A-Za-z0-9_-]+"),
    re.compile(r"(?i)\b(?:token|password|secret|api[_-]?key)=\S+"),
)
_MAX_STRING_LENGTH = 1000


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
        error_message=_redact_string(error_message) if error_message is not None else None,
    )


def redact_monitor_operation_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _SENSITIVE_KEY_RE.search(key_text):
                redacted[key_text] = "[redacted]"
            else:
                redacted[key_text] = redact_monitor_operation_value(item)
        return redacted
    if isinstance(value, list):
        return [redact_monitor_operation_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_monitor_operation_value(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _redact_string(value: str) -> str:
    redacted = value
    for pattern in _SECRET_VALUE_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    if len(redacted) > _MAX_STRING_LENGTH:
        return f"{redacted[:_MAX_STRING_LENGTH]}...[truncated]"
    return redacted


def _drop_none(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}
