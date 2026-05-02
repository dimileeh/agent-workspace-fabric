"""Provider credential readiness checks for local service mode."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import awf.service.provider_readiness as provider_readiness
from awf.service.config import ServiceSettings
from awf.service.provider_readiness import (
    ProviderReadinessError,
    collect_agent_readiness,
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
        database_url="sqlite+aiosqlite:///:memory:",
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
    assert url == "http://ollama.local:11434/api/version"
    return SimpleNamespace(status_code=200, text='{"version":"0.1.0"}')


@pytest.mark.unit
def test_provider_readiness_validates_aliases_and_rejects_unknown() -> None:
    assert validate_provider_names(["claude", "opencode", "codex", "docker", ""]) == {
        "claude_code",
        "opencode",
        "codex",
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
        "gemini",
        "opencode",
        "docker",
    }


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
    gemini_secret = "gemini_green_secret"
    ollama_secret = "ollama_green_secret"
    env = {
        "AWF_GITHUB_TOKEN": github_secret,
        "ANTHROPIC_API_KEY": anthropic_secret,
        "GEMINI_API_KEY": gemini_secret,
        "OLLAMA_API_KEY": ollama_secret,
        "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1",
    }
    subprocess_calls: list[list[str]] = []

    def _run(args: list[str], **kwargs: object) -> Any:
        subprocess_calls.append(args)
        assert args == ["gh", "auth", "status", "--hostname", "github.com"]
        assert github_secret not in args
        subprocess_env = kwargs["env"]
        assert isinstance(subprocess_env, dict)
        assert subprocess_env["GH_TOKEN"] == github_secret
        return _completed(stdout="logged in\n")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ=env,
        run_subprocess=_run,
        http_get=_ollama_ok,
    )

    assert payload["status"] == "ok"
    providers = payload["providers"]
    assert set(providers) == {"github", "codex", "claude_code", "gemini", "opencode", "docker"}
    assert all(provider["ok"] is True for provider in providers.values())
    assert providers["github"]["capabilities"] == ["pr_create", "comment", "merge"]
    assert subprocess_calls == [["gh", "auth", "status", "--hostname", "github.com"]]
    serialized = json.dumps(payload, sort_keys=True)
    for secret in (
        github_secret,
        "codex_file_secret",
        anthropic_secret,
        gemini_secret,
        ollama_secret,
    ):
        assert secret not in serialized


@pytest.mark.unit
def test_provider_readiness_codex_isolated_file_auth_reports_least_privilege(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True)
    (codex_home / "auth.json").write_text('{"token":"codex_file_secret"}')
    (codex_home / "config.toml").write_text("model = 'gpt-5.5'\n")
    (codex_home / "installation_id").write_text("installation-secret\n")
    (codex_home / "sessions").mkdir()
    (codex_home / "sessions" / "session.jsonl").write_text("session-secret\n")
    (codex_home / "logs_2.sqlite").write_text("log-secret\n")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    codex = payload["providers"]["codex"]
    assert codex["ok"] is True
    assert codex["status"] == "ok"
    assert codex["reason"] == "CODEX_FILE_AUTH_PRESENT"
    assert codex["credential_scope"] == "isolated_workspace"
    assert codex["isolation"] == "per_workspace_copy"
    assert codex["warnings"] == []
    assert {
        source["signal"]
        for source in codex["credential_sources"]
    } >= {"~/.codex/auth.json", "~/.codex/config.toml", "~/.codex/installation_id"}
    serialized = json.dumps(payload, sort_keys=True)
    for secret in (
        "codex_file_secret",
        "installation-secret",
        "session-secret",
        "log-secret",
    ):
        assert secret not in serialized


@pytest.mark.unit
def test_provider_readiness_codex_rules_directory_is_reported(tmp_path: Path) -> None:
    codex_home = tmp_path / "home" / ".codex"
    (codex_home / "rules").mkdir(parents=True)

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    codex = payload["providers"]["codex"]
    assert codex["reason"] == "CODEX_FILE_AUTH_PRESENT"
    assert "~/.codex/rules" in {
        source["signal"] for source in codex["credential_sources"]
    }


@pytest.mark.unit
def test_provider_readiness_codex_empty_directory_is_reported(tmp_path: Path) -> None:
    (tmp_path / "home" / ".codex").mkdir(parents=True)

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    codex = payload["providers"]["codex"]
    assert codex["reason"] == "CODEX_FILE_AUTH_PRESENT"
    assert codex["credential_sources"] == [
        {
            "type": "path",
            "signal": "~/.codex",
            "credential_scope": "isolated_workspace",
            "isolation": "per_workspace_copy",
        }
    ]


@pytest.mark.unit
def test_provider_readiness_codex_static_env_auth_warns_without_leaking_value(
    tmp_path: Path,
) -> None:
    token = "sk-proj-codex-env-secret"

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"OPENAI_API_KEY": token},
        run_subprocess=_unexpected_subprocess,
    )

    codex = payload["providers"]["codex"]
    assert codex["ok"] is True
    assert codex["status"] == "ok"
    assert codex["reason"] == "CODEX_ENV_AUTH_PRESENT"
    assert codex["credential_scope"] == "static_env_token"
    assert codex["isolation"] == "service_env"
    assert codex["credential_sources"] == [
        {
            "type": "env",
            "signal": "OPENAI_API_KEY",
            "credential_scope": "static_env_token",
            "isolation": "service_env",
        }
    ]
    assert {warning["reason"] for warning in codex["warnings"]} == {
        "STATIC_TOKEN_FALLBACK"
    }
    serialized = json.dumps(payload, sort_keys=True)
    assert token not in serialized
    assert "OPENAI_API_KEY" in serialized


@pytest.mark.unit
def test_provider_readiness_codex_missing_warns_by_default_and_fails_when_strict(
    tmp_path: Path,
) -> None:
    default_payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    default_codex = default_payload["providers"]["codex"]
    assert default_payload["status"] == "ok"
    assert default_codex["ok"] is False
    assert default_codex["status"] == "warn"
    assert default_codex["reason"] == "CODEX_AUTH_MISSING"
    assert default_codex["credential_scope"] == "not_observed"
    assert default_codex["isolation"] == "none"

    strict_payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={},
        strict_providers={"codex"},
        run_subprocess=_unexpected_subprocess,
    )

    strict_codex = strict_payload["providers"]["codex"]
    assert strict_payload["status"] == "fail"
    assert strict_payload["strict_providers"] == ["codex"]
    assert strict_codex["status"] == "fail"
    assert strict_codex["reason"] == "CODEX_AUTH_MISSING"


@pytest.mark.unit
def test_provider_readiness_existing_file_providers_report_credential_scope(
    tmp_path: Path,
) -> None:
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
    env = {
        "AWF_GITHUB_TOKEN": "ghp_env_fallback_secret",
        "OPENAI_API_KEY": "sk-proj-codex-fallback-secret",
        "ANTHROPIC_API_KEY": "sk-ant-env-fallback-secret",
        "GEMINI_API_KEY": "gemini-env-fallback-secret",
        "OLLAMA_API_KEY": "ollama-env-fallback-secret",
        "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1",
    }

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ=env,
        run_subprocess=lambda _args, **_kwargs: _completed(stdout="logged in\n"),
        http_get=_ollama_ok,
    )

    for name in ("github", "codex", "claude_code", "gemini", "opencode"):
        provider = payload["providers"][name]
        assert provider["ok"] is True
        assert provider["credential_scope"] == "static_env_token"
        assert provider["isolation"] == "service_env"
        assert any(
            warning["reason"] == "STATIC_TOKEN_FALLBACK"
            for warning in provider["warnings"]
        )
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
    assert any(
        warning["reason"] == "DOCKER_HOST_BROAD_CONTROL"
        for warning in docker["warnings"]
    )


@pytest.mark.unit
def test_provider_readiness_docker_registry_auth_is_observed_not_read(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    docker_home = home / ".docker"
    docker_home.mkdir(parents=True)
    (docker_home / "config.json").write_text(
        '{"auths":{"ghcr.io":{"auth":"docker_file_secret"}}}'
    )
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
        source["signal"]
        for source in rules_payload["providers"]["codex"]["credential_sources"]
    } == {"~/.codex/rules"}
    assert {
        source["signal"]
        for source in empty_payload["providers"]["codex"]["credential_sources"]
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
def test_provider_readiness_primary_scope_and_isolation_fallbacks() -> None:
    assert provider_readiness._primary_credential_scope(
        [{"credential_scope": "unknown"}]
    ) == "not_observed"
    assert provider_readiness._primary_isolation([{"isolation": "unknown"}]) == "none"


@pytest.mark.unit
def test_provider_readiness_missing_github_token_warns_by_default(tmp_path: Path) -> None:
    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    github = payload["providers"]["github"]
    assert github["status"] == "warn"
    assert github["ok"] is False
    assert github["reason"] == "GITHUB_TOKEN_ENV_MISSING"
    assert payload["status"] == "ok"


@pytest.mark.unit
def test_provider_readiness_github_strict_missing_token_fails(tmp_path: Path) -> None:
    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={},
        strict_providers={"github"},
        run_subprocess=_unexpected_subprocess,
    )

    github = payload["providers"]["github"]
    assert github["status"] == "fail"
    assert github["ok"] is False
    assert github["reason"] == "GITHUB_TOKEN_ENV_MISSING"
    assert payload["status"] == "fail"
    assert payload["strict_providers"] == ["github"]


@pytest.mark.unit
def test_provider_readiness_keyring_only_github_warning_is_actionable(tmp_path: Path) -> None:
    home = tmp_path / "home"
    gh_config = home / ".config" / "gh"
    gh_config.mkdir(parents=True)
    (gh_config / "hosts.yml").write_text("oauth_token: ghp_file_secret")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    github = payload["providers"]["github"]
    assert github["reason"] == "GITHUB_KEYRING_ONLY_NOT_VISIBLE_IN_COMPOSE"
    assert "AWF_GITHUB_TOKEN" in str(github)
    assert "GH_TOKEN" in str(github)
    assert "ghp_file_secret" not in json.dumps(payload, sort_keys=True)


@pytest.mark.unit
def test_provider_readiness_empty_host_home_defaults_to_user_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text('{"token":"claude_file_secret"}')
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(home))

    payload = collect_agent_readiness(
        _settings(tmp_path, host_home=""),
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    claude = payload["providers"]["claude_code"]
    assert claude["ok"] is True
    assert claude["reason"] == "CLAUDE_FILE_AUTH_PRESENT"
    assert "claude_file_secret" not in json.dumps(payload, sort_keys=True)


@pytest.mark.unit
def test_provider_readiness_github_settings_token_cli_missing(tmp_path: Path) -> None:
    github_secret = "github_pat_settings_secret"

    def _run(args: list[str], **_kwargs: object) -> Any:
        assert args == ["gh", "auth", "status", "--hostname", "github.com"]
        raise FileNotFoundError("gh")

    payload = collect_agent_readiness(
        _settings(tmp_path, github_token=github_secret),
        environ={},
        run_subprocess=_run,
    )

    github = payload["providers"]["github"]
    assert github["reason"] == "GITHUB_CLI_NOT_FOUND"
    assert github["signals"] == ["AWF_GITHUB_TOKEN"]
    assert github_secret not in json.dumps(payload, sort_keys=True)


@pytest.mark.unit
def test_provider_readiness_github_auth_timeout(tmp_path: Path) -> None:
    def _run(args: list[str], **_kwargs: object) -> Any:
        raise subprocess.TimeoutExpired(args, timeout=5)

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"AWF_GITHUB_TOKEN": "ghp_timeout_secret"},
        run_subprocess=_run,
    )

    github = payload["providers"]["github"]
    assert github["reason"] == "GITHUB_AUTH_TIMEOUT"
    assert "ghp_timeout_secret" not in json.dumps(payload, sort_keys=True)


@pytest.mark.unit
def test_provider_readiness_github_runner_exception_is_redacted(tmp_path: Path) -> None:
    def _run(_args: list[str], **_kwargs: object) -> Any:
        raise RuntimeError("transport failed for ghp_exception_secret")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"AWF_GITHUB_TOKEN": "ghp_exception_secret"},
        run_subprocess=_run,
    )

    github = payload["providers"]["github"]
    assert github["reason"] == "GITHUB_AUTH_UNUSABLE"
    serialized = json.dumps(payload, sort_keys=True)
    assert "ghp_exception_secret" not in serialized
    assert "<redacted>" in serialized


@pytest.mark.unit
def test_provider_readiness_claude_env_present(tmp_path: Path) -> None:
    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"ANTHROPIC_API_KEY": "anthropic_secret"},
        run_subprocess=_unexpected_subprocess,
    )

    claude = payload["providers"]["claude_code"]
    assert claude["ok"] is True
    assert claude["reason"] == "CLAUDE_ENV_AUTH_PRESENT"
    assert claude["signals"] == ["ANTHROPIC_API_KEY"]
    assert "anthropic_secret" not in json.dumps(payload, sort_keys=True)


@pytest.mark.unit
def test_provider_readiness_claude_file_present(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text('{"token":"claude_file_secret"}')

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    claude = payload["providers"]["claude_code"]
    assert claude["ok"] is True
    assert claude["reason"] == "CLAUDE_FILE_AUTH_PRESENT"
    assert "claude_file_secret" not in json.dumps(payload, sort_keys=True)


@pytest.mark.unit
def test_provider_readiness_gemini_file_present(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".gemini").mkdir(parents=True)
    (home / ".gemini" / "oauth_creds.json").write_text("gemini_file_secret")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={},
        run_subprocess=_unexpected_subprocess,
    )

    gemini = payload["providers"]["gemini"]
    assert gemini["ok"] is True
    assert gemini["reason"] == "GEMINI_FILE_AUTH_PRESENT"
    assert "gemini_file_secret" not in json.dumps(payload, sort_keys=True)


@pytest.mark.unit
def test_provider_readiness_gemini_google_application_credentials_visible(
    tmp_path: Path,
) -> None:
    credentials = tmp_path / "google.json"
    credentials.write_text("google_file_secret")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"GOOGLE_APPLICATION_CREDENTIALS": str(credentials)},
        run_subprocess=_unexpected_subprocess,
    )

    gemini = payload["providers"]["gemini"]
    assert gemini["ok"] is True
    assert gemini["reason"] == "GEMINI_ENV_AUTH_PRESENT"
    assert gemini["signals"] == ["GOOGLE_APPLICATION_CREDENTIALS"]
    assert "google_file_secret" not in json.dumps(payload, sort_keys=True)


@pytest.mark.unit
def test_provider_readiness_gemini_missing_google_application_credentials_is_actionable(
    tmp_path: Path,
) -> None:
    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"GOOGLE_APPLICATION_CREDENTIALS": str(tmp_path / "missing.json")},
        run_subprocess=_unexpected_subprocess,
    )

    gemini = payload["providers"]["gemini"]
    assert gemini["ok"] is False
    assert gemini["reason"] == "GEMINI_AUTH_MISSING"
    assert gemini["signals"] == ["GOOGLE_APPLICATION_CREDENTIALS"]
    assert "file is not visible" in gemini["message"]


@pytest.mark.unit
def test_provider_readiness_opencode_ollama_file_present(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)
    (home / ".config" / "opencode" / "config.json").write_text("opencode_file_secret")
    (home / ".ollama" / "models").mkdir(parents=True)
    (home / ".ollama" / "config.json").write_text("ollama_file_secret")
    (home / ".ollama" / "models" / "blob").write_text("model_blob_secret")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1"},
        run_subprocess=_unexpected_subprocess,
        http_get=_ollama_ok,
    )

    opencode = payload["providers"]["opencode"]
    assert opencode["ok"] is True
    assert opencode["reason"] == "OPENCODE_FILE_AUTH_PRESENT"
    serialized = json.dumps(payload, sort_keys=True)
    assert "opencode_file_secret" not in serialized
    assert "ollama_file_secret" not in serialized
    assert "model_blob_secret" not in serialized


@pytest.mark.unit
def test_provider_readiness_opencode_ollama_unreachable_fails_when_strict(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)

    def _http_get(_url: str, *, timeout: float) -> Any:
        assert timeout > 0
        raise RuntimeError("connection refused")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1"},
        strict_providers={"opencode"},
        run_subprocess=_unexpected_subprocess,
        http_get=_http_get,
    )

    opencode = payload["providers"]["opencode"]
    assert opencode["status"] == "fail"
    assert opencode["reason"] == "OLLAMA_HOST_UNREACHABLE"
    assert "connection refused" in opencode["detail"]


@pytest.mark.unit
def test_provider_readiness_opencode_ollama_redirect_fails(tmp_path: Path) -> None:
    def _http_get(_url: str, *, timeout: float) -> Any:
        assert timeout > 0
        return SimpleNamespace(status_code=302, text="redirect to login")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={
            "OLLAMA_API_KEY": "ollama_env_secret",
            "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1",
        },
        strict_providers={"opencode"},
        run_subprocess=_unexpected_subprocess,
        http_get=_http_get,
    )

    opencode = payload["providers"]["opencode"]
    assert opencode["status"] == "fail"
    assert opencode["reason"] == "OLLAMA_HOST_UNREACHABLE"
    assert opencode["detail"] == "HTTP 302: redirect to login"


@pytest.mark.unit
def test_provider_readiness_opencode_ollama_file_reason_without_opencode_config(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".ollama").mkdir(parents=True)
    (home / ".ollama" / "id_ed25519").write_text("ollama_private_key_secret")
    urls: list[str] = []

    def _http_get(url: str, *, timeout: float) -> Any:
        urls.append(url)
        return SimpleNamespace(status_code=200, text="ok")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"OLLAMA_HOST": "ollama.local:11434"},
        run_subprocess=_unexpected_subprocess,
        http_get=_http_get,
    )

    opencode = payload["providers"]["opencode"]
    assert opencode["ok"] is True
    assert opencode["reason"] == "OLLAMA_FILE_AUTH_PRESENT"
    assert urls == ["http://ollama.local:11434/api/version"]
    assert "ollama_private_key_secret" not in json.dumps(payload, sort_keys=True)


@pytest.mark.unit
def test_provider_readiness_opencode_default_host_gateway_falls_back_to_localhost(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)
    urls: list[str] = []

    def _http_get(url: str, *, timeout: float) -> Any:
        urls.append(url)
        if url == "http://host.docker.internal:11434/api/version":
            raise RuntimeError("nodename nor servname provided")
        return SimpleNamespace(status_code=200, text="ok")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={},
        run_subprocess=_unexpected_subprocess,
        http_get=_http_get,
    )

    opencode = payload["providers"]["opencode"]
    assert opencode["ok"] is True
    assert opencode["reason"] == "OPENCODE_FILE_AUTH_PRESENT"
    assert urls == [
        "http://host.docker.internal:11434/api/version",
        "http://localhost:11434/api/version",
    ]


@pytest.mark.unit
def test_provider_readiness_opencode_env_only_reason_when_ollama_reachable(
    tmp_path: Path,
) -> None:
    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={
            "OLLAMA_API_KEY": "ollama_env_secret",
            "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1",
        },
        run_subprocess=_unexpected_subprocess,
        http_get=_ollama_ok,
    )

    opencode = payload["providers"]["opencode"]
    assert opencode["ok"] is True
    assert opencode["reason"] == "OLLAMA_ENV_AUTH_PRESENT"
    assert "ollama_env_secret" not in json.dumps(payload, sort_keys=True)


@pytest.mark.unit
def test_provider_readiness_opencode_http_error_detail_is_redacted(tmp_path: Path) -> None:
    def _http_get(_url: str, *, timeout: float) -> Any:
        return SimpleNamespace(status_code=401, text="bad token ghp_ollama_secret")

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={
            "OLLAMA_API_KEY": "ghp_ollama_secret",
            "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.local:11434/v1",
        },
        run_subprocess=_unexpected_subprocess,
        http_get=_http_get,
    )

    opencode = payload["providers"]["opencode"]
    assert opencode["reason"] == "OLLAMA_HOST_UNREACHABLE"
    serialized = json.dumps(payload, sort_keys=True)
    assert "ghp_ollama_secret" not in serialized
    assert "<redacted>" in serialized


@pytest.mark.unit
def test_provider_readiness_truncates_verbose_details(tmp_path: Path) -> None:
    def _run(_args: list[str], **_kwargs: object) -> Any:
        return _completed(returncode=1, stderr="failure " * 50)

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"AWF_GITHUB_TOKEN": "ghp_verbose_secret"},
        run_subprocess=_run,
    )

    detail = payload["providers"]["github"]["detail"]
    assert isinstance(detail, str)
    assert len(detail) == 240
    assert detail.endswith("\N{HORIZONTAL ELLIPSIS}")


@pytest.mark.unit
def test_provider_readiness_default_subprocess_and_http_wrappers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = _completed(stdout="ok")
    calls: list[tuple[list[str], float]] = []

    def _subprocess_run(args: list[str], **kwargs: object) -> Any:
        calls.append((args, kwargs["timeout"]))
        return completed

    def _httpx_get(url: str, *, timeout: float) -> Any:
        assert url == "http://example.test/api/version"
        assert timeout == 1.5
        return SimpleNamespace(status_code=200, text="ok")

    monkeypatch.setattr(provider_readiness.subprocess, "run", _subprocess_run)
    monkeypatch.setattr(provider_readiness.httpx, "get", _httpx_get)

    assert provider_readiness._run_subprocess(
        ["gh", "auth", "status"],
        check=False,
        capture_output=True,
        text=True,
        timeout=1.5,
        env={},
    ) is completed
    assert calls == [(["gh", "auth", "status"], 1.5)]
    assert provider_readiness._http_get("http://example.test/api/version", timeout=1.5).text == "ok"


@pytest.mark.unit
@pytest.mark.parametrize(
    "secret",
    [
        "ghp_providerreadinesssecret",
        "gho_providerreadinesssecret",
        "github_pat_providerreadinesssecret",
        "sk-proj-provider-readiness-secret",
        "sk-ant-provider-readiness-secret",
        "sk-providerReadinessSecret1234567890",
        "AIzaProviderReadinessSecret",
        "xoxb-provider-readiness-secret",
    ],
)
def test_provider_readiness_redacts_known_token_patterns(secret: str) -> None:
    assert provider_readiness._redact(f"token {secret}", frozenset()) == "token <redacted>"


@pytest.mark.unit
@pytest.mark.parametrize("identifier", ["sk-live-abc12345", "sk-test-abc12345"])
def test_provider_readiness_preserves_non_secret_sk_identifiers(identifier: str) -> None:
    assert provider_readiness._redact(f"diagnostic {identifier}", frozenset()) == (
        f"diagnostic {identifier}"
    )


@pytest.mark.unit
def test_provider_readiness_redacts_secret_values_from_details(tmp_path: Path) -> None:
    github_secret = "ghp_super_secret"
    env = {
        "AWF_GITHUB_TOKEN": github_secret,
        "ANTHROPIC_API_KEY": "anthropic_secret",
        "GEMINI_API_KEY": "gemini_secret",
    }

    def _run(args: list[str], **_kwargs: object) -> Any:
        assert args == ["gh", "auth", "status", "--hostname", "github.com"]
        return _completed(
            returncode=1,
            stderr=(
                "failed cloning https://user:ghp_super_secret@github.com/org/repo "
                "with bearer anthropic_secret and gemini_secret"
            ),
        )

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ=env,
        run_subprocess=_run,
    )

    github = payload["providers"]["github"]
    assert github["reason"] == "GITHUB_AUTH_UNUSABLE"
    serialized = json.dumps(payload, sort_keys=True)
    assert github_secret not in serialized
    assert "anthropic_secret" not in serialized
    assert "gemini_secret" not in serialized
    assert "<redacted>" in serialized


@pytest.mark.unit
def test_provider_readiness_preserves_long_diagnostic_ids_in_details(
    tmp_path: Path,
) -> None:
    github_secret = "ghp_diagnostic_secret"
    image_digest = "a" * 64
    container_id = "b" * 40
    error_payload_id = "payload_" + ("c" * 40)

    def _run(args: list[str], **_kwargs: object) -> Any:
        assert args == ["gh", "auth", "status", "--hostname", "github.com"]
        return _completed(
            returncode=1,
            stderr=(
                f"failed for token {github_secret}; "
                f"image sha256:{image_digest}; "
                f"container {container_id}; "
                f"payload {error_payload_id}"
            ),
        )

    payload = collect_agent_readiness(
        _settings(tmp_path),
        environ={"AWF_GITHUB_TOKEN": github_secret},
        run_subprocess=_run,
    )

    detail = payload["providers"]["github"]["detail"]
    assert github_secret not in detail
    assert "<redacted>" in detail
    assert image_digest in detail
    assert container_id in detail
    assert error_payload_id in detail


@pytest.mark.unit
def test_provider_readiness_helper_fallbacks_handle_unknown_shapes() -> None:
    assert provider_readiness._primary_credential_scope(
        [{"credential_scope": "custom_scope"}]
    ) == "not_observed"
    assert provider_readiness._primary_isolation([{"isolation": "custom_isolation"}]) == "none"
    assert provider_readiness._provider_warning_values({"warnings": "not-a-list"}) == []

    summary = provider_readiness._security_summary(
        {
            "github": {"status": "warn", "reason": "GITHUB_TOKEN_ENV_MISSING"},
            "codex": {"status": "ok", "warnings": ["ignored"]},
            "docker": {
                "status": "ok",
                "warnings": [
                    {"reason": "DOCKER_HOST_BROAD_CONTROL", "severity": "warning"}
                ],
            },
        }
    )

    assert summary["status"] == "warning"
    assert summary["warning_count"] == 1
    assert summary["providers_with_warnings"] == ["github", "codex", "docker"]
    assert summary["reason_codes"] == [
        "DOCKER_HOST_BROAD_CONTROL",
        "GITHUB_TOKEN_ENV_MISSING",
    ]
