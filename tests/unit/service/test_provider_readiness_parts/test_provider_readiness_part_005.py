"""Codex provider credential readiness checks for local service mode."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from awf.service.provider_readiness import collect_agent_readiness

from .test_provider_readiness_part_001 import (
    _settings,
    _unexpected_subprocess,
)


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
    (codex_home / "logs_2.db").write_text("log-secret\n")

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
    assert {source["signal"] for source in codex["credential_sources"]} >= {
        "~/.codex/auth.json",
        "~/.codex/config.toml",
        "~/.codex/installation_id",
    }
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
    assert "~/.codex/rules" in {source["signal"] for source in codex["credential_sources"]}


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
    assert {warning["reason"] for warning in codex["warnings"]} == {"STATIC_TOKEN_FALLBACK"}
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
