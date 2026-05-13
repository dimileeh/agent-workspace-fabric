"""Helpers for preserving primary workspace failure evidence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import ValidationRun, Workspace, WorkspaceEvent

PRIMARY_FAILURE_KEY = "primary_failure"
SECONDARY_FAILURE_KEY = "secondary_failure"


async def load_primary_failure_snapshot(
    session: AsyncSession,
    workspace: Workspace,
) -> dict[str, Any] | None:
    """Return durable primary failure evidence for ``workspace`` when present."""

    latest_failed_event = await _latest_failed_state_event(
        session,
        workspace.id,
        require_primary_failure=True,
    )
    if latest_failed_event is None:
        latest_failed_event = await _latest_failed_state_event(session, workspace.id)
    latest_validation_run = await _latest_failed_validation_run(session, workspace.id)
    event_payload = _mapping(latest_failed_event.payload if latest_failed_event else None)
    embedded_primary = _mapping(event_payload.get(PRIMARY_FAILURE_KEY) if event_payload else None)

    # Embedded primary payloads can outlive a resumed workspace after its live
    # failure fields are cleared, so they may enrich but not bootstrap evidence.
    has_workspace_evidence = bool(workspace.failure_reason or workspace.failure_message)
    has_validation_evidence = (
        latest_validation_run is not None
        and workspace.failure_reason == FailureReason.validation_failure.value
    )
    if not has_workspace_evidence and not has_validation_evidence:
        return None

    snapshot: dict[str, Any] = {}
    if embedded_primary:
        snapshot.update(_jsonable_mapping(embedded_primary))

    if workspace.failure_reason and "failure_reason" not in snapshot:
        snapshot["failure_reason"] = workspace.failure_reason
    primary_failure_reason = _string(snapshot.get("failure_reason"))
    primary_is_validation_failure = primary_failure_reason == FailureReason.validation_failure.value
    has_embedded_validation_run = _mapping(snapshot.get("validation_run")) is not None
    validation_run_to_attach = (
        latest_validation_run
        if primary_is_validation_failure and not has_embedded_validation_run
        else None
    )

    message = (
        _string(snapshot.get("message"))
        or workspace.failure_message
        or _payload_str(event_payload, "message")
    )
    if message:
        snapshot["message"] = message

    event_reason_code = _payload_str(event_payload, "reason_code") or (
        latest_failed_event.reason_code if latest_failed_event else None
    )
    validation_reason_code = (
        validation_run_to_attach.reason_code if validation_run_to_attach is not None else None
    )
    reason_code = (
        validation_reason_code
        if primary_is_validation_failure and validation_reason_code
        else _string(snapshot.get("reason_code")) or event_reason_code or validation_reason_code
    )
    if reason_code:
        snapshot["reason_code"] = reason_code

    details = _mapping(snapshot.get("details")) or _mapping(
        event_payload.get("details") if event_payload else None
    )
    if details:
        snapshot["details"] = _jsonable_mapping(details)

    if validation_run_to_attach is not None:
        validation_snapshot = _validation_run_snapshot(validation_run_to_attach)
        snapshot["validation_run"] = validation_snapshot
        coverage = _mapping(validation_snapshot.get("coverage"))
        if coverage:
            snapshot["coverage"] = _jsonable_mapping(coverage)
            _copy_if_present(snapshot, coverage, "percent", "coverage_percent")
            _copy_if_present(snapshot, coverage, "minimum_percent", "coverage_minimum_percent")
            _copy_if_present(snapshot, coverage, "threshold", "coverage_threshold")
            _copy_if_present(snapshot, coverage, "failing_test_node_ids")
            _copy_if_present(snapshot, coverage, "failing_test_evidence")

    return snapshot or None


def primary_failure_reason_code(
    primary_failure: Mapping[str, Any] | None,
    *,
    fallback: str,
) -> str:
    if primary_failure is not None:
        reason_code = _string(primary_failure.get("reason_code"))
        if reason_code:
            return reason_code
    return fallback


def build_preserved_failure_payload(
    primary_failure: Mapping[str, Any],
    *,
    secondary_failure: Mapping[str, Any],
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    primary = _jsonable_mapping(primary_failure)
    secondary = _jsonable_mapping(secondary_failure)
    payload: dict[str, Any] = dict(_jsonable_mapping(extra or {}))
    reason_code = _string(primary.get("reason_code"))
    message = _string(primary.get("message"))
    if reason_code:
        payload["reason_code"] = reason_code
    if message:
        payload["message"] = message
    details = _mapping(primary.get("details"))
    if details:
        payload["details"] = _jsonable_mapping(details)
    payload[PRIMARY_FAILURE_KEY] = primary
    payload[SECONDARY_FAILURE_KEY] = secondary
    return payload


def attach_primary_failure(
    payload: Mapping[str, Any],
    primary_failure: Mapping[str, Any] | None,
) -> dict[str, Any]:
    updated = _jsonable_mapping(payload)
    if primary_failure is not None:
        updated[PRIMARY_FAILURE_KEY] = _jsonable_mapping(primary_failure)
    return updated


async def _latest_failed_state_event(
    session: AsyncSession,
    workspace_id: str,
    *,
    require_primary_failure: bool = False,
) -> WorkspaceEvent | None:
    stmt = select(WorkspaceEvent).where(
        WorkspaceEvent.workspace_id == workspace_id,
        WorkspaceEvent.event_type == "workspace.state_changed",
        WorkspaceEvent.new_state == WorkspaceStatus.failed.value,
    )
    if require_primary_failure:
        stmt = stmt.where(func.json_typeof(WorkspaceEvent.payload[PRIMARY_FAILURE_KEY]) == "object")
    stmt = stmt.order_by(WorkspaceEvent.occurred_at.desc(), WorkspaceEvent.id.desc()).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _latest_failed_validation_run(
    session: AsyncSession,
    workspace_id: str,
) -> ValidationRun | None:
    stmt = (
        select(ValidationRun)
        .where(
            ValidationRun.workspace_id == workspace_id,
            ValidationRun.status == "failed",
        )
        .order_by(
            ValidationRun.finished_at.desc().nullslast(),
            ValidationRun.started_at.desc(),
            ValidationRun.id.desc(),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _validation_run_snapshot(run: ValidationRun) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "id": run.id,
        "status": run.status,
        "reason_code": run.reason_code,
        "tier": run.tier,
        "attempt_id": run.attempt_id,
        "command_set_hash": run.command_set_hash,
        "commands": _jsonable(run.commands),
        "base_commit": run.base_commit,
        "base_sha": run.base_sha,
        "workspace_head_sha": run.workspace_head_sha,
        "target_branch": run.target_branch,
        "target_head_sha": run.target_head_sha,
        "profile_name": run.profile_name,
        "profile_version": run.profile_version,
        "profile_source": run.profile_source,
        "resolved_profile_digest": run.resolved_profile_digest,
        "environment_identity_digest": run.environment_identity_digest,
        "environment_identity_inputs": _jsonable(run.environment_identity_inputs),
        "started_at": _jsonable(run.started_at),
        "finished_at": _jsonable(run.finished_at),
        "log_stream_refs": _jsonable(run.log_stream_refs),
        "coverage": _jsonable(run.coverage),
    }
    return {key: value for key, value in snapshot.items() if value is not None}


def _copy_if_present(
    target: dict[str, Any],
    source: Mapping[str, Any],
    source_key: str,
    target_key: str | None = None,
) -> None:
    if source_key in source:
        target[target_key or source_key] = _jsonable(source[source_key])


def _payload_str(payload: Mapping[str, Any] | None, key: str) -> str | None:
    if payload is None:
        return None
    return _string(payload.get(key))


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _jsonable_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): _jsonable(item) for key, item in value.items()}


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return _jsonable_mapping(value)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_jsonable(item) for item in value]
    return value
