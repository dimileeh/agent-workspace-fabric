"""OpenAI Codex CLI adapter.

Codex reference: the ``codex exec`` non-interactive mode. We always pass
``--dangerously-bypass-approvals-and-sandbox`` because the container is
already a sandbox — approval prompts would just hang the run.
"""

from __future__ import annotations

from awf.adapters.base import AgentAdapter, register_adapter
from awf.db.enums import AgentRuntime
from awf.profiles.compose import AGENT_AUTH_ENV_VARS

# Codex/OpenAI auth and config entries that ``AGENT_AUTH_ENV_VARS`` owns. The
# hosted passthrough derives these from the shared source of truth so hosted
# Codex can resolve the same credential/config names the local Compose path
# surfaces.
_CODEX_OPENAI_ENV_NAMES = frozenset(
    name
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_API_TOKEN",
        "CODEX_API_KEY",
        "CODEX_AUTH_TOKEN",
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "OPENAI_ORGANIZATION",
        "OPENAI_PROJECT",
        "OPENAI_PROJECT_ID",
    )
    if name in AGENT_AUTH_ENV_VARS
)


@register_adapter
class CodexAdapter(AgentAdapter):
    """Adapter that runs OpenAI Codex CLI in AWF workspaces."""

    runtime = AgentRuntime.codex

    @property
    def name(self) -> AgentRuntime:
        """Return the Codex runtime identity."""
        return AgentRuntime.codex

    def get_provider(self, model: str | None) -> str:
        """Return the provider family used for Codex runs."""
        del model
        return "openai"

    @property
    def hosted_env_passthrough_names(self) -> tuple[str, ...]:
        """Codex hosted credential contract.

        Names only — the hosted executor resolves values out-of-band; secret
        values are never transported, logged, or persisted by Core. The
        Codex/OpenAI auth and config entries are derived from
        ``AGENT_AUTH_ENV_VARS`` (the shared source of truth) so hosted Codex
        can resolve the same credential, base URL, organization, and project
        settings that a local Compose run would surface.
        """
        return tuple(name for name in AGENT_AUTH_ENV_VARS if name in _CODEX_OPENAI_ENV_NAMES)

    def _cli_args(self, *, model: str | None) -> list[str]:
        args = ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"]
        selected_model = model or self._default_model
        if selected_model:
            args += ["--model", selected_model]
        if self._default_effort:
            args += ["-c", f'model_reasoning_effort="{self._default_effort}"']
        args.append("-")
        return args
