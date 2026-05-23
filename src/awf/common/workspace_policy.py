"""Helpers for reading workspace task policy fields."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

# Fallback source branch for ``sync_release_pr`` worktrees. Shared across the
# control, service, and node layers so the release-sync default cannot drift
# between task admission, provisioning, and execution.
DEFAULT_RELEASE_SYNC_SOURCE_BRANCH = "development"


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
