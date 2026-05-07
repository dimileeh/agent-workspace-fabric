from __future__ import annotations

from sqlalchemy import inspect

from awf.db.enums import TaskClass
from awf.db.models import (
    ValidationRun,
    Workspace,
    stale_reason_blocks_merge,
    stale_reason_severity,
)

DOCS_TASK_SCOPE_VIOLATION_STALE_REASON = "docs_task_scope_violation"
VALIDATION_INSUFFICIENT_TIER_STALE_REASON = "validation_insufficient_tier"

__all__ = [
    "DOCS_TASK_SCOPE_VIOLATION_STALE_REASON",
    "VALIDATION_INSUFFICIENT_TIER_STALE_REASON",
    "compute_stale_reason",
    "compute_stale_reason_for_attempt",
    "stale_reason_blocks_merge",
    "stale_reason_required_action",
    "stale_reason_severity",
]


def stale_reason_required_action(reason_code: str | None) -> str | None:
    if not stale_reason_blocks_merge(reason_code):
        return None
    if reason_code == VALIDATION_INSUFFICIENT_TIER_STALE_REASON:
        return "validate"
    if reason_code == DOCS_TASK_SCOPE_VIOLATION_STALE_REASON:
        return "resolve_task_scope"
    return "rebase"


def _task_class_tier(task_class: str | None) -> int:
    if task_class == TaskClass.migration_task.value:
        return 3
    if task_class in (
        TaskClass.refactor_task.value,
        TaskClass.dependency_task.value,
        TaskClass.build_config_task.value,
    ):
        return 2
    return 1


def compute_stale_reason(workspace: Workspace) -> tuple[str | None, str | None]:
    """Return (stale_reason, required_next_action)."""
    state = inspect(workspace)
    operations = workspace.operations if "operations" not in state.unloaded else []
    validation_runs = workspace.validation_runs if "validation_runs" not in state.unloaded else []

    rebase_time = None
    for op in operations:
        if (
            op.type == "rebase"
            and op.status == "succeeded"
            and (rebase_time is None or op.created_at > rebase_time)
        ):
            rebase_time = op.created_at

    has_rebased = rebase_time is not None

    required_tier = _task_class_tier(workspace.task_class)
    if has_rebased:
        required_tier = max(required_tier, 2)

    default_op_tier = 1
    actual_tier = 1
    if workspace.resolved_profile and "validation" in workspace.resolved_profile:
        default_op_tier = workspace.resolved_profile["validation"].get("requested_tier", 1)

    for op in operations:
        if op.type == "validate" and op.status == "succeeded":
            op_tier = default_op_tier
            if isinstance(op.payload, dict):
                if isinstance(op.payload.get("requested_tier"), int):
                    op_tier = op.payload["requested_tier"]
                elif isinstance(op.payload.get("validation"), dict) and isinstance(
                    op.payload["validation"].get("requested_tier"),
                    int,
                ):
                    op_tier = op.payload["validation"]["requested_tier"]

            if rebase_time and op.created_at > rebase_time:
                op_tier = max(op_tier, 2)

            actual_tier = max(actual_tier, op_tier)

    satisfied_validation_run_tier = 0
    deferred_required_coverage = False
    for run in validation_runs:
        if rebase_time and run.started_at <= rebase_time:
            continue

        if run.status == "succeeded" and _validation_run_deferred_required_coverage(run):
            deferred_required_coverage = True
            continue

        run_tier = _successful_validation_run_tier(run)
        if run_tier is None:
            continue

        satisfied_validation_run_tier = max(satisfied_validation_run_tier, run_tier)
        actual_tier = max(actual_tier, run_tier)

    if deferred_required_coverage and satisfied_validation_run_tier < required_tier:
        return VALIDATION_INSUFFICIENT_TIER_STALE_REASON, "validate"

    if actual_tier < required_tier:
        return VALIDATION_INSUFFICIENT_TIER_STALE_REASON, "validate"

    return None, None


def compute_stale_reason_for_attempt(
    workspace: Workspace,
    *,
    attempt_id: str,
) -> tuple[str | None, str | None]:
    """Return stale state using validation provenance for one attempt only."""
    state = inspect(workspace)
    operations = workspace.operations if "operations" not in state.unloaded else []
    validation_runs = workspace.validation_runs if "validation_runs" not in state.unloaded else []

    rebase_time = None
    for op in operations:
        if (
            op.type == "rebase"
            and op.status == "succeeded"
            and (rebase_time is None or op.created_at > rebase_time)
        ):
            rebase_time = op.created_at

    required_tier = _task_class_tier(workspace.task_class)
    if rebase_time is not None:
        required_tier = max(required_tier, 2)

    actual_tier = 0
    for run in validation_runs:
        if run.attempt_id != attempt_id:
            continue
        run_tier = _successful_validation_run_tier(run)
        if run_tier is None:
            continue

        if rebase_time and run.started_at <= rebase_time:
            continue

        actual_tier = max(actual_tier, run_tier)

    if actual_tier < required_tier:
        return VALIDATION_INSUFFICIENT_TIER_STALE_REASON, "validate"

    return None, None


def _successful_validation_run_tier(run: ValidationRun) -> int | None:
    if run.status != "succeeded":
        return None
    if _validation_run_deferred_required_coverage(run):
        return None
    return run.tier


def _validation_run_deferred_required_coverage(run: ValidationRun) -> bool:
    """Return True when a successful run only satisfied the targeted edit gate.

    PR-bound workspaces may intentionally skip the expensive final coverage
    command before opening/updating a PR. That targeted-only run is useful
    evidence for the edit, but it must not clear merge eligibility for a
    profile whose final gate is coverage.
    """

    for command in run.commands or []:
        if command.get("phase") != "coverage":
            continue
        if (
            command.get("evidence_status") == "skipped_by_policy"
            and command.get("evidence_reason_code") == "TARGETED_EDIT_GATE"
        ):
            return True
    return False
