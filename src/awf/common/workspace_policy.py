"""Helpers for reading workspace task policy fields."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast


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
