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
    # Answer both a non-worker-reachable DNS host (``ollama.local``) and the
    # worker-reachable ``localhost`` so callers that need the create-time daemon
    # probe to actually run can point at a host that is not deferred away.
    if url in {
        "http://ollama.local:11434/api/version",
        "http://localhost:11434/api/version",
    }:
        return SimpleNamespace(status_code=200, text='{"version":"0.1.0"}')
    if url in {
        "http://ollama.local:11434/api/tags",
        "http://localhost:11434/api/tags",
    }:
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
        # ``localhost`` is worker-reachable so the OpenCode create-time daemon probe
        # runs for the ``:cloud`` model rather than being deferred as non-reachable.
        "AWF_OPENCODE_OLLAMA_BASE_URL": "http://localhost:11434/v1",
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

    # ``localhost`` is worker-reachable, so the create-time daemon probe runs and
    # exercises the cloud-absent-from-tags disposition. (A ``:cloud`` model at a
    # daemon URL the worker cannot reach defers instead — see
    # ``test_selected_opencode_preflight_cloud_model_with_creds_non_worker_reachable_url_defers``.)
    def _http_get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        urls.append(url)
        if url == "http://localhost:11434/api/version":
            return SimpleNamespace(status_code=200, text='{"version":"0.1.0"}')
        if url == "http://localhost:11434/api/tags":
            return SimpleNamespace(
                status_code=200,
                text='{"models":[{"name":"other-model:latest"}]}',
            )
        raise AssertionError(f"unexpected Ollama probe URL: {url}")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="opencode",
        task_policy={"agent_model": "ollama/kimi-k2.6:cloud"},
        environ={"AWF_OPENCODE_OLLAMA_BASE_URL": "http://localhost:11434/v1"},
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
        "http://localhost:11434/api/version",
        "http://localhost:11434/api/tags",
    ]


@pytest.mark.unit
def test_selected_opencode_preflight_absent_non_cloud_model_is_pull_pending(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)

    # ``localhost`` is worker-reachable, so the create-time daemon probe runs (the
    # #569 host-unreachable skip does not apply); a sidecar DNS name is covered by
    # ``test_selected_opencode_preflight_local_model_non_worker_reachable_url_defers``.
    def _http_get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        if url == "http://localhost:11434/api/version":
            return SimpleNamespace(status_code=200, text='{"version":"0.1.0"}')
        if url == "http://localhost:11434/api/tags":
            return SimpleNamespace(
                status_code=200,
                text='{"models":[{"name":"other-model:latest"}]}',
            )
        raise AssertionError(f"unexpected Ollama probe URL: {url}")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="opencode",
        task_policy={"agent_model": "ollama/llama4:70b"},
        environ={"AWF_OPENCODE_OLLAMA_BASE_URL": "http://localhost:11434/v1"},
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

    # A worker-reachable URL (``localhost``) whose daemon is down still blocks: the
    # #569 skip only defers a daemon URL the worker cannot reach at all.
    def _http_get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        if url == "http://localhost:11434/api/version":
            return SimpleNamespace(status_code=200, text='{"version":"0.1.0"}')
        if url == "http://localhost:11434/api/tags":
            raise RuntimeError("connection refused")
        raise AssertionError(f"unexpected Ollama probe URL: {url}")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="opencode",
        task_policy={"agent_model": "ollama/llama4:70b"},
        environ={"AWF_OPENCODE_OLLAMA_BASE_URL": "http://localhost:11434/v1"},
        run_subprocess=_runtime_cli_ok("opencode"),
        http_get=_http_get,
    )

    assert result["probe_status"] == "fail"
    assert result["reason_code"] == "OLLAMA_MODEL_PROBE_FAILED"
    assert result["blocks_launch"] is True


@pytest.mark.unit
def test_selected_opencode_preflight_authless_local_model_reachable_daemon_allowed(
    tmp_path: Path,
) -> None:
    # No ~/.config/opencode, no ~/.ollama auth files, no OLLAMA_API_KEY. A local
    # ``ollama/``-prefixed model is served by the host daemon, whose /api/tags
    # and /api/pull need no OpenCode/Ollama Cloud credential. With the daemon
    # reachable the strict auth gate is waived (carve-out symmetric to
    # OPENCODE_NON_OLLAMA_PROVIDER_SELECTED) so admission can proceed. ``localhost``
    # is worker-reachable, so the daemon probe runs rather than the #569 skip.
    def _http_get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        if url == "http://localhost:11434/api/version":
            return SimpleNamespace(status_code=200, text='{"version":"0.1.0"}')
        if url == "http://localhost:11434/api/tags":
            return SimpleNamespace(
                status_code=200,
                text='{"models":[{"name":"llama4:70b"}]}',
            )
        raise AssertionError(f"unexpected Ollama probe URL: {url}")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="opencode",
        task_policy={"agent_model": "ollama/llama4:70b"},
        environ={"AWF_OPENCODE_OLLAMA_BASE_URL": "http://localhost:11434/v1"},
        run_subprocess=_runtime_cli_ok("opencode"),
        http_get=_http_get,
    )

    assert result["provider"] == "opencode"
    assert result["auth_status"] == "ok"
    # Authless: no credential source is observed, so the auth source falls back
    # to the credential scope rather than naming a credential.
    assert result["auth_source"] == "not_observed"
    assert result["credential_scope"] == "not_observed"
    # The model is already present locally, so launch is fully ready.
    assert result["probe_status"] == "ok"
    assert result["reason_code"] == "PROVIDER_READY"
    assert result["override_required"] is False
    assert result["blocks_launch"] is False


@pytest.mark.unit
def test_selected_opencode_preflight_authless_local_absent_model_is_pull_pending(
    tmp_path: Path,
) -> None:
    # Authless local model that is not yet present: the waived auth gate lets the
    # pull-pending probe run, so admission is non-blocking and the executor
    # pre-agent step can auto-pull rather than the workspace being rejected at
    # create time with OPENCODE_OLLAMA_AUTH_MISSING. ``localhost`` is worker-
    # reachable, so the carve-out daemon probe runs rather than the #569 skip.
    def _http_get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        if url == "http://localhost:11434/api/version":
            return SimpleNamespace(status_code=200, text='{"version":"0.1.0"}')
        if url == "http://localhost:11434/api/tags":
            return SimpleNamespace(
                status_code=200,
                text='{"models":[{"name":"other-model:latest"}]}',
            )
        raise AssertionError(f"unexpected Ollama probe URL: {url}")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="opencode",
        task_policy={"agent_model": "ollama/llama4:70b"},
        environ={"AWF_OPENCODE_OLLAMA_BASE_URL": "http://localhost:11434/v1"},
        run_subprocess=_runtime_cli_ok("opencode"),
        http_get=_http_get,
    )

    assert result["auth_status"] == "ok"
    assert result["probe_status"] == "pending"
    assert result["reason_code"] == "OLLAMA_MODEL_PULL_PENDING"
    assert result["blocks_launch"] is False


@pytest.mark.unit
def test_selected_opencode_preflight_authless_cloud_model_still_requires_creds(
    tmp_path: Path,
) -> None:
    # The carve-out is for local models only: a ``:cloud`` model is served
    # remotely and still requires the OpenCode/Ollama Cloud credential, so with
    # no auth signal it must block with OPENCODE_OLLAMA_AUTH_MISSING without even
    # probing the daemon.
    def _no_http(url: str, *, timeout: float) -> Any:
        raise AssertionError(f"unexpected Ollama probe URL: {url}")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="opencode",
        task_policy={"agent_model": "ollama/kimi-k2.6:cloud"},
        environ={"AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1"},
        run_subprocess=_unexpected_subprocess,
        http_get=_no_http,
    )

    assert result["auth_status"] == "fail"
    assert result["reason_code"] == "OPENCODE_OLLAMA_AUTH_MISSING"
    assert result["blocks_launch"] is True


@pytest.mark.unit
def test_selected_opencode_preflight_authless_local_model_unreachable_daemon_blocks(
    tmp_path: Path,
) -> None:
    # Authless local model at a worker-reachable URL (``localhost``) whose daemon is
    # down: the waiver is conditional on daemon reachability, so with no credential
    # present this still blocks with OPENCODE_OLLAMA_AUTH_MISSING. Only the cheap
    # /api/version probe runs before the gate falls through. (A daemon URL the worker
    # cannot reach at all is deferred instead — see
    # ``test_selected_opencode_preflight_authless_local_non_worker_reachable_url_defers``.)
    seen: list[str] = []

    def _http_get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        seen.append(url)
        raise RuntimeError("connection refused")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="opencode",
        task_policy={"agent_model": "ollama/llama4:70b"},
        environ={"AWF_OPENCODE_OLLAMA_BASE_URL": "http://localhost:11434/v1"},
        run_subprocess=_unexpected_subprocess,
        http_get=_http_get,
    )

    assert result["auth_status"] == "fail"
    assert result["reason_code"] == "OPENCODE_OLLAMA_AUTH_MISSING"
    assert result["blocks_launch"] is True
    assert seen == ["http://localhost:11434/api/version"]


@pytest.mark.unit
def test_selected_opencode_preflight_local_model_non_worker_reachable_url_defers(
    tmp_path: Path,
) -> None:
    # #569 symmetry: a profile Ollama URL like ``http://ollama-sidecar:11434`` is a
    # workspace Compose service DNS name the create/retry admission process cannot
    # reach. A worker-side /api/version|/api/tags probe would falsely block the
    # workspace with OLLAMA_HOST_UNREACHABLE before the executor pre-agent step (which
    # already skips the same probe) could defer it. Admission must instead skip the
    # Ollama daemon probe and defer to the agent container where the sidecar IS
    # reachable. Auth is present here, so the skip is purely about host reachability.
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)

    def _no_http(url: str, *, timeout: float) -> Any:
        raise AssertionError(f"daemon must not be probed for a sidecar URL: {url}")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="opencode",
        task_policy={"agent_model": "ollama/llama4:70b"},
        environ={"AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama-sidecar:11434"},
        run_subprocess=_runtime_cli_ok("opencode"),
        http_get=_no_http,
    )

    assert result["provider"] == "opencode"
    assert result["model"] == "ollama/llama4:70b"
    assert result["reason_code"] == "OPENCODE_OLLAMA_HOST_NOT_WORKER_REACHABLE"
    assert result["probe_status"] == "unavailable"
    assert result["auth_status"] == "ok"
    assert result["override_required"] is False
    assert result["blocks_launch"] is False


@pytest.mark.unit
def test_selected_opencode_preflight_authless_local_non_worker_reachable_url_defers(
    tmp_path: Path,
) -> None:
    # The reviewer's other reported reason code: an authless local model at a sidecar
    # URL currently blocks with OPENCODE_OLLAMA_AUTH_MISSING because the worker cannot
    # reach the daemon to waive the auth gate. The host-reachability skip must apply
    # here too (no credential, no daemon probe) and defer to the agent container.
    def _no_http(url: str, *, timeout: float) -> Any:
        raise AssertionError(f"daemon must not be probed for a sidecar URL: {url}")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="opencode",
        task_policy={"agent_model": "ollama/llama4:70b"},
        environ={"AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama-sidecar:11434"},
        run_subprocess=_runtime_cli_ok("opencode"),
        http_get=_no_http,
    )

    assert result["reason_code"] == "OPENCODE_OLLAMA_HOST_NOT_WORKER_REACHABLE"
    assert result["probe_status"] == "unavailable"
    assert result["blocks_launch"] is False


@pytest.mark.unit
def test_selected_opencode_preflight_cloud_model_with_creds_non_worker_reachable_url_defers(
    tmp_path: Path,
) -> None:
    # PRRT_kwDOSJAM6s6JV_Rl: a ``:cloud`` Ollama model (e.g. ``glm-5.1:cloud``) with
    # valid OpenCode credentials and a sidecar daemon URL the worker cannot reach must
    # also defer the worker-side /api/version probe to the agent container. The defer
    # was previously gated on a *local* model, so a cloud model fell through to
    # _check_opencode and blocked with OLLAMA_HOST_UNREACHABLE before the executor's
    # sidecar skip (which already defers for any non-host-reachable URL) could run.
    # Credentials are present here, so the cloud auth gate is satisfied.
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)

    def _no_http(url: str, *, timeout: float) -> Any:
        raise AssertionError(f"daemon must not be probed for a sidecar URL: {url}")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="opencode",
        task_policy={"agent_model": "glm-5.1:cloud"},
        environ={"AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama-sidecar:11434"},
        run_subprocess=_runtime_cli_ok("opencode"),
        http_get=_no_http,
    )

    assert result["provider"] == "opencode"
    assert result["model"] == "glm-5.1:cloud"
    assert result["reason_code"] == "OPENCODE_OLLAMA_HOST_NOT_WORKER_REACHABLE"
    assert result["probe_status"] == "unavailable"
    assert result["auth_status"] == "ok"
    assert result["override_required"] is False
    assert result["blocks_launch"] is False


@pytest.mark.unit
def test_selected_opencode_preflight_cloud_model_without_creds_non_worker_reachable_url_blocks(
    tmp_path: Path,
) -> None:
    # The ``:cloud`` defer is gated on visible OpenCode/Ollama credentials: a cloud
    # model is served remotely and still needs the cloud credential. With none, the
    # worker must NOT defer and must NOT probe the unreachable sidecar — admission
    # blocks with OPENCODE_OLLAMA_AUTH_MISSING (the host probe never runs because the
    # credential gate fails first).
    def _no_http(url: str, *, timeout: float) -> Any:
        raise AssertionError(f"daemon must not be probed when creds are missing: {url}")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="opencode",
        task_policy={"agent_model": "glm-5.1:cloud"},
        environ={"AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama-sidecar:11434"},
        run_subprocess=_unexpected_subprocess,
        http_get=_no_http,
    )

    assert result["reason_code"] == "OPENCODE_OLLAMA_AUTH_MISSING"
    assert result["auth_status"] == "fail"
    assert result["blocks_launch"] is True


@pytest.mark.unit
def test_selected_opencode_preflight_non_worker_reachable_url_still_blocks_missing_cli(
    tmp_path: Path,
) -> None:
    # Skipping the Ollama daemon probe for a sidecar URL must not also skip the
    # generic OpenCode CLI availability check: a runtime image missing the
    # ``opencode`` binary still has to block admission here rather than be admitted
    # as ready and only fail later as an agent command failure.
    def _no_http(url: str, *, timeout: float) -> Any:
        raise AssertionError(f"daemon must not be probed for a sidecar URL: {url}")

    def _runtime_cli_missing(args: list[str], **_kwargs: object) -> Any:
        assert args[-1] == "command -v opencode"
        return _completed(returncode=1, stderr="opencode: not found")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="opencode",
        task_policy={"agent_model": "ollama/llama4:70b"},
        environ={"AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama-sidecar:11434"},
        run_subprocess=_runtime_cli_missing,
        http_get=_no_http,
    )

    assert result["probe_status"] == "fail"
    assert result["reason_code"] == "OPENCODE_RUNTIME_CLI_NOT_FOUND"
    assert result["blocks_launch"] is True


@pytest.mark.unit
@pytest.mark.parametrize("model", ["openai/gpt-oss", "anthropic/claude-sonnet"])
def test_selected_opencode_preflight_non_ollama_provider_model_missing_creds_blocks(
    tmp_path: Path,
    model: str,
) -> None:
    # #554: a provider-qualified non-Ollama model served by an OpenCode cloud
    # provider needs an OpenCode/provider credential. With no ~/.config/opencode
    # and no provider API key visible, create-time readiness must FAIL up front
    # with the clear OPENCODE_PROVIDER_AUTH_MISSING reason (symmetric to the
    # OPENCODE_OLLAMA_AUTH_MISSING cloud-Ollama gate) instead of deferring to the
    # provider and surfacing a confusing agent-CLI error later. Neither the Ollama
    # probe nor the OpenCode CLI probe runs in the no-creds path.
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
    assert result["reason_code"] == "OPENCODE_PROVIDER_AUTH_MISSING"
    assert result["auth_status"] == "fail"
    assert result["probe_status"] == "skipped"
    assert result["override_required"] is True
    assert result["blocks_launch"] is True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("model", "expected_hint"),
    [
        ("openai/gpt-oss", "OPENAI_API_KEY / OPENAI_API_TOKEN"),
        ("anthropic/claude-sonnet", "ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN"),
        ("google/gemini-pro", "GEMINI_API_KEY / GOOGLE_API_KEY"),
        ("xai/grok", "XAI_API_KEY"),
        ("mystery/model", "the provider API key"),
    ],
)
def test_selected_opencode_preflight_non_ollama_auth_missing_hint_is_provider_accurate(
    tmp_path: Path,
    model: str,
    expected_hint: str,
) -> None:
    # The auth-missing fix message must name the provider's own credential env
    # var(s) (GEMINI_API_KEY for google/..., XAI_API_KEY for xai/...) rather than
    # a hardcoded openai/anthropic example, so the operator follows the right fix.
    # An unknown provider prefix falls back to a generic "the provider API key".
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

    assert result["reason_code"] == "OPENCODE_PROVIDER_AUTH_MISSING"
    assert f"set {expected_hint}." in result["message"]


@pytest.mark.unit
@pytest.mark.parametrize("model", ["openai/gpt-oss", "anthropic/claude-sonnet"])
def test_selected_opencode_preflight_non_ollama_provider_model_with_config_creds_defers(
    tmp_path: Path,
    model: str,
) -> None:
    # #554: ~/.config/opencode is OpenCode's own multi-provider credential store,
    # so its presence satisfies the create-time credential gate for any provider.
    # With creds present the behavior is unchanged from #553: the Ollama
    # auth/daemon preflight is skipped (deferred to the provider), only the
    # generic OpenCode runtime-CLI probe runs, and the workspace is admitted.
    (tmp_path / "home" / ".config" / "opencode").mkdir(parents=True)

    def _no_http(url: str, *, timeout: float) -> Any:
        raise AssertionError(f"unexpected Ollama probe URL: {url}")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="opencode",
        task_policy={"agent_model": model},
        environ={},
        run_subprocess=_runtime_cli_ok("opencode"),
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
@pytest.mark.parametrize(
    ("model", "env_key", "env_value"),
    [
        ("openai/gpt-oss", "OPENAI_API_KEY", "sk-proj-opencode-readiness"),
        ("anthropic/claude-sonnet", "ANTHROPIC_API_KEY", "sk-ant-opencode-readiness"),
    ],
)
def test_selected_opencode_preflight_non_ollama_provider_model_with_env_key_defers(
    tmp_path: Path,
    model: str,
    env_key: str,
    env_value: str,
) -> None:
    # #554: with no ~/.config/opencode, the provider API key matching the model's
    # provider prefix (OPENAI_API_KEY for openai/..., ANTHROPIC_API_KEY for
    # anthropic/...) satisfies the credential gate, so admission defers to the
    # provider exactly as when ~/.config/opencode is present.
    def _no_http(url: str, *, timeout: float) -> Any:
        raise AssertionError(f"unexpected Ollama probe URL: {url}")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="opencode",
        task_policy={"agent_model": model},
        environ={env_key: env_value},
        run_subprocess=_runtime_cli_ok("opencode"),
        http_get=_no_http,
    )

    assert result["provider"] == "opencode"
    assert result["model"] == model
    assert result["reason_code"] == "OPENCODE_NON_OLLAMA_PROVIDER_SELECTED"
    assert result["probe_status"] == "unavailable"
    assert result["auth_status"] == "ok"
    assert result["blocks_launch"] is False


@pytest.mark.unit
@pytest.mark.parametrize("model", ["openai/gpt-oss", "anthropic/claude-sonnet"])
def test_selected_opencode_preflight_non_ollama_provider_model_blocks_missing_cli(
    tmp_path: Path,
    model: str,
) -> None:
    # Skipping the Ollama preflight for a provider-qualified non-Ollama model must
    # not also skip the generic OpenCode CLI availability check: a runtime image
    # missing the ``opencode`` binary has to block admission here rather than be
    # admitted as ready and only fail later as an agent command failure. Provide
    # ~/.config/opencode so the #554 auth gate passes and the missing-CLI block is
    # the contract under test.
    (tmp_path / "home" / ".config" / "opencode").mkdir(parents=True)

    def _no_http(url: str, *, timeout: float) -> Any:
        raise AssertionError(f"unexpected Ollama probe URL: {url}")

    def _runtime_cli_missing(args: list[str], **_kwargs: object) -> Any:
        assert args[-1] == "command -v opencode"
        return _completed(returncode=1, stderr="opencode: not found")

    result = selected_provider_readiness_preflight(
        _settings(tmp_path),
        agent="opencode",
        task_policy={"agent_model": model},
        environ={},
        run_subprocess=_runtime_cli_missing,
        http_get=_no_http,
    )

    assert result["provider"] == "opencode"
    assert result["model"] == model
    assert result["probe_status"] == "fail"
    assert result["reason_code"] == "OPENCODE_RUNTIME_CLI_NOT_FOUND"
    assert result["blocks_launch"] is True


@pytest.mark.unit
def test_opencode_provider_credentials_present_classifier(tmp_path: Path) -> None:
    # #554 credential detection: ~/.config/opencode satisfies any provider; a
    # provider-matched env key satisfies its provider; an unknown provider with
    # no config dir is unsatisfied.
    from awf.service.provider_readiness_helpers import _opencode_provider_credentials_present

    config_home = tmp_path / "with_config"
    (config_home / ".config" / "opencode").mkdir(parents=True)
    assert _opencode_provider_credentials_present("openai/gpt-oss", {}, config_home) == (
        True,
        "~/.config/opencode",
    )

    bare_home = tmp_path / "bare"
    bare_home.mkdir()
    assert _opencode_provider_credentials_present(
        "openai/gpt-oss", {"OPENAI_API_KEY": "sk-proj-x"}, bare_home
    ) == (True, "OPENAI_API_KEY")
    assert _opencode_provider_credentials_present(
        "anthropic/claude-sonnet", {"ANTHROPIC_AUTH_TOKEN": "sk-ant-x"}, bare_home
    ) == (True, "ANTHROPIC_AUTH_TOKEN")
    # An unknown provider prefix is satisfied only by ~/.config/opencode.
    assert _opencode_provider_credentials_present(
        "mystery/model", {"OPENAI_API_KEY": "sk-proj-x"}, bare_home
    ) == (False, None)
    # The matching provider key must be present, not just any provider key.
    assert _opencode_provider_credentials_present(
        "openai/gpt-oss", {"ANTHROPIC_API_KEY": "sk-ant-x"}, bare_home
    ) == (False, None)


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
