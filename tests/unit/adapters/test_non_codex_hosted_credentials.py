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
# the hosted contract cannot drift from the shared source of truth. Backend
# credentials/config (AWS_*, ANTHROPIC_VERTEX_PROJECT_ID, CLOUD_ML_REGION,
# GOOGLE_APPLICATION_CREDENTIALS) are NOT in AGENT_AUTH_ENV_VARS, so they must
# not be advertised as ambient hosted passthrough by default. Profile-declared
# same-name slots are covered separately below.
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
)
# ``GOOGLE_APPLICATION_CREDENTIALS`` is intentionally NOT surfaced here — it is a
# file-backed credential (its value is a filesystem path), and the hosted
# request (``AgentRuntimeExecRequest``) carries no file/secret ref or mount. The
# local Compose path bind-mounts the referenced file via ``_build_host_auth_mounts``
# so ADC works locally, but env-only passthrough on the hosted path would inject a
# dangling path and silently break Vertex/ADC auth (PR #751 thread
# PRRT_kwDOSJAM6s6Pas4k). A future file/secret-ref mechanism on the hosted
# request is required to support it; until then it is not advertised as env-only.
_CLAUDE_NAMES = _CLAUDE_CODE_DERIVED_AUTH_NAMES
_CURSOR_NAMES = ("CURSOR_API_KEY",)
_GEMINI_NAMES = (
    "GEMINI_API_KEY",
    "GEMINI_API_KEY_AUTH_MECHANISM",
    "GOOGLE_API_KEY",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_GENAI_USE_GCA",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    # ``GOOGLE_APPLICATION_CREDENTIALS`` intentionally omitted — see
    # ``_CLAUDE_CODE_BACKEND_AUTH_NAMES`` note above (file-backed credential;
    # env-only passthrough injects a dangling path on the hosted path).
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
        for name in _CLAUDE_CODE_BACKEND_AUTH_NAMES:
            assert name not in request.env_passthrough_names, name
        # ``GOOGLE_APPLICATION_CREDENTIALS`` is file-backed and must NOT be
        # advertised as env-only passthrough — see
        # ``test_gemini_does_not_advertise_file_backed_google_application_credentials``
        # (PR #751 thread PRRT_kwDOSJAM6s6Pas4k). The local Compose path
        # bind-mounts the file; the hosted request has no file/secret ref, so
        # surfacing it would inject a dangling path and break Vertex/ADC auth.
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in request.env_passthrough_names

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
    async def test_gemini_does_not_advertise_file_backed_google_application_credentials(
        self,
    ) -> None:
        """``GOOGLE_APPLICATION_CREDENTIALS`` is file-backed and must not be env-only passthrough.

        Regression for PR #751 thread PRRT_kwDOSJAM6s6Pas4k: the local Compose
        path bind-mounts the referenced credentials file into the agent
        container via ``_build_host_auth_mounts`` so the path the env var points
        at actually exists and ADC/Vertex auth works. The hosted (non-compose)
        path resolves env-passthrough names to env *values* out-of-band and
        injects them as env vars, but ``AgentRuntimeExecRequest`` carries no
        file/secret ref or mount, so the hosted executor has no signal that this
        name is file-backed and must also mount the file. Surfacing it would
        inject a dangling ``GOOGLE_APPLICATION_CREDENTIALS=/some/path`` with the
        file absent, silently breaking ADC/Vertex auth even though the same
        workspace is ready under Compose. The name must NOT be advertised as
        env-only passthrough until a file/secret-ref mechanism exists on the
        hosted request; the value/config names (API keys, Vertex toggles,
        project/location, access token) are still surfaced because they are not
        file paths.
        """
        adapter = _build(GeminiAdapter)
        request = await _run(adapter)
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in request.env_passthrough_names
        # The value/config names that ARE env-safe remain surfaced.
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
    async def test_hosted_request_surfaces_profile_env_secret_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same-name profile env secrets stay resolvable on the hosted path.

        Regression for PR #754 thread PRRT_kwDOSJAM6s6PswPc: when a profile
        declares an arbitrary project secret such as ``NPM_TOKEN: ${NPM_TOKEN}``,
        local Compose substitutes the worker value into the agent container at
        stack launch. The hosted path skips that worker-resolved value from
        ``profile_env`` for secret safety, so it must still include the name in
        ``env_passthrough_names`` even though no adapter advertises ``NPM_TOKEN``.
        """
        compose_file = _write_compose(
            tmp_path,
            environment={
                "NPM_TOKEN": "${NPM_TOKEN}",
                "OLLAMA_HOST": "http://ollama.profile:11434",
            },
        )
        monkeypatch.setenv("NPM_TOKEN", "npm_worker_secret")
        adapter = _build(OpenCodeAdapter)
        request = await _run(adapter, compose_file=compose_file)

        assert "NPM_TOKEN" in request.env_passthrough_names
        assert "NPM_TOKEN" not in dict(request.profile_env)
        blob = (
            request.prompt_stdin.decode("utf-8", "replace")
            + "\x00".join(request.cli_args)
            + "\x00".join(request.env_passthrough_names)
            + "\x00".join(f"{k}={v}" for k, v in request.profile_env)
        )
        assert "npm_worker_secret" not in blob
        assert "${NPM_TOKEN}" not in blob
        assert not any("=" in name for name in request.env_passthrough_names)
        assert ("OLLAMA_HOST", "http://ollama.profile:11434") in request.profile_env

    @pytest.mark.unit
    async def test_hosted_profile_passthrough_does_not_reintroduce_file_backed_adc(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Profile passthrough must not bypass adapter ADC file-backed exclusions.

        Regression for PR #754 thread PRRT_kwDOSJAM6s6PuWqQ: Compose generation
        can add ``GOOGLE_APPLICATION_CREDENTIALS: ${GOOGLE_APPLICATION_CREDENTIALS}``
        from ``AGENT_AUTH_ENV_VARS`` when the worker has file-backed ADC. The
        Gemini and Claude adapters intentionally omit that name from hosted
        env-only passthrough because the hosted request carries no file/mount
        contract. The generic profile passthrough union must not add it back.
        """
        compose_file = _write_compose(
            tmp_path,
            environment={
                "GOOGLE_APPLICATION_CREDENTIALS": "${GOOGLE_APPLICATION_CREDENTIALS}",
                "NPM_TOKEN": "${NPM_TOKEN}",
            },
        )
        monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/host/adc.json")
        monkeypatch.setenv("NPM_TOKEN", "npm_worker_secret")

        for adapter_cls in (ClaudeCodeAdapter, GeminiAdapter):
            adapter = _build(adapter_cls)
            request = await _run(adapter, compose_file=compose_file)

            assert "GOOGLE_APPLICATION_CREDENTIALS" not in request.env_passthrough_names
            assert "GOOGLE_APPLICATION_CREDENTIALS" not in dict(request.profile_env)
            assert "NPM_TOKEN" in request.env_passthrough_names
            assert "NPM_TOKEN" not in dict(request.profile_env)

    @pytest.mark.unit
    async def test_hosted_request_carries_cross_name_env_secret_aliases(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cross-name env secret leases keep source-to-target metadata for hosted runs.

        Local Compose resolves ``ANTHROPIC_API_KEY: ${MY_ANTHROPIC_TOKEN}`` at
        stack launch, so the agent container receives ``ANTHROPIC_API_KEY`` even
        though the worker secret source is named ``MY_ANTHROPIC_TOKEN``. Hosted
        execution cannot recover that from target-name passthrough, and
        ``profile_env`` must not carry the secret value, so the request exposes
        a names-only alias for the hosted executor to resolve out-of-band.
        """
        compose_file = _write_compose(
            tmp_path,
            environment={
                "ANTHROPIC_API_KEY": "${MY_ANTHROPIC_TOKEN}",
                "NPM_TOKEN": "${NPM_TOKEN}",
            },
        )
        monkeypatch.setenv("MY_ANTHROPIC_TOKEN", _SECRET_VALUE)
        monkeypatch.setenv("NPM_TOKEN", "npm_worker_secret")
        adapter = _build(ClaudeCodeAdapter)
        request = await _run(adapter, compose_file=compose_file)

        assert "ANTHROPIC_API_KEY" not in request.env_passthrough_names
        assert "NPM_TOKEN" in request.env_passthrough_names
        assert request.env_passthrough_aliases == (("ANTHROPIC_API_KEY", "MY_ANTHROPIC_TOKEN"),)
        assert "ANTHROPIC_API_KEY" not in dict(request.profile_env)
        blob = (
            request.prompt_stdin.decode("utf-8", "replace")
            + "\x00".join(request.cli_args)
            + "\x00".join(request.env_passthrough_names)
            + "\x00".join(
                f"{target}={source}" for target, source in request.env_passthrough_aliases
            )
            + "\x00".join(f"{key}={value}" for key, value in request.profile_env)
        )
        assert _SECRET_VALUE not in blob
        assert "npm_worker_secret" not in blob
        assert "${MY_ANTHROPIC_TOKEN}" not in blob
        assert not any("=" in name for name in request.env_passthrough_names)

    @pytest.mark.unit
    async def test_hosted_passthrough_suppresses_profile_owned_auth_slot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare ``${OPENAI_API_KEY}`` worker-resolved slot stays in hosted passthrough.

        The local ``docker compose exec`` path skips compose-declared auth keys
        from exec-time ``-e`` passthrough because the *running container already
        has them* — Docker Compose substituted ``${OPENAI_API_KEY}`` from the
        worker shell at stack launch. The hosted (non-compose) path has no
        compose env block, so it must surface the same name in
        ``env_passthrough_names`` for the hosted executor to resolve the worker
        credential out-of-band, mirroring the local Compose container. A profile-
        owned auth *literal* (e.g. ``OPENAI_API_KEY: sk-profile``) is a separate
        case: it is redacted from ``profile_env`` and excluded from passthrough
        (the hosted executor resolves auth out-of-band via the adapter contract,
        not by re-resolving a profile-owned secret literal).

        Regression for PR #751 thread PRRT_kwDOSJAM6s6Pi7sN: previously a bare
        ``${OPENAI_API_KEY}`` slot was excluded from passthrough AND skipped from
        ``profile_env``, so the hosted job lost the worker credential the local
        Compose container received at stack launch.
        """
        compose_file = _write_compose(tmp_path, environment={"OPENAI_API_KEY": "${OPENAI_API_KEY}"})
        adapter = _build(OpenCodeAdapter)
        # Pin the worker env so the bare-slot result is deterministic regardless
        # of the ambient test/CI environment: with the variable set the bare
        # slot stays in passthrough; with it unset it is excluded.
        monkeypatch.setenv("OPENAI_API_KEY", _SECRET_VALUE)
        request = await _run(adapter, compose_file=compose_file)
        # Bare ${OPENAI_API_KEY} with the variable worker-set -> stays in
        # passthrough for hosted out-of-band resolution (the local Compose
        # container received the worker value at stack launch).
        assert "OPENAI_API_KEY" in request.env_passthrough_names
        # The worker secret value is never transported (names only).
        assert _SECRET_VALUE not in "".join(f"{k}={v}" for k, v in request.profile_env)
        # Non-profile-owned provider key names still pass through.
        assert "ANTHROPIC_API_KEY" in request.env_passthrough_names

        # With the variable UNSET the bare slot stays excluded (no worker value
        # to resolve out-of-band; Compose substitutes "" for an unset bare
        # reference). A fresh adapter/executor captures the unset call alone.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        adapter_unset = _build(OpenCodeAdapter)
        request_unset = await _run(adapter_unset, compose_file=compose_file)
        assert "OPENAI_API_KEY" not in request_unset.env_passthrough_names

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
    async def test_claude_code_profile_declared_bedrock_slots_are_resolvable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Profile-declared Bedrock slots preserve local Compose parity.

        Regression for PR #754 thread PRRT_kwDOSJAM6s6P8RKB: Claude backend
        credentials must not be surfaced from ambient hosted-worker env by
        default, but a profile that explicitly declares same-name Bedrock slots
        still makes the local Compose container receive those values at stack
        launch. Hosted runs must carry the names for out-of-band resolution
        without transporting the secret values in the request payload.
        """
        compose_file = _write_compose(
            tmp_path,
            environment={
                "CLAUDE_CODE_USE_BEDROCK": "${CLAUDE_CODE_USE_BEDROCK}",
                "AWS_REGION": "${AWS_REGION}",
                "AWS_ACCESS_KEY_ID": "${AWS_ACCESS_KEY_ID}",
                "AWS_SECRET_ACCESS_KEY": "${AWS_SECRET_ACCESS_KEY}",
                "AWS_SESSION_TOKEN": "${AWS_SESSION_TOKEN}",
                "AWS_BEARER_TOKEN_BEDROCK": "${AWS_BEARER_TOKEN_BEDROCK}",
            },
        )
        monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA_TEST_IDENTIFIER")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws_secret_do_not_leak")
        monkeypatch.setenv("AWS_SESSION_TOKEN", "aws_session_do_not_leak")
        monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock_bearer_do_not_leak")

        adapter = _build(ClaudeCodeAdapter)
        request = await _run(adapter, compose_file=compose_file)

        for name in (
            "CLAUDE_CODE_USE_BEDROCK",
            "AWS_REGION",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_BEARER_TOKEN_BEDROCK",
        ):
            assert name in request.env_passthrough_names, name
            assert name not in dict(request.profile_env)
        blob = (
            request.prompt_stdin.decode("utf-8", "replace")
            + "\x00".join(request.cli_args)
            + "\x00".join(request.env_passthrough_names)
            + "\x00".join(f"{key}={value}" for key, value in request.profile_env)
        )
        assert "aws_secret_do_not_leak" not in blob
        assert "aws_session_do_not_leak" not in blob
        assert "bedrock_bearer_do_not_leak" not in blob

    @pytest.mark.unit
    async def test_hosted_offloads_blocking_compose_parse_to_worker_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hosted path must not block the event loop on compose-file I/O.

        Regression for PR #751 thread PRRT_kwDOSJAM6s6PWZD1: the Compose path
        wraps ``agent_exec_env_passthrough`` (synchronous compose read/parse)
        in ``asyncio.to_thread`` so concurrent agent runs do not stall the event
        loop. The hosted path's compose-env helpers perform the same
        synchronous read/YAML-parse and must be offloaded the same way, or
        concurrent hosted runs serialize on blocking I/O. This test asserts
        those calls run off the event loop thread.
        """
        import awf.adapters.base as base_module
        import awf.profiles.compose as compose_module

        loop_thread_id = threading.get_ident()

        seen_thread_ids: dict[str, int] = {}
        parse_calls = 0

        real_parse = base_module.try_compose_agent_env_and_postgres_passwords
        real_filter = base_module.filter_hosted_env_passthrough_names
        real_profile_names = base_module.hosted_profile_env_passthrough_names
        real_aliases = base_module.hosted_profile_env_passthrough_aliases
        real_github_names = base_module.hosted_github_token_passthrough_names
        real_literal = base_module.literal_profile_env_from_compose

        def _tracked_parse(compose_file, *, worker_env):
            nonlocal parse_calls
            seen_thread_ids["parse"] = threading.get_ident()
            parse_calls += 1
            return real_parse(compose_file, worker_env=worker_env)

        def _tracked_filter(names, *, compose_file, compose_env):
            seen_thread_ids["filter"] = threading.get_ident()
            return real_filter(names, compose_file=compose_file, compose_env=compose_env)

        def _tracked_profile_names(compose_file, *, compose_env):
            seen_thread_ids["profile_names"] = threading.get_ident()
            return real_profile_names(compose_file, compose_env=compose_env)

        def _tracked_aliases(compose_file, *, compose_env):
            seen_thread_ids["aliases"] = threading.get_ident()
            return real_aliases(compose_file, compose_env=compose_env)

        def _tracked_github_names(compose_file, *, compose_env):
            seen_thread_ids["github_names"] = threading.get_ident()
            return real_github_names(compose_file, compose_env=compose_env)

        def _tracked_literal(compose_file, *, compose_env, postgres_passwords):
            seen_thread_ids["literal"] = threading.get_ident()
            return real_literal(
                compose_file,
                compose_env=compose_env,
                postgres_passwords=postgres_passwords,
            )

        def _unexpected_helper_parse(compose_file):
            raise AssertionError(f"helper re-parsed compose file: {compose_file}")

        monkeypatch.setattr(
            base_module,
            "try_compose_agent_env_and_postgres_passwords",
            _tracked_parse,
        )
        monkeypatch.setattr(
            compose_module,
            "_try_agent_environment_from_compose_file",
            _unexpected_helper_parse,
        )
        monkeypatch.setattr(base_module, "filter_hosted_env_passthrough_names", _tracked_filter)
        monkeypatch.setattr(
            base_module,
            "hosted_profile_env_passthrough_names",
            _tracked_profile_names,
        )
        monkeypatch.setattr(
            base_module,
            "hosted_profile_env_passthrough_aliases",
            _tracked_aliases,
        )
        monkeypatch.setattr(
            base_module,
            "hosted_github_token_passthrough_names",
            _tracked_github_names,
        )
        monkeypatch.setattr(base_module, "literal_profile_env_from_compose", _tracked_literal)

        compose_file = _write_compose(
            tmp_path, environment={"OLLAMA_HOST": "http://ollama.profile:11434"}
        )
        adapter = _build(ClaudeCodeAdapter)
        request = await _run(adapter, compose_file=compose_file)

        # The single blocking compose-file parse ran off the event loop, and
        # the helper calls reused that parse instead of re-reading the file.
        assert parse_calls == 1
        assert "parse" in seen_thread_ids, "hosted compose parse was not dispatched off-loop"
        assert "filter" in seen_thread_ids, "hosted filter parse was not dispatched off-loop"
        assert "profile_names" in seen_thread_ids, (
            "hosted profile parse was not dispatched off-loop"
        )
        assert "aliases" in seen_thread_ids, "hosted alias parse was not dispatched off-loop"
        assert "github_names" in seen_thread_ids, (
            "hosted GitHub token parse was not dispatched off-loop"
        )
        assert "literal" in seen_thread_ids, "hosted literal parse was not dispatched off-loop"
        assert seen_thread_ids["parse"] != loop_thread_id
        assert seen_thread_ids["filter"] != loop_thread_id
        assert seen_thread_ids["profile_names"] != loop_thread_id
        assert seen_thread_ids["aliases"] != loop_thread_id
        assert seen_thread_ids["github_names"] != loop_thread_id
        assert seen_thread_ids["literal"] != loop_thread_id
        # The offloaded parse result still flows through to the hosted request.
        assert ("OLLAMA_HOST", "http://ollama.profile:11434") in request.profile_env

    @pytest.mark.unit
    async def test_hosted_request_maps_github_token_aliases_from_awf_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hosted request maps GitHub token aliases so hosted ``gh`` works.

        Regression for PR #751 thread PRRT_kwDOSJAM6s6PXFPz: when a workspace is
        launched with ``AWF_GITHUB_TOKEN`` in the worker env, the local Compose
        path injects ``GH_TOKEN: ${AWF_GITHUB_TOKEN}`` and
        ``GITHUB_TOKEN: ${AWF_GITHUB_TOKEN}`` into the agent env block so the
        local agent container can run ``gh``. The hosted (non-compose) path has
        no compose env block substitution, so the hosted request must carry
        explicit alias mappings from the worker-visible source to the gh-visible
        targets.

        The hosted request's ``env_passthrough_aliases`` must include both
        aliases so the hosted executor resolves ``AWF_GITHUB_TOKEN`` out-of-band
        and injects it as ``GH_TOKEN`` / ``GITHUB_TOKEN``. Names and aliases
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

        # The documented worker source is carried only as the source of
        # gh-visible aliases instead of being emitted as a plain passthrough
        # name that local Compose does not expose.
        assert "AWF_GITHUB_TOKEN" not in request.env_passthrough_names
        assert "GH_TOKEN" not in request.env_passthrough_names
        assert "GITHUB_TOKEN" not in request.env_passthrough_names
        assert ("GH_TOKEN", "AWF_GITHUB_TOKEN") in request.env_passthrough_aliases
        assert ("GITHUB_TOKEN", "AWF_GITHUB_TOKEN") in request.env_passthrough_aliases
        # No secret value reaches the request: names only carry no value, and
        # profile_env never carries the worker-resolved slot.
        blob = (
            request.prompt_stdin.decode("utf-8", "replace")
            + "\x00".join(request.cli_args)
            + "\x00".join(request.env_passthrough_names)
            + "\x00".join(
                f"{target}={source}" for target, source in request.env_passthrough_aliases
            )
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
    async def test_hosted_request_surfaces_github_token_source_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hosted request carries the GitHub token SOURCE name so hosted ``gh`` works.

        Regression for PR #751 thread PRRT_kwDOSJAM6s6PYNGv: when the worker only
        has ``AWF_GITHUB_TOKEN`` set (the documented service token), the local
        Compose path injects ``GH_TOKEN: ${AWF_GITHUB_TOKEN}`` /
        ``GITHUB_TOKEN: ${AWF_GITHUB_TOKEN}`` and Docker substitutes the
        placeholder at stack launch, so the local agent container can run ``gh``.
        The hosted (non-compose) path resolves ``env_passthrough_names`` by name
        out-of-band, so resolving ``GH_TOKEN`` / ``GITHUB_TOKEN`` finds nothing in
        that setup and the hosted monitor-repair agent loses GitHub CLI access.

        The hosted request's ``env_passthrough_aliases`` must therefore map
        ``GH_TOKEN`` / ``GITHUB_TOKEN`` from ``AWF_GITHUB_TOKEN`` so the hosted
        executor can resolve the credential from the source name and inject the
        gh-visible aliases. Names and aliases only — secret values are never
        transported, and the placeholder value never appears in the request.
        """
        compose_file = _write_compose(
            tmp_path,
            environment={
                "GH_TOKEN": "${AWF_GITHUB_TOKEN}",
                "GITHUB_TOKEN": "${AWF_GITHUB_TOKEN}",
                "OLLAMA_HOST": "http://ollama.profile:11434",
            },
        )
        # The common setup: only the documented AWF source token is set; the
        # gh-visible aliases are absent from the worker env.
        monkeypatch.setenv("AWF_GITHUB_TOKEN", "ghp_worker_secret")
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        adapter = _build(OpenCodeAdapter)
        request = await _run(adapter, compose_file=compose_file)

        # The chosen source name is carried only as the source of gh-visible
        # aliases. Plain passthrough names resolve by their own name, so adding
        # ``AWF_GITHUB_TOKEN`` there would expose an extra env var that the
        # local Compose path does not expose.
        assert "AWF_GITHUB_TOKEN" not in request.env_passthrough_names
        assert "GH_TOKEN" not in request.env_passthrough_names
        assert "GITHUB_TOKEN" not in request.env_passthrough_names
        assert ("GH_TOKEN", "AWF_GITHUB_TOKEN") in request.env_passthrough_aliases
        assert ("GITHUB_TOKEN", "AWF_GITHUB_TOKEN") in request.env_passthrough_aliases
        # No secret value reaches the request: names only carry no value, and
        # profile_env never carries the worker-resolved slot.
        blob = (
            request.prompt_stdin.decode("utf-8", "replace")
            + "\x00".join(request.cli_args)
            + "\x00".join(request.env_passthrough_names)
            + "\x00".join(
                f"{target}={source}" for target, source in request.env_passthrough_aliases
            )
            + "\x00".join(f"{k}={v}" for k, v in request.profile_env)
        )
        assert "ghp_worker_secret" not in blob
        assert "${AWF_GITHUB_TOKEN}" not in blob
        assert "AWF_GITHUB_TOKEN" not in dict(request.profile_env)
        assert "GH_TOKEN" not in dict(request.profile_env)
        assert "GITHUB_TOKEN" not in dict(request.profile_env)
        # The non-GitHub profile-owned literal still carries via profile_env.
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

    @pytest.mark.unit
    async def test_hosted_request_skips_worker_github_token_when_profile_owns_awf_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A profile-owned AWF source token is not shadowed on the hosted path."""
        compose_file = _write_compose(
            tmp_path,
            environment={
                "AWF_GITHUB_TOKEN": "ghp_profile_token_secret",
                "OLLAMA_HOST": "http://ollama.profile:11434",
            },
        )
        monkeypatch.setenv("AWF_GITHUB_TOKEN", "ghp_worker_secret")
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        adapter = _build(OpenCodeAdapter)
        request = await _run(adapter, compose_file=compose_file)

        assert "AWF_GITHUB_TOKEN" not in request.env_passthrough_names
        assert "AWF_GITHUB_TOKEN" not in dict(request.profile_env)
        blob = (
            request.prompt_stdin.decode("utf-8", "replace")
            + "\x00".join(request.cli_args)
            + "\x00".join(request.env_passthrough_names)
            + "\x00".join(f"{k}={v}" for k, v in request.profile_env)
        )
        assert "ghp_worker_secret" not in blob
        assert "ghp_profile_token_secret" not in blob
        assert ("OLLAMA_HOST", "http://ollama.profile:11434") in request.profile_env
