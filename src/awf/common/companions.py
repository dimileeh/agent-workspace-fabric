"""Helpers for workspace companion service metadata."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

COMPANION_POLICY_KEY = "companions"
COMPANION_WORKTREE_MARKER = "__companion__"
RESERVED_COMPANION_SERVICE_NAMES = frozenset({"agent", "docker"})
_GIT_BRANCH_COMPONENT_FORBIDDEN_CHARS = frozenset(" ~^:?*[\\")


def companion_name_is_git_branch_component(companion_name: str) -> bool:
    """Return whether a companion name is safe as one Git branch component."""
    return bool(companion_name) and not (
        "/" in companion_name
        or companion_name.startswith(".")
        or companion_name.endswith(".")
        or companion_name.endswith(".lock")
        or ".." in companion_name
        or "@{" in companion_name
        or any(
            char in _GIT_BRANCH_COMPONENT_FORBIDDEN_CHARS or ord(char) < 32 or ord(char) == 127
            for char in companion_name
        )
    )


def companion_volume_source_is_repo_relative(source: str) -> bool:
    """Return whether a companion volume source should resolve inside its repo."""
    return source.startswith(".") or "/" in source or "\\" in source


def companion_worktree_id(workspace_id: str, companion_name: str) -> str:
    """Return the managed worktree id for one workspace companion."""
    return f"{workspace_id}{COMPANION_WORKTREE_MARKER}{companion_name}"


def companion_branch_name(
    *,
    branch_prefix: str,
    workspace_id: str,
    companion_name: str,
) -> str:
    """Return the deterministic local branch name for a companion checkout."""
    return f"{branch_prefix}/{workspace_id}/companion/{companion_name}"


def parent_workspace_id_from_companion_worktree_id(worktree_id: str) -> str | None:
    """Return the parent workspace id for a companion worktree id, if any."""
    if COMPANION_WORKTREE_MARKER not in worktree_id:
        return None
    parent, companion = worktree_id.split(COMPANION_WORKTREE_MARKER, 1)
    if not parent.startswith("ws_") or not companion:
        return None
    return parent


def workspace_and_companion_ids(
    workspace_id: str, task_policy: Mapping[str, Any]
) -> tuple[str, ...]:
    """Return the primary workspace id and any companion worktree ids."""
    ids = [workspace_id]
    for companion in companions_from_task_policy(task_policy):
        name = companion.get("name")
        if isinstance(name, str) and name:
            ids.append(companion_worktree_id(workspace_id, name))
    return tuple(ids)


def companions_from_task_policy(
    task_policy: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], ...]:
    """Extract normalized companion request dictionaries from task policy."""
    if not isinstance(task_policy, Mapping):
        return ()
    raw = task_policy.get(COMPANION_POLICY_KEY)
    if not isinstance(raw, list):
        return ()
    companions: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, Mapping):
            companions.append(dict(item))
    return tuple(companions)
