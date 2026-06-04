"""Additional provider credential readiness checks for local service mode."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import awf.service.provider_readiness as provider_readiness
import awf.service.provider_readiness_helpers as provider_readiness_helpers
from awf.service.provider_readiness import collect_agent_readiness

from .test_provider_readiness_part_001 import (
    _completed,
    _ollama_ok,
    _settings,
    _unexpected_subprocess,
)


@pytest.mark.unit
def test_provider_readiness_codex_directory_fallback_and_rules_are_sources(
    tmp_path: Path,
) -> None:
    rules_home = tmp_path / "rules-home"
    (rules_home / ".codex" / "rules").mkdir(parents=True)
    rules_payload = collect_agent_readiness(
        _settings(tmp_path, host_home=str(rules_home)),
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    empty_home = tmp_path / "empty-home"
    (empty_home / ".codex").mkdir(parents=True)
    empty_payload = collect_agent_readiness(
        _settings(tmp_path, host_home=str(empty_home)),
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    assert {
        source["signal"] for source in rules_payload["providers"]["codex"]["credential_sources"]
    } == {"~/.codex/rules"}
    assert {
        source["signal"] for source in empty_payload["providers"]["codex"]["credential_sources"]
    } == {"~/.codex"}


@pytest.mark.unit
def test_provider_readiness_security_summary_tolerates_sparse_warning_payloads() -> None:
    security = provider_readiness._security_summary(
        {
            "github": {"status": "ok", "warnings": "not-a-list"},
            "codex": {
                "status": "ok",
                "warnings": ["ignored", {"reason": "STATIC_TOKEN_FALLBACK"}],
            },
            "docker": {
                "status": "warn",
                "reason": "DOCKER_AUTH_NOT_OBSERVED",
                "warnings": [],
            },
        }
    )

    assert security["status"] == "warning"
    assert security["warning_count"] == 1
    assert security["providers_with_warnings"] == ["codex", "docker"]
    assert security["reason_codes"] == [
        "DOCKER_AUTH_NOT_OBSERVED",
        "STATIC_TOKEN_FALLBACK",
    ]


@pytest.mark.unit
def test_provider_readiness_provider_result_defaults_unknown_source_metadata() -> None:
    result = provider_readiness._provider_result(
        ok=True,
        strict=False,
        reason="CUSTOM_AUTH_PRESENT",
        message="Custom provider auth was observed.",
        secrets=frozenset(),
        credential_sources=[
            {
                "type": "path",
                "signal": "~/.custom/auth.json",
                "credential_scope": "custom_scope",
                "isolation": "custom_isolation",
            }
        ],
    )

    assert result["credential_scope"] == "not_observed"
    assert result["isolation"] == "none"


@pytest.mark.unit
def test_provider_readiness_codex_directory_sources_include_rules_and_directory(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    codex_home = home / ".codex"
    (codex_home / "rules").mkdir(parents=True)

    rule_sources = provider_readiness._codex_file_sources(home)
    assert [source["signal"] for source in rule_sources] == ["~/.codex/rules"]

    (codex_home / "rules").rmdir()
    directory_sources = provider_readiness._codex_file_sources(home)
    assert [source["signal"] for source in directory_sources] == ["~/.codex"]


@pytest.mark.unit
def test_provider_readiness_security_summary_collects_provider_warnings(
    tmp_path: Path,
) -> None:
    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"OPENAI_API_KEY": "sk-proj-security-summary-secret"},
        run_subprocess=_unexpected_subprocess,
    )

    security = payload["security"]
    assert security["status"] == "warning"
    assert security["warning_count"] >= 1
    assert "codex" in security["providers_with_warnings"]
    assert "STATIC_TOKEN_FALLBACK" in security["reason_codes"]
    assert "DOCKER_HOST_BROAD_CONTROL" in security["reason_codes"]
    assert "sk-proj-security-summary-secret" not in json.dumps(security, sort_keys=True)


@pytest.mark.unit
def test_provider_readiness_existing_file_providers_report_credential_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pin the Claude isolation posture so this assertion does not depend on
    # whether the test host happens to support overlayfs + CAP_SYS_ADMIN.
    monkeypatch.setattr(
        provider_readiness, "claude_auth_isolation_label", lambda **_: "per_workspace_copy"
    )
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text('{"token":"claude_file_secret"}')
    (home / ".claude.json").write_text('{"oauth":"claude_json_secret"}')
    (home / ".gemini").mkdir()
    (home / ".gemini" / "oauth_creds.json").write_text("gemini_file_secret")
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".config" / "opencode" / "opencode.json").write_text("opencode_file_secret")
    (home / ".ollama").mkdir()
    (home / ".ollama" / "id_ed25519").write_text("ollama_file_secret")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1"},
        run_subprocess=_unexpected_subprocess,
        http_get=_ollama_ok,
    )

    for name in ("claude_code", "gemini", "opencode"):
        provider = payload["providers"][name]
        assert provider["ok"] is True
        assert provider["credential_scope"] == "isolated_workspace"
        assert provider["isolation"] == "per_workspace_copy"
        assert provider["credential_sources"]
    serialized = json.dumps(payload, sort_keys=True)
    for secret in (
        "claude_file_secret",
        "claude_json_secret",
        "gemini_file_secret",
        "opencode_file_secret",
        "ollama_file_secret",
    ):
        assert secret not in serialized


@pytest.mark.unit
def test_provider_readiness_env_fallbacks_report_security_warnings(
    tmp_path: Path,
) -> None:
    """Environment fallback auth reports static-token security warnings."""
    env = {
        "AWF_GITHUB_TOKEN": "ghp_env_fallback_secret",
        "OPENAI_API_KEY": "sk-proj-codex-fallback-secret",
        "ANTHROPIC_API_KEY": "sk-ant-env-fallback-secret",
        "CURSOR_API_KEY": "cursor-env-fallback-secret",
        "GEMINI_API_KEY": "gemini-env-fallback-secret",
        "OLLAMA_API_KEY": "ollama-env-fallback-secret",
        "XAI_API_KEY": "xai-env-fallback-secret",
        "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1",
    }

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ=env,
        run_subprocess=lambda _args, **_kwargs: _completed(stdout="logged in\n"),
        http_get=_ollama_ok,
    )

    for name in ("github", "codex", "claude_code", "cursor", "gemini", "opencode", "grok"):
        provider = payload["providers"][name]
        assert provider["ok"] is True
        assert provider["credential_scope"] == "static_env_token"
        assert provider["isolation"] == "service_env"
        assert any(warning["reason"] == "STATIC_TOKEN_FALLBACK" for warning in provider["warnings"])
    serialized = json.dumps(payload, sort_keys=True)
    for secret in env.values():
        assert secret not in serialized


@pytest.mark.unit
def test_provider_readiness_docker_reports_host_daemon_broad_control_warning(
    tmp_path: Path,
) -> None:
    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    docker = payload["providers"]["docker"]
    assert docker["ok"] is True
    assert docker["status"] == "ok"
    assert docker["reason"] == "DOCKER_HOST_CONFIGURED"
    assert docker["credential_scope"] == "docker_host_control"
    assert docker["isolation"] == "host_daemon"
    assert any(warning["reason"] == "DOCKER_HOST_BROAD_CONTROL" for warning in docker["warnings"])


@pytest.mark.unit
def test_provider_readiness_docker_registry_auth_is_observed_not_read(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    docker_home = home / ".docker"
    docker_home.mkdir(parents=True)
    (docker_home / "config.json").write_text('{"auths":{"ghcr.io":{"auth":"docker_file_secret"}}}')
    env_auth = '{"auths":{"registry.example":{"auth":"docker_env_secret"}}}'

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"DOCKER_AUTH_CONFIG": env_auth},
        run_subprocess=_unexpected_subprocess,
    )

    docker = payload["providers"]["docker"]
    source_signals = {source["signal"] for source in docker["credential_sources"]}
    assert "DOCKER_AUTH_CONFIG" in source_signals
    assert "~/.docker/config.json" in source_signals
    serialized = json.dumps(payload, sort_keys=True)
    assert "docker_file_secret" not in serialized
    assert "docker_env_secret" not in serialized


@pytest.mark.unit
def test_provider_readiness_docker_reports_missing_auth_without_host_signal(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    result = provider_readiness._check_docker_provider(
        replace(_settings(tmp_path), docker_host=""),
        environ={},
        host_home=home,
        strict=True,
        secrets=frozenset(),
    )

    assert result["status"] == "fail"
    assert result["reason"] == "DOCKER_AUTH_NOT_OBSERVED"
    assert result["credential_scope"] == "not_observed"
    assert result["isolation"] == "none"


@pytest.mark.unit
def test_provider_readiness_docker_without_host_or_registry_warns(tmp_path: Path) -> None:
    payload = collect_agent_readiness(
        _settings(tmp_path, docker_host=""),
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    docker = payload["providers"]["docker"]
    assert docker["ok"] is False
    assert docker["status"] == "warn"
    assert docker["reason"] == "DOCKER_AUTH_NOT_OBSERVED"
    assert docker["credential_scope"] == "not_observed"
    assert docker["isolation"] == "none"


@pytest.mark.unit
def test_provider_readiness_docker_config_path_is_reported_without_reading_secret(
    tmp_path: Path,
) -> None:
    docker_config = tmp_path / "docker-config"
    docker_config.mkdir()
    (docker_config / "config.json").write_text(
        '{"auths":{"registry.example":{"auth":"docker_config_secret"}}}'
    )

    result = provider_readiness._check_docker_provider(
        replace(_settings(tmp_path), docker_host=""),
        environ={"DOCKER_CONFIG": str(docker_config)},
        host_home=tmp_path / "home",
        strict=False,
        secrets=frozenset({"docker_config_secret"}),
    )

    assert result["status"] == "ok"
    assert result["reason"] == "DOCKER_REGISTRY_AUTH_PRESENT"
    assert result["credential_scope"] == "read_only_host_path"
    assert result["isolation"] == "read_only_bind"
    assert "docker_config_secret" not in json.dumps(result, sort_keys=True)


@pytest.mark.unit
def test_provider_readiness_docker_config_path_reports_registry_auth(
    tmp_path: Path,
) -> None:
    docker_config = tmp_path / "docker-config"
    docker_config.mkdir()
    (docker_config / "config.json").write_text(
        '{"auths":{"registry.example":{"auth":"docker_config_secret"}}}'
    )

    payload = collect_agent_readiness(
        _settings(tmp_path, docker_host=""),
        environ={"DOCKER_CONFIG": str(docker_config)},
        run_subprocess=_unexpected_subprocess,
    )

    docker = payload["providers"]["docker"]
    assert docker["ok"] is True
    assert docker["reason"] == "DOCKER_REGISTRY_AUTH_PRESENT"
    assert docker["credential_scope"] == "read_only_host_path"
    assert docker["isolation"] == "read_only_bind"
    assert docker["credential_sources"] == [
        {
            "type": "path",
            "signal": "DOCKER_CONFIG/config.json",
            "credential_scope": "read_only_host_path",
            "isolation": "read_only_bind",
        }
    ]
    assert "docker_config_secret" not in json.dumps(payload, sort_keys=True)


@pytest.mark.unit
def test_provider_readiness_docker_config_env_does_not_fall_back_to_home(
    tmp_path: Path,
) -> None:
    docker_config = tmp_path / "missing-docker-config"
    home_docker = tmp_path / "home" / ".docker"
    home_docker.mkdir(parents=True)
    (home_docker / "config.json").write_text("home_docker_secret")

    result = provider_readiness._check_docker_provider(
        replace(_settings(tmp_path), docker_host=""),
        environ={"DOCKER_CONFIG": str(docker_config)},
        host_home=tmp_path / "home",
        strict=False,
        secrets=frozenset({"home_docker_secret"}),
    )

    assert result["status"] == "warn"
    assert result["reason"] == "DOCKER_AUTH_NOT_OBSERVED"
    assert "home_docker_secret" not in json.dumps(result, sort_keys=True)


@pytest.mark.unit
def test_provider_readiness_explicit_missing_docker_config_does_not_fallback(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    docker_home = home / ".docker"
    docker_home.mkdir(parents=True)
    (docker_home / "config.json").write_text(
        '{"auths":{"registry.example":{"auth":"docker_home_secret"}}}'
    )

    payload = collect_agent_readiness(
        _settings(tmp_path, host_home=str(home), docker_host=""),
        environ={"DOCKER_CONFIG": str(tmp_path / "missing-docker-config")},
        run_subprocess=_unexpected_subprocess,
    )

    docker = payload["providers"]["docker"]
    assert docker["ok"] is False
    assert docker["reason"] == "DOCKER_AUTH_NOT_OBSERVED"
    assert docker["credential_sources"] == []
    assert "docker_home_secret" not in json.dumps(payload, sort_keys=True)


@pytest.mark.unit
def test_ollama_model_probe_reports_missing_model_with_prior_failures() -> None:
    def _http_get(url: str, *, timeout: float) -> Any:
        assert timeout > 0
        if url.endswith("/bad"):
            return SimpleNamespace(status_code=503, text="offline")
        return SimpleNamespace(status_code=200, text='{"models":[{"name":"glm-5.1:cloud"}]}')

    result = provider_readiness._probe_ollama_model(
        ("http://ollama.local/bad", "http://ollama.local/api/tags"),
        model="kimi-k2.6:cloud",
        http_get=_http_get,
        secrets=frozenset(),
    )

    assert result["status"] == "fail"
    assert result["reason_code"] == "OLLAMA_MODEL_NOT_AVAILABLE"
    assert "probe_failures=" in result["detail"]


@pytest.mark.unit
def test_provider_readiness_security_summary_handles_malformed_warning_payloads() -> None:
    summary = provider_readiness._security_summary(
        {
            "github": {
                "status": "warn",
                "reason": "GITHUB_TOKEN_ENV_MISSING",
                "warnings": "not-a-list",
            },
            "codex": {"status": "ok", "warnings": ["bad-warning"]},
            "docker": {
                "status": "warn",
                "reason": "DOCKER_AUTH_NOT_OBSERVED",
                "warnings": [],
            },
        }
    )

    assert summary["status"] == "ok"
    assert summary["warning_count"] == 0
    assert summary["providers_with_warnings"] == ["github", "codex", "docker"]
    assert summary["reason_codes"] == [
        "DOCKER_AUTH_NOT_OBSERVED",
        "GITHUB_TOKEN_ENV_MISSING",
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("exc", "reason_code"),
    [
        (FileNotFoundError("docker missing"), "DOCKER_CLI_NOT_FOUND"),
        (
            subprocess.TimeoutExpired(cmd=["docker"], timeout=5),
            "CODEX_RUNTIME_CLI_PROBE_TIMEOUT",
        ),
        (RuntimeError("probe blew up sk-runtime-secret"), "CODEX_RUNTIME_CLI_PROBE_ERROR"),
    ],
)
def test_agent_runtime_cli_probe_reports_structured_failures(
    tmp_path: Path,
    exc: Exception,
    reason_code: str,
) -> None:
    def _run(_: list[str], **_kwargs: object) -> Any:
        raise exc

    result = provider_readiness._probe_agent_runtime_cli(
        _settings(tmp_path),
        executable="codex",
        provider="codex",
        environ={},
        run_subprocess=_run,
        secrets=frozenset({"sk-runtime-secret"}),
    )

    assert result["status"] == "fail"
    assert result["reason_code"] == reason_code
    assert "sk-runtime-secret" not in json.dumps(result, sort_keys=True)


@pytest.mark.unit
def test_cli_auth_probe_reports_success_and_nonzero_failure() -> None:
    ok = provider_readiness._probe_cli_auth_status(
        provider_label="Codex",
        args=["codex", "auth", "status"],
        failure_reason="CODEX_AUTH_FAILED",
        timeout_reason="CODEX_AUTH_TIMEOUT",
        missing_reason="CODEX_AUTH_CLI_MISSING",
        error_reason="CODEX_AUTH_ERROR",
        environ={},
        run_subprocess=lambda _args, **_kwargs: _completed(stdout="ok\n"),
        secrets=frozenset(),
    )
    failed = provider_readiness._probe_cli_auth_status(
        provider_label="Codex",
        args=["codex", "auth", "status"],
        failure_reason="CODEX_AUTH_FAILED",
        timeout_reason="CODEX_AUTH_TIMEOUT",
        missing_reason="CODEX_AUTH_CLI_MISSING",
        error_reason="CODEX_AUTH_ERROR",
        environ={},
        run_subprocess=lambda _args, **_kwargs: _completed(returncode=1, stderr="bad auth"),
        secrets=frozenset(),
    )

    assert ok == {"status": "ok", "reason_code": "CODEX_AUTH_OK"}
    assert failed["status"] == "fail"
    assert failed["reason_code"] == "CODEX_AUTH_FAILED"
    assert failed["detail"] == "bad auth"


@pytest.mark.unit
def test_redaction_parts_cover_urls_empty_secrets_existing_redactions_and_merges() -> None:
    redacted, parts = provider_readiness._redact_with_redaction_parts(
        "connect http://user:pass@example.test with sk-providersecret123456",
        frozenset({""}),
    )

    assert "user:pass" not in redacted
    assert "sk-providersecret123456" not in redacted
    assert parts is not None
    assert provider_readiness_helpers._slice_redaction_segments(
        [("literal", "before"), ("redaction", ""), ("literal", "after")],
        6,
        16,
    ) == [("redaction", "")]
    assert provider_readiness_helpers._merge_literal_redaction_segments(
        [("literal", ""), ("literal", "a"), ("literal", "b")]
    ) == [("literal", "ab")]
