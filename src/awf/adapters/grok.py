"""xAI Grok Build CLI adapter.

Official Grok Build docs:
- https://docs.x.ai/build/cli/headless-scripting documents ``grok -p``,
  ``--always-approve``, ``--no-alt-screen``, ``--no-auto-update``,
  ``--output-format``, and ``--model`` for headless scripting.
- https://docs.x.ai/build/enterprise documents ``XAI_API_KEY`` authentication
  for non-interactive environments.
- https://docs.x.ai/developers/models/grok-build-0.1 documents the
  ``grok-build-0.1`` model used by AWF defaults.
"""

from __future__ import annotations

from awf.adapters.base import AgentAdapter, register_adapter
from awf.db.enums import AgentRuntime


@register_adapter
class GrokAdapter(AgentAdapter):
    runtime = AgentRuntime.grok

    @property
    def name(self) -> AgentRuntime:
        return AgentRuntime.grok

    def get_provider(self, model: str | None) -> str:
        del model
        return "xai"

    def _cli_args(self, *, model: str | None) -> list[str]:
        args = [
            "--always-approve",
            "--no-alt-screen",
            "--no-auto-update",
            "--output-format",
            "plain",
        ]
        if selected_model := _model_for_effort(model=model, effort=self._default_effort):
            args += ["-m", selected_model]
        return ["sh", "-c", _grok_launcher_script(), "awf-grok", *args]


def _grok_launcher_script() -> str:
    return 'set -eu\nprompt="$(cat)"\nexec grok -p "$prompt" "$@"\n'


def _model_for_effort(*, model: str | None, effort: str | None) -> str | None:
    """Return the model to pass to Grok for an AWF effort selection.

    Grok Build does not document a portable reasoning-effort CLI flag analogous
    to Gemini's ``thinkingLevel``. AWF therefore treats effort as model
    preserving for Grok; operators can request a different documented Grok Build
    model through AWF's normal model override path.
    """

    del effort
    return model
