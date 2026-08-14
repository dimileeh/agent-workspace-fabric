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
        """Build the agy print-mode command; prompt bridged via ``$(cat)``."""
        selected_model = model or self._default_model
        # Effort is accepted and recorded on the adapter but intentionally
        # unmapped in v1 (no model_selection mapping yet), even though agy
        # exposes ``--effort``. Keep that axis out of argv until a deliberate
        # mapping lands in adapters/model_selection.py.
        _ = self._default_effort

        model_flag = ""
        if selected_model:
            model_flag = f" --model {shlex.quote(selected_model)}"

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
