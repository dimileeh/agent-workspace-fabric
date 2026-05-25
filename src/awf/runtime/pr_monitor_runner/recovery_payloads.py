"""PR monitor recovery payload helpers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from awf.db.enums import OperationType
from awf.db.models import Operation, Workspace
from awf.runtime.planning import CONFORMANCE_REQUIRES_AWF_VALIDATION
from awf.runtime.pr_monitor_runner.constants import (
    _ACTIVE_RECOVERY_OPERATION_STATUSES,
    _PLANNING_VALIDATION_HANDOFF_EVENT,
    _POST_VALIDATION_CONFORMANCE_SATISFIED_EVENT,
)
from awf.service.provider_recovery import (
    PROVIDER_MODEL_CIRCUIT_OPEN_REASON,
    PROVIDER_RECOVERY_STATE_KEY,
)


def _task_policy_with_monitor_circuit_retry_state(
    task_policy: Mapping[str, Any] | None,
    *,
    provider: str,
    model: str,
    cooldown_until: datetime | None,
    last_reason_code: str | None,
) -> dict[str, Any]:
    """Return workspace task policy with monitor retry metadata for provider recovery."""
    policy = dict(task_policy or {})
    raw_state = policy.get(PROVIDER_RECOVERY_STATE_KEY)
    recovery_state = dict(raw_state) if isinstance(raw_state, Mapping) else {}
    recovery_state.update(
        {
            "action": "retry",
            "decision_reason_code": PROVIDER_MODEL_CIRCUIT_OPEN_REASON,
            "source_provider": provider,
            "source_model": model,
            "source_reason_code": last_reason_code or PROVIDER_MODEL_CIRCUIT_OPEN_REASON,
            "target_provider": provider,
            "target_model": model,
        }
    )
    recovery_state["not_before"] = (cooldown_until or datetime.now(UTC)).isoformat()
    policy[PROVIDER_RECOVERY_STATE_KEY] = recovery_state
    return policy


def _normalize_conformance_handoff_reason_code(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().upper().replace("-", "_")


def _monitor_recovery_conformance_payload(workspace: Workspace) -> dict[str, Any] | None:
    """Return conformance handoff context for monitor-dispatched validation recovery."""
    events = getattr(workspace, "events", None) or []
    for event in reversed(events):
        event_type = getattr(event, "event_type", None)
        if event_type == _POST_VALIDATION_CONFORMANCE_SATISFIED_EVENT:
            return None
        if event_type != _PLANNING_VALIDATION_HANDOFF_EVENT:
            continue
        raw_payload = getattr(event, "payload", None)
        payload = raw_payload if isinstance(raw_payload, Mapping) else {}
        reason_code = (
            _normalize_conformance_handoff_reason_code(payload.get("report_reason_code"))
            or _normalize_conformance_handoff_reason_code(payload.get("reason_code"))
            or _normalize_conformance_handoff_reason_code(getattr(event, "reason_code", None))
        )
        if reason_code != CONFORMANCE_REQUIRES_AWF_VALIDATION:
            continue
        conformance: dict[str, Any] = {
            "reason_code": reason_code,
            "report_reason_code": reason_code,
        }
        for key in ("summary", "gaps", "plan_path", "report_path", "iteration", "max_iterations"):
            if key in payload:
                conformance[key] = payload[key]
        return {"conformance": conformance}
    return None


def _is_active_pr_monitor_recovery_operation(operation: Operation) -> bool:
    """Return whether an operation is an unfinished PR monitor recovery action."""
    return (
        operation.status in _ACTIVE_RECOVERY_OPERATION_STATUSES
        and operation.type == OperationType.validate.value
        and isinstance(operation.payload, dict)
        and operation.payload.get("source") == "pr_monitor"
        and operation.payload.get("recovery_mode") is not None
    )
