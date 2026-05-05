"""Workspace coordination warning helpers.

Owned-path overlap is advisory in AWF: it should give agents and operators
context, but it must not act like an admission lock.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from awf.common.coordination import MAX_COORDINATION_WARNING_OVERLAPS
from awf.db.repositories import OwnedPathOverlap, owned_path_overlap_match

COORDINATION_TASK_POLICY_KEY: Final = "coordination"
COORDINATION_WARNINGS_KEY: Final = "warnings"
OWNED_PATH_OVERLAP_RISK_CODE: Final = "OWNED_PATH_OVERLAP_RISK"
OWNED_PATH_OVERLAP_RISK_MESSAGE: Final = (
    "Owned paths overlap active workspaces; coordinate before editing the same paths."
)
COORDINATION_WARNING_SEVERITY_ADVISORY: Final = "advisory"
OWNED_PATH_OVERLAP_BLOCKS_LAUNCH: Final = False
OWNED_PATH_OVERLAP_TRIGGER_TYPE: Final = "path_overlap"
OWNED_PATH_OVERLAP_STALE_REASON_CODE: Final = "STALE_OVERLAP"
MAX_COORDINATION_WARNING_WORKSPACES: Final = 20
MAX_COORDINATION_WARNINGS: Final = 5


def owned_path_overlap_coordination_warnings(
    overlaps: Sequence[OwnedPathOverlap],
) -> list[dict[str, Any]]:
    """Build bounded advisory coordination warnings for active owned-path overlaps."""

    if not overlaps:
        return []
    workspace_ids: dict[str, None] = {}
    overlap_items: list[dict[str, Any]] = []
    for overlap in overlaps:
        if len(workspace_ids) < MAX_COORDINATION_WARNING_WORKSPACES:
            workspace_ids.setdefault(overlap.workspace_id, None)
        if len(overlap_items) >= MAX_COORDINATION_WARNING_OVERLAPS:
            continue
        item: dict[str, Any] = {
            "workspace_id": overlap.workspace_id,
            "existing_path": overlap.existing_path,
            "requested_path": overlap.requested_path,
        }
        match = owned_path_overlap_match(overlap.existing_path, overlap.requested_path)
        if match is not None:
            item["match_reason_code"] = match.match_reason_code
            item["explanation"] = match.explanation
        overlap_items.append(item)

    return [
        {
            "warning_code": OWNED_PATH_OVERLAP_RISK_CODE,
            "message": OWNED_PATH_OVERLAP_RISK_MESSAGE,
            "severity": COORDINATION_WARNING_SEVERITY_ADVISORY,
            "blocks_launch": OWNED_PATH_OVERLAP_BLOCKS_LAUNCH,
            "workspace_ids": list(workspace_ids),
            "overlaps": overlap_items,
            "overlap_count": len(overlaps),
            "overlaps_truncated": len(overlaps) > len(overlap_items),
            "stale_policy_context": {
                "trigger_type": OWNED_PATH_OVERLAP_TRIGGER_TYPE,
                "stale_reason_code": OWNED_PATH_OVERLAP_STALE_REASON_CODE,
            },
        }
    ]


def task_policy_with_coordination_warnings(
    task_policy: Mapping[str, Any] | None,
    warnings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return task policy with recomputed coordination warnings.

    Existing task-policy keys such as model and out-of-scope policy are
    preserved. The warning list is replaced so retries do not carry stale
    overlapping workspace ids from prior attempts.
    """

    policy = dict(task_policy or {})
    coordination = policy.get(COORDINATION_TASK_POLICY_KEY)
    coordination_payload = dict(coordination) if isinstance(coordination, Mapping) else {}
    sanitized_warnings = coordination_warnings_from_payload(warnings)
    if sanitized_warnings:
        coordination_payload[COORDINATION_WARNINGS_KEY] = sanitized_warnings
    else:
        coordination_payload.pop(COORDINATION_WARNINGS_KEY, None)

    if coordination_payload:
        policy[COORDINATION_TASK_POLICY_KEY] = coordination_payload
    else:
        policy.pop(COORDINATION_TASK_POLICY_KEY, None)
    return policy


def coordination_warnings_from_task_policy(
    task_policy: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    coordination = (task_policy or {}).get(COORDINATION_TASK_POLICY_KEY)
    if not isinstance(coordination, Mapping):
        return []
    warnings = coordination.get(COORDINATION_WARNINGS_KEY)
    if not isinstance(warnings, Sequence) or isinstance(warnings, str | bytes):
        return []
    return coordination_warnings_from_payload(warnings)


def coordination_warnings_from_payload(
    warnings: Sequence[Mapping[str, Any] | object],
) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for warning in warnings:
        if len(sanitized) >= MAX_COORDINATION_WARNINGS:
            break
        if not isinstance(warning, Mapping):
            continue
        warning_code = _string(warning.get("warning_code")) or OWNED_PATH_OVERLAP_RISK_CODE
        severity = _string(warning.get("severity")) or COORDINATION_WARNING_SEVERITY_ADVISORY
        if severity != COORDINATION_WARNING_SEVERITY_ADVISORY:
            severity = COORDINATION_WARNING_SEVERITY_ADVISORY
        overlaps = _overlap_items(warning.get("overlaps"))
        overlap_count = _int_value(warning.get("overlap_count")) or len(overlaps)
        stale_policy_context = _string_mapping(warning.get("stale_policy_context"))
        sanitized.append(
            {
                "warning_code": warning_code,
                "message": _string(warning.get("message")) or OWNED_PATH_OVERLAP_RISK_MESSAGE,
                "severity": severity,
                "blocks_launch": _bool_value(warning.get("blocks_launch")),
                "workspace_ids": _string_list(warning.get("workspace_ids")),
                "overlaps": overlaps,
                "overlap_count": overlap_count,
                "overlaps_truncated": bool(warning.get("overlaps_truncated", False)),
                "stale_policy_context": stale_policy_context,
            }
        )
    return sanitized


def _overlap_items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    items: list[dict[str, Any]] = []
    for item in value:
        if len(items) >= MAX_COORDINATION_WARNING_OVERLAPS:
            break
        if not isinstance(item, Mapping):
            continue
        workspace_id = _string(item.get("workspace_id"))
        existing_path = _string(item.get("existing_path"))
        requested_path = _string(item.get("requested_path"))
        if workspace_id is None or existing_path is None or requested_path is None:
            continue
        payload: dict[str, Any] = {
            "workspace_id": workspace_id,
            "existing_path": existing_path,
            "requested_path": requested_path,
        }
        match_reason_code = _string(item.get("match_reason_code"))
        explanation = _string(item.get("explanation"))
        if match_reason_code is not None:
            payload["match_reason_code"] = match_reason_code
        if explanation is not None:
            payload["explanation"] = explanation
        items.append(payload)
    return items


def _string_list(value: object) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()][
        :MAX_COORDINATION_WARNING_WORKSPACES
    ]


def _string_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(item, str)}


def _string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _int_value(value: object) -> int:
    if isinstance(value, int):
        return max(0, value)
    return 0


def _bool_value(value: object) -> bool:
    return value if isinstance(value, bool) else False
