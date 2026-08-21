"""Central model/effort defaults for AWF-managed agent CLIs."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from awf.adapters.base import AgentDefaults
from awf.adapters.model_selection import CURSOR_DEFAULT_MODEL
from awf.db.enums import AgentRuntime

DEFAULT_AGENT_DEFAULTS: Mapping[AgentRuntime, AgentDefaults] = MappingProxyType(
    {
        AgentRuntime.claude_code: AgentDefaults(model="claude-opus-5", effort="xhigh"),
        AgentRuntime.codex: AgentDefaults(model="gpt-5.6-sol", effort="xhigh"),
        # Cursor Auto has provider-specific Cost/Balance/Intelligence routing
        # profiles, not a portable reasoning-effort flag. The account/team owns
        # the Auto profile; eligible teams can pass Cursor's parameterized
        # auto-smart selector as an explicit model override.
        AgentRuntime.cursor: AgentDefaults(model=CURSOR_DEFAULT_MODEL),
        # Antigravity API-key mode (agy 1.1.13) accepts exactly the slugs in
        # ANTIGRAVITY_API_KEY_MODE_MODELS; gemini-3.1-pro-preview is the
        # Pro-class default. Effort is accepted/recorded but never emitted:
        # agy rejects --effort for all models in API-key mode (OAuth-only
        # composite slugs such as gemini-3.6-flash-high).
        AgentRuntime.antigravity: AgentDefaults(model="gemini-3.1-pro-preview", effort="xhigh"),
        AgentRuntime.opencode: AgentDefaults(model="ollama/kimi-k2.6:cloud", effort="xhigh"),
        # The Grok Build CLI reports grok-build as the current default coding model.
        AgentRuntime.grok: AgentDefaults(model="grok-build", effort="xhigh"),
    }
)

HISTORICAL_AGENT_DEFAULTS: Mapping[AgentRuntime, AgentDefaults] = MappingProxyType(
    {
        **DEFAULT_AGENT_DEFAULTS,
        # Pre-Auto Cursor adoptions with an explicit model and omitted effort
        # persisted agent_effort="xhigh" from the former default. Keep that
        # historical fill so idempotent replays still match stored policy.
        AgentRuntime.cursor: AgentDefaults(model=CURSOR_DEFAULT_MODEL, effort="xhigh"),
        # Retired runtimes retained so historical adoptions resolve implicit effort
        # and historical workspace projections report last-known defaults.
        AgentRuntime.gemini: AgentDefaults(model="gemini-3.1-pro-preview", effort="xhigh"),
    }
)


def defaults_with_model_overrides(
    model_overrides: Mapping[AgentRuntime, str] | None,
    *,
    base: Mapping[AgentRuntime, AgentDefaults] = HISTORICAL_AGENT_DEFAULTS,
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
