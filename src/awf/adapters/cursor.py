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
from awf.adapters.model_selection import (
    CURSOR_DEFAULT_THINKING_MODEL as CURSOR_DEFAULT_THINKING_MODEL,
)
from awf.adapters.model_selection import (
    cursor_model_for_effort,
    cursor_selected_model,
)
from awf.db.enums import AgentRuntime


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
        selected_model = self._selected_model_for_run(model=model)
        args = ["cursor-agent", "-p", "--force"]
        if selected_model:
            args.extend(["-m", selected_model])
        args.extend(["--output-format", "text"])
        return args

    def _selected_model_for_run(self, *, model: str | None) -> str | None:
        """Return the Cursor model AWF actually asks the CLI to use."""
        return cursor_selected_model(
            model=model,
            default_model=self._default_model,
            effort=self._default_effort,
        )


def _cursor_model_for_effort(*, model: str | None, effort: str | None) -> str | None:
    """Map AWF effort to Cursor's documented portable controls.

    Cursor documents model selection but no portable reasoning-effort flag.
    AWF therefore never emits an undocumented thinking flag: explicit models are
    respected unchanged, and high/xhigh/max without a model select the
    documented thinking-capable default model variant.
    """
    return cursor_model_for_effort(model=model, effort=effort)
