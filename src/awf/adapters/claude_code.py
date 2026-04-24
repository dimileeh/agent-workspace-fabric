"""Anthropic Claude Code CLI adapter.

Uses ``claude`` in non-interactive "print" mode (``-p``) with
``--dangerously-skip-permissions`` so the CLI doesn't prompt inside a
container.
"""

from __future__ import annotations

from awf.adapters.base import AgentAdapter, register_adapter
from awf.db.enums import AgentRuntime


@register_adapter
class ClaudeCodeAdapter(AgentAdapter):
    runtime = AgentRuntime.claude_code

    @property
    def name(self) -> AgentRuntime:
        return AgentRuntime.claude_code

    def _cli_args(self, *, prompt: str, model: str | None) -> list[str]:
        args = ["claude", "--dangerously-skip-permissions"]
        if model:
            args += ["--model", model]
        if self._default_effort:
            args += ["--effort", self._default_effort]
        args += ["-p", prompt]
        return args
