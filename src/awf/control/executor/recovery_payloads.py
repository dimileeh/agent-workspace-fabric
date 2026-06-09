"""Executor recovery payload helpers."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast

from awf.control.executor.constants import (
    _RECOVERY_ACTIVE_OPERATION_STATUSES,
    _VALIDATE_ONLY_RECOVERY_MODES,
    _VALIDATE_ONLY_RECOVERY_SOURCES,
)
from awf.control.executor.metadata import _int_or_none
from awf.control.executor.types import _PlanningValidationHandoff, _RebaseRecoveryResult
from awf.db.enums import OperationType
from awf.profiles.models import WorkspaceProfile
from awf.runtime.planning import (
    CONFORMANCE_REQUIRES_AWF_VALIDATION,
    PlanConformanceReport,
    PlanConformanceStatus,
    render_workspace_path,
)


def _get_active_recovery_payload(workspace: Any) -> dict[str, Any] | None:
    """Return the active monitor/operator recovery payload (or ``None``).

    Recovery operations use a pending/running operation with ``recovery_mode``
    set. Validate-only recovery is recorded as ``validate``; public operator
    rebase requests are recorded as ``rebase`` but run through the same
    rebase-only executor path.
    """
    operations = list(getattr(workspace, "operations", None) or [])

    def _op_rank(operation: Any) -> tuple[float, str]:
        stamp = getattr(operation, "started_at", None) or getattr(operation, "created_at", None)
        stamp_value = stamp.timestamp() if isinstance(stamp, datetime) else 0.0
        return (stamp_value, str(getattr(operation, "id", "")))

    operations.sort(key=_op_rank, reverse=True)
    for operation in operations:
        if operation.status not in _RECOVERY_ACTIVE_OPERATION_STATUSES:
            continue
        operation_type = getattr(operation, "type", None)
        if operation_type not in {
            OperationType.validate.value,
            OperationType.rebase.value,
        }:
            continue
        payload = operation.payload
        if not _is_validate_only_recovery_payload(payload):
            continue
        recovery_payload = cast(dict[str, Any], payload)
        if (
            operation_type == OperationType.rebase.value
            and recovery_payload.get("recovery_mode") != "rebase_only"
        ):
            continue
        return recovery_payload
    return None


def _is_validate_only_recovery_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    return (
        payload.get("source") in _VALIDATE_ONLY_RECOVERY_SOURCES
        and payload.get("recovery_mode") in _VALIDATE_ONLY_RECOVERY_MODES
    )


def _planning_validation_handoff_from_recovery_payload(
    *,
    workspace_id: str,
    profile: WorkspaceProfile,
    recovery_payload: Mapping[str, Any],
) -> _PlanningValidationHandoff | None:
    conformance_payload = recovery_payload.get("conformance")
    conformance = conformance_payload if isinstance(conformance_payload, Mapping) else {}
    reason_code = _recovery_conformance_reason_code(recovery_payload, conformance)
    if reason_code != CONFORMANCE_REQUIRES_AWF_VALIDATION:
        return None
    try:
        plan_path = render_workspace_path(
            _recovery_conformance_path(
                conformance,
                key="plan_path",
                fallback=profile.planning.plan_path,
            ),
            workspace_id=workspace_id,
        )
        report_path = render_workspace_path(
            _recovery_conformance_path(
                conformance,
                key="report_path",
                fallback=profile.planning.conformance_report_path,
            ),
            workspace_id=workspace_id,
        )
    except ValueError:
        plan_path = render_workspace_path(
            profile.planning.plan_path,
            workspace_id=workspace_id,
        )
        report_path = render_workspace_path(
            profile.planning.conformance_report_path,
            workspace_id=workspace_id,
        )
    gaps = _recovery_conformance_gaps(conformance)
    summary_value = conformance.get("summary")
    summary = (
        summary_value.strip()
        if isinstance(summary_value, str) and summary_value.strip()
        else "Conformance requires AWF-owned validation evidence."
    )
    iteration = _int_or_none(conformance.get("iteration"))
    if iteration is None:
        iteration = _int_or_none(recovery_payload.get("iteration"))
    max_iterations = _int_or_none(conformance.get("max_iterations"))
    if max_iterations is None:
        max_iterations = _int_or_none(recovery_payload.get("max_iterations"))
    return _PlanningValidationHandoff(
        report=PlanConformanceReport(
            status=PlanConformanceStatus.needs_iteration,
            summary=summary,
            gaps=gaps or ("AWF-owned validation evidence is missing or stale.",),
            reason_code=CONFORMANCE_REQUIRES_AWF_VALIDATION,
        ),
        plan_path=plan_path,
        report_path=report_path,
        iteration=iteration if iteration is not None else 0,
        max_iterations=(
            max_iterations if max_iterations is not None else profile.planning.max_iterations
        ),
    )


def _recovery_conformance_reason_code(
    recovery_payload: Mapping[str, Any],
    conformance: Mapping[str, Any],
) -> str | None:
    for value in (
        conformance.get("report_reason_code"),
        conformance.get("reason_code"),
        recovery_payload.get("conformance_reason_code"),
        recovery_payload.get("conformance_handoff_reason_code"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip().upper().replace("-", "_")
    return None


def _recovery_conformance_path(
    conformance: Mapping[str, Any],
    *,
    key: str,
    fallback: str,
) -> str:
    value = conformance.get(key)
    return value if isinstance(value, str) and value.strip() else fallback


def _recovery_conformance_gaps(conformance: Mapping[str, Any]) -> tuple[str, ...]:
    value = conformance.get("gaps")
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, str) and value.strip():
        return (value.strip(),)
    return ()


def _recovery_needs_existing_pr_push(
    recovery_payload: Mapping[str, Any],
    *,
    validated_workspace_head_sha: str | None,
    rebase_recovery_result: _RebaseRecoveryResult | None,
) -> bool:
    """Return true when recovery produced local commits that are not on the PR yet."""
    if not validated_workspace_head_sha:
        return False
    validated_head = validated_workspace_head_sha.strip()
    if not validated_head:
        return False
    recovery_mode = recovery_payload.get("recovery_mode")
    if recovery_mode == "validate_only":
        if rebase_recovery_result is not None:
            return False
        source_head_sha = recovery_payload.get("source_head_sha")
        if not isinstance(source_head_sha, str) or not source_head_sha.strip():
            return False
        return validated_head != source_head_sha.strip()
    if recovery_mode == "rebase_only":
        if rebase_recovery_result is None:
            return False
        if rebase_recovery_result.requires_pr_update:
            return True
        # Otherwise push only when AWF-owned work, such as validation fixes or a
        # conformance report commit, advanced HEAD past the recorded recovery head.
        return validated_head != rebase_recovery_result.head_sha
    return False
