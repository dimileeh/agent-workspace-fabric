"""Hosted credential contract tests for env-auth adapters other than Codex.

The Compose path derives safe exec passthrough env names from
``AGENT_AUTH_ENV_VARS`` so an env-auth adapter (Claude Code, Cursor, Gemini,
Grok, OpenCode) authenticates locally. The hosted (non-compose) path instead
reads each adapter's ``hosted_env_passthrough_names``; the base default is
empty, so each env-auth adapter must surface the Compose-equivalent safe env
names itself or the hosted request arrives with ``env_passthrough_names=()``
and the monitor repair run cannot inject credentials. Names only — secret
values are never transported.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import awf.adapters.registry  # noqa: F401 — populate registry
from awf.adapters.claude_code import ClaudeCodeAdapter
from awf.adapters.cursor import CursorAdapter
from awf.adapters.gemini import GeminiAdapter
from awf.adapters.grok import GrokAdapter
from awf.adapters.opencode import OpenCodeAdapter
from awf.adapters.runtime_executor import (
    AgentRuntimeExecRequest,
    AgentRuntimeExecResult,
)
from awf.common.commands import FakeCommandRunner
from awf.db.enums import AgentRuntime

_PROMPT = "Fix the typo in README."
_COMPOSE_PROJECT = "awf_ws_xyz"
_COMPOSE_FILE = Path("/fake/path/compose.yml")
_SECRET_VALUE = "sk-non-codex-secret-do-not-leak"

_CLAUDE_NAMES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)
_CURSOR_NAMES = ("CURSOR_API_KEY",)
_GEMINI_NAMES = (
    "GEMINI_API_KEY",
    "GEMINI_API_KEY_AUTH_MECHANISM",
    "GOOGLE_API_KEY",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_GENAI_USE_GCA",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_ACCESS_TOKEN",
)
_GROK_NAMES = ("XAI_API_KEY",)
_OPENCODE_NAMES = (
    "AWF_OPENCODE_OLLAMA_BASE_URL",
    "OLLAMA_HOST",
    "OLLAMA_API_KEY",
    # Provider-qualified non-Ollama models (openai/..., anthropic/...,
    # google/..., xai/...) are admitted at create time when the matching
    # provider API key is present and passed locally via AGENT_AUTH_ENV_VARS;
    # the hosted path must surface the same names or the hosted job launches
    # without auth for such a model.
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "XAI_API_KEY",
    # Non-secret OpenCode shell-tool runtime tuning (bash-tool timeout). The
    # local Compose path carries this via AGENT_AUTH_ENV_VARS; the hosted path
    # must surface the same name or the hosted job falls back to OpenCode's own
    # bash timeout while the same workspace behaves differently under Compose.
    "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS",
)


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[AgentRuntimeExecRequest] = []

    async def execute(self, request: AgentRuntimeExecRequest) -> AgentRuntimeExecResult:
        self.calls.append(request)
        return AgentRuntimeExecResult(returncode=0, stdout="ok", stderr="")


def _build(adapter_cls: type) -> object:
    return adapter_cls(runner=FakeCommandRunner(), runtime_executor=_RecordingExecutor())


async def _run(adapter: object) -> AgentRuntimeExecRequest:
    await adapter.run(
        compose_project=_COMPOSE_PROJECT,
        compose_file=_COMPOSE_FILE,
        prompt=_PROMPT,
        workspace_id="ws_non_codex",
    )
    executor = adapter._runtime_executor  # type: ignore[attr-defined]
    return executor.calls[0]


class TestNonCodexHostedCredentials:
    @pytest.mark.unit
    async def test_claude_code_surfaces_anthropic_env_names(self) -> None:
        adapter = _build(ClaudeCodeAdapter)
        request = await _run(adapter)
        assert request.agent_runtime is AgentRuntime.claude_code
        for name in _CLAUDE_NAMES:
            assert name in request.env_passthrough_names, name

    @pytest.mark.unit
    async def test_cursor_surfaces_cursor_api_key_name(self) -> None:
        adapter = _build(CursorAdapter)
        request = await _run(adapter)
        assert request.agent_runtime is AgentRuntime.cursor
        assert "CURSOR_API_KEY" in request.env_passthrough_names

    @pytest.mark.unit
    async def test_gemini_surfaces_google_env_names(self) -> None:
        adapter = _build(GeminiAdapter)
        request = await _run(adapter)
        assert request.agent_runtime is AgentRuntime.gemini
        for name in _GEMINI_NAMES:
            assert name in request.env_passthrough_names, name

    @pytest.mark.unit
    async def test_grok_surfaces_xai_api_key_name(self) -> None:
        adapter = _build(GrokAdapter)
        request = await _run(adapter)
        assert request.agent_runtime is AgentRuntime.grok
        assert "XAI_API_KEY" in request.env_passthrough_names

    @pytest.mark.unit
    async def test_opencode_surfaces_ollama_env_names(self) -> None:
        adapter = _build(OpenCodeAdapter)
        request = await _run(adapter)
        assert request.agent_runtime is AgentRuntime.opencode
        for name in _OPENCODE_NAMES:
            assert name in request.env_passthrough_names, name

    @pytest.mark.unit
    async def test_hosted_request_has_no_secret_values(self) -> None:
        adapter = _build(ClaudeCodeAdapter)
        request = await _run(adapter)
        blob = (
            request.prompt_stdin.decode("utf-8", "replace")
            + "\x00".join(request.cli_args)
            + "\x00".join(request.env_passthrough_names)
        )
        assert _SECRET_VALUE not in blob
        assert "sk-" not in blob
        # Only the *names* are present, never an assigned value.
        assert not any("=" in name for name in request.env_passthrough_names)
