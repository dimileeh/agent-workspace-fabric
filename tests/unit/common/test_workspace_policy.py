"""Workspace policy parsing helpers."""

from __future__ import annotations

import pytest

from awf.common.workspace_policy import (
    DEFAULT_RELEASE_SYNC_SOURCE_BRANCH,
    agent_model_from_task_policy,
    release_sync_source_branch,
)


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


def test_release_sync_source_branch_strips_configured_branch_and_defaults() -> None:
    assert (
        release_sync_source_branch({"release_sync": {"source_branch": " release/next "}})
        == "release/next"
    )
    assert (
        release_sync_source_branch({"release_sync": {"source_branch": "  "}})
        == DEFAULT_RELEASE_SYNC_SOURCE_BRANCH
    )
    assert (
        release_sync_source_branch({"release_sync": "legacy"}) == DEFAULT_RELEASE_SYNC_SOURCE_BRANCH
    )
    assert release_sync_source_branch(None) == DEFAULT_RELEASE_SYNC_SOURCE_BRANCH
