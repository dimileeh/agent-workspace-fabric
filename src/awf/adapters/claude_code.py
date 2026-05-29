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
        del model
        return "anthropic"

    def _cli_args(self, *, model: str | None) -> list[str]:
        args = ["claude", "--dangerously-skip-permissions"]
        if model:
            args += ["--model", model]
        if self._default_effort:
            args += ["--effort", _claude_effort_for_awf_effort(self._default_effort)]
        args.append("-p")
        return args


def _claude_effort_for_awf_effort(effort: str) -> str:
    """Normalize AWF's effort policy to Claude Code's ``--effort`` flag.

    The ``claude`` CLI accepts the same effort ladder AWF uses
    (``low``, ``medium``, ``high``, ``xhigh``, ``max``), so the requested effort
    is propagated as-is. In particular ``xhigh`` stays ``xhigh`` and is not
    collapsed to ``max``.
    """
    return effort.lower()
