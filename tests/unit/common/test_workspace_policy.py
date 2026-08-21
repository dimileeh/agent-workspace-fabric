"""Workspace policy parsing helpers."""

from __future__ import annotations

import pytest

from awf.common.workspace_policy import (
    CURSOR_AUTO_MODE_POLICY_KEY,
    DEFAULT_RELEASE_SYNC_SOURCE_BRANCH,
    agent_model_from_task_policy,
    cursor_auto_model_selector,
    release_sync_source_branch,
)
from awf.db.enums import CursorAutoMode


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


@pytest.mark.parametrize("mode", list(CursorAutoMode))
def test_agent_model_from_task_policy_derives_selector_from_cursor_auto_mode(
    mode: CursorAutoMode,
) -> None:
    """Cursor Auto persists mode without agent_model; circuit breakers need the selector."""

    assert agent_model_from_task_policy({CURSOR_AUTO_MODE_POLICY_KEY: mode.value}) == (
        cursor_auto_model_selector(mode)
    )


def test_agent_model_from_task_policy_prefers_cursor_auto_mode_over_stale_agent_model() -> None:
    mode = CursorAutoMode.intelligence
    assert agent_model_from_task_policy(
        {
            CURSOR_AUTO_MODE_POLICY_KEY: mode.value,
            "agent_model": "stale-fixed-model",
        }
    ) == cursor_auto_model_selector(mode)


def test_agent_model_from_task_policy_uses_agent_model_when_auto_mode_absent() -> None:
    assert agent_model_from_task_policy({"agent_model": "composer-2"}) == "composer-2"


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
