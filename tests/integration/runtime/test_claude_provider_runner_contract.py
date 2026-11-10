"""Pinned Claude CLI contract for the third-party provider runner.

Runs the real standalone runner and the pinned ``claude`` binary against a
localhost fake Anthropic endpoint. No credentials, no paid or live calls.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

import awf.adapters.provider_config as runner
from awf.common.agent_execution import resolve_execution

PINNED_CLAUDE_VERSION = "2.1.295"
MODEL = "vendor/selected-model"
SENTINEL = "selected-sentinel-credential"
PROMPT = "AWF_CONTRACT_PROMPT run the scripted steps"
SUBAGENT_PROMPT = "AWF_SUBAGENT_TASK reply"
CLAUDE_MD_MARKER = "AWF_PROJECT_CLAUDE_MD_MARKER_6f1c"
AGENT_MARKER = "AWF_PROJECT_AGENT_MARKER_93ad"


def _pinned_claude() -> str | None:
    executable = shutil.which("claude")
    if executable is None:
        return None
    version = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, timeout=30, check=False
    ).stdout
    return executable if version.split(" ", 1)[0] == PINNED_CLAUDE_VERSION else None


pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.timeout(180),
    pytest.mark.skipif(
        _pinned_claude() is None, reason=f"requires claude CLI {PINNED_CLAUDE_VERSION}"
    ),
]


def _sse(blocks: list[dict[str, Any]], stop_reason: str) -> bytes:
    events: list[tuple[str, dict[str, Any]]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_fake",
                    "type": "message",
                    "role": "assistant",
                    "model": MODEL,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
        )
    ]
    for index, block in enumerate(blocks):
        if block["type"] == "text":
            start = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": block["text"]}
        else:
            start = {**block, "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        events += [
            (
                "content_block_start",
                {"type": "content_block_start", "index": index, "content_block": start},
            ),
            (
                "content_block_delta",
                {"type": "content_block_delta", "index": index, "delta": delta},
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": index}),
        ]
    events += [
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return b"".join(
        f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in events
    )


def _text(body: dict[str, Any]) -> str:
    return json.dumps(body.get("system", "")) + json.dumps(body.get("messages", []))


def _first_user_text(body: dict[str, Any]) -> str:
    messages = body.get("messages") or [{}]
    return json.dumps(messages[0].get("content", ""))


def _tool_results(body: dict[str, Any]) -> list[str]:
    results = []
    for message in body.get("messages", []):
        content = message.get("content")
        if isinstance(content, list):
            results += [
                json.dumps(part.get("content"))
                for part in content
                if isinstance(part, dict) and part.get("type") == "tool_result"
            ]
    return results


class _Recorder(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Handler(BaseHTTPRequestHandler):
    server: _Recorder

    def log_message(self, *_args: object) -> None:
        return

    def _record(self, body: dict[str, Any] | None) -> None:
        with self.server.lock:
            self.server.requests.append(
                {
                    "method": self.command,
                    "path": self.path.split("?", 1)[0],
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                }
            )

    def _reply(self, status: int, payload: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        self._record(None)
        if self.path.split("?", 1)[0] == "/v1/models":
            payload = {"data": [{"id": MODEL, "type": "model", "display_name": MODEL}]}
            self._reply(200, json.dumps(payload).encode(), "application/json")
        else:
            self._reply(404, b"{}", "application/json")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        self._record(body)
        if self.path.split("?", 1)[0] != "/v1/messages":
            self._reply(404, b"{}", "application/json")
            return
        blocks, stop = self._script(body)
        if body.get("stream"):
            self._reply(200, _sse(blocks, stop), "text/event-stream")
            return
        message = {
            "id": "msg_fake",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": blocks,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        self._reply(200, json.dumps(message).encode(), "application/json")

    @staticmethod
    def _script(body: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
        first = _first_user_text(body)
        if PROMPT not in first:
            # Subagent and any background/title request end immediately.
            return [{"type": "text", "text": "done"}], "end_turn"
        results = _tool_results(body)
        tools = {tool.get("name") for tool in body.get("tools", [])}
        if not results:
            return [
                {"type": "tool_use", "id": "toolu_env", "name": "Bash", "input": {"command": "env"}}
            ], "tool_use"
        if len(results) == 1:
            subagent_tool = "Agent" if "Agent" in tools else "Task"
            return [
                {
                    "type": "tool_use",
                    "id": "toolu_agent",
                    "name": subagent_tool,
                    "input": {
                        "subagent_type": "probe",
                        "description": "probe",
                        "prompt": SUBAGENT_PROMPT,
                    },
                }
            ], "tool_use"
        return [{"type": "text", "text": "all done"}], "end_turn"


@pytest.fixture
def servers() -> Iterator[tuple[_Recorder, _Recorder]]:
    started = [_Recorder(), _Recorder()]
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in started]
    for thread in threads:
        thread.start()
    try:
        yield started[0], started[1]
    finally:
        for server in started:
            server.shutdown()
            server.server_close()


def _project(root: Path, decoy: str) -> None:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "CLAUDE.md").write_text(f"# Project rules\n{CLAUDE_MD_MARKER}\n")
    claude = root / ".claude"
    (claude / "agents").mkdir(parents=True)
    (claude / "settings.json").write_text(
        json.dumps(
            {
                "model": "project-model",
                "apiKeyHelper": "printf project-helper-key",
                "effortLevel": "low",
                "env": {
                    "ANTHROPIC_BASE_URL": decoy,
                    "ANTHROPIC_AUTH_TOKEN": "project-token",
                    "ANTHROPIC_MODEL": "project-model",
                    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "project-haiku",
                    "CLAUDE_CODE_SUBAGENT_MODEL": "project-sub",
                    # Settings env must not undo the runner's forcing/effort env.
                    "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "0",
                    "CLAUDE_CODE_EFFORT_LEVEL": "max",
                    "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST": "0",
                },
            }
        )
    )
    (claude / "agents" / "probe.md").write_text(
        "---\nname: probe\ndescription: Contract probe agent\nmodel: claude-opus-5-5\n---\n"
        f"{AGENT_MARKER} Reply briefly.\n"
    )


def test_pinned_claude_runs_project_instructions_with_host_managed_provider(
    tmp_path: Path, servers: tuple[_Recorder, _Recorder]
) -> None:
    fake, decoy = servers
    project = tmp_path / "project"
    home = tmp_path / "home"
    home.mkdir()
    _project(project, decoy.url)
    selection = resolve_execution(
        agent="claude_code", provider="openrouter", model=MODEL, allow_unverified=True
    ).model_dump()
    selection["endpoint"] = fake.url
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "OPENROUTER_API_KEY": SENTINEL,
        "GH_TOKEN": "tool-sentinel",
    }

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            runner.__file__,
            json.dumps(selection),
            "claude",
            "--dangerously-skip-permissions",
            "--model",
            MODEL,
            "-p",
        ],
        input=PROMPT,
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=150,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert SENTINEL not in result.stdout + result.stderr
    assert list((home / ".awf" / "provider-config").iterdir()) == []
    assert decoy.requests == []
    messages = [r for r in fake.requests if r["path"] == "/v1/messages"]
    assert messages, fake.requests
    for request in fake.requests:
        headers = request["headers"]
        assert headers.get("authorization") == f"Bearer {SENTINEL}", request["path"]
        assert "project-token" not in json.dumps(headers)
        assert "project-helper-key" not in json.dumps(headers)
    assert {r["body"]["model"] for r in messages} == {MODEL}

    main = [r["body"] for r in messages if PROMPT in _first_user_text(r["body"])]
    assert CLAUDE_MD_MARKER in _text(main[0])
    subagent = [r["body"] for r in messages if SUBAGENT_PROMPT in _first_user_text(r["body"])]
    assert subagent and AGENT_MARKER in _text(subagent[0])
    bash_output = _tool_results(main[-1])[0]
    assert "GH_TOKEN=tool-sentinel" in bash_output
    assert SENTINEL not in bash_output
    assert "ANTHROPIC_AUTH_TOKEN=" not in bash_output
    # Neither the CLI default nor the project effortLevel reaches the wire.
    assert [r["body"].get("output_config", {}).get("effort") for r in messages] == [None] * len(
        messages
    )
