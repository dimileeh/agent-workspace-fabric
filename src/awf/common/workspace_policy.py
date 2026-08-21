"""Helpers for reading workspace task policy fields."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from awf.db.enums import CursorAutoMode

# Fallback source branch for ``sync_release_pr`` worktrees. Shared across the
# control, service, and node layers so the release-sync default cannot drift
# between task admission, provisioning, and execution.
DEFAULT_RELEASE_SYNC_SOURCE_BRANCH = "development"
PR_ADOPTION_EXECUTION_MODE_LOCAL = "local"
PR_ADOPTION_EXECUTION_MODE_HOSTED = "hosted"
CURSOR_AUTO_MODE_POLICY_KEY = "cursor_auto_mode"

_CURSOR_AUTO_MODE_WIRE_VALUES: Mapping[CursorAutoMode, str] = {
    CursorAutoMode.cost: "cost",
    CursorAutoMode.balance: "balanced",
    CursorAutoMode.intelligence: "intelligence",
}


def cursor_auto_mode_from_task_policy(task_policy: object) -> CursorAutoMode | None:
    """Return a canonical persisted Cursor Auto mode, if configured."""

    if not isinstance(task_policy, Mapping):
        return None
    value = task_policy.get(CURSOR_AUTO_MODE_POLICY_KEY)
    if not isinstance(value, str):
        return None
    try:
        return CursorAutoMode(value.strip())
    except ValueError:
        return None


def cursor_auto_model_selector(mode: CursorAutoMode | str) -> str:
    """Return Cursor's parameterized Router selector for one public AWF mode."""

    resolved = mode if isinstance(mode, CursorAutoMode) else CursorAutoMode(mode)
    wire_value = _CURSOR_AUTO_MODE_WIRE_VALUES[resolved]
    return f"auto-smart[optimize_for={wire_value}]"


def release_sync_source_branch(task_policy: object) -> str:
    """Return the ``sync_release_pr`` source branch from policy, or the default.

    Centralizes the ``task_policy["release_sync"]["source_branch"]`` navigation
    so the control (executor) and node (provisioner) layers cannot drift in how
    they read or default the persisted release-sync source branch.
    """

    if isinstance(task_policy, Mapping):
        block = task_policy.get("release_sync")
        if isinstance(block, Mapping):
            source = block.get("source_branch")
            if isinstance(source, str) and source.strip():
                return source.strip()
    return DEFAULT_RELEASE_SYNC_SOURCE_BRANCH


def agent_model_from_task_policy(task_policy: object) -> str | None:
    """Return the nonblank task policy agent model, if one is configured."""

    if not isinstance(task_policy, Mapping):
        return None
    policy = cast(Mapping[str, Any], task_policy)
    value = policy.get("agent_model")
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def pr_adoption_execution_policy(task_policy: object) -> dict[str, str]:
    """Return the persisted PR-adoption execution policy, defaulting to local.

    Older adoption rows predate the explicit execution policy. They must remain
    local so hosted behavior is never inferred from environment, missing Docker,
    or absent Compose metadata.
    """

    mode = PR_ADOPTION_EXECUTION_MODE_LOCAL
    if isinstance(task_policy, Mapping):
        adoption = task_policy.get("pr_adoption")
        if isinstance(adoption, Mapping):
            execution = adoption.get("execution")
            if isinstance(execution, Mapping):
                value = execution.get("mode")
                if value == PR_ADOPTION_EXECUTION_MODE_HOSTED:
                    mode = PR_ADOPTION_EXECUTION_MODE_HOSTED
    return {"mode": mode}


def pr_adoption_is_hosted(task_policy: object) -> bool:
    """Return true only for explicit hosted PR-adoption execution policy."""

    return pr_adoption_execution_policy(task_policy)["mode"] == PR_ADOPTION_EXECUTION_MODE_HOSTED
