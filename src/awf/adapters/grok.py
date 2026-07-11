"""xAI Grok Build CLI adapter.

Official Grok Build docs:
- https://docs.x.ai/build/cli/headless-scripting documents ``grok -p``,
  ``--always-approve``, ``--no-alt-screen``, ``--no-auto-update``,
  ``--output-format``, and ``--model`` for headless scripting.
- The same page documents ``grok agent stdio`` as an ACP agent mode over
  stdin/stdout, which AWF uses so prompts stay off process argv.
- https://docs.x.ai/build/enterprise documents ``XAI_API_KEY`` authentication
  for non-interactive environments.
- `grok models` reports ``grok-build`` as the default Grok Build coding model.
"""

from __future__ import annotations

import shlex

from awf.adapters.base import AgentAdapter, register_adapter
from awf.db.enums import AgentRuntime


@register_adapter
class GrokAdapter(AgentAdapter):
    """Adapter that runs xAI Grok Build CLI in AWF workspaces."""

    runtime = AgentRuntime.grok

    @property
    def name(self) -> AgentRuntime:
        """Return the Grok runtime identity."""
        return AgentRuntime.grok

    def get_provider(self, model: str | None) -> str:
        """Return the provider family used for Grok runs."""
        del model
        return "xai"

    @property
    def hosted_env_passthrough_names(self) -> tuple[str, ...]:
        """Grok hosted credential contract.

        Names only — secret values are never transported. Mirrors the
        ``XAI_API_KEY`` entry in ``AGENT_AUTH_ENV_VARS`` so a hosted executor
        can resolve and inject the same credential a local Compose run would
        surface.
        """
        return ("XAI_API_KEY",)

    def _cli_args(self, *, model: str | None) -> list[str]:
        args = [
            "--always-approve",
            "--no-auto-update",
        ]
        selected_model = self._selected_model_for_run(model=model)
        if selected_model := _model_for_effort(
            model=selected_model,
            effort=self._default_effort,
        ):
            args += ["-m", selected_model]
        return ["sh", "-c", _grok_launcher_script(), "awf-grok", *args]


def _grok_launcher_script() -> str:
    return (
        "# Launch grok agent stdio over ACP; AWF's prompt stays on stdin.\n"
        f'exec python3 -c {shlex.quote(_GROK_ACP_BRIDGE_SCRIPT)} "$@"\n'
    )


_GROK_ACP_BRIDGE_SCRIPT = r"""
import json
import os
import select
import subprocess
import sys
import time

_DRAIN_INTERVAL_SECONDS = 0.15
_DRAIN_IDLE_TIMEOUT_SECONDS = 5.0


def _request(proc, request_id, method, params, text_chunks):
    assert proc.stdin is not None
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    }
    proc.stdin.write((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))
    proc.stdin.flush()
    while True:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError(f"grok agent stdio exited before {method} completed")
        message = json.loads(line.decode("utf-8"))
        _capture_text_update(message, text_chunks)
        if message.get("id") != request_id:
            continue
        if error := message.get("error"):
            raise RuntimeError(error.get("message") or json.dumps(error))
        return message.get("result") or {}


def _capture_text_update(message, text_chunks):
    if message.get("method") != "session/update":
        return
    update = (message.get("params") or {}).get("update") or {}
    content = update.get("content") or {}
    text = content.get("text")
    if update.get("sessionUpdate") == "agent_message_chunk" and text:
        text_chunks.append(text)


def _drain_remaining_updates(proc, text_chunks):
    _close_stdin(proc)
    idle_deadline = time.monotonic() + _DRAIN_IDLE_TIMEOUT_SECONDS
    while True:
        assert proc.stdout is not None
        remaining = idle_deadline - time.monotonic()
        if remaining <= 0:
            break
        readable, _, _ = select.select(
            [proc.stdout],
            [],
            [],
            min(_DRAIN_INTERVAL_SECONDS, remaining),
        )
        if not readable:
            continue
        line = proc.stdout.readline()
        if not line:
            break
        _capture_text_update(json.loads(line.decode("utf-8")), text_chunks)
        idle_deadline = time.monotonic() + _DRAIN_IDLE_TIMEOUT_SECONDS


def _auth_method_ids(auth_methods):
    ids = set()
    for method in auth_methods or ():
        if isinstance(method, dict) and isinstance(method.get("id"), str):
            ids.add(method["id"])
        elif isinstance(method, str):
            ids.add(method)
    return ids


def _close_stdin(proc):
    if proc.stdin is not None:
        try:
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass


def _close_grok(proc):
    _close_stdin(proc)
    if proc.poll() is not None:
        return proc.returncode or 0
    try:
        return proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            return proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            return proc.wait()


def main():
    prompt = sys.stdin.read()
    proc = subprocess.Popen(
        ["grok", *sys.argv[1:], "agent", "stdio"],
        bufsize=0,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    text_chunks = []
    try:
        request_id = 1
        init = _request(
            proc,
            request_id,
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": True,
                },
            },
            text_chunks,
        )
        request_id += 1
        auth_ids = _auth_method_ids(init.get("authMethods"))
        if os.environ.get("XAI_API_KEY") and "xai.api_key" in auth_ids:
            method_id = "xai.api_key"
        elif "cached_token" in auth_ids:
            method_id = "cached_token"
        else:
            raise RuntimeError("Run `grok login` first, or set XAI_API_KEY.")
        _request(
            proc,
            request_id,
            "authenticate",
            {"methodId": method_id, "_meta": {"headless": True}},
            text_chunks,
        )
        request_id += 1
        session = _request(
            proc,
            request_id,
            "session/new",
            {"cwd": os.getcwd(), "mcpServers": []},
            text_chunks,
        )
        request_id += 1
        session_id = session.get("sessionId")
        if not session_id:
            raise RuntimeError("grok agent stdio did not return a session id")
        _request(
            proc,
            request_id,
            "session/prompt",
            {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": prompt}],
            },
            text_chunks,
        )
        _drain_remaining_updates(proc, text_chunks)
        output = "".join(text_chunks)
        if output:
            sys.stdout.write(output)
            if not output.endswith("\n"):
                sys.stdout.write("\n")
            sys.stdout.flush()
        return _close_grok(proc)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        exit_code = _close_grok(proc)
        return exit_code if exit_code != 0 else 1
    finally:
        _close_grok(proc)


raise SystemExit(main())
"""


def _model_for_effort(*, model: str | None, effort: str | None) -> str | None:
    """Return the model to pass to Grok for an AWF effort selection.

    Grok Build does not document a portable reasoning-effort CLI flag analogous
    to Gemini's ``thinkingLevel``. AWF therefore treats effort as model
    preserving for Grok; operators can request a different documented Grok Build
    model through AWF's normal model override path.
    """

    del effort
    return model
