"""Clarification-map completeness for every AgentRuntime (antigravity + future)."""

from __future__ import annotations

import pytest

from awf.db.enums import AgentRuntime
from awf.node.stack_launcher import _CLARIFICATION_RUNTIME_ENV_NAMES
from awf.node.stack_launcher_auth_helpers import _CLARIFICATION_RUNTIME_AUTH_MOUNT_TARGETS


@pytest.mark.unit
def test_clarification_runtime_env_names_cover_every_agent_runtime() -> None:
    """Missing map entries KeyError on clarification re-ask — cover all runtimes."""
    assert set(_CLARIFICATION_RUNTIME_ENV_NAMES) == set(AgentRuntime)


@pytest.mark.unit
def test_clarification_runtime_auth_mount_targets_cover_every_agent_runtime() -> None:
    """Missing mount-target map entries KeyError on clarification re-ask."""
    assert set(_CLARIFICATION_RUNTIME_AUTH_MOUNT_TARGETS) == set(AgentRuntime)


@pytest.mark.unit
def test_antigravity_clarification_maps_match_cursor_shaped_contract() -> None:
    """Antigravity is env-key auth like cursor: API keys, no file auth mounts."""
    assert _CLARIFICATION_RUNTIME_ENV_NAMES[AgentRuntime.antigravity] == frozenset(
        {"GEMINI_API_KEY"}
    )
    assert _CLARIFICATION_RUNTIME_AUTH_MOUNT_TARGETS[AgentRuntime.antigravity] == frozenset()
