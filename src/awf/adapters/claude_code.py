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

    def get_provider(self, model: str | None) -> str:
        return "anthropic"

    def _cli_args(self, *, prompt: str, model: str | None) -> list[str]:
        args = ["claude", "--dangerously-skip-permissions"]
        if model:
            args += ["--model", model]
        if self._default_effort:
            args += ["--effort", _claude_effort_for_awf_effort(self._default_effort)]
        args += ["-p", prompt]
        return args


def _claude_effort_for_awf_effort(effort: str) -> str:
    """Map AWF's abstract effort policy to Claude Code's concrete flag.

    Recent Claude Code builds accept ``xhigh`` and ``max``; older builds only
    accept ``max`` as their top effort. Use ``max`` for AWF's ``xhigh`` so
    dogfood workspaces keep running across both CLI generations.
    """
    normalized = effort.lower()
    if normalized in {"xhigh", "max"}:
        return "max"
    return normalized
