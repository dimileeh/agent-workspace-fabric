"""Service configuration tests."""

from __future__ import annotations

import json

import pytest

from awf.common.config import DEFAULT_MIN_FREE_DISK_BYTES, Settings
from awf.service.config import resolve_service_settings, service_config_payload


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
