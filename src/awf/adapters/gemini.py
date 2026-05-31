"""Google Gemini CLI adapter.

Uses ``gemini --yolo`` to skip all approvals (the container is the sandbox).
"""

from __future__ import annotations

import json

from awf.adapters.base import AgentAdapter, register_adapter
from awf.db.enums import AgentRuntime


@register_adapter
class GeminiAdapter(AgentAdapter):
    runtime = AgentRuntime.gemini

    @property
    def name(self) -> AgentRuntime:
        return AgentRuntime.gemini

    def get_provider(self, model: str | None) -> str:
        del model
        return "google"

    def _cli_args(self, *, model: str | None) -> list[str]:
        # Gemini CLI 0.41.2 documents -p/--prompt as non-interactive mode;
        # its value is appended to stdin, so AWF keeps the real prompt on
        # stdin and uses an empty value only as the headless-mode trigger.
        args = ["--skip-trust", "--yolo", "-p", ""]
        selected_model = model or self._default_model
        if selected_model:
            args += ["--model", selected_model]
        if not self._default_effort:
            return ["gemini", *args]

        settings = _settings_for_effort(model=selected_model, effort=self._default_effort)
        script = (
            "set -eu\n"
            'settings_path="${TMPDIR:-/tmp}/awf-gemini-settings-${WORKSPACE_ID:-default}.json"\n'
            "cat > \"$settings_path\" <<'AWF_GEMINI_SETTINGS'\n"
            f"{json.dumps(settings, separators=(',', ':'))}\n"
            "AWF_GEMINI_SETTINGS\n"
            'export GEMINI_CLI_SYSTEM_SETTINGS_PATH="$settings_path"\n'
            'export GEMINI_CLI_TRUST_WORKSPACE="${GEMINI_CLI_TRUST_WORKSPACE:-true}"\n'
            'exec gemini "$@"\n'
        )
        return ["sh", "-lc", script, "awf-gemini", *args]


def _settings_for_effort(*, model: str | None, effort: str) -> dict[str, object]:
    thinking_level = _thinking_level_for_effort(effort)
    if thinking_level is None:
        return {}

    match: dict[str, str] = {"model": model} if model else {}
    return {
        "modelConfigs": {
            "overrides": [
                {
                    "match": match,
                    "modelConfig": {
                        "generateContentConfig": {
                            "thinkingConfig": {"thinkingLevel": thinking_level}
                        }
                    },
                }
            ]
        }
    }


def _thinking_level_for_effort(effort: str) -> str | None:
    normalized = effort.lower()
    if normalized in {"high", "xhigh", "max"}:
        # Gemini CLI exposes HIGH as the highest portable thinking level
        # in settings. There is no direct xhigh CLI flag in 0.39.1.
        return "HIGH"
    return None
