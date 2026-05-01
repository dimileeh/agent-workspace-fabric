"""OpenAI Codex CLI adapter.

Codex reference: the ``codex exec`` non-interactive mode. We always pass
``--dangerously-bypass-approvals-and-sandbox`` because the container is
already a sandbox — approval prompts would just hang the run.
"""

from __future__ import annotations

from awf.adapters.base import AgentAdapter, register_adapter
from awf.db.enums import AgentRuntime


@register_adapter
class CodexAdapter(AgentAdapter):
    runtime = AgentRuntime.codex

    @property
    def name(self) -> AgentRuntime:
        return AgentRuntime.codex

    def get_provider(self, model: str | None) -> str:
        del model
        return "openai"

    def _cli_args(self, *, prompt: str, model: str | None) -> list[str]:
        args = ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"]
        if model:
            args += ["--model", model]
        if self._default_effort:
            args += ["-c", f'model_reasoning_effort="{self._default_effort}"']
        args.append(prompt)
        return args
