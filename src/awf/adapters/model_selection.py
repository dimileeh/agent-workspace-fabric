"""Shared model-selection helpers for agent runtime metadata."""

from __future__ import annotations

from awf.db.enums import AgentRuntime

CURSOR_DEFAULT_THINKING_MODEL = "sonnet-4-thinking"
"""Default Cursor model variant AWF uses when high effort must select a model."""


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

    Explicit model overrides win first. A custom default model that is not
    Cursor's thinking default is treated as operator-controlled and bypasses
    effort mapping, even for high/xhigh efforts. Effort mapping only selects
    the thinking model when no default is set or the default already matches
    AWF's Cursor thinking default.
    """

    if model:
        return model
    if default_model and default_model != CURSOR_DEFAULT_THINKING_MODEL:
        return default_model
    if effort is None:
        return default_model
    return cursor_model_for_effort(model=None, effort=effort)


def cursor_model_for_effort(*, model: str | None, effort: str | None) -> str | None:
    """Map AWF effort to Cursor's documented portable model controls."""

    if model:
        return model
    if effort is None:
        return None
    if effort.strip().lower() in {"high", "xhigh", "max"}:
        return CURSOR_DEFAULT_THINKING_MODEL
    return None
