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


def _ollama_base_url_prelude() -> str:
    """Shell prelude that resolves ``AWF_OPENCODE_OLLAMA_BASE_URL`` for launch.

    The agent's OpenCode config reads the daemon URL solely from
    ``AWF_OPENCODE_OLLAMA_BASE_URL`` (the generated ``baseURL`` is
    ``{env:AWF_OPENCODE_OLLAMA_BASE_URL}``). AWF's worker-side Ollama
    preflight/auto-pull (``provider_readiness_helpers._ollama_api_urls``) resolves
    the same daemon from ``AWF_OPENCODE_OLLAMA_BASE_URL`` first, then ``OLLAMA_HOST``,
    then the default — and defaults a port-less authority to ``:11434`` for *either*
    key. Normalize the resolved value here through the same scheme/port/``/v1`` rules
    so launch and preflight always agree on the daemon: a profile that set only
    ``OLLAMA_HOST`` (else the agent would talk to the default ``host.docker.internal``
    daemon while AWF probes the profile one), *and* an explicit
    ``AWF_OPENCODE_OLLAMA_BASE_URL`` that omits a port (e.g. ``http://ollama-sidecar/v1``
    — else the agent would collapse to the scheme default port 80 while AWF probed
    ``:11434``). A port-less host (a sidecar service name like ``ollama-sidecar``, or
    the bare default-host form) inherits Ollama's default daemon port (11434); a value
    that already carries a port is left intact. An explicit
    ``AWF_OPENCODE_OLLAMA_BASE_URL`` still wins over ``OLLAMA_HOST``; with neither set
    we fall back to the default.
    """
    return (
        '__awf_ollama_host="${AWF_OPENCODE_OLLAMA_BASE_URL:-}"\n'
        'if [ -z "$__awf_ollama_host" ]; then\n'
        '  __awf_ollama_host="${OLLAMA_HOST:-}"\n'
        "fi\n"
        'if [ -n "$__awf_ollama_host" ]; then\n'
        '  case "$__awf_ollama_host" in\n'
        "    *://*) : ;;\n"
        '    *) __awf_ollama_host="http://$__awf_ollama_host" ;;\n'
        "  esac\n"
        # Default the Ollama daemon port (11434) when the authority omits one,
        # so a port-less explicit base URL or OLLAMA_HOST does not resolve to
        # port 80 while the worker-side probe/pull builder targets :11434.
        '  __awf_ollama_scheme="${__awf_ollama_host%%://*}"\n'
        '  __awf_ollama_rest="${__awf_ollama_host#*://}"\n'
        '  __awf_ollama_authority="${__awf_ollama_rest%%/*}"\n'
        '  __awf_ollama_path="${__awf_ollama_rest#"$__awf_ollama_authority"}"\n'
        # Strip any userinfo (``user:pass@``) before deciding whether a port is
        # present, so a colon inside credentials is not mistaken for a port
        # separator (which would leave the value at the scheme default port 80
        # while the worker-side probe/pull builder defaults it to :11434).
        '  __awf_ollama_hostport="${__awf_ollama_authority##*@}"\n'
        '  case "$__awf_ollama_hostport" in\n'
        "    '['*']') __awf_ollama_authority=\"$__awf_ollama_authority:11434\" ;;\n"
        "    '['*']:'*) : ;;\n"
        "    *:*) : ;;\n"
        "    '') : ;;\n"
        '    *) __awf_ollama_authority="$__awf_ollama_authority:11434" ;;\n'
        "  esac\n"
        '  __awf_ollama_host="$__awf_ollama_scheme://$__awf_ollama_authority$__awf_ollama_path"\n'
        '  __awf_ollama_host="${__awf_ollama_host%/}"\n'
        '  case "$__awf_ollama_host" in\n'
        "    */v1) : ;;\n"
        '    *) __awf_ollama_host="$__awf_ollama_host/v1" ;;\n'
        "  esac\n"
        '  AWF_OPENCODE_OLLAMA_BASE_URL="$__awf_ollama_host"\n'
        "else\n"
        f'  AWF_OPENCODE_OLLAMA_BASE_URL="{DEFAULT_OLLAMA_OPENAI_BASE_URL}"\n'
        "fi\n"
        "export AWF_OPENCODE_OLLAMA_BASE_URL\n"
    )


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
        + _ollama_base_url_prelude()
        + "cat > \"$config_path\" <<'AWF_OPENCODE_CONFIG'\n"
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
