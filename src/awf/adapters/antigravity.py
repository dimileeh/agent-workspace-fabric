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
existing file while preserving unrelated keys). ANTIGRAVITY_API_KEY is
retired.

Output uses ``stream-json`` so the idle stdout watchdog sees continuous events
(``text``/``json`` buffer until completion). Raw stream-json is *not* the run's
stdout because AWF's provider-neutral verdict protocol consumes plaintext final
records. A small Python bridge therefore runs ``agy`` and decodes each event as
it arrives. Assistant text goes to stdout; a result-only response is its fallback.
When ``agy`` repeats assistant text in its final result event, the duplicate stays
on stderr so stdout still contains exactly one agent response. Other events also
go to stderr, keeping the watchdog active without polluting protocol output.
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


# Decodes ``agy --output-format stream-json`` into the plaintext stdout AWF's
# verdict protocol expects, without giving up the continuous-output property that
# feeds the idle watchdog (PRRT_kwDOSJAM6s6Zi2YW). Runs ``agy`` as a child so
# its exit status is preserved (a shell pipeline would report the decoder's).
# Assistant text (or result text when no assistant text was emitted) -> stdout;
# every other event -> stderr. Non-JSON lines pass through unchanged.
_ANTIGRAVITY_STREAM_JSON_DECODER = r"""
import json
import subprocess
import sys


def _text_blocks(content):
    if isinstance(content, str):
        return [content]
    texts = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                texts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    texts.append(text)
    return texts


def _event_text(event):
    if not isinstance(event, dict):
        return ""
    kind = event.get("type")
    if kind == "assistant":
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else event.get("content")
        return "".join(_text_blocks(content))
    if kind == "result":
        result = event.get("result")
        return result if isinstance(result, str) else ""
    return ""


def main():
    try:
        proc = subprocess.Popen(["agy"] + sys.argv[1:], stdout=subprocess.PIPE)
    except OSError as exc:
        sys.stderr.write("failed to start agy: %s\n" % exc)
        return 127
    emitted_stdout_lines = set()
    emitted_stdout_blocks = []
    while True:
        raw = proc.stdout.readline()
        if not raw:
            break
        line = raw.decode("utf-8", "replace")
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except ValueError:
            # Not stream-json (agy warning / plaintext fallback): already the
            # shape verdict parsing wants, so keep it on stdout verbatim.
            sys.stdout.write(line if line.endswith("\n") else line + "\n")
            sys.stdout.flush()
            continue
        text = _event_text(event)
        if text:
            kind = event.get("type") if isinstance(event, dict) else None
            result_text = text.rstrip("\n")
            if (
                kind == "result"
                and result_text
                and (
                    result_text in emitted_stdout_lines
                    or result_text in emitted_stdout_blocks
                )
            ):
                sys.stderr.write(stripped + "\n")
                sys.stderr.flush()
                continue
            sys.stdout.write(text if text.endswith("\n") else text + "\n")
            sys.stdout.flush()
            block = text.rstrip("\n")
            if block:
                emitted_stdout_blocks.append(block)
            for line in text.splitlines():
                stripped_line = line.strip()
                if stripped_line:
                    emitted_stdout_lines.add(stripped_line)
            continue
        # Tool / progress events keep the idle watchdog fed on stderr; stdout
        # stays plaintext so a JSON-wrapped marker cannot garble the verdict.
        sys.stderr.write(stripped + "\n")
        sys.stderr.flush()
    return proc.wait()


raise SystemExit(main())
"""


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

        Names only — secret values are never transported. Authenticates with
        ``GEMINI_API_KEY`` (AI Studio key). ANTIGRAVITY_API_KEY is retired.
        """
        return ("GEMINI_API_KEY",)

    def _cli_args(self, *, model: str | None) -> list[str]:
        """Build the agy print-mode command; prompt bridged via ``$(cat)``.

        ``agy`` is launched through ``_ANTIGRAVITY_STREAM_JSON_DECODER`` so
        stream-json events reach AWF as one plaintext response while non-text
        events still stream on stderr for the idle watchdog.

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
            # agy runs under the stream-json decoder (its exit status is
            # propagated); flags and the final ``-p`` pair are forwarded verbatim.
            f"exec python3 -c {shlex.quote(_ANTIGRAVITY_STREAM_JSON_DECODER)} "
            "--dangerously-skip-permissions --output-format stream-json "
            f'--print-timeout 24h{model_flag} -p "$awf_prompt"\n'
        )
        return ["sh", "-lc", script]
