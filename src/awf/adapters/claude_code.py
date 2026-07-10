"""Anthropic Claude Code CLI adapter.

Uses ``claude`` in non-interactive "print" mode (``-p``) with
``--dangerously-skip-permissions`` so the CLI doesn't prompt inside a
container.
"""

from __future__ import annotations

from awf.adapters.base import AgentAdapter, register_adapter
from awf.db.enums import AgentRuntime
from awf.profiles.compose import AGENT_AUTH_ENV_VARS

# Anthropic / Claude Code auth and backend-toggle entries that
# ``AGENT_AUTH_ENV_VARS`` owns. The hosted passthrough derives these from the
# shared source of truth so the hosted contract cannot drift when
# ``AGENT_AUTH_ENV_VARS`` is extended for this adapter.
#
# Bedrock / Vertex backend credentials/config (``AWS_*``,
# ``ANTHROPIC_VERTEX_PROJECT_ID``, ``CLOUD_ML_REGION``, and ADC) are NOT in
# ``AGENT_AUTH_ENV_VARS``. Do not advertise them as ambient hosted passthrough:
# the local Compose exec path would not pass those names into the same
# workspace. Profiles that explicitly declare same-name backend env slots are
# preserved by the generic hosted profile passthrough helpers, which mirrors the
# values the local Compose container received at stack launch without exposing
# secret values in the request payload.
_CLAUDE_CODE_AUTH_ENV_NAMES = frozenset(
    name
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
    )
    if name in AGENT_AUTH_ENV_VARS
)


@register_adapter
class ClaudeCodeAdapter(AgentAdapter):
    """Adapter that runs Anthropic Claude Code in AWF workspaces."""

    runtime = AgentRuntime.claude_code

    @property
    def name(self) -> AgentRuntime:
        """Return the Claude Code runtime identity."""
        return AgentRuntime.claude_code

    @property
    def runtime_scratch_paths(self) -> tuple[str, ...]:
        """Return Claude Code checkout-local scratch paths AWF should ignore."""
        # ``claude`` creates nested git worktrees for its isolated subagents
        # under ``.claude/worktrees/`` inside the checkout. Exclude that
        # agent-runtime state from AWF's validation-cleanliness guard.
        return (".claude/worktrees/",)

    @property
    def hosted_env_passthrough_names(self) -> tuple[str, ...]:
        """Claude Code hosted credential contract.

        Names only — secret values are never transported. The Anthropic /
        Claude Code auth and backend-toggle entries are derived from
        ``AGENT_AUTH_ENV_VARS`` (the shared source of truth) so a hosted
        executor can resolve and inject the same credentials a local Compose
        run would surface, and the hosted contract cannot silently drift when
        ``AGENT_AUTH_ENV_VARS`` is extended for this adapter.

        ``AGENT_AUTH_ENV_VARS`` exposes the ``CLAUDE_CODE_USE_BEDROCK`` /
        ``CLAUDE_CODE_USE_VERTEX`` backend toggles but not the AWS / Vertex
        credentials those modes require to actually authenticate. Those backend
        names are intentionally not included as ambient hosted passthrough
        because the local Compose path does not pass them either. When a profile
        explicitly declares same-name backend env slots, the hosted profile
        passthrough helpers add those names separately, preserving local
        Compose parity without resolving unrelated ambient credentials.
        """
        return tuple(name for name in AGENT_AUTH_ENV_VARS if name in _CLAUDE_CODE_AUTH_ENV_NAMES)

    def get_provider(self, model: str | None) -> str:
        """Return the provider family used for Claude Code runs."""
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
