from __future__ import annotations

from awf.db.enums import TaskClass, WorkspaceStatus
from awf.db.models import Workspace

def _task_class_tier(task_class: str | None) -> int:
    if task_class == TaskClass.migration_task.value:
        return 3
    if task_class in (TaskClass.refactor_task.value, TaskClass.dependency_task.value, TaskClass.build_config_task.value):
        return 2
    return 1

from sqlalchemy import inspect

def compute_stale_reason(workspace: Workspace) -> tuple[str | None, str | None]:
    """Return (stale_reason, required_next_action)."""
    if workspace.status != WorkspaceStatus.monitoring_pr.value:
        return None, None

    state = inspect(workspace)
    if "operations" not in state.unloaded:
        operations = workspace.operations
    else:
        operations = []

    has_rebased = any(op.type == "rebase" and op.status == "succeeded" for op in operations)
    
    required_tier = _task_class_tier(workspace.task_class)
    if has_rebased:
        required_tier = max(required_tier, 2)
        
    actual_tier = 1
    if workspace.resolved_profile and "validation" in workspace.resolved_profile:
        actual_tier = workspace.resolved_profile["validation"].get("requested_tier", 1)
        
    if actual_tier < required_tier:
        return "validation_insufficient_tier", "validate"

    return None, None
