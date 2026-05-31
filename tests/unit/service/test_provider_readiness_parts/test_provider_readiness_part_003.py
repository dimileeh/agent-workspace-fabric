"""Additional provider credential readiness checks for local service mode."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import awf.service.provider_readiness as provider_readiness
from awf.service.provider_readiness import collect_agent_readiness

from .test_provider_readiness_part_001 import _settings, _unexpected_subprocess


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
