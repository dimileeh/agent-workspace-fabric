"""Unit tests for awf.adapters.defaults module."""

from __future__ import annotations

import pytest

from awf.adapters.base import AgentDefaults
from awf.adapters.defaults import (
    HISTORICAL_AGENT_DEFAULTS,
    defaults_with_model_overrides,
)
from awf.db.enums import AgentRuntime


@pytest.mark.unit
def test_defaults_with_model_overrides_none() -> None:
    merged = defaults_with_model_overrides(None)
    assert merged == dict(HISTORICAL_AGENT_DEFAULTS)


@pytest.mark.unit
def test_defaults_with_model_overrides_preserves_effort() -> None:
    overrides = {AgentRuntime.codex: "gpt-6-ultra"}
    merged = defaults_with_model_overrides(overrides)
    assert merged[AgentRuntime.codex] == AgentDefaults(
        model="gpt-6-ultra",
        effort="xhigh",
    )
    # Other runtimes retain their default model and effort
    assert merged[AgentRuntime.claude_code] == HISTORICAL_AGENT_DEFAULTS[AgentRuntime.claude_code]


@pytest.mark.unit
def test_defaults_with_model_overrides_handles_unknown_runtime_or_empty_base() -> None:
    # Custom/unknown runtime or runtime not present in base
    overrides = {AgentRuntime.codex: "gpt-5.5"}
    merged = defaults_with_model_overrides(overrides, base={})
    assert merged[AgentRuntime.codex] == AgentDefaults(model="gpt-5.5", effort=None)


@pytest.mark.unit
def test_selected_runtime_model_for_defaults_explicit() -> None:
    from awf.adapters.model_selection import selected_runtime_model_for_defaults

    res = selected_runtime_model_for_defaults(
        agent=AgentRuntime.codex,
        explicit_model="custom-model",
        default_model="default-model",
        effort="high",
    )
    assert res == "custom-model"


@pytest.mark.unit
def test_selected_runtime_model_for_defaults_cursor() -> None:
    from awf.adapters.model_selection import (
        CURSOR_DEFAULT_THINKING_MODEL,
        selected_runtime_model_for_defaults,
    )

    res = selected_runtime_model_for_defaults(
        agent=AgentRuntime.cursor,
        explicit_model=None,
        default_model=None,
        effort="high",
    )
    assert res == CURSOR_DEFAULT_THINKING_MODEL


@pytest.mark.unit
def test_selected_runtime_model_for_defaults_other_agent() -> None:
    from awf.adapters.model_selection import selected_runtime_model_for_defaults

    res = selected_runtime_model_for_defaults(
        agent=AgentRuntime.codex,
        explicit_model=None,
        default_model="gpt-5",
        effort="high",
    )
    assert res == "gpt-5"
