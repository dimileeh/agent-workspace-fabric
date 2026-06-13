"""OpenCode CLI adapter.

OpenCode runs non-interactively via ``opencode run``. For AWF's Ollama
integration we inject a small inline config that points the OpenCode
``ollama`` provider at the host Ollama daemon. The agent container then talks
to Ollama through Docker's host gateway while AWF still owns the workspace
lifecycle.
"""

from __future__ import annotations

import json

from awf.adapters.base import AgentAdapter, register_adapter
from awf.db.enums import AgentRuntime

OPENCODE_OLLAMA_CLOUD_MODELS = (
    "kimi-k2.6:cloud",
    "glm-5.1:cloud",
    "gemma4:31b-cloud",
    "deepseek-v4-pro:cloud",
)
"""Default Ollama models AWF declares in the OpenCode config.

This is a *default fallback* set, **not** an allowlist gate. The host Ollama
daemon is the source of truth for which models are usable; the selected model
is always threaded into ``provider.ollama.models`` (see
``_opencode_config_for_effort``) so OpenCode never rejects a model the daemon
can serve. The tuple only supplies the default model when none is requested.
"""

DEFAULT_OLLAMA_OPENAI_BASE_URL = "http://host.docker.internal:11434/v1"


@register_adapter
class OpenCodeAdapter(AgentAdapter):
    runtime = AgentRuntime.opencode

    @property
    def name(self) -> AgentRuntime:
        return AgentRuntime.opencode

    def get_provider(self, model: str | None) -> str:
        active_model = model or self._default_model or OPENCODE_OLLAMA_CLOUD_MODELS[0]
        if "/" in active_model:
            return active_model.split("/", 1)[0]
        return "ollama"

    def _cli_args(self, *, model: str | None) -> list[str]:
        requested_model = (model or self._default_model or OPENCODE_OLLAMA_CLOUD_MODELS[0]).strip()
        if not requested_model:
            requested_model = OPENCODE_OLLAMA_CLOUD_MODELS[0]
        selected_model = _qualified_model(requested_model)
        script = _opencode_launcher_script(effort=self._default_effort, model=requested_model)
        args = [
            "sh",
            "-c",
            script,
            "awf-opencode",
            "--dangerously-skip-permissions",
            "--model",
            selected_model,
        ]
        if variant := _variant_for_effort(self._default_effort):
            args.extend(["--variant", variant, "--thinking"])
        args.append("Follow the instructions in the attached AWF prompt file exactly.")
        return args


def _qualified_model(model: str) -> str:
    if "/" in model:
        return model
    return f"ollama/{model}"


def _config_model_key(model: str) -> str:
    """Return the OpenCode ``provider.ollama.models`` key for a model reference.

    Strips a leading ``ollama/`` provider prefix while preserving the ``:tag`` /
    ``:cloud`` suffix, so the config key agrees with the bare model name the
    Ollama daemon serves and with the ``--model`` flag derivation.
    """
    raw = model.strip()
    if "/" in raw:
        provider, remainder = raw.split("/", 1)
        if provider == "ollama" and remainder:
            return remainder
    return raw


def _is_ollama_model(model: str) -> bool:
    """Return whether ``model`` is served by the Ollama provider.

    A bare reference (no provider prefix) defaults to Ollama, as does an
    explicit ``ollama/`` prefix — even when the remainder itself contains a
    ``/`` (e.g. ``ollama/hf.co/user/model`` for a daemon-served HF model). Any
    other provider prefix (e.g. ``openai/...``) belongs to that provider.
    """
    raw = model.strip()
    if "/" not in raw:
        return True
    provider, _ = raw.split("/", 1)
    return provider == "ollama"


def _opencode_launcher_script(*, effort: str | None, model: str | None = None) -> str:
    config = _opencode_config_for_effort(effort=effort, model=model)
    config_json = json.dumps(config, separators=(",", ":"))
    return (
        "set -eu\n"
        "prompt_path=\n"
        "config_path=\n"
        "child_pid=\n"
        "cleanup() {\n"
        '  if [ -n "$child_pid" ] && kill -0 "$child_pid" 2>/dev/null; then\n'
        '    kill "$child_pid" 2>/dev/null || true\n'
        '    wait "$child_pid" 2>/dev/null || true\n'
        "  fi\n"
        '  [ -n "$prompt_path" ] && rm -f "$prompt_path" 2>/dev/null || true\n'
        '  [ -n "$config_path" ] && rm -f "$config_path" 2>/dev/null || true\n'
        "}\n"
        "forward_signal() {\n"
        '  sig="$1"\n'
        '  code="$2"\n'
        '  if [ -n "$child_pid" ]; then\n'
        '    kill "-$sig" "$child_pid" 2>/dev/null || true\n'
        '    wait "$child_pid" 2>/dev/null || true\n'
        "    child_pid=\n"
        "  fi\n"
        '  exit "$code"\n'
        "}\n"
        "trap cleanup EXIT\n"
        "trap 'forward_signal HUP 129' HUP\n"
        "trap 'forward_signal INT 130' INT\n"
        "trap 'forward_signal TERM 143' TERM\n"
        'config_path="$(mktemp "${TMPDIR:-/tmp}/awf-opencode-config.XXXXXX.json")"\n'
        "export AWF_OPENCODE_OLLAMA_BASE_URL="
        '"${AWF_OPENCODE_OLLAMA_BASE_URL:-'
        f"{DEFAULT_OLLAMA_OPENAI_BASE_URL}"
        '}"\n'
        "cat > \"$config_path\" <<'AWF_OPENCODE_CONFIG'\n"
        f"{config_json}\n"
        "AWF_OPENCODE_CONFIG\n"
        'export OPENCODE_CONFIG_CONTENT="$(cat "$config_path")"\n'
        'prompt_path="$(mktemp "${TMPDIR:-/tmp}/awf-opencode-prompt.XXXXXX.md")"\n'
        'cat > "$prompt_path"\n'
        'opencode run --file "$prompt_path" "$@" &\n'
        "child_pid=$!\n"
        "set +e\n"
        'wait "$child_pid"\n'
        "status=$?\n"
        "set -e\n"
        "child_pid=\n"
        'exit "$status"\n'
    )


def _opencode_config_for_effort(
    *,
    effort: str | None,
    model: str | None = None,
) -> dict[str, object]:
    model_config: dict[str, object] = {"name": ""}
    if _thinking_enabled(effort):
        # Ollama Cloud exposes thinking-capable models; OpenCode's provider
        # config asks Ollama for thinking, while the CLI ``--variant`` flag
        # carries the requested reasoning effort for OpenCode versions that
        # support it.
        model_config["options"] = {"think": True}

    # Always declare the selected model (plus the default fallback set) so
    # OpenCode never rejects a daemon-served model. The host Ollama daemon — not
    # this list — is the source of truth; the tuple is only a sensible default.
    model_keys = list(OPENCODE_OLLAMA_CLOUD_MODELS)
    if model:
        selected_key = _config_model_key(model)
        # Only Ollama-served models belong in the ``ollama`` provider block. A
        # provider-qualified name (e.g. ``openai/...``) is left for its own
        # provider, not misrouted through Ollama. Decide by the original
        # provider prefix, not by whether the normalized key contains a ``/`` —
        # a daemon-served ``ollama/hf.co/...`` model keeps a ``/`` in its key
        # and must still be declared.
        if selected_key and _is_ollama_model(model) and selected_key not in model_keys:
            model_keys.append(selected_key)
    models = {key: {**model_config, "name": key} for key in model_keys}
    return {
        "$schema": "https://opencode.ai/config.json",
        "permission": "allow",
        "provider": {
            "ollama": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Ollama",
                "options": {
                    "baseURL": "{env:AWF_OPENCODE_OLLAMA_BASE_URL}",
                },
                "models": models,
            },
        },
    }


def _thinking_enabled(effort: str | None) -> bool:
    if effort is None:
        return False
    return effort.lower() in {"high", "xhigh", "max"}


def _variant_for_effort(effort: str | None) -> str | None:
    if effort is None:
        return None
    normalized = effort.lower()
    if normalized in {"xhigh", "max"}:
        return "max"
    if normalized == "high":
        return "high"
    return None
