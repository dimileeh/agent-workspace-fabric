"""Central model/effort defaults for AWF-managed agent CLIs."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from awf.adapters.base import AgentDefaults
from awf.db.enums import AgentRuntime

DEFAULT_AGENT_DEFAULTS: Mapping[AgentRuntime, AgentDefaults] = MappingProxyType(
    {
        AgentRuntime.claude_code: AgentDefaults(model="claude-opus-4-8", effort="xhigh"),
        AgentRuntime.codex: AgentDefaults(model="gpt-5.5", effort="xhigh"),
        # Gemini CLI 0.39+ documents Gemini 3.1 Pro Preview as the direct
        # Pro-class model ID when the account has access.
        AgentRuntime.gemini: AgentDefaults(model="gemini-3.1-pro-preview", effort="xhigh"),
        AgentRuntime.opencode: AgentDefaults(model="ollama/kimi-k2.6:cloud", effort="xhigh"),
    }
)


def defaults_with_model_overrides(
    model_overrides: Mapping[AgentRuntime, str] | None,
    *,
    base: Mapping[AgentRuntime, AgentDefaults] = DEFAULT_AGENT_DEFAULTS,
) -> dict[AgentRuntime, AgentDefaults]:
    """Merge legacy model-only overrides with the central defaults.

    Older call sites and tests pass ``default_models``. Keep those source
    compatible while still applying the default effort policy for agents
    whose model is not overridden.
    """
    merged = dict(base)
    for runtime, model in (model_overrides or {}).items():
        existing = merged.get(runtime)
        merged[runtime] = AgentDefaults(
            model=model,
            effort=existing.effort if existing is not None else None,
        )
    return merged
