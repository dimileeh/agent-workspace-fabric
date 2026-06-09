"""Companion worktree helpers for workspace filesystem GC."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from awf.common.companions import companion_worktree_id, companions_from_task_policy

if TYPE_CHECKING:
    from awf.db.models import Workspace


def companion_worktree_paths_for_gc(workspace: Workspace, *, work_dir: Path) -> tuple[Path, ...]:
    return tuple(
        work_dir / "git" / "worktrees" / companion_worktree_id(workspace.id, str(companion["name"]))
        for companion in companions_from_task_policy(workspace.task_policy)
        if _has_companion_worktree_target(companion)
    )


def companion_worktree_remove_targets(workspace: Workspace) -> tuple[tuple[str, str], ...]:
    return tuple(
        (companion_worktree_id(workspace.id, str(companion["name"])), str(companion["repo_url"]))
        for companion in companions_from_task_policy(workspace.task_policy)
        if _has_companion_worktree_target(companion)
    )


def _has_companion_worktree_target(companion: dict[str, object]) -> bool:
    return "name" in companion and "repo_url" in companion
