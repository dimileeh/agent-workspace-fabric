"""Per-invocation provider isolation, also installed standalone in agent images.

This module deliberately uses only the standard library. Runtime credentials
are resolved inside the agent from its declared environment, never in argv.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, TextIO

EXECUTION_CONTRACT_VERSION = 1
RUNTIME_CONFIG_PATH = "/opt/awf/provider_config.py"
RUNTIME_PYTHON_PATH = "/usr/local/bin/python3"
_PROVIDER_PREFIXES = (
    "OPENAI_",
    "CODEX_",
    "ANTHROPIC_",
    "CLAUDE_",
    "OLLAMA_",
    "OPENROUTER_",
    "OPENCODE_",
    "GEMINI_",
    "GOOGLE_GEMINI_",
    "GOOGLE_VERTEX_",
    "CURSOR_",
    "XAI_",
    "AWS_BEDROCK_",
    "AWS_ENDPOINT_URL_BEDROCK",
    "AWF_SELECTED_PROVIDER_",
    "AWF_OLLAMA_",
    "AWF_OPENCODE_",
)


def runtime_failure_reason(stderr: str) -> str | None:
    candidate = stderr.strip()
    known = {
        "EXECUTION_RUNTIME_CONTRACT_UNSUPPORTED",
        "EXECUTION_PROJECT_CONFIGURATION_UNSUPPORTED",
        "EXECUTION_PROVIDER_CONFIGURATION_FAILED",
        "EXECUTION_CONTRACT_UNSUPPORTED",
        "EXECUTION_PROVIDER_UNSUPPORTED",
        "EXECUTION_AGENT_UNSUPPORTED",
        "PROVIDER_CREDENTIAL_MISSING",
        "PROVIDER_MODEL_UNAVAILABLE",
        "PROVIDER_ENDPOINT_UNREACHABLE",
        "PROVIDER_RESPONSE_INVALID",
    }
    return (
        candidate
        if candidate in known or re.fullmatch(r"PROVIDER_HTTP_[345][0-9]{2}", candidate)
        else None
    )


def competing_provider_env(name: str) -> bool:
    # Cloud credentials/regions/profiles can be separately declared tool access.
    # Only model-backend overrides compete with the selected connection.
    return name.startswith(_PROVIDER_PREFIXES) or name in {
        "GOOGLE_API_KEY",
        "GOOGLE_MODEL",
        "AWS_BEARER_TOKEN_BEDROCK",
    }


def provider_environment(
    selection: Mapping[str, Any], environ: Mapping[str, str]
) -> dict[str, str]:
    """Retain tool/forge env, selecting only the chosen model credential."""
    if selection.get("version") != EXECUTION_CONTRACT_VERSION:
        raise ValueError("EXECUTION_CONTRACT_UNSUPPORTED")
    env = {k: v for k, v in environ.items() if not competing_provider_env(k)}
    if selection["provider"] == "openrouter":
        source = str(selection["credential_ref"]).removeprefix("env/")
        # Profile leases/aliases arrive at the conventional target name.
        credential = environ.get("OPENROUTER_API_KEY") or environ.get(source)
        if not credential:
            raise ValueError("PROVIDER_CREDENTIAL_MISSING")
    elif selection["provider"] == "ollama":
        credential = "ollama"  # daemon owns cloud auth; never mount its credential store
    else:
        raise ValueError("EXECUTION_PROVIDER_UNSUPPORTED")
    if selection.get("credential_ref"):
        env.pop(str(selection["credential_ref"]).removeprefix("env/"), None)
    if selection["agent"] == "codex":
        env["AWF_SELECTED_PROVIDER_KEY"] = credential
    elif selection["agent"] == "claude_code":
        env.update(
            ANTHROPIC_BASE_URL=selection["endpoint"],
            ANTHROPIC_AUTH_TOKEN=credential,
            ANTHROPIC_API_KEY="",
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        )
    else:
        raise ValueError("EXECUTION_AGENT_UNSUPPORTED")
    return env


def safe_provider_config(agent: str, source: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        ("developer_instructions", "instructions")
        if agent == "codex"
        else ("permissions", "language")
    )
    config = {k: source[k] for k in keys if k in source}
    if agent == "codex" and "mcp_servers" in source:
        config["mcp_servers"] = {
            name: {k: server[k] for k in ("command", "args", "cwd", "enabled") if k in server}
            for name, server in source["mcp_servers"].items()
        }
    return config


@contextmanager
def isolated_provider_config(
    selection: Mapping[str, Any],
    *,
    root: Path,
    environ: Mapping[str, str],
    source_config: Mapping[str, Any],
) -> Iterator[tuple[list[str], dict[str, str], Path]]:
    env = provider_environment(selection, environ)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="awf-provider-", dir=root) as temporary:
        directory = Path(temporary)
        if selection["agent"] == "codex":
            config: dict[str, Any] = {
                k: source_config[k]
                for k in ("developer_instructions", "instructions")
                if k in source_config
            }
            config.update(model=selection["model"], model_provider="awf_selected")
            lines = [f"{key} = {json.dumps(value)}" for key, value in config.items()]
            lines += [
                "[model_providers.awf_selected]",
                'name = "AWF selected connection"',
                f"base_url = {json.dumps(selection['endpoint'] + '/v1')}",
                'wire_api = "responses"',
                'env_key = "AWF_SELECTED_PROVIDER_KEY"',
                "requires_openai_auth = false",
                "request_max_retries = 0",
                "stream_max_retries = 0",
            ]
            for name, server in source_config.get("mcp_servers", {}).items():
                lines.append(f"[mcp_servers.{json.dumps(name)}]")
                for key in ("command", "args", "cwd", "enabled"):
                    if key in server:
                        lines.append(f"{key} = {json.dumps(server[key])}")
            path = directory / "config.toml"
            path.write_text("\n".join(lines) + "\n")
            env["CODEX_HOME"] = str(directory)
            # Explicit selection overrides all lower layers. Untrusted project
            # config is ignored by the pinned CLI; AGENTS.md still applies.
            args = []
            for key, value in config.items():
                args += ["-c", f"{key}={json.dumps(value)}"]
            git_root = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            project_root = git_root.stdout.strip() if git_root.returncode == 0 else str(Path.cwd())
            args += ["-c", f'projects.{json.dumps(project_root)}.trust_level="untrusted"']
        else:
            config = {
                k: source_config[k] for k in ("permissions", "language") if k in source_config
            }
            config["model"] = selection["model"]
            path = directory / "settings.json"
            path.write_text(json.dumps(config))
            env["CLAUDE_CONFIG_DIR"] = str(directory)
            args = ["--setting-sources", "", "--settings", str(path)]
        path.chmod(0o600)
        yield args, env, directory


def probe_connection(selection: Mapping[str, Any], environ: Mapping[str, str]) -> dict[str, Any]:
    """Bounded readiness from the actual agent network; never coding evidence."""
    endpoint = selection["endpoint"]
    path = "/api/tags" if selection["provider"] == "ollama" else "/v1/models"
    headers = {}
    if selection["provider"] == "openrouter":
        credential = (
            provider_environment(selection, environ)["AWF_SELECTED_PROVIDER_KEY"]
            if selection["agent"] == "codex"
            else provider_environment(selection, environ)["ANTHROPIC_AUTH_TOKEN"]
        )
        headers["Authorization"] = "Bearer " + credential
    request = urllib.request.Request(endpoint + path, headers=headers)

    # Do not follow redirects carrying credentials to another endpoint.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(
            self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
        ) -> None:
            del req, fp, code, msg, headers, newurl
            return

    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
            data = json.loads(response.read(2_000_001))
        entries = data["models"] if selection["provider"] == "ollama" else data["data"]
        names = (
            {m.get("name") for m in data["models"]}
            if selection["provider"] == "ollama"
            else {m.get("id") for m in data["data"]}
        )
        model = selection["model"]
        if model not in names:
            model += ":latest"
        if model not in names:
            return {"ok": False, "reason_code": "PROVIDER_MODEL_UNAVAILABLE"}
        return {
            "ok": True,
            "reason_code": "PROVIDER_ENDPOINT_READY",
            "evidence": "runtime_network_readiness_only",
            "model_version": next(
                (m.get("digest") for m in entries if m.get("name", m.get("id")) == model),
                None,
            ),
        }
    except urllib.error.HTTPError as error:
        return {"ok": False, "reason_code": f"PROVIDER_HTTP_{error.code}"}
    except (OSError, urllib.error.URLError):
        return {"ok": False, "reason_code": "PROVIDER_ENDPOINT_UNREACHABLE"}
    except (ValueError, KeyError, TypeError, AttributeError):
        return {"ok": False, "reason_code": "PROVIDER_RESPONSE_INVALID"}


def main() -> int:
    if sys.argv[1:] == ["--contract-version"]:
        print(EXECUTION_CONTRACT_VERSION)
        return 0
    selection = json.loads(sys.argv[1])
    if selection.get("version") != EXECUTION_CONTRACT_VERSION:
        print("EXECUTION_RUNTIME_CONTRACT_UNSUPPORTED", file=sys.stderr)
        return 78
    if sys.argv[2:] == ["--probe-json"]:
        print(json.dumps(probe_connection(selection, os.environ)))
        return 0
    source = (
        Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
        if selection["agent"] == "codex"
        else Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
        / "settings.json"
    )
    safe_source = Path("/opt/awf/selected-provider-source.json")
    source_config: dict[str, Any] = {}
    if source.is_file():
        source_config = (
            tomllib.loads(source.read_text())
            if source.suffix == ".toml"
            else json.loads(source.read_text())
        )
    if safe_source.is_file():
        source_config = json.loads(safe_source.read_text())
    # Project agents and managed settings can select independent models/auth.
    # These configurations require a later runtime contract, never native fallback.
    unsupported_paths = (
        (Path("/etc/codex/config.toml"),)
        if selection["agent"] == "codex"
        else (Path("/etc/claude-code/managed-settings.json"), Path.cwd() / ".claude" / "agents")
    )
    if any(path.exists() for path in unsupported_paths):
        print("EXECUTION_PROJECT_CONFIGURATION_UNSUPPORTED", file=sys.stderr)
        return 78
    probe = probe_connection(selection, os.environ)
    if not probe["ok"]:
        print(probe["reason_code"], file=sys.stderr)
        return 78

    def cancelled(signum: int, _frame: Any) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, cancelled)
    with isolated_provider_config(
        selection,
        root=Path.home() / ".awf" / "provider-config",
        environ=os.environ,
        source_config=source_config,
    ) as (extra, env, _directory):
        cli = sys.argv[2:]
        # Insert after the executable/subcommand, before the stdin prompt marker.
        index = 2 if selection["agent"] == "codex" else 1
        cli[index:index] = extra
        return run_cli_redacted(cli, env)


def run_cli_redacted(cli: list[str], env: Mapping[str, str]) -> int:
    """Remove the selected credential inside the runtime, before streaming it."""
    secrets = tuple(
        v
        for k, v in env.items()
        if k in {"AWF_SELECTED_PROVIDER_KEY", "ANTHROPIC_AUTH_TOKEN"} and v and v != "ollama"
    )

    def emit(data: bytes, target: TextIO) -> None:
        text = data.decode("utf-8", errors="replace")
        for secret in secrets:
            text = text.replace(secret, "<redacted>")
        target.write(text)
        target.flush()

    process = subprocess.Popen(
        cli,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    pending: dict[int, bytes] = {}
    try:
        with selectors.DefaultSelector() as selector:
            for source, target in ((process.stdout, sys.stdout), (process.stderr, sys.stderr)):
                assert source is not None
                selector.register(source, selectors.EVENT_READ, target)
                pending[source.fileno()] = b""
            drain_deadline = None
            while selector.get_map():
                if process.poll() is not None:
                    if drain_deadline is None:
                        drain_deadline = time.monotonic() + 1
                    if time.monotonic() >= drain_deadline:
                        break
                for key, _events in selector.select(timeout=0.1):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        emit(pending.pop(key.fd), key.data)
                        selector.unregister(key.fileobj)
                        continue
                    lines = (pending[key.fd] + chunk).split(b"\n")
                    pending[key.fd] = lines.pop()
                    for line in lines:
                        emit(line + b"\n", key.data)
            for key in selector.get_map().values():
                emit(pending[key.fd], key.data)
        return process.wait()
    finally:
        # The leader may already have exited while background tools own its pipes.
        # Kill the whole session's process group, including TERM-resistant tools.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        finally:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=1)
            assert process.stdout is not None and process.stderr is not None
            process.stdout.close()
            process.stderr.close()


if __name__ == "__main__":  # pragma: no cover - standalone image entrypoint
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print(
            runtime_failure_reason(str(error)) or "EXECUTION_PROVIDER_CONFIGURATION_FAILED",
            file=sys.stderr,
        )
        raise SystemExit(78) from None
