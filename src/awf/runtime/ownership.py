"""Shared ownership-repair helpers for AWF runtime helpers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

from awf.node.git_manager import (
    linked_worktree_git_dir,
    repair_agent_writable_worktree,
)

AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE = "AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED"

EXECUTOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME = (
    "executor.agent_runtime_ownership_repair_failed"
)
MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME = "monitor.agent_runtime_ownership_repair_failed"


def _mirror_path_from_linked_git_dir(linked_git_dir: Path) -> Path:
    """Resolve a linked worktree's mirror from an already trusted gitdir read."""
    commondir = linked_git_dir / "commondir"
    try:
        if commondir.is_file():
            raw_common_dir = commondir.read_text(encoding="utf-8").strip()
            if raw_common_dir:
                common_dir = Path(raw_common_dir)
                if not common_dir.is_absolute():
                    common_dir = linked_git_dir / common_dir
                return common_dir.resolve()
        return linked_git_dir.parent.parent.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            "refusing ownership repair: cannot resolve mirror path from linked-worktree "
            f"git metadata {linked_git_dir}"
        ) from exc


def _validated_layout_mirror_for_worktree(
    worktree_path: Path, workspace_id: str
) -> tuple[Path, Path]:
    """Resolve and validate the linked-worktree mirror and gitdir.

    Control-plane control over git pointers has been compromised during
    monitor recoveries; trust only mirrored worktree pointers that stay under
    the expected ``<worktrees_root>/../mirrors`` hierarchy for this
    worktree path and match this workspace's metadata entry.
    """
    linked_git_dir = linked_worktree_git_dir(worktree_path)
    if linked_git_dir is None:
        raise ValueError(
            "refusing ownership repair: cannot read linked-worktree git metadata "
            f"for workspace {worktree_path}"
        )

    mirror_path = _mirror_path_from_linked_git_dir(linked_git_dir)
    expected_mirror_root = worktree_path.parent.parent / "mirrors"
    resolved_expected_root = expected_mirror_root.resolve()
    resolved_mirror = mirror_path.resolve()
    if not resolved_mirror.is_relative_to(resolved_expected_root):
        raise ValueError(
            "refusing ownership repair: mirror path is outside expected mirrors root "
            f"for workspace {worktree_path}: {resolved_mirror}"
        )

    expected_worktree_git_root = resolved_mirror / "worktrees"
    if linked_git_dir.parent.resolve() != expected_worktree_git_root:
        raise ValueError(
            "refusing ownership repair: linked-worktree metadata points to another "
            f"workspace. expected parent {expected_worktree_git_root}, got {linked_git_dir.parent}"
        )

    if not linked_git_dir.name.startswith(workspace_id):
        raise ValueError(
            "refusing ownership repair: linked-worktree metadata points to another "
            f"workspace. expected workspace id {workspace_id}, got {linked_git_dir.name}"
        )

    linked_worktree_suffix = linked_git_dir.name.removeprefix(workspace_id)
    if linked_worktree_suffix and not linked_worktree_suffix.isdigit():
        raise ValueError(
            "refusing ownership repair: linked-worktree metadata points to another "
            f"workspace. expected workspace id {workspace_id}, got {linked_git_dir.name}"
        )

    return mirror_path, linked_git_dir


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
