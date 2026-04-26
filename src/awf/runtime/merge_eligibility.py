from __future__ import annotations

from sqlalchemy import inspect

from awf.db.enums import TaskClass, WorkspaceStatus
from awf.db.models import Workspace


def _task_class_tier(task_class: str | None) -> int:
    if task_class == TaskClass.migration_task.value:
        return 3
    if task_class in (TaskClass.refactor_task.value, TaskClass.dependency_task.value, TaskClass.build_config_task.value):
        return 2
    return 1


def compute_stale_reason(workspace: Workspace) -> tuple[str | None, str | None]:
    """Return (stale_reason, required_next_action)."""
    if workspace.status != WorkspaceStatus.monitoring_pr.value:
        return None, None

    state = inspect(workspace)
    operations = workspace.operations if "operations" not in state.unloaded else []

    rebase_time = None
    for op in operations:
        if op.type == "rebase" and op.status == "succeeded":
            if rebase_time is None or op.created_at > rebase_time:
                rebase_time = op.created_at

    has_rebased = rebase_time is not None

    required_tier = _task_class_tier(workspace.task_class)
    if has_rebased:
        required_tier = max(required_tier, 2)

    actual_tier = 1
    if workspace.resolved_profile and "validation" in workspace.resolved_profile:
        actual_tier = workspace.resolved_profile["validation"].get("requested_tier", 1)

    for op in operations:
        if op.type == "validate" and op.status == "succeeded":
            op_tier = actual_tier
            if isinstance(op.payload, dict):
                if isinstance(op.payload.get("requested_tier"), int):
                    op_tier = op.payload["requested_tier"]
                elif isinstance(op.payload.get("validation"), dict) and isinstance(op.payload["validation"].get("requested_tier"), int):
                    op_tier = op.payload["validation"]["requested_tier"]
            
            if rebase_time and op.created_at > rebase_time:
                op_tier = max(op_tier, 2)
                
            actual_tier = max(actual_tier, op_tier)

    if actual_tier < required_tier:
        return "validation_insufficient_tier", "validate"

    return None, None
