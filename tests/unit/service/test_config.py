"""Service configuration tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from awf.common.config import DEFAULT_MIN_FREE_DISK_BYTES, Settings
from awf.service.config import (
    _redact_database_url,
    resolve_service_settings,
    service_config_payload,
)


@pytest.mark.unit
def test_agent_watchdog_defaults_are_conservative_and_documented_in_payload() -> None:
    settings = resolve_service_settings(Settings(_env_file=None), environ={})
    payload = service_config_payload(settings)
    rendered = json.dumps(payload)

    assert settings.agent_wall_timeout_seconds == 7200
    assert settings.agent_idle_timeout_seconds == 900
    assert payload["agent_wall_timeout_seconds"] == 7200
    assert payload["agent_idle_timeout_seconds"] == 900
    assert "agent_wall_timeout_seconds" in rendered
    assert "agent_idle_timeout_seconds" in rendered


@pytest.mark.unit
def test_agent_watchdog_settings_flow_from_settings_to_service_settings() -> None:
    base = Settings(
        _env_file=None,
        agent_wall_timeout_seconds=1234,
        agent_idle_timeout_seconds=56,
    )

    settings = resolve_service_settings(base, environ={})

    assert settings.agent_wall_timeout_seconds == 1234
    assert settings.agent_idle_timeout_seconds == 56


@pytest.mark.unit
def test_min_free_disk_threshold_defaults_to_conservative_10_gib_payload() -> None:
    settings = resolve_service_settings(Settings(_env_file=None), environ={})
    payload = service_config_payload(settings)

    assert DEFAULT_MIN_FREE_DISK_BYTES == 10 * 1024 * 1024 * 1024
    assert settings.min_free_disk_bytes == DEFAULT_MIN_FREE_DISK_BYTES
    assert payload["min_free_disk_bytes"] == DEFAULT_MIN_FREE_DISK_BYTES


@pytest.mark.unit
def test_min_free_disk_threshold_flows_from_settings_to_service_settings() -> None:
    base = Settings(_env_file=None, min_free_disk_bytes=123456)

    settings = resolve_service_settings(base, environ={"AWF_MIN_FREE_DISK_BYTES": "123456"})

    assert settings.min_free_disk_bytes == 123456


@pytest.mark.unit
def test_local_service_config_resolves_stable_worker_node_id_in_payload() -> None:
    settings = resolve_service_settings(Settings(_env_file=None), environ={})
    payload = service_config_payload(settings)

    assert settings.node_id == "local"
    assert payload["node_id"] == "local"


@pytest.mark.unit
def test_explicit_worker_node_id_is_preserved_for_non_local_multi_node_deployments() -> None:
    base = Settings(_env_file=None, worker_node_id="prod-node-a")

    settings = resolve_service_settings(
        base,
        environ={"AWF_WORKER_NODE_ID": "prod-node-a"},
    )

    assert settings.node_id == "prod-node-a"


@pytest.mark.unit
def test_local_service_accepts_standard_gh_token_fallback() -> None:
    settings = resolve_service_settings(
        Settings(_env_file=None),
        environ={"GH_TOKEN": "ghp_service_token"},
    )

    assert settings.github_token == "ghp_service_token"


@pytest.mark.unit
def test_awf_github_token_precedes_standard_gh_token_fallback() -> None:
    settings = resolve_service_settings(
        Settings(_env_file=None, github_token="ghp_awf_token"),
        environ={
            "AWF_GITHUB_TOKEN": "ghp_awf_token",
            "GH_TOKEN": "ghp_standard_token",
        },
    )

    assert settings.github_token == "ghp_awf_token"


@pytest.mark.unit
def test_redact_database_url_handles_malformed_secret_values() -> None:
    assert _redact_database_url("postgresql://user:secret@host:bad/db") == "<redacted>"
    assert _redact_database_url("") == ""


@pytest.mark.unit
def test_local_service_compose_sets_stable_worker_node_id_for_control_plane_services() -> None:
    compose_path = Path(__file__).resolve().parents[3] / "docker" / "compose" / "local-service.yml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    for service_name in ("api", "worker", "migrate"):
        service = compose["services"][service_name]
        assert service["environment"]["AWF_WORKER_NODE_ID"] == "local"
        assert service["environment"]["AWF_GITHUB_TOKEN"].startswith("${AWF_GITHUB_TOKEN:-")
        assert service["environment"]["GH_TOKEN"].startswith("${AWF_GITHUB_TOKEN:-")
        assert service["environment"]["GITHUB_TOKEN"].startswith("${AWF_GITHUB_TOKEN:-")
