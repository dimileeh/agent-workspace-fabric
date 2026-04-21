"""Google Gemini CLI adapter.

Uses ``gemini --yolo`` to skip all approvals (the container is the sandbox).
"""

from __future__ import annotations

from awf.adapters.base import AgentAdapter, register_adapter
from awf.db.enums import AgentRuntime


@register_adapter
class GeminiAdapter(AgentAdapter):
    runtime = AgentRuntime.gemini

    @property
    def name(self) -> AgentRuntime:
        return AgentRuntime.gemini

    def _cli_args(self, *, prompt: str, model: str | None) -> list[str]:
        args = ["gemini", "--yolo"]
        if model:
            args += ["-m", model]
        args += ["-p", prompt]
        return args
