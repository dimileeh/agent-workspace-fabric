"""Service configuration tests."""

from __future__ import annotations

import json

import pytest

from awf.common.config import Settings
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
