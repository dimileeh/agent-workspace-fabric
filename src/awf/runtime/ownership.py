"""Shared ownership-repair helpers for AWF runtime helpers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

from awf.node.git_manager import (
    _linked_worktree_git_dir,
    mirror_path_for_worktree,
    repair_agent_writable_worktree,
)

AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE = "AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED"

EXECUTOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME = (
    "executor.agent_runtime_ownership_repair_failed"
)
MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME = "monitor.agent_runtime_ownership_repair_failed"


def _validated_layout_mirror_for_worktree(
    worktree_path: Path, workspace_id: str
) -> tuple[Path | None, Path | None]:
    """Resolve and validate the linked-worktree mirror and gitdir.

    Control-plane control over git pointers has been compromised during
    monitor recoveries; trust only mirrored worktree pointers that stay under
    the expected ``<worktrees_root>/../mirrors`` hierarchy for this
    worktree path and match this workspace's metadata entry.
    """
    mirror_path = mirror_path_for_worktree(worktree_path)
    linked_worktree_git_dir = _linked_worktree_git_dir(worktree_path)
    if mirror_path is None:
        return None, linked_worktree_git_dir

    expected_mirror_root = worktree_path.parent.parent / "mirrors"
    resolved_expected_root = expected_mirror_root.resolve()
    resolved_mirror = mirror_path.resolve()
    if not resolved_mirror.is_relative_to(resolved_expected_root):
        raise ValueError(
            "refusing ownership repair: mirror path is outside expected mirrors root "
            f"for workspace {worktree_path}: {resolved_mirror}"
        )

    if linked_worktree_git_dir is None:
        raise ValueError(
            "refusing ownership repair: cannot read linked-worktree git metadata "
            f"for workspace {worktree_path}"
        )

    expected_worktree_git_root = resolved_mirror / "worktrees"
    if linked_worktree_git_dir.parent.resolve() != expected_worktree_git_root:
        raise ValueError(
            "refusing ownership repair: linked-worktree metadata points to another "
            f"workspace. expected parent {expected_worktree_git_root}, got {linked_worktree_git_dir.parent}"
        )

    if not linked_worktree_git_dir.name.startswith(workspace_id):
        raise ValueError(
            "refusing ownership repair: linked-worktree metadata points to another "
            f"workspace. expected workspace id {workspace_id}, got {linked_worktree_git_dir.name}"
        )

    linked_worktree_suffix = linked_worktree_git_dir.name.removeprefix(workspace_id)
    if linked_worktree_suffix and not linked_worktree_suffix.isdigit():
        raise ValueError(
            "refusing ownership repair: linked-worktree metadata points to another "
            f"workspace. expected workspace id {workspace_id}, got {linked_worktree_git_dir.name}"
        )

    return mirror_path, linked_worktree_git_dir


class _LoggerProtocol(Protocol):
    """Protocol contract for ownership-repair logging callsites."""

    def exception(
        self,
        event: str,
        *,
        workspace_id: str,
        worktree_path: str,
        reason: str,
        reason_code: str,
    ) -> None:
        """Emit a structured exception event for ownership-repair failures."""
        ...


async def repair_agent_runtime_ownership(
    *,
    logger: _LoggerProtocol,
    workspace_id: str,
    worktree_path: Path,
    reason: str,
    event_name: str,
    reason_code: str = AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
) -> bool:
    """Attempt to repair runtime ownership for an agent worktree."""
    try:
        layout_mirror, linked_worktree_git_dir = _validated_layout_mirror_for_worktree(
            worktree_path, workspace_id
        )
        await asyncio.to_thread(
            repair_agent_writable_worktree,
            layout_mirror,
            worktree_path,
            linked_git_dir=linked_worktree_git_dir,
        )
    except Exception:
        logger.exception(
            event_name,
            workspace_id=workspace_id,
            worktree_path=str(worktree_path),
            reason=reason,
            reason_code=reason_code,
        )
        return False
    return True
