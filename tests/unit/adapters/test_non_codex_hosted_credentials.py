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

import threading
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


def _write_compose(
    tmp_path: Path,
    environment: dict[str, str] | None = None,
) -> Path:
    """Write a minimal readable compose file with a single ``agent`` service.

    ``environment=None`` (default) writes a service with no env block so
    compose/profile-owned exclusions do not suppress any passthrough names. A
    mapping writes each key/value pair into the agent service's
    ``environment`` block.
    """
    services: dict[str, dict[str, object]] = {"agent": {"image": "agent:latest"}}
    if environment is not None:
        services["agent"]["environment"] = environment
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump({"services": services}),
        encoding="utf-8",
    )
    return compose_file


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
        compose_file = _write_compose(tmp_path)
        adapter = _build(OpenCodeAdapter)
        request = await _run(adapter, compose_file=compose_file)
        assert request.agent_runtime is AgentRuntime.opencode
        for name in _OPENCODE_NAMES:
            assert name in request.env_passthrough_names, name
        # No profile-owned env declared on the agent service -> empty profile_env.
        assert request.profile_env == ()

    @pytest.mark.unit
    async def test_hosted_request_has_no_secret_values(self, tmp_path: Path) -> None:
        # Run ClaudeCodeAdapter against a populated ``profile_env`` so the leak
        # path is exercised for real profile-owned values (not just the empty
        # ``profile_env`` case). ``OLLAMA_HOST`` is a non-secret profile-owned
        # literal; the secret-safety assertions must still hold against it, and
        # the literal value is carried via ``profile_env``.
        compose_file = _write_compose(
            tmp_path, environment={"OLLAMA_HOST": "http://ollama.profile:11434"}
        )
        adapter = _build(ClaudeCodeAdapter)
        request = await _run(adapter, compose_file=compose_file)
        blob = (
            request.prompt_stdin.decode("utf-8", "replace")
            + "\x00".join(request.cli_args)
            + "\x00".join(request.env_passthrough_names)
            + "\x00".join(f"{k}={v}" for k, v in request.profile_env)
        )
        assert _SECRET_VALUE not in blob
        assert "sk-" not in blob
        # Only the *names* are present, never an assigned value.
        assert not any("=" in name for name in request.env_passthrough_names)
        # ``profile_env`` carries literal profile values, never worker-resolved
        # ``${NAME}`` secret placeholders (those stay in env_passthrough_names
        # for out-of-band resolution by the hosted executor).
        assert not any("${" in value for _key, value in request.profile_env)
        # The populated profile-owned literal is actually carried so the leak
        # path is real (an empty ``profile_env`` would make the ${...} assertion
        # vacuously pass).
        assert ("OLLAMA_HOST", "http://ollama.profile:11434") in request.profile_env

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
        compose_file = _write_compose(tmp_path, environment={"OPENAI_API_KEY": "${OPENAI_API_KEY}"})
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
        compose_file = _write_compose(
            tmp_path, environment={"OLLAMA_HOST": "http://ollama.profile:11434"}
        )
        adapter = _build(OpenCodeAdapter)
        request = await _run(adapter, compose_file=compose_file)
        assert "AWF_OPENCODE_OLLAMA_BASE_URL" not in request.env_passthrough_names
        # The profile-owned lower-precedence key is itself profile-owned, so it
        # is excluded too — the hosted executor does not re-resolve it either.
        assert "OLLAMA_HOST" not in request.env_passthrough_names
        # The hosted executor has no compose env block, so the literal
        # profile-owned value the local container received at stack launch
        # must be carried via ``profile_env`` or the hosted job launches with
        # neither Ollama endpoint and OpenCode falls back to the default daemon
        # instead of the profile-owned one.
        assert ("OLLAMA_HOST", "http://ollama.profile:11434") in request.profile_env

    @pytest.mark.unit
    async def test_hosted_passthrough_keeps_worker_resolved_defaulted_region(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A defaulted region with a worker override stays in hosted passthrough.

        Regression for PR #751 thread PRRT_kwDOSJAM6s6PVH0t: when the profile
        declares ``AWS_REGION: ${AWS_REGION:-us-west-2}`` and the worker env has
        ``AWS_REGION`` set, the local Compose container receives the worker
        value at stack launch. The hosted path resolves ``profile_env`` against
        the worker env and skips the worker-resolved value (carrying it would
        embed a secret), so ``AWS_REGION`` must stay in ``env_passthrough_names``
        for the hosted executor to resolve out-of-band — otherwise the hosted job
        launches with neither the worker override nor the profile default.
        """
        compose_file = _write_compose(
            tmp_path, environment={"AWS_REGION": "${AWS_REGION:-us-west-2}"}
        )
        monkeypatch.setenv("AWS_REGION", "eu-central-1")
        adapter = _build(ClaudeCodeAdapter)
        request = await _run(adapter, compose_file=compose_file)
        # Worker-set defaulted form stays in passthrough for out-of-band resolution.
        assert "AWS_REGION" in request.env_passthrough_names
        # The worker value is NOT carried in profile_env (no-secret-values contract).
        assert "AWS_REGION" not in dict(request.profile_env)
        # No ${...} placeholder leaks into profile_env.
        assert not any("${" in value for _key, value in request.profile_env)

    @pytest.mark.unit
    async def test_hosted_offloads_blocking_compose_parse_to_worker_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hosted path must not block the event loop on compose-file I/O.

        Regression for PR #751 thread PRRT_kwDOSJAM6s6PWZD1: the Compose path
        wraps ``agent_exec_env_passthrough`` (synchronous compose read/parse)
        in ``asyncio.to_thread`` so concurrent agent runs do not stall the event
        loop. The hosted path's ``filter_hosted_env_passthrough_names`` and
        ``literal_profile_env_from_compose`` perform the same synchronous
        read/YAML-parse and must be offloaded the same way, or concurrent
        hosted runs serialize on blocking I/O. This test asserts both calls run
        off the event loop thread.
        """
        import awf.adapters.base as base_module

        loop_thread_id = threading.get_ident()

        seen_thread_ids: dict[str, int] = {}

        real_filter = base_module.filter_hosted_env_passthrough_names
        real_literal = base_module.literal_profile_env_from_compose

        def _tracked_filter(names, *, compose_file):
            seen_thread_ids["filter"] = threading.get_ident()
            return real_filter(names, compose_file=compose_file)

        def _tracked_literal(compose_file):
            seen_thread_ids["literal"] = threading.get_ident()
            return real_literal(compose_file)

        monkeypatch.setattr(base_module, "filter_hosted_env_passthrough_names", _tracked_filter)
        monkeypatch.setattr(base_module, "literal_profile_env_from_compose", _tracked_literal)

        compose_file = _write_compose(
            tmp_path, environment={"OLLAMA_HOST": "http://ollama.profile:11434"}
        )
        adapter = _build(ClaudeCodeAdapter)
        request = await _run(adapter, compose_file=compose_file)

        # Both blocking compose-file parses ran, and neither ran on the event
        # loop's thread (asyncio.to_thread dispatches to a worker thread).
        assert "filter" in seen_thread_ids, "hosted filter parse was not dispatched off-loop"
        assert "literal" in seen_thread_ids, "hosted literal parse was not dispatched off-loop"
        assert seen_thread_ids["filter"] != loop_thread_id
        assert seen_thread_ids["literal"] != loop_thread_id
        # The offloaded parse result still flows through to the hosted request.
        assert ("OLLAMA_HOST", "http://ollama.profile:11434") in request.profile_env

    @pytest.mark.unit
    async def test_hosted_request_surfaces_github_token_aliases(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hosted request carries GitHub token alias names so hosted ``gh`` works.

        Regression for PR #751 thread PRRT_kwDOSJAM6s6PXFPz: when a workspace is
        launched with ``AWF_GITHUB_TOKEN`` in the worker env, the local Compose
        path injects ``GH_TOKEN: ${AWF_GITHUB_TOKEN}`` and
        ``GITHUB_TOKEN: ${AWF_GITHUB_TOKEN}`` into the agent env block so the
        local agent container can run ``gh``. The hosted (non-compose) path has
        no compose env block substitution, so without surfacing these alias
        names the hosted executor cannot resolve the credential and the hosted
        monitor-repair agent loses GitHub CLI access even though the same
        workspace has it under Compose.

        The hosted request's ``env_passthrough_names`` must include both
        aliases so the hosted executor can resolve them out-of-band. Names
        only — secret values are never transported, and the placeholder value
        never appears in the request. ``profile_env`` must NOT carry the
        worker-resolved slot (no-secret-values contract unchanged).
        """
        # Compose env block as the local Compose path would render it: both
        # GitHub token aliases as bare ``${AWF_GITHUB_TOKEN}`` placeholders.
        compose_file = _write_compose(
            tmp_path,
            environment={
                "GH_TOKEN": "${AWF_GITHUB_TOKEN}",
                "GITHUB_TOKEN": "${AWF_GITHUB_TOKEN}",
                "OLLAMA_HOST": "http://ollama.profile:11434",
            },
        )
        monkeypatch.setenv("AWF_GITHUB_TOKEN", "ghp_worker_secret")
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        adapter = _build(OpenCodeAdapter)
        request = await _run(adapter, compose_file=compose_file)

        # Both GitHub token aliases are surfaced for hosted out-of-band
        # resolution so the hosted executor can inject the worker token.
        assert "GH_TOKEN" in request.env_passthrough_names
        assert "GITHUB_TOKEN" in request.env_passthrough_names
        # No secret value reaches the request: names only carry no value, and
        # profile_env never carries the worker-resolved slot.
        blob = (
            request.prompt_stdin.decode("utf-8", "replace")
            + "\x00".join(request.cli_args)
            + "\x00".join(request.env_passthrough_names)
            + "\x00".join(f"{k}={v}" for k, v in request.profile_env)
        )
        assert "ghp_worker_secret" not in blob
        assert "${AWF_GITHUB_TOKEN}" not in blob
        assert "GH_TOKEN" not in dict(request.profile_env)
        assert "GITHUB_TOKEN" not in dict(request.profile_env)
        # The non-GitHub profile-owned literal still carries via profile_env
        # (the GitHub token carry does not regress profile_env behaviour).
        assert ("OLLAMA_HOST", "http://ollama.profile:11434") in request.profile_env

    @pytest.mark.unit
    async def test_hosted_request_skips_worker_github_token_when_profile_owns_alias(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A profile-owned GitHub token alias is not shadowed on the hosted path.

        Mirrors the local Compose path's group-precedence rule: when the profile
        owns ``GITHUB_TOKEN`` (e.g. via a secret lease rendering
        ``GITHUB_TOKEN: ${MY_PROFILE_LEASE_TOKEN}``), the local path does NOT
        inject the worker ``GH_TOKEN`` (higher precedence) or ``gh`` would use
        the worker credential instead of the profile-owned token. The hosted
        path must apply the same rule: the higher-precedence worker ``GH_TOKEN``
        is NOT surfaced, and the profile-owned ``GITHUB_TOKEN`` is not
        re-resolved from the worker either (it is compose-declared, so the
        hosted filter excludes it; the GitHub token helper skips it because it
        is profile-owned). Result: neither worker alias shadows the
        profile-owned token.
        """
        compose_file = _write_compose(
            tmp_path,
            environment={
                # Profile owns the lower-precedence alias via a secret lease.
                "GITHUB_TOKEN": "${MY_PROFILE_LEASE_TOKEN}",
                "OLLAMA_HOST": "http://ollama.profile:11434",
            },
        )
        monkeypatch.setenv("AWF_GITHUB_TOKEN", "ghp_worker_secret")
        monkeypatch.setenv("GH_TOKEN", "ghp_worker_gh_token_secret")
        adapter = _build(OpenCodeAdapter)
        request = await _run(adapter, compose_file=compose_file)

        # The higher-precedence worker GH_TOKEN is NOT surfaced (would shadow
        # the profile-owned GITHUB_TOKEN). The profile-owned GITHUB_TOKEN is
        # not re-resolved from the worker either (compose-declared).
        assert "GH_TOKEN" not in request.env_passthrough_names
        assert "GITHUB_TOKEN" not in request.env_passthrough_names
        # No worker GitHub token value reaches the request.
        blob = (
            request.prompt_stdin.decode("utf-8", "replace")
            + "\x00".join(request.cli_args)
            + "\x00".join(request.env_passthrough_names)
            + "\x00".join(f"{k}={v}" for k, v in request.profile_env)
        )
        assert "ghp_worker_secret" not in blob
        assert "ghp_worker_gh_token_secret" not in blob
