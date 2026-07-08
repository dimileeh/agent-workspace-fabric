"""Anthropic Claude Code CLI adapter.

Uses ``claude`` in non-interactive "print" mode (``-p``) with
``--dangerously-skip-permissions`` so the CLI doesn't prompt inside a
container.
"""

from __future__ import annotations

from awf.adapters.base import AgentAdapter, register_adapter
from awf.db.enums import AgentRuntime
from awf.profiles.compose import AGENT_AUTH_ENV_VARS

# Anthropic / Claude Code auth and backend-toggle entries that ``AGENT_AUTH_ENV_VARS``
# owns. The hosted passthrough derives these from the shared source of truth so the
# hosted contract cannot drift when ``AGENT_AUTH_ENV_VARS`` is extended for this
# adapter. Bedrock / Vertex *backend* credentials (``AWS_*``, Vertex project / region
# / ADC) are NOT in ``AGENT_AUTH_ENV_VARS`` — the toggle is, the credentials it
# requires are not — so they stay a static supplement below.
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

# Bedrock / Vertex backend credentials that ``AGENT_AUTH_ENV_VARS`` does not surface.
# The hosted executor only resolves names whose backing values exist, so the union is
# safe regardless of which backend is active.
_CLAUDE_CODE_BACKEND_AUTH_ENV_NAMES = (
    # Amazon Bedrock backend auth (used when CLAUDE_CODE_USE_BEDROCK=1). The AWS SDK
    # credential chain resolves region via AWS_REGION then AWS_DEFAULT_REGION then
    # the active profile; AWS_PROFILE selects a shared-credentials profile.
    # AWS_BEARER_TOKEN_BEDROCK is the Bedrock API-key auth path that needs no IAM
    # credentials.
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_BEARER_TOKEN_BEDROCK",
    # Google Vertex AI / Agent Platform backend auth (used when
    # CLAUDE_CODE_USE_VERTEX=1). ANTHROPIC_VERTEX_PROJECT_ID selects the GCP project
    # and CLOUD_ML_REGION selects the endpoint region; GOOGLE_APPLICATION_CREDENTIALS
    # supplies ADC for the GCP SDK chain.
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "CLOUD_ML_REGION",
    "GOOGLE_APPLICATION_CREDENTIALS",
)


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

        Names only — secret values are never transported. The Anthropic /
        Claude Code auth and backend-toggle entries are derived from
        ``AGENT_AUTH_ENV_VARS`` (the shared source of truth) so a hosted
        executor can resolve and inject the same credentials a local Compose
        run would surface, and the hosted contract cannot silently drift when
        ``AGENT_AUTH_ENV_VARS`` is extended for this adapter.

        ``AGENT_AUTH_ENV_VARS`` exposes the ``CLAUDE_CODE_USE_BEDROCK`` /
        ``CLAUDE_CODE_USE_VERTEX`` backend toggles but not the AWS / Vertex
        credentials those modes require to actually authenticate. Surface the
        backend-specific auth env vars here too — without them a hosted run can
        flip the toggle and still fail to authenticate against Bedrock or
        Vertex. The hosted executor only resolves names whose backing values
        exist, so the union is safe regardless of which backend is active.
        """
        return (
            *(name for name in AGENT_AUTH_ENV_VARS if name in _CLAUDE_CODE_AUTH_ENV_NAMES),
            *_CLAUDE_CODE_BACKEND_AUTH_ENV_NAMES,
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
