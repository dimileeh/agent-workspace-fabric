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
import yaml

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
from awf.profiles.compose import AGENT_AUTH_ENV_VARS

_PROMPT = "Fix the typo in README."
_COMPOSE_PROJECT = "awf_ws_xyz"
_COMPOSE_FILE = Path("/fake/path/compose.yml")
_SECRET_VALUE = "sk-non-codex-secret-do-not-leak"

# Claude Code auth / backend-toggle names are derived from AGENT_AUTH_ENV_VARS so
# the hosted contract cannot drift from the shared source of truth. The Bedrock /
# Vertex *backend* credentials (AWS_*, ANTHROPIC_VERTEX_PROJECT_ID, CLOUD_ML_REGION,
# GOOGLE_APPLICATION_CREDENTIALS) are NOT in AGENT_AUTH_ENV_VARS — the toggle is,
# the credentials it requires are not — so they stay a static supplement asserted
# alongside the derived set.
_CLAUDE_CODE_DERIVED_AUTH_NAMES = frozenset(
    name
    for name in AGENT_AUTH_ENV_VARS
    if name
    in {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
    }
)
_CLAUDE_CODE_BACKEND_AUTH_NAMES = (
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_BEARER_TOKEN_BEDROCK",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "CLOUD_ML_REGION",
    "GOOGLE_APPLICATION_CREDENTIALS",
)
_CLAUDE_NAMES = _CLAUDE_CODE_DERIVED_AUTH_NAMES | frozenset(_CLAUDE_CODE_BACKEND_AUTH_NAMES)
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


async def _run(
    adapter: object,
    *,
    compose_file: Path = _COMPOSE_FILE,
) -> AgentRuntimeExecRequest:
    await adapter.run(
        compose_project=_COMPOSE_PROJECT,
        compose_file=compose_file,
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
    async def test_opencode_surfaces_ollama_env_names(self, tmp_path: Path) -> None:
        # Use a readable compose whose agent service declares no env keys so the
        # compose/profile-owned exclusions (which the hosted path now applies
        # via ``filter_hosted_env_passthrough_names``) do not suppress any
        # names. An unreadable compose fails closed and would drop
        # ``AWF_OPENCODE_OLLAMA_BASE_URL`` — the same conservatism the local
        # ``agent_exec_env_passthrough`` applies.
        compose_file = tmp_path / "compose.yml"
        compose_file.write_text(
            yaml.safe_dump({"services": {"agent": {"image": "agent:latest"}}}),
            encoding="utf-8",
        )
        adapter = _build(OpenCodeAdapter)
        request = await _run(adapter, compose_file=compose_file)
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

    @pytest.mark.unit
    async def test_hosted_passthrough_suppresses_profile_owned_auth_slot(
        self, tmp_path: Path
    ) -> None:
        """A profile-owned auth/env slot must not be reintroduced by the hosted path.

        When the compose agent service already declares ``OPENAI_API_KEY`` (a
        placeholder, a lease-rendered value, or a profile literal), the local
        ``docker compose exec`` path suppresses it from exec-time ``-e``
        passthrough. The hosted path must apply the same exclusion or the hosted
        executor resolves an inherited worker credential the local path and
        readiness overlay deliberately keep out of the agent environment.
        """
        compose_file = tmp_path / "compose.yml"
        compose_file.write_text(
            yaml.safe_dump(
                {
                    "services": {
                        "agent": {
                            "image": "agent:latest",
                            "environment": {
                                "OPENAI_API_KEY": "${OPENAI_API_KEY}",
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        adapter = _build(OpenCodeAdapter)
        request = await _run(adapter, compose_file=compose_file)
        assert "OPENAI_API_KEY" not in request.env_passthrough_names
        # Non-profile-owned provider key names still pass through.
        assert "ANTHROPIC_API_KEY" in request.env_passthrough_names

    @pytest.mark.unit
    async def test_hosted_passthrough_suppresses_shadowing_worker_ollama_base_url(
        self, tmp_path: Path
    ) -> None:
        """A higher-precedence worker Ollama base URL key is suppressed when the profile owns the lower one.

        When the profile declares ``OLLAMA_HOST`` and the worker also carries
        ``AWF_OPENCODE_OLLAMA_BASE_URL`` (higher precedence), the local path
        suppresses the higher-precedence key so the profile-owned daemon wins.
        The hosted path must apply the same shadowing exclusion.
        """
        compose_file = tmp_path / "compose.yml"
        compose_file.write_text(
            yaml.safe_dump(
                {
                    "services": {
                        "agent": {
                            "image": "agent:latest",
                            "environment": {
                                "OLLAMA_HOST": "http://ollama.profile:11434",
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        adapter = _build(OpenCodeAdapter)
        request = await _run(adapter, compose_file=compose_file)
        assert "AWF_OPENCODE_OLLAMA_BASE_URL" not in request.env_passthrough_names
        # The profile-owned lower-precedence key is itself profile-owned, so it
        # is excluded too — the hosted executor does not re-resolve it either.
        assert "OLLAMA_HOST" not in request.env_passthrough_names
