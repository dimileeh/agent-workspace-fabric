"""Cursor CLI adapter.

Uses the official Cursor CLI ``cursor-agent`` binary in print/headless mode.
The integration follows Cursor's CLI overview and headless documentation:

- https://cursor.com/docs/cli/overview
- https://cursor.com/docs/cli/headless

AWF streams the wrapped prompt on stdin and uses ``-p`` only to select print
mode, with ``--force`` always present so Cursor can apply file edits inside the
workspace container without an interactive approval prompt.
"""

from __future__ import annotations

from awf.adapters.base import AgentAdapter, register_adapter
from awf.db.enums import AgentRuntime

CURSOR_DEFAULT_THINKING_MODEL = "sonnet-4-thinking"
"""Default Cursor model variant AWF uses when high effort must select a model."""


@register_adapter
class CursorAdapter(AgentAdapter):
    """Run Cursor's headless CLI inside an AWF workspace container."""

    runtime = AgentRuntime.cursor

    @property
    def name(self) -> AgentRuntime:
        """Return the AWF runtime enum for Cursor workspaces."""
        return AgentRuntime.cursor

    def get_provider(self, model: str | None) -> str:
        """Report Cursor as the provider regardless of selected model."""
        del model
        return "cursor"

    def _cli_args(self, *, model: str | None) -> list[str]:
        """Build the cursor-agent print-mode command arguments."""
        selected_model = _cursor_selected_model(
            model=model,
            default_model=self._default_model,
            effort=self._default_effort,
        )
        args = ["cursor-agent", "-p", "--force"]
        if selected_model:
            args.extend(["-m", selected_model])
        args.extend(["--output-format", "text"])
        return args


def _cursor_selected_model(
    *,
    model: str | None,
    default_model: str | None,
    effort: str | None,
) -> str | None:
    """Return the Cursor model for one run.

    ``model`` is an explicit per-run override. The bound Cursor thinking model
    default is effort-derived, so lower efforts without an override should not
    inherit it and accidentally force thinking mode.
    """

    if model:
        return model
    if default_model and default_model != CURSOR_DEFAULT_THINKING_MODEL:
        return default_model
    if effort is None:
        return default_model
    return _cursor_model_for_effort(model=None, effort=effort)


def _cursor_model_for_effort(*, model: str | None, effort: str | None) -> str | None:
    """Map AWF effort to Cursor's documented portable controls.

    Cursor documents model selection but no portable reasoning-effort flag.
    AWF therefore never emits an undocumented thinking flag: explicit models are
    respected unchanged, and high/xhigh/max without a model select the
    documented thinking-capable default model variant.
    """

    if model:
        return model
    if effort is None:
        return None
    if effort.strip().lower() in {"high", "xhigh", "max"}:
        return CURSOR_DEFAULT_THINKING_MODEL
    return None
