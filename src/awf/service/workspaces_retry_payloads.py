"""Compact payload helpers for workspace retry flows.

Mechanically extracted from ``awf.service.workspaces_retry`` so that module stays
under the first-party line-count guardrail. Pure data shaping only — no retry
orchestration. Re-exported from ``workspaces_retry`` for import compatibility.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from awf.db.enums import WorkspaceStatus
from awf.db.models import Workspace


def _latest_failed_state_event(workspace: Workspace) -> Any | None:
    """Return the most recent workspace state-changed event with a failed status, or None."""
    for event in reversed(getattr(workspace, "events", []) or []):
        if (
            getattr(event, "event_type", None) == "workspace.state_changed"
            and getattr(event, "new_state", None) == WorkspaceStatus.failed.value
        ):
            return event
    return None


def _compact_conformance_payload(value: object) -> dict[str, Any] | None:
    """Extract a compact conformance payload with only relevant string and integer fields."""
    if not isinstance(value, Mapping):
        return None
    payload: dict[str, Any] = {}
    for key in (
        "summary",
        "reason_code",
        "report_reason_code",
        "plan_path",
        "report_path",
    ):
        item = value.get(key)
        if isinstance(item, str):
            payload[key] = item
    gaps = value.get("gaps")
    if isinstance(gaps, list):
        payload["gaps"] = [gap for gap in gaps if isinstance(gap, str)]
    for key in ("iterations_used", "max_iterations"):
        item = value.get(key)
        if isinstance(item, int):
            payload[key] = item
    return payload or None


def _compact_planning_scope_payload(value: object) -> dict[str, Any] | None:
    """Extract a compact planning-scope payload with only relevant string and list fields."""
    if not isinstance(value, Mapping):
        return None
    payload: dict[str, Any] = {}
    for key in (
        "scope_phase",
        "recommended_action",
        "recovery_strategy",
        "salvage_policy",
        "plan_artifact",
    ):
        item = value.get(key)
        if isinstance(item, str) and item:
            payload[key] = item
    for key in ("required_paths", "offending_paths", "offending_commands"):
        items = _compact_string_list(value.get(key))
        if items:
            payload[key] = items
    if "offending_paths" not in payload:
        forbidden = _compact_string_list(value.get("forbidden_paths"))
        if forbidden:
            payload["offending_paths"] = forbidden
    fallback_model = _compact_fallback_model(value.get("fallback_model"))
    if fallback_model is not None:
        payload["fallback_model"] = fallback_model
    return payload or None


def _compact_string_list(value: object) -> list[str]:
    """Filter a value to a list of non-empty strings."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _compact_fallback_model(value: object) -> dict[str, str] | None:
    """Extract a compact fallback model dict with model name and optional source."""
    if not isinstance(value, Mapping):
        return None
    model = value.get("model")
    source = value.get("source")
    if not isinstance(model, str) or not model.strip():
        return None
    payload = {"model": model.strip()}
    if isinstance(source, str) and source.strip():
        payload["source"] = source.strip()
    return payload


def _compact_salvage_payload(value: object) -> dict[str, str] | None:
    """Extract a compact salvage payload with hint, worktree, branch, and remote-push fields."""
    if not isinstance(value, Mapping):
        return None
    payload = {
        key: item
        for key in ("hint", "worktree_path", "branch_name", "remote_push_branch")
        if isinstance((item := value.get(key)), str) and item
    }
    return payload or None


def _payload_str(payload: Mapping[str, Any], key: str) -> str | None:
    """Return a string value from a payload dict by key, or None if not a string."""
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _retry_evidence_gaps(evidence: Mapping[str, Any]) -> list[str]:
    """Extract a list of non-empty evidence gap strings from conformance evidence."""
    value = evidence.get("gaps")
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _optional_retry_evidence_str(value: object) -> str | None:
    """Return a stripped non-empty string from a value, or None."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _approved_planning_scope_fallback_model(
    workspace: Workspace,
) -> dict[str, str] | None:
    """Return the approved fallback model from the workspace's planning-scope recovery policy."""
    task_policy = getattr(workspace, "task_policy", None)
    if not isinstance(task_policy, Mapping):
        return None
    recovery_policy = task_policy.get("planning_scope_recovery")
    if not isinstance(recovery_policy, Mapping):
        return None
    model = recovery_policy.get("approved_fallback_model")
    if not isinstance(model, str) or not model.strip():
        return None
    return {
        "model": model.strip(),
        "source": "task_policy.planning_scope_recovery.approved_fallback_model",
    }
