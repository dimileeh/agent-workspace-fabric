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
    """Return the effective task policy agent model, if one is configured.

    Cursor Auto workspaces persist ``cursor_auto_mode`` without ``agent_model``.
    Derive the Router selector from that mode so scheduler, capacity, and
    PR-monitor circuit-breaker lookups target the same model the executor runs.
    When both keys are present (invalid/legacy), prefer the Auto selector to
    match executor helpers; provider recovery clears Auto mode before writing a
    fixed fallback model.
    """

    if not isinstance(task_policy, Mapping):
        return None
    policy = cast(Mapping[str, Any], task_policy)
    cursor_auto_mode = cursor_auto_mode_from_task_policy(policy)
    if cursor_auto_mode is not None:
        return cursor_auto_model_selector(cursor_auto_mode)
    value = policy.get("agent_model")
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def canonical_agent_model_for_cursor_auto(
    *,
    model: str | None,
    cursor_auto_mode: CursorAutoMode | str | None,
) -> str | None:
    """Normalize agent model for Cursor Auto mode persistence and idempotency.

    Request validation treats ``model='auto'`` as equivalent to omitting model
    when ``cursor_auto_mode`` is set. Persist and compare the omitted form so
    those two allowed requests do not conflict on replay/reattach.
    Portable plain ``auto`` without a Cursor Auto mode stays explicit.
    """

    if model is None:
        return None
    stripped = model.strip()
    if not stripped:
        return None
    if cursor_auto_mode is not None and stripped == "auto":
        return None
    return stripped


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
