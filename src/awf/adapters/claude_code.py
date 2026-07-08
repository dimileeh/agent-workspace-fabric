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

    @property
    def runtime_scratch_paths(self) -> tuple[str, ...]:
        # ``claude`` creates nested git worktrees for its isolated subagents
        # under ``.claude/worktrees/`` inside the checkout. Exclude that
        # agent-runtime state from AWF's validation-cleanliness guard.
        return (".claude/worktrees/",)

    @property
    def hosted_env_passthrough_names(self) -> tuple[str, ...]:
        """Claude Code hosted credential contract.

        Names only — secret values are never transported. Mirrors the
        ``ANTHROPIC_*`` / ``CLAUDE_CODE_*`` auth and endpoint entries in
        ``AGENT_AUTH_ENV_VARS`` so a hosted executor can resolve and inject the
        same credentials a local Compose run would surface, including the
        Bedrock/Vertex backends. The hosted executor resolves values
        out-of-band.
        """
        return (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_SMALL_FAST_MODEL",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
        )

    def get_provider(self, model: str | None) -> str:
        del model
        return "anthropic"

    def _cli_args(self, *, model: str | None) -> list[str]:
        args = ["claude", "--dangerously-skip-permissions"]
        selected_model = model or self._default_model
        if selected_model:
            args += ["--model", selected_model]
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
