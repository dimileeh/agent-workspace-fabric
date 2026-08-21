"""Shared model-selection helpers for agent runtime metadata."""

from __future__ import annotations

from awf.db.enums import AgentRuntime

CURSOR_DEFAULT_MODEL = "auto"
"""Portable Cursor default; the provider/team owns Auto routing policy."""


def selected_runtime_model_for_defaults(
    *,
    agent: AgentRuntime | None,
    explicit_model: str | None,
    default_model: str | None,
    effort: str | None,
) -> str | None:
    """Return the model AWF will explicitly select for a workspace run."""

    if explicit_model:
        return explicit_model
    if agent is AgentRuntime.cursor:
        return cursor_selected_model(
            model=None,
            default_model=default_model,
            effort=effort,
        )
    return default_model


def cursor_selected_model(
    *,
    model: str | None,
    default_model: str | None,
    effort: str | None,
) -> str | None:
    """Return the Cursor model selected for one run.

    Explicit model overrides win first, including Cursor Router's official
    parameterized ``auto-smart[optimize_for=...]`` selectors. Generic AWF
    reasoning effort does not select a Cursor model or Auto routing profile.
    Cursor owns those provider-specific controls through its model selector and
    team policy.
    """

    if model:
        return model
    del effort
    return default_model


def cursor_model_for_effort(*, model: str | None, effort: str | None) -> str | None:
    """Preserve an explicit model without treating effort as a Cursor control."""

    del effort
    return model
