"""Workspace policy parsing helpers."""

from __future__ import annotations

import pytest

from awf.common.workspace_policy import agent_model_from_task_policy


def test_agent_model_from_task_policy_strips_nonblank_model() -> None:
    assert agent_model_from_task_policy({"agent_model": "  gpt-5.3-codex  "}) == "gpt-5.3-codex"


@pytest.mark.parametrize(
    "task_policy",
    [
        None,
        {},
        {"agent_model": ""},
        {"agent_model": "   "},
        {"agent_model": 123},
        "legacy-policy",
    ],
)
def test_agent_model_from_task_policy_ignores_missing_or_invalid_values(
    task_policy: object,
) -> None:
    assert agent_model_from_task_policy(task_policy) is None
