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

    @property
    def hosted_env_passthrough_names(self) -> tuple[str, ...]:
        """Gemini hosted credential contract.

        Names only — secret values are never transported. Mirrors the
        ``GEMINI_*`` / ``GOOGLE_*`` auth, Vertex, and ADC entries in
        ``AGENT_AUTH_ENV_VARS`` so a hosted executor can resolve and inject the
        same credentials a local Compose run would surface, including the
        Vertex AI / Application Default Credentials backends.

        ``GOOGLE_APPLICATION_CREDENTIALS`` is intentionally NOT surfaced here:
        it is a *file-backed* credential (its value is a filesystem path), and
        the hosted request (``AgentRuntimeExecRequest``) carries no file/secret
        ref or mount. The local Compose path bind-mounts the referenced file via
        ``_build_host_auth_mounts`` so the path the env var points at exists and
        ADC/Vertex auth works, but env-only passthrough on the hosted path would
        inject a dangling ``GOOGLE_APPLICATION_CREDENTIALS=/some/path`` with the
        file absent, silently breaking ADC/Vertex auth even though the same
        workspace is ready under Compose. A future file/secret-ref mechanism on
        the hosted request is required to support it; until then it is not
        advertised as env-only (PR #751 thread PRRT_kwDOSJAM6s6Pas4k).
        """
        return (
            "GEMINI_API_KEY",
            "GEMINI_API_KEY_AUTH_MECHANISM",
            "GOOGLE_API_KEY",
            "GOOGLE_GENAI_USE_VERTEXAI",
            "GOOGLE_GENAI_USE_GCA",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_CLOUD_LOCATION",
            "GOOGLE_CLOUD_ACCESS_TOKEN",
        )

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
