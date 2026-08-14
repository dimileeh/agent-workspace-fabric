"""Google Antigravity CLI adapter.

Uses the official Antigravity CLI ``agy`` binary in print/headless mode.
Headless docs: https://antigravity.google/docs/cli/headless

AWF streams the wrapped prompt on stdin. ``agy`` 1.1.x does not accept prompts
on stdin and has no ``--prompt-file`` flag; a single ``-p`` argv also hits the
kernel ``MAX_ARG_STRLEN`` (~128KiB) ceiling for large AWF prompts. The adapter
therefore uses a shell preamble that writes stdin to a container temp file and
asks ``agy`` (with ``--dangerously-skip-permissions``) to read and execute that
file — the locked tempfile fallback adapted for a CLI that lacks ``--prompt-file``.

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
        """Build the agy print-mode command via a stdin→tempfile shell preamble."""
        selected_model = model or self._default_model
        # Effort is accepted and recorded on the adapter but intentionally
        # unmapped in v1 (no model_selection mapping yet), even though agy
        # exposes ``--effort``. Keep that axis out of argv until a deliberate
        # mapping lands in adapters/model_selection.py.
        _ = self._default_effort

        model_flag = ""
        if selected_model:
            model_flag = f" --model {shlex.quote(selected_model)}"

        # Seed modelProvider=gemini when an API key is present so headless
        # AI Studio auth works without an interactive login. Do not alias
        # ANTIGRAVITY_API_KEY onto GEMINI_API_KEY (explicit over clever).
        script = (
            "set -eu\n"
            'prompt_path="${TMPDIR:-/tmp}/awf-agy-prompt-${WORKSPACE_ID:-default}.txt"\n'
            'settings_dir="${HOME}/.gemini/antigravity-cli"\n'
            'mkdir -p "$settings_dir"\n'
            'if [ -n "${ANTIGRAVITY_API_KEY:-}${GEMINI_API_KEY:-}" ] '
            '&& [ ! -f "$settings_dir/settings.json" ]; then\n'
            "  printf '%s\\n' '{\"modelProvider\":\"gemini\"}' "
            '> "$settings_dir/settings.json"\n'
            "fi\n"
            'cat > "$prompt_path"\n'
            'exec agy -p "Read and execute the full task prompt at $prompt_path. '
            'Treat that file as the complete operator instructions." '
            "--dangerously-skip-permissions --output-format stream-json "
            f"--print-timeout 24h{model_flag}\n"
        )
        return ["sh", "-lc", script]
