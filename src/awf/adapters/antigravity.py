"""Google Antigravity CLI adapter.

Uses the official Antigravity CLI ``agy`` binary in print/headless mode.
Headless docs: https://antigravity.google/docs/cli/headless

Verified ``agy`` 1.1.13 contract (operator evidence; trust over older docs):

- ``-p`` / ``--print`` is a **value-consuming** string flag. ``agy -p --model X``
  makes the prompt the literal string ``--model`` and turns ``X`` into a
  positional that stops further flag parsing (later flags silently ignored;
  stdin never read). ``-p`` with no following token errors "flag needs an
  argument". ``-p -`` sends an empty prompt (stdin ignored).
- There is **no** bare-``-p``-reads-stdin mode and **no** ``--prompt-file``.
- The only stdin-compatible transport is a shell bridge: read the AWF prompt
  with ``awf_prompt=$(cat)``, then pass ``-p "$awf_prompt"`` as the **last**
  flag pair after all other options.
- A single argv string hits the kernel ``MAX_ARG_STRLEN`` (~128KiB) ceiling;
  the preamble fails closed above 100000 bytes rather than surfacing noisy
  ``execve`` ``E2BIG``. Empty prompts also fail closed (agy would otherwise
  idle-chat).
- API-key mode accepts **exactly** the model slugs in
  ``ANTIGRAVITY_API_KEY_MODE_MODELS`` (agy hardcodes them; e.g.
  ``gemini-3.7-flash`` exists in the Gemini API but agy rejects it).
- ``--effort`` is rejected by agy for **all** models in API-key mode. Effort
  exists only on the OAuth path as composite model slugs (e.g.
  ``gemini-3.6-flash-high``). AWF still accepts and records effort on the
  adapter for policy/observability, but never emits ``--effort``.

AWF still streams the wrapped prompt on docker-exec stdin into ``sh -lc``;
only the inner ``agy`` argv uses the ``$(cat)`` bridge.

Auth: agy 1.1.13 reads only ``GEMINI_API_KEY``. API-key mode requires
``settings.json`` ``{"modelProvider":"gemini"}`` **and** a non-empty
``GEMINI_API_KEY``. The preamble seeds that settings file when
``GEMINI_API_KEY`` is set (create if missing; upsert ``modelProvider`` on an
existing file while preserving unrelated keys) — never on
``ANTIGRAVITY_API_KEY`` alone, and never by aliasing credentials across env
names.

Output uses ``stream-json`` so the idle stdout watchdog sees continuous events
(``text``/``json`` buffer until completion).
"""

from __future__ import annotations

import shlex

from awf.adapters.base import AgentAdapter, register_adapter
from awf.db.enums import AgentRuntime

# Exact API-key mode model slugs hardcoded by agy 1.1.13. Mirrors the
# ANTIGRAVITY_VERSION pin in docker/agent-runtime.Dockerfile — re-verify this
# frozenset when that pin is bumped (agy may add/remove slugs; Gemini API
# availability is not authoritative).
ANTIGRAVITY_API_KEY_MODE_MODELS: frozenset[str] = frozenset(
    {
        "gemini-3.1-pro-preview",
        "gemini-3.5-flash",
        "gemini-3.6-flash",
    }
)


def _api_key_mode_model_reject_message(model: str) -> str:
    """Human-readable reject text for a model outside the API-key allowlist."""
    valid = ", ".join(sorted(ANTIGRAVITY_API_KEY_MODE_MODELS))
    return f"antigravity API-key mode does not accept model {model!r}; valid slugs: {valid}"


@register_adapter
class AntigravityAdapter(AgentAdapter):
    """Run Antigravity's headless CLI inside an AWF workspace container."""

    runtime = AgentRuntime.antigravity

    @property
    def name(self) -> AgentRuntime:
        """Return the AWF runtime enum for Antigravity workspaces."""
        return AgentRuntime.antigravity

    def get_provider(self, model: str | None) -> str:
        """Report Antigravity as its own provider (never ``google``/gemini)."""
        del model
        return "antigravity"

    @property
    def hosted_env_passthrough_names(self) -> tuple[str, ...]:
        """Antigravity hosted credential contract.

        Names only — secret values are never transported. Primary key is
        ``ANTIGRAVITY_API_KEY``; ``GEMINI_API_KEY`` is also forwarded (agy 1.1.x
        AI Studio path). No ``GOOGLE_API_KEY`` aliasing.
        """
        return ("ANTIGRAVITY_API_KEY", "GEMINI_API_KEY")

    def _cli_args(self, *, model: str | None) -> list[str]:
        """Build the agy print-mode command; prompt bridged via ``$(cat)``.

        Effort is accepted and recorded on the adapter (``self._default_effort``)
        for policy/observability, but never emitted as ``--effort``: agy
        rejects that flag for every model in API-key mode (OAuth uses
        composite slugs such as ``gemini-3.6-flash-high`` instead).

        Model allowlisting is API-key-mode only. ``_cli_args`` does not know
        the credential mode (hosted OAuth injects credentials later), so
        non-allowlisted models pass through into argv and are rejected only
        inside the ``GEMINI_API_KEY`` shell branch at runtime.
        """
        selected_model = model or self._default_model
        _ = self._default_effort

        model_flag = ""
        api_key_model_reject = ""
        if selected_model:
            model_flag = f" --model {shlex.quote(selected_model)}"
            if selected_model not in ANTIGRAVITY_API_KEY_MODE_MODELS:
                # Gate on GEMINI_API_KEY below — OAuth composite slugs must
                # still reach the runtime when API-key mode is inactive.
                api_key_model_reject = (
                    f"  printf '%s\\n' "
                    f"{shlex.quote(_api_key_mode_model_reject_message(selected_model))} "
                    ">&2\n"
                    "  exit 1\n"
                )
        # Seed/upsert modelProvider=gemini only when GEMINI_API_KEY is present —
        # that mode hard-requires GEMINI_API_KEY (agy does not read
        # ANTIGRAVITY_API_KEY). Do not alias credentials across env names.
        # Missing file: create minimal settings. Existing file: set
        # modelProvider via jq and preserve unrelated keys.
        # Prompt transport: awf_prompt=$(cat) then -p "$awf_prompt" last.
        script = (
            "set -eu\n"
            'settings_dir="${HOME}/.gemini/antigravity-cli"\n'
            'mkdir -p "$settings_dir"\n'
            'if [ -n "${GEMINI_API_KEY:-}" ]; then\n'
            '  if [ ! -f "$settings_dir/settings.json" ]; then\n'
            "    printf '%s\\n' '{\"modelProvider\":\"gemini\"}' "
            '> "$settings_dir/settings.json"\n'
            "  else\n"
            "    jq '.modelProvider = \"gemini\"' "
            '"$settings_dir/settings.json" '
            '> "$settings_dir/settings.json.tmp"\n'
            '    mv "$settings_dir/settings.json.tmp" '
            '"$settings_dir/settings.json"\n'
            "  fi\n"
            f"{api_key_model_reject}"
            "fi\n"
            "awf_prompt=$(cat)\n"
            'if [ -z "$awf_prompt" ]; then\n'
            "  printf '%s\\n' \"agy prompt is empty; refusing to start idle chat\" >&2\n"
            "  exit 1\n"
            "fi\n"
            # Portable length check (no bash ${#var}); printf '%s' avoids a
            # trailing newline that would inflate wc -c.
            "prompt_len=$(printf '%s' \"$awf_prompt\" | wc -c)\n"
            'if [ "$prompt_len" -gt 100000 ]; then\n'
            "  printf '%s\\n' "
            '"agy prompt exceeds 100000 bytes '
            "(kernel MAX_ARG_STRLEN ~128KiB single-arg ceiling); "
            'refusing to exec" >&2\n'
            "  exit 1\n"
            "fi\n"
            "exec agy --dangerously-skip-permissions --output-format stream-json "
            f'--print-timeout 24h{model_flag} -p "$awf_prompt"\n'
        )
        return ["sh", "-lc", script]
