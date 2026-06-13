"""Provider credential readiness checks for local service mode."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import awf.service.provider_readiness as provider_readiness
from awf.service.config import ServiceSettings
from awf.service.provider_readiness import (
    ProviderReadinessError,
    collect_agent_readiness,
    selected_provider_readiness_preflight,
    validate_provider_names,
)


def _settings(
    tmp_path: Path,
    *,
    github_token: str | None = None,
    docker_host: str | None = None,
    host_home: str | None = None,
) -> ServiceSettings:
    return ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url="postgresql+asyncpg://awf:awf_dev@localhost:5433/awf",
        docker_host=f"unix://{tmp_path / 'docker.sock'}" if docker_host is None else docker_host,
        agent_runtime_image="awf-agent-runtime:latest",
        work_dir=str(tmp_path / "work"),
        api_token=None,
        github_token=github_token,
        worker_poll_interval_seconds=0.1,
        worker_max_concurrent_provisions=1,
        host_home=str(tmp_path / "home") if host_home is None else host_home,
    )


def _completed(*, returncode: int = 0, stdout: str = "", stderr: str = "") -> Any:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _unexpected_subprocess(args: list[str], **_kwargs: object) -> Any:
    raise AssertionError(f"unexpected subprocess call: {args}")


def _ollama_ok(url: str, *, timeout: float) -> Any:
    assert timeout > 0
    if url == "http://ollama.local:11434/api/version":
        return SimpleNamespace(status_code=200, text='{"version":"0.1.0"}')
    if url == "http://ollama.local:11434/api/tags":
        return SimpleNamespace(
            status_code=200,
            text='{"models":[{"name":"kimi-k2.6:cloud"}]}',
        )
    raise AssertionError(f"unexpected Ollama probe URL: {url}")


def _runtime_cli_ok(expected_executable: str) -> Any:
    def _run(args: list[str], **_kwargs: object) -> Any:
        assert args == [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "awf-agent-runtime:latest",
            "-lc",
            f"command -v {expected_executable}",
        ]
        return _completed(stdout=f"/usr/bin/{expected_executable}\n")

    return _run


@pytest.mark.unit
def test_provider_readiness_public_secret_env_key_classifier() -> None:
    """Classify public-looking keys separately from secret environment keys."""
    assert provider_readiness.is_secret_env_key("OPENAI_API_KEY")
    assert provider_readiness.is_secret_env_key("custom-token")
    assert provider_readiness.is_secret_env_key("PASSWORD")
    assert provider_readiness.is_secret_env_key("workspace_client_secret")
    assert provider_readiness.is_secret_env_key("PRIVATE_KEY")
    assert provider_readiness.is_secret_env_key("PRIVATEKEY")
    assert provider_readiness.is_secret_env_key("APIKEY")
    assert provider_readiness.is_secret_env_key("ACCESSKEY")
    assert provider_readiness.is_secret_env_key("SSH_PRIVATE_KEY")
    assert provider_readiness.is_secret_env_key("custom-private-key")
    assert not provider_readiness.is_secret_env_key("PUBLIC_URL")
    assert not provider_readiness.is_secret_env_key("TOKEN_BUCKET_SIZE")


@pytest.mark.unit
def test_provider_readiness_validates_aliases_and_rejects_unknown() -> None:
    assert validate_provider_names(
        ["claude", "cursor", "opencode", "codex", "grok", "docker", ""]
    ) == {
        "claude_code",
        "cursor",
        "opencode",
        "codex",
        "grok",
        "docker",
    }

    with pytest.raises(ProviderReadinessError, match="unknown provider"):
        validate_provider_names(["github", "bogus"])


@pytest.mark.unit
def test_provider_readiness_validates_codex_and_docker_providers(tmp_path: Path) -> None:
    assert validate_provider_names(["codex", "docker"]) == {"codex", "docker"}

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    assert set(payload["providers"]) == {
        "github",
        "codex",
        "claude_code",
        "cursor",
        "gemini",
        "opencode",
        "grok",
        "docker",
    }


@pytest.mark.unit
def test_selected_provider_preflight_blocks_missing_strict_auth(tmp_path: Path) -> None:
    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="codex",
        task_policy={},
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    assert result["provider"] == "codex"
    assert result["agent"] == "codex"
    assert result["model"] == "gpt-5.5"
    assert result["readiness_status"] == "blocked"
    assert result["auth_status"] == "fail"
    assert result["auth_source"] == "not_observed"
    assert result["probe_status"] == "skipped"
    assert result["reason_code"] == "CODEX_AUTH_MISSING"
    assert result["override_required"] is True
    assert result["override_used"] is False
    assert result["blocks_launch"] is True


@pytest.mark.unit
def test_selected_provider_preflight_checks_only_selected_provider(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)
    env = {
        "AWF_GITHUB_TOKEN": "ghp_unrelated_provider_secret",
        "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1",
        "OPENAI_API_KEY": "sk-proj-selected-codex-secret",
    }
    subprocess_calls: list[list[str]] = []
    http_urls: list[str] = []

    def _run(args: list[str], **_kwargs: object) -> Any:
        subprocess_calls.append(args)
        return _completed(stdout="logged in\n")

    def _http_get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        http_urls.append(url)
        return SimpleNamespace(status_code=200, text='{"version":"0.1.0"}')

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="codex",
        task_policy={},
        environ=env,
        run_subprocess=_run,
        http_get=_http_get,
    )

    assert result["provider"] == "codex"
    assert result["readiness_status"] == "ready"
    assert subprocess_calls == [
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "awf-agent-runtime:latest",
            "-lc",
            "command -v codex",
        ]
    ]
    assert http_urls == []


@pytest.mark.unit
def test_selected_provider_preflight_blocks_unsupported_runtime(tmp_path: Path) -> None:
    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="unknown-agent",
        task_policy=None,
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    assert result["provider"] == "unknown"
    assert result["agent"] == "unknown-agent"
    assert result["model"] is None
    assert result["model_source"] == "unavailable"
    assert result["readiness_status"] == "blocked"
    assert result["auth_status"] == "unknown"
    assert result["auth_source"] == "not_observed"
    assert result["probe_status"] == "skipped"
    assert result["reason_code"] == "UNSUPPORTED_AGENT_RUNTIME"
    assert result["blocks_launch"] is True
    assert provider_readiness.provider_readiness_preflight_from_task_policy(None) is None


@pytest.mark.unit
def test_selected_provider_preflight_override_preserves_original_reason(
    tmp_path: Path,
) -> None:
    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="codex",
        task_policy={},
        override=True,
        override_reason="operator verified temporary auth repair",
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    assert result["readiness_status"] == "admitted_with_override"
    assert result["reason_code"] == "CODEX_AUTH_MISSING"
    assert result["override_required"] is True
    assert result["override_used"] is True
    assert result["override_reason"] == "operator verified temporary auth repair"
    assert result["blocks_launch"] is False


@pytest.mark.unit
def test_selected_provider_preflight_maps_agents_to_effective_models(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text('{"token":"codex_file_secret"}')
    (home / ".claude").mkdir()
    (home / ".gemini").mkdir()
    (home / ".config" / "opencode").mkdir(parents=True)
    env = {
        "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1",
        "CURSOR_API_KEY": "cursor_secret",
        "XAI_API_KEY": "xai-selected-grok-secret",
    }
    probe_calls: list[list[str]] = []

    def _run(args: list[str], **_kwargs: object) -> Any:
        probe_calls.append(args)
        return _completed(stdout="authenticated\n")

    cases = [
        ("codex", "codex", "gpt-custom", "ok"),
        ("claude_code", "claude_code", "claude-opus-4-8", "ok"),
        ("cursor", "cursor", "sonnet-4-thinking", "ok"),
        ("gemini", "gemini", "gemini-3.1-pro-preview", "ok"),
        ("opencode", "opencode", "ollama/kimi-k2.6:cloud", "ok"),
        ("grok", "grok", "grok-build", "ok"),
    ]
    for agent, provider, expected_model, expected_probe_status in cases:
        task_policy = {"agent_model": expected_model} if agent == "codex" else {}
        result = selected_provider_readiness_preflight(
            _settings(tmp_path),
            agent=agent,
            task_policy=task_policy,
            environ=env,
            run_subprocess=_run,
            http_get=_ollama_ok,
        )

        assert result["provider"] == provider
        assert result["model"] == expected_model
        assert result["readiness_status"] == "ready"
        assert result["probe_status"] == expected_probe_status
        assert result["blocks_launch"] is False

    assert [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "sh",
        "awf-agent-runtime:latest",
        "-lc",
        "command -v codex",
    ] in probe_calls
    assert [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "sh",
        "awf-agent-runtime:latest",
        "-lc",
        "command -v claude",
    ] in probe_calls
    assert [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "sh",
        "awf-agent-runtime:latest",
        "-lc",
        "command -v cursor-agent",
    ] in probe_calls
    assert [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "sh",
        "awf-agent-runtime:latest",
        "-lc",
        "command -v gemini",
    ] in probe_calls
    assert [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "sh",
        "awf-agent-runtime:latest",
        "-lc",
        "command -v opencode",
    ] in probe_calls
    assert [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "sh",
        "awf-agent-runtime:latest",
        "-lc",
        "command -v grok",
    ] in probe_calls


@pytest.mark.unit
def test_selected_cursor_preflight_requires_env_key_and_runtime_cli(
    tmp_path: Path,
) -> None:
    """Cursor selected preflight requires both API-key auth and cursor-agent."""
    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="cursor",
        task_policy={},
        environ={"CURSOR_API_KEY": "cursor_secret"},
        run_subprocess=_runtime_cli_ok("cursor-agent"),
    )

    assert result["provider"] == "cursor"
    assert result["agent"] == "cursor"
    assert result["model"] == "sonnet-4-thinking"
    assert result["model_source"] == "default"
    assert result["readiness_status"] == "ready"
    assert result["auth_status"] == "ok"
    assert result["auth_source"] == "CURSOR_API_KEY"
    assert result["probe_status"] == "ok"
    assert result["reason_code"] == "PROVIDER_READY"
    assert result["blocks_launch"] is False
    serialized = json.dumps(result, sort_keys=True)
    assert "cursor_secret" not in serialized


@pytest.mark.unit
def test_selected_cursor_preflight_lower_effort_uses_implicit_runtime_model(
    tmp_path: Path,
) -> None:
    """Lower Cursor effort without a model reports Cursor's implicit runtime model."""
    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="cursor",
        task_policy={"agent_effort": "medium"},
        environ={"CURSOR_API_KEY": "cursor_secret"},
        run_subprocess=_runtime_cli_ok("cursor-agent"),
    )

    assert result["provider"] == "cursor"
    assert result["agent"] == "cursor"
    assert result["model"] is None
    assert result["model_source"] == "default"
    assert result["readiness_status"] == "ready"
    assert result["probe_status"] == "ok"
    assert result["reason_code"] == "PROVIDER_READY"
    assert result["blocks_launch"] is False


@pytest.mark.unit
def test_selected_cursor_preflight_blocks_missing_env_key(tmp_path: Path) -> None:
    """Cursor selected preflight blocks launch when API-key auth is absent."""
    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="cursor",
        task_policy={},
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    assert result["provider"] == "cursor"
    assert result["agent"] == "cursor"
    assert result["model"] == "sonnet-4-thinking"
    assert result["readiness_status"] == "blocked"
    assert result["auth_status"] == "fail"
    assert result["auth_source"] == "not_observed"
    assert result["probe_status"] == "skipped"
    assert result["reason_code"] == "CURSOR_AUTH_MISSING"
    assert result["blocks_launch"] is True


@pytest.mark.unit
def test_selected_cursor_preflight_blocks_missing_runtime_cli(tmp_path: Path) -> None:
    """Cursor selected preflight blocks launch when cursor-agent is missing."""
    secret = "cursor_missing_cli_secret"

    def _run(args: list[str], **kwargs: object) -> Any:
        """Simulate a missing cursor-agent executable."""
        assert args[-1] == "command -v cursor-agent"
        assert kwargs["env"]["CURSOR_API_KEY"] == secret
        return _completed(returncode=1, stderr=f"cursor-agent missing with {secret}")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="cursor",
        task_policy={},
        environ={"CURSOR_API_KEY": secret},
        run_subprocess=_run,
    )

    assert result["provider"] == "cursor"
    assert result["auth_status"] == "ok"
    assert result["probe_status"] == "fail"
    assert result["reason_code"] == "CURSOR_RUNTIME_CLI_NOT_FOUND"
    assert result["blocks_launch"] is True
    serialized = json.dumps(result, sort_keys=True)
    assert secret not in serialized
    assert "<redacted>" in serialized


@pytest.mark.unit
def test_provider_readiness_cursor_env_auth_requires_runtime_cli(tmp_path: Path) -> None:
    """Strict Cursor readiness reports a CLI probe failure after auth succeeds."""
    secret = "cursor_provider_readiness_secret"
    calls: list[list[str]] = []

    def _run(args: list[str], **kwargs: object) -> Any:
        """Record the Cursor runtime probe and return a missing CLI result."""
        calls.append(args)
        assert args[-1] == "command -v cursor-agent"
        assert kwargs["env"]["CURSOR_API_KEY"] == secret
        return _completed(returncode=1, stderr=f"missing cursor-agent for {secret}")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"CURSOR_API_KEY": secret},
        strict_providers=["cursor"],
        run_subprocess=_run,
    )

    cursor = payload["providers"]["cursor"]
    assert cursor["ok"] is False
    assert cursor["status"] == "fail"
    assert cursor["reason"] == "CURSOR_RUNTIME_CLI_NOT_FOUND"
    assert cursor["credential_scope"] == "static_env_token"
    assert cursor["isolation"] == "service_env"
    assert cursor["credential_sources"] == [
        {
            "type": "env",
            "signal": "CURSOR_API_KEY",
            "credential_scope": "static_env_token",
            "isolation": "service_env",
        }
    ]
    assert cursor["runtime_cli_probe"]["status"] == "fail"
    assert cursor["runtime_cli_probe"]["reason_code"] == "CURSOR_RUNTIME_CLI_NOT_FOUND"
    assert calls == [
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "awf-agent-runtime:latest",
            "-lc",
            "command -v cursor-agent",
        ]
    ]
    serialized = json.dumps(payload, sort_keys=True)
    assert secret not in serialized
    assert "<redacted>" in serialized


@pytest.mark.unit
def test_selected_opencode_preflight_cloud_model_absent_from_tags_is_non_blocking(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)
    urls: list[str] = []

    def _http_get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        urls.append(url)
        if url == "http://ollama.local:11434/api/version":
            return SimpleNamespace(status_code=200, text='{"version":"0.1.0"}')
        if url == "http://ollama.local:11434/api/tags":
            return SimpleNamespace(
                status_code=200,
                text='{"models":[{"name":"other-model:latest"}]}',
            )
        raise AssertionError(f"unexpected Ollama probe URL: {url}")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="opencode",
        task_policy={"agent_model": "ollama/kimi-k2.6:cloud"},
        environ={"AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1"},
        run_subprocess=_runtime_cli_ok("opencode"),
        http_get=_http_get,
    )

    assert result["provider"] == "opencode"
    assert result["model"] == "ollama/kimi-k2.6:cloud"
    assert result["auth_status"] == "ok"
    # A ``:cloud`` model is served remotely; it must not block launch even when
    # it is absent from local /api/tags (regression for the old wrong block).
    assert result["probe_status"] == "ok"
    assert result["reason_code"] == "PROVIDER_READY"
    assert result["blocks_launch"] is False
    # Preflight never pulls — only the cheap version + tags probes run.
    assert urls == [
        "http://ollama.local:11434/api/version",
        "http://ollama.local:11434/api/tags",
    ]


@pytest.mark.unit
def test_selected_opencode_preflight_absent_non_cloud_model_is_pull_pending(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)

    def _http_get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        if url == "http://ollama.local:11434/api/version":
            return SimpleNamespace(status_code=200, text='{"version":"0.1.0"}')
        if url == "http://ollama.local:11434/api/tags":
            return SimpleNamespace(
                status_code=200,
                text='{"models":[{"name":"other-model:latest"}]}',
            )
        raise AssertionError(f"unexpected Ollama probe URL: {url}")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="opencode",
        task_policy={"agent_model": "ollama/llama4:70b"},
        environ={"AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1"},
        run_subprocess=_runtime_cli_ok("opencode"),
        http_get=_http_get,
    )

    # Absent non-cloud model is pullable: non-blocking, but the disposition is
    # surfaced so the operator sees the pending pull.
    assert result["probe_status"] == "pending"
    assert result["reason_code"] == "OLLAMA_MODEL_PULL_PENDING"
    assert result["blocks_launch"] is False


@pytest.mark.unit
def test_selected_opencode_preflight_blocks_when_daemon_unreachable(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)

    def _http_get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        if url == "http://ollama.local:11434/api/version":
            return SimpleNamespace(status_code=200, text='{"version":"0.1.0"}')
        if url == "http://ollama.local:11434/api/tags":
            raise RuntimeError("connection refused")
        raise AssertionError(f"unexpected Ollama probe URL: {url}")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="opencode",
        task_policy={"agent_model": "ollama/llama4:70b"},
        environ={"AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1"},
        run_subprocess=_runtime_cli_ok("opencode"),
        http_get=_http_get,
    )

    assert result["probe_status"] == "fail"
    assert result["reason_code"] == "OLLAMA_MODEL_PROBE_FAILED"
    assert result["blocks_launch"] is True


@pytest.mark.unit
@pytest.mark.parametrize("model", ["openai/gpt-oss", "anthropic/claude-sonnet"])
def test_selected_opencode_preflight_non_ollama_provider_model_skips_ollama_check(
    tmp_path: Path,
    model: str,
) -> None:
    # No ~/.config/opencode, no ~/.ollama auth files, no OLLAMA_API_KEY: the
    # Ollama auth/host preflight would otherwise return OPENCODE_OLLAMA_AUTH_MISSING
    # and block create-time admission. A provider-qualified non-Ollama model is
    # served by the selected provider, so the Ollama preflight must be skipped
    # here too (mirroring the executor pre-agent skip) and never run a probe.
    def _no_http(url: str, *, timeout: float) -> Any:
        raise AssertionError(f"unexpected Ollama probe URL: {url}")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="opencode",
        task_policy={"agent_model": model},
        environ={},
        run_subprocess=_unexpected_subprocess,
        http_get=_no_http,
    )

    assert result["provider"] == "opencode"
    assert result["model"] == model
    assert result["reason_code"] == "OPENCODE_NON_OLLAMA_PROVIDER_SELECTED"
    assert result["probe_status"] == "unavailable"
    assert result["auth_status"] == "ok"
    assert result["override_required"] is False
    assert result["blocks_launch"] is False


@pytest.mark.unit
def test_selected_opencode_preflight_suppresses_recovered_tags_fallback_logs(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger=provider_readiness.__name__)
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)
    urls: list[str] = []

    def _http_get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        urls.append(url)
        if url == "http://host.docker.internal:11434/api/version":
            raise RuntimeError("version fallback recovered")
        if url == "http://localhost:11434/api/version":
            return SimpleNamespace(status_code=200, text='{"version":"0.1.0"}')
        if url == "http://host.docker.internal:11434/api/tags":
            raise RuntimeError("tags fallback recovered")
        if url == "http://localhost:11434/api/tags":
            return SimpleNamespace(
                status_code=200,
                text='{"models":[{"name":"kimi-k2.6:cloud"}]}',
            )
        raise AssertionError(f"unexpected Ollama probe URL: {url}")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="opencode",
        task_policy={},
        environ={},
        run_subprocess=_runtime_cli_ok("opencode"),
        http_get=_http_get,
    )

    assert result["readiness_status"] == "ready"
    assert result["reason_code"] == "PROVIDER_READY"
    assert result["probe_status"] == "ok"
    assert urls == [
        "http://host.docker.internal:11434/api/version",
        "http://localhost:11434/api/version",
        "http://host.docker.internal:11434/api/tags",
        "http://localhost:11434/api/tags",
    ]
    assert "provider_readiness.ollama_probe_exception" not in caplog.text
    assert "provider_readiness.ollama_model_probe_exception" not in caplog.text
    assert "Traceback" not in caplog.text
    assert "fallback recovered" not in caplog.text


@pytest.mark.unit
def test_selected_claude_preflight_requires_usable_non_secret_probe(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text('{"oauth":"claude_file_secret"}')
    token = "sk-ant-stale-oauth-secret"

    def _run(args: list[str], **kwargs: object) -> Any:
        assert args == [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "awf-agent-runtime:latest",
            "-lc",
            "command -v claude",
        ]
        assert kwargs["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == token
        return _completed(returncode=1, stderr=f"missing cli with token {token}")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="claude_code",
        task_policy={},
        environ={"CLAUDE_CODE_OAUTH_TOKEN": token},
        run_subprocess=_run,
    )

    assert result["auth_status"] == "ok"
    assert result["probe_status"] == "fail"
    assert result["reason_code"] == "CLAUDE_RUNTIME_CLI_NOT_FOUND"
    assert result["blocks_launch"] is True
    serialized = json.dumps(result, sort_keys=True)
    assert token not in serialized
    assert "claude_file_secret" not in serialized
    assert "<redacted>" in serialized


@pytest.mark.unit
def test_cli_auth_probe_failure_modes_are_structured_and_redacted() -> None:
    def _missing(_args: list[str], **_kwargs: object) -> Any:
        raise FileNotFoundError("missing-cli")

    def _timeout(args: list[str], **_kwargs: object) -> Any:
        raise subprocess.TimeoutExpired(args, timeout=0.1)

    def _unexpected(_args: list[str], **_kwargs: object) -> Any:
        raise RuntimeError("transport leaked sk-ant-probe-secret")

    missing = provider_readiness._probe_cli_auth_status(
        provider_label="Probe",
        args=["probe", "auth", "status"],
        failure_reason="PROBE_AUTH_FAILED",
        timeout_reason="PROBE_AUTH_TIMEOUT",
        missing_reason="PROBE_CLI_NOT_FOUND",
        error_reason="PROBE_AUTH_ERROR",
        environ={},
        run_subprocess=_missing,
        secrets=frozenset(),
    )
    timeout = provider_readiness._probe_cli_auth_status(
        provider_label="Probe",
        args=["probe", "auth", "status"],
        failure_reason="PROBE_AUTH_FAILED",
        timeout_reason="PROBE_AUTH_TIMEOUT",
        missing_reason="PROBE_CLI_NOT_FOUND",
        error_reason="PROBE_AUTH_ERROR",
        environ={},
        run_subprocess=_timeout,
        secrets=frozenset(),
    )
    unexpected = provider_readiness._probe_cli_auth_status(
        provider_label="Probe",
        args=["probe", "auth", "status"],
        failure_reason="PROBE_AUTH_FAILED",
        timeout_reason="PROBE_AUTH_TIMEOUT",
        missing_reason="PROBE_CLI_NOT_FOUND",
        error_reason="PROBE_AUTH_ERROR",
        environ={},
        run_subprocess=_unexpected,
        secrets=frozenset({"sk-ant-probe-secret"}),
    )

    assert missing["reason_code"] == "PROBE_CLI_NOT_FOUND"
    assert timeout["reason_code"] == "PROBE_AUTH_TIMEOUT"
    assert unexpected["reason_code"] == "PROBE_AUTH_ERROR"
    assert "sk-ant-probe-secret" not in json.dumps(unexpected, sort_keys=True)
    assert "<redacted>" in unexpected["detail"]


@pytest.mark.unit
def test_agent_runtime_cli_probe_failure_modes_are_structured_and_redacted(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger=provider_readiness.__name__)
    settings = _settings(tmp_path)

    def _missing(_args: list[str], **_kwargs: object) -> Any:
        raise FileNotFoundError("docker")

    def _timeout(args: list[str], **_kwargs: object) -> Any:
        raise subprocess.TimeoutExpired(args, timeout=0.1)

    def _unexpected(_args: list[str], **_kwargs: object) -> Any:
        raise RuntimeError("runtime probe leaked sk-proj-runtime-secret")

    missing = provider_readiness._probe_agent_runtime_cli(
        settings,
        executable="codex",
        provider="codex",
        environ={},
        run_subprocess=_missing,
        secrets=frozenset(),
    )
    timeout = provider_readiness._probe_agent_runtime_cli(
        settings,
        executable="claude",
        provider="claude_code",
        environ={},
        run_subprocess=_timeout,
        secrets=frozenset(),
    )
    unexpected = provider_readiness._probe_agent_runtime_cli(
        settings,
        executable="gemini",
        provider="gemini",
        environ={},
        run_subprocess=_unexpected,
        secrets=frozenset({"sk-proj-runtime-secret"}),
    )

    assert missing["reason_code"] == "DOCKER_CLI_NOT_FOUND"
    assert timeout["reason_code"] == "CLAUDE_RUNTIME_CLI_PROBE_TIMEOUT"
    assert unexpected["reason_code"] == "GEMINI_RUNTIME_CLI_PROBE_ERROR"
    assert "sk-proj-runtime-secret" not in json.dumps(unexpected, sort_keys=True)
    assert "<redacted>" in unexpected["detail"]
    assert "provider_readiness.agent_runtime_cli_probe_exception" in caplog.text
    assert "sk-proj-runtime-secret" not in caplog.text


@pytest.mark.unit
def test_agent_runtime_cli_probe_reports_success_and_missing_cli(tmp_path: Path) -> None:
    settings = _settings(tmp_path)

    ok = provider_readiness._probe_agent_runtime_cli(
        settings,
        executable="opencode",
        provider="opencode",
        environ={},
        run_subprocess=lambda _args, **_kwargs: _completed(stdout="/usr/bin/opencode\n"),
        secrets=frozenset(),
    )
    missing = provider_readiness._probe_agent_runtime_cli(
        settings,
        executable="opencode",
        provider="opencode",
        environ={},
        run_subprocess=lambda _args, **_kwargs: _completed(
            returncode=127,
            stderr="opencode: not found with token sk-proj-opencode-secret",
        ),
        secrets=frozenset({"sk-proj-opencode-secret"}),
    )

    assert ok == {
        "status": "ok",
        "reason_code": "OPENCODE_RUNTIME_CLI_AVAILABLE",
        "detail": "/usr/bin/opencode",
    }
    assert missing["status"] == "fail"
    assert missing["reason_code"] == "OPENCODE_RUNTIME_CLI_NOT_FOUND"
    assert "awf-agent-runtime:latest" in missing["message"]
    assert "sk-proj-opencode-secret" not in json.dumps(missing, sort_keys=True)
    assert "<redacted>" in missing["detail"]


@pytest.mark.unit
def test_cli_auth_probe_reports_success_and_unusable_auth() -> None:
    success = provider_readiness._probe_cli_auth_status(
        provider_label="Probe",
        args=["probe", "auth", "status"],
        failure_reason="PROBE_AUTH_FAILED",
        timeout_reason="PROBE_AUTH_TIMEOUT",
        missing_reason="PROBE_CLI_NOT_FOUND",
        error_reason="PROBE_AUTH_ERROR",
        environ={},
        run_subprocess=lambda _args, **_kwargs: _completed(stdout="ok"),
        secrets=frozenset(),
    )
    failure = provider_readiness._probe_cli_auth_status(
        provider_label="Probe",
        args=["probe", "auth", "status"],
        failure_reason="PROBE_AUTH_FAILED",
        timeout_reason="PROBE_AUTH_TIMEOUT",
        missing_reason="PROBE_CLI_NOT_FOUND",
        error_reason="PROBE_AUTH_ERROR",
        environ={},
        run_subprocess=lambda _args, **_kwargs: _completed(
            returncode=1,
            stderr="invalid token sk-proj-auth-secret",
        ),
        secrets=frozenset({"sk-proj-auth-secret"}),
    )

    assert success == {"status": "ok", "reason_code": "PROBE_AUTH_OK"}
    assert failure["reason_code"] == "PROBE_AUTH_FAILED"
    assert "sk-proj-auth-secret" not in json.dumps(failure, sort_keys=True)
    assert "<redacted>" in failure["detail"]


@pytest.mark.unit
def test_provider_readiness_rejects_unreachable_internal_provider(tmp_path: Path) -> None:
    with pytest.raises(AssertionError, match="unsupported provider"):
        provider_readiness._check_provider_readiness(  # type: ignore[arg-type]
            "not_a_provider",
            _settings(tmp_path),
            environ={},
            host_home=tmp_path / "home",
            strict=False,
            run_subprocess=_unexpected_subprocess,
            http_get=_ollama_ok,
            secrets=frozenset(),
        )


@pytest.mark.unit
def test_selected_launch_probe_skips_when_provider_or_model_is_unavailable(
    tmp_path: Path,
) -> None:
    assert provider_readiness._selected_launch_probe(
        "codex",
        settings=_settings(tmp_path),
        provider_result={"ok": False},
        model="gpt-5.5",
        environ={},
        run_subprocess=_unexpected_subprocess,
        http_get=_ollama_ok,
        secrets=frozenset(),
    ) == {"status": "skipped"}
    assert provider_readiness._selected_launch_probe(
        "codex",
        settings=_settings(tmp_path),
        provider_result={"ok": True},
        model=None,
        environ={},
        run_subprocess=_unexpected_subprocess,
        http_get=_ollama_ok,
        secrets=frozenset(),
    ) == {"status": "skipped"}


@pytest.mark.unit
def test_selected_launch_probe_returns_runtime_failure_and_unavailable_provider(
    tmp_path: Path,
) -> None:
    runtime_failure = provider_readiness._selected_launch_probe(
        "codex",
        settings=_settings(tmp_path),
        provider_result={"ok": True},
        model="gpt-5.5",
        environ={},
        run_subprocess=lambda _args, **_kwargs: _completed(returncode=127, stderr="missing"),
        http_get=_ollama_ok,
        secrets=frozenset(),
    )
    unavailable = provider_readiness._selected_launch_probe(
        "docker",
        settings=_settings(tmp_path),
        provider_result={"ok": True},
        model="docker-host",
        environ={},
        run_subprocess=_unexpected_subprocess,
        http_get=_ollama_ok,
        secrets=frozenset(),
    )

    assert runtime_failure["reason_code"] == "CODEX_RUNTIME_CLI_NOT_FOUND"
    assert unavailable == {
        "status": "unavailable",
        "reason_code": "PROVIDER_PROBE_UNAVAILABLE",
    }


@pytest.mark.unit
def test_selected_gemini_preflight_requires_usable_non_secret_probe(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".gemini").mkdir(parents=True)
    (home / ".gemini" / "oauth_creds.json").write_text("gemini_file_secret")
    token = "AIzaGeminiProbeSecret"

    def _run(args: list[str], **kwargs: object) -> Any:
        assert args == [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "awf-agent-runtime:latest",
            "-lc",
            "command -v gemini",
        ]
        assert kwargs["env"]["GEMINI_API_KEY"] == token
        return _completed(returncode=1, stdout=f"missing cli with token {token}")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="gemini",
        task_policy={},
        environ={"GEMINI_API_KEY": token},
        run_subprocess=_run,
    )

    assert result["auth_status"] == "ok"
    assert result["probe_status"] == "fail"
    assert result["reason_code"] == "GEMINI_RUNTIME_CLI_NOT_FOUND"
    assert result["blocks_launch"] is True
    serialized = json.dumps(result, sort_keys=True)
    assert token not in serialized
    assert "gemini_file_secret" not in serialized
    assert "<redacted>" in serialized


@pytest.mark.unit
def test_selected_gemini_preflight_uses_agent_runtime_cli_not_api_cli(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".gemini").mkdir(parents=True)
    calls: list[list[str]] = []

    def _run(args: list[str], **_kwargs: object) -> Any:
        calls.append(args)
        if args[0] == "gemini":
            raise FileNotFoundError("api container gemini is absent")
        return _completed(stdout="/usr/bin/gemini\n")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="gemini",
        task_policy={},
        environ={},
        run_subprocess=_run,
    )

    assert result["provider"] == "gemini"
    assert result["readiness_status"] == "ready"
    assert result["probe_status"] == "ok"
    assert result["reason_code"] == "PROVIDER_READY"
    assert result["blocks_launch"] is False
    assert calls == [
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "awf-agent-runtime:latest",
            "-lc",
            "command -v gemini",
        ]
    ]


@pytest.mark.unit
def test_selected_grok_preflight_requires_xai_api_key_and_runtime_cli(
    tmp_path: Path,
) -> None:
    token = "xai-runtime-secret"
    calls: list[list[str]] = []

    def _run(args: list[str], **kwargs: object) -> Any:
        calls.append(args)
        assert kwargs["env"]["XAI_API_KEY"] == token
        return _completed(stdout="/usr/local/bin/grok\n")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="grok",
        task_policy={},
        environ={"XAI_API_KEY": token},
        run_subprocess=_run,
    )

    assert result["provider"] == "grok"
    assert result["agent"] == "grok"
    assert result["model"] == "grok-build"
    assert result["readiness_status"] == "ready"
    assert result["auth_status"] == "ok"
    assert result["auth_source"] == "XAI_API_KEY"
    assert result["probe_status"] == "ok"
    assert result["reason_code"] == "PROVIDER_READY"
    assert result["blocks_launch"] is False
    assert calls == [
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "awf-agent-runtime:latest",
            "-lc",
            "command -v grok",
        ]
    ]
    serialized = json.dumps(result, sort_keys=True)
    assert token not in serialized


@pytest.mark.unit
def test_selected_grok_preflight_uses_file_auth_before_xai_api_key(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".grok").mkdir(parents=True)
    (home / ".grok" / "auth.json").write_text('{"token":"grok_file_secret"}')
    token = "xai-env-fallback-secret"

    def _run(args: list[str], **kwargs: object) -> Any:
        assert args[-1] == "command -v grok"
        assert kwargs["env"]["XAI_API_KEY"] == token
        return _completed(stdout="/usr/local/bin/grok\n")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="grok",
        task_policy={},
        environ={"XAI_API_KEY": token},
        run_subprocess=_run,
    )

    assert result["provider"] == "grok"
    assert result["readiness_status"] == "ready"
    assert result["auth_status"] == "ok"
    assert result["auth_source"] == "~/.grok/auth.json"
    assert result["probe_status"] == "ok"
    assert result["reason_code"] == "PROVIDER_READY"
    serialized = json.dumps(result, sort_keys=True)
    assert token not in serialized
    assert "grok_file_secret" not in serialized


@pytest.mark.unit
def test_selected_grok_preflight_blocks_missing_xai_api_key(tmp_path: Path) -> None:
    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="grok",
        task_policy={},
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    assert result["provider"] == "grok"
    assert result["readiness_status"] == "blocked"
    assert result["auth_status"] == "fail"
    assert result["probe_status"] == "skipped"
    assert result["reason_code"] == "GROK_AUTH_MISSING"
    assert result["blocks_launch"] is True


@pytest.mark.unit
def test_selected_grok_preflight_blocks_missing_runtime_cli_and_redacts_key(
    tmp_path: Path,
) -> None:
    token = "xai-missing-cli-secret"

    def _run(args: list[str], **kwargs: object) -> Any:
        assert args[-1] == "command -v grok"
        assert kwargs["env"]["XAI_API_KEY"] == token
        return _completed(returncode=1, stderr=f"grok: not found for {token}")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="grok",
        task_policy={},
        environ={"XAI_API_KEY": token},
        run_subprocess=_run,
    )

    assert result["auth_status"] == "ok"
    assert result["probe_status"] == "fail"
    assert result["reason_code"] == "GROK_RUNTIME_CLI_NOT_FOUND"
    assert result["blocks_launch"] is True
    serialized = json.dumps(result, sort_keys=True)
    assert token not in serialized
    assert "<redacted>" in serialized


@pytest.mark.unit
def test_selected_codex_preflight_blocks_when_runtime_cli_missing(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text('{"token":"codex_file_secret"}')

    def _run(args: list[str], **_kwargs: object) -> Any:
        assert args[-1] == "command -v codex"
        return _completed(returncode=1, stderr="codex: not found")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="codex",
        task_policy={},
        environ={},
        run_subprocess=_run,
    )

    assert result["auth_status"] == "ok"
    assert result["probe_status"] == "fail"
    assert result["reason_code"] == "CODEX_RUNTIME_CLI_NOT_FOUND"
    assert result["blocks_launch"] is True


@pytest.mark.unit
def test_preflight_payload_filters_sparse_provider_metadata() -> None:
    provider_result = {
        "ok": True,
        "status": "ok",
        "credential_scope": "fallback_scope",
        "credential_sources": [
            "ignored",
            {},
            {"type": "env", "signal": 42, "credential_scope": "static_env_token"},
            {"signal": "VISIBLE_SIGNAL", "isolation": "service_env"},
        ],
        "warnings": [
            {"reason": "STATIC_TOKEN_FALLBACK", "message": "uses env", "severity": "warning"},
            "ignored",
        ],
    }

    payload = provider_readiness._launch_preflight_payload(
        agent="codex",
        provider="codex",
        model="gpt-5.5",
        model_source="default",
        provider_result=provider_result,
        probe={"status": "unavailable"},
        reason_code="PROVIDER_READY",
        message="ready",
        override=False,
        override_reason=None,
        checked_at=provider_readiness.datetime(2026, 5, 3, tzinfo=provider_readiness.UTC),
        secrets=frozenset(),
    )

    assert payload["readiness_status"] == "ready"
    assert payload["probe_status"] == "unavailable"
    assert payload["auth_source"] == "fallback_scope"
    assert payload["credential_sources"] == [
        {"type": "env", "credential_scope": "static_env_token"},
        {"signal": "VISIBLE_SIGNAL", "isolation": "service_env"},
    ]
    assert payload["warnings"] == [
        {"reason": "STATIC_TOKEN_FALLBACK", "message": "uses env", "severity": "warning"}
    ]
    assert provider_readiness._credential_sources({"credential_sources": "bad-shape"}) == []


@pytest.mark.unit
def test_preflight_reason_and_message_report_missing_model() -> None:
    provider_result = {"ok": True, "status": "ok"}
    probe = {"status": "ok"}

    assert (
        provider_readiness._preflight_reason_code(
            provider_result=provider_result,
            probe=probe,
            model=None,
        )
        == "MODEL_NOT_SELECTED"
    )
    assert (
        provider_readiness._preflight_message(
            provider_result=provider_result,
            probe=probe,
            model=None,
        )
        == "No effective model was selected for the workspace agent."
    )


@pytest.mark.unit
def test_provider_readiness_preflight_snapshot_and_text_redaction(tmp_path: Path) -> None:
    snapshot = {"provider": "codex", "reason_code": "PROVIDER_READY"}

    assert (
        provider_readiness.provider_readiness_preflight_from_task_policy(
            {"provider_readiness_preflight": snapshot}
        )
        == snapshot
    )
    assert (
        provider_readiness.provider_readiness_preflight_from_task_policy(
            {"provider_readiness_preflight": "bad-shape"}
        )
        is None
    )
    redacted = provider_readiness.redact_launch_preflight_text(
        _settings(tmp_path),
        "token sk-proj-redact-text-secret",
        environ={"OPENAI_API_KEY": "sk-proj-redact-text-secret"},
    )
    assert redacted == "token <redacted>"


@pytest.mark.unit
def test_preflight_payload_records_redacted_override_reason_parts() -> None:
    payload = provider_readiness._launch_preflight_payload(
        agent="codex",
        provider="codex",
        model="gpt-5.5",
        model_source="default",
        provider_result={"ok": False, "status": "fail", "reason": "CODEX_AUTH_MISSING"},
        probe={"status": "skipped"},
        reason_code="CODEX_AUTH_MISSING",
        message="missing",
        override=True,
        override_reason="operator checked sk-proj-override-secret manually",
        checked_at=provider_readiness.datetime(2026, 5, 4, tzinfo=provider_readiness.UTC),
        secrets=frozenset({"sk-proj-override-secret"}),
    )

    assert payload["override_reason"] == "operator checked <redacted> manually"
    assert payload["override_reason_redaction_parts"] == [
        "operator checked ",
        " manually",
    ]


@pytest.mark.unit
def test_provider_readiness_all_green(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text('{"token":"codex_file_secret"}')
    (home / ".codex" / "config.toml").write_text("model = 'gpt-5.5'\n")
    (home / ".codex" / "installation_id").write_text("installation-123\n")
    (home / ".claude").mkdir(parents=True)
    (home / ".gemini").mkdir()
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".ollama").mkdir()
    (home / ".ollama" / "config.json").write_text("ollama-file-secret")
    github_secret = "ghp_green_secret"
    anthropic_secret = "sk-ant-green-secret"
    cursor_secret = "cursor_green_secret"
    gemini_secret = "gemini_green_secret"
    ollama_secret = "ollama_green_secret"
    xai_secret = "xai_green_secret"
    env = {
        "AWF_GITHUB_TOKEN": github_secret,
        "ANTHROPIC_API_KEY": anthropic_secret,
        "CURSOR_API_KEY": cursor_secret,
        "GEMINI_API_KEY": gemini_secret,
        "OLLAMA_API_KEY": ollama_secret,
        "XAI_API_KEY": xai_secret,
        "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1",
    }
    subprocess_calls: list[list[str]] = []

    def _run(args: list[str], **kwargs: object) -> Any:
        """Return successful auth and runtime probes for all providers."""
        subprocess_calls.append(args)
        if args == ["gh", "auth", "status", "--hostname", "github.com"]:
            assert github_secret not in args
            subprocess_env = kwargs["env"]
            assert isinstance(subprocess_env, dict)
            assert subprocess_env["GH_TOKEN"] == github_secret
            return _completed(stdout="logged in\n")
        assert args == [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "awf-agent-runtime:latest",
            "-lc",
            "command -v cursor-agent",
        ]
        assert kwargs["env"]["CURSOR_API_KEY"] == cursor_secret
        return _completed(stdout="/usr/local/bin/cursor-agent\n")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ=env,
        run_subprocess=_run,
        http_get=_ollama_ok,
    )

    assert payload["status"] == "ok"
    providers = payload["providers"]
    assert set(providers) == {
        "github",
        "codex",
        "claude_code",
        "cursor",
        "gemini",
        "opencode",
        "grok",
        "docker",
    }
    assert all(provider["ok"] is True for provider in providers.values())
    assert providers["github"]["capabilities"] == ["pr_create", "comment", "merge"]
    assert subprocess_calls == [
        ["gh", "auth", "status", "--hostname", "github.com"],
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "awf-agent-runtime:latest",
            "-lc",
            "command -v cursor-agent",
        ],
    ]
    serialized = json.dumps(payload, sort_keys=True)
    for secret in (
        github_secret,
        "codex_file_secret",
        anthropic_secret,
        cursor_secret,
        gemini_secret,
        ollama_secret,
        xai_secret,
    ):
        assert secret not in serialized
