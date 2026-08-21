"""Unit tests for awf.adapters.defaults module."""

from __future__ import annotations

import pytest

from awf.adapters.base import AgentDefaults
from awf.adapters.defaults import (
    DEFAULT_AGENT_DEFAULTS,
    HISTORICAL_AGENT_DEFAULTS,
    defaults_with_model_overrides,
)
from awf.adapters.model_selection import CURSOR_DEFAULT_MODEL
from awf.db.enums import AgentRuntime


@pytest.mark.unit
def test_historical_cursor_retains_legacy_effort_while_live_default_omits_it() -> None:
    assert DEFAULT_AGENT_DEFAULTS[AgentRuntime.cursor] == AgentDefaults(
        model=CURSOR_DEFAULT_MODEL,
        effort=None,
    )
    assert HISTORICAL_AGENT_DEFAULTS[AgentRuntime.cursor] == AgentDefaults(
        model=CURSOR_DEFAULT_MODEL,
        effort="xhigh",
    )
    merged = defaults_with_model_overrides({AgentRuntime.cursor: "gpt-5"})
    assert merged[AgentRuntime.cursor] == AgentDefaults(model="gpt-5", effort="xhigh")


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
        CURSOR_DEFAULT_MODEL,
        selected_runtime_model_for_defaults,
    )

    res = selected_runtime_model_for_defaults(
        agent=AgentRuntime.cursor,
        explicit_model=None,
        default_model=CURSOR_DEFAULT_MODEL,
        effort="high",
    )
    assert res == "auto"


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
