"""The OpenRouter management key stays in API/worker settings memory only."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from awf.common.config import Settings
from awf.service.config import resolve_service_settings, service_config_payload
from awf.service.openrouter_management import (
    OPENROUTER_MANAGEMENT_KEY_ENV,
    scrub_openrouter_management_environ,
)

_SENTINEL = "sk-or-mgmt-sentinel-0123456789"


def test_settings_read_the_unprefixed_service_variable_and_hide_it(monkeypatch):
    monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", _SENTINEL)
    monkeypatch.setenv("AWF_OPENROUTER_WORKSPACE_LIMIT_USD", "25.5")

    settings = Settings(_env_file=None)

    assert settings.openrouter_management_key == _SENTINEL
    assert settings.openrouter_workspace_limit_usd == 25.5
    assert _SENTINEL not in repr(settings)
    assert _SENTINEL not in str(settings)


def test_blank_management_key_is_unset_and_defaults_are_bounded(monkeypatch):
    monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", "  ")
    monkeypatch.delenv("AWF_OPENROUTER_WORKSPACE_LIMIT_USD", raising=False)

    settings = Settings(_env_file=None)

    assert settings.openrouter_management_key is None
    assert settings.openrouter_workspace_limit_usd == 10.0
    assert settings.openrouter_usage_poll_interval_seconds == 120.0
    assert settings.openrouter_key_reconcile_interval_seconds == 900.0
    for invalid in ("0", "-1", "1000000"):
        monkeypatch.setenv("AWF_OPENROUTER_WORKSPACE_LIMIT_USD", invalid)
        with pytest.raises(ValueError):
            Settings(_env_file=None)


def test_service_settings_mirror_the_key_and_redact_it_in_payloads(monkeypatch):
    monkeypatch.setenv("OPENROUTER_MANAGEMENT_KEY", _SENTINEL)

    service = resolve_service_settings(Settings(_env_file=None), environ={})

    assert service.openrouter_management_key == _SENTINEL
    assert _SENTINEL not in repr(service)
    payload = service_config_payload(service)
    assert payload["openrouter_management_key"] == "<redacted>"
    assert _SENTINEL not in str(payload)
    assert payload["openrouter_workspace_limit_usd"] == 10.0


def test_scrub_removes_the_variable_from_the_process_environment(monkeypatch):
    monkeypatch.setenv(OPENROUTER_MANAGEMENT_KEY_ENV, _SENTINEL)
    settings = Settings(_env_file=None)

    scrub_openrouter_management_environ()

    assert OPENROUTER_MANAGEMENT_KEY_ENV not in os.environ
    assert settings.openrouter_management_key == _SENTINEL
    # Idempotent when the variable is already gone.
    scrub_openrouter_management_environ()


def _environment(service: dict) -> dict[str, str]:
    raw = service.get("environment") or []
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    return dict(item.split("=", 1) if "=" in item else (item, "") for item in raw)


def test_compose_forwards_management_settings_to_api_and_worker_only():
    services = yaml.safe_load(Path("docker/compose/local-service.yml").read_text())["services"]
    for name in ("api", "worker"):
        environment = _environment(services[name])
        assert environment["OPENROUTER_MANAGEMENT_KEY"] == "${OPENROUTER_MANAGEMENT_KEY:-}"
        assert (
            environment["AWF_OPENROUTER_WORKSPACE_LIMIT_USD"]
            == "${AWF_OPENROUTER_WORKSPACE_LIMIT_USD:-}"
        )
    for name, service in services.items():
        if name in {"api", "worker"}:
            continue
        rendered = yaml.safe_dump(service)
        assert "OPENROUTER_MANAGEMENT_KEY" not in rendered, name
        assert "AWF_OPENROUTER_WORKSPACE_LIMIT_USD" not in rendered, name
    migrate = _environment(services["migrate"])
    assert set(migrate) == {"AWF_SERVICE_NAME", "AWF_ENV", "AWF_LOG_LEVEL", "AWF_DATABASE_URL"}
    assert migrate["AWF_DATABASE_URL"] == _environment(services["api"])["AWF_DATABASE_URL"]


def test_workspace_stack_template_never_references_the_management_key():
    template = Path("docker/compose/workspace.base.yml.j2").read_text()
    assert "OPENROUTER_MANAGEMENT_KEY" not in template


def test_blank_compose_forwarded_limit_keeps_the_default(monkeypatch):
    monkeypatch.setenv("AWF_OPENROUTER_WORKSPACE_LIMIT_USD", "")

    assert Settings(_env_file=None).openrouter_workspace_limit_usd == 10.0


class _Engine:
    async def dispose(self) -> None:
        return None


async def test_api_lifespan_scrubs_the_key_after_settings_resolve(monkeypatch):
    from fastapi import FastAPI

    from awf.api import app as app_module

    monkeypatch.setenv(OPENROUTER_MANAGEMENT_KEY_ENV, _SENTINEL)
    settings = Settings(_env_file=None)
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    monkeypatch.setattr(app_module, "make_engine", lambda _url: _Engine())
    monkeypatch.setattr(app_module, "make_session_factory", lambda _engine: object())

    async def _not_ahead(_engine):
        return None

    monkeypatch.setattr(app_module, "verify_database_schema_not_ahead", _not_ahead)
    async with app_module._lifespan(FastAPI()):
        assert OPENROUTER_MANAGEMENT_KEY_ENV not in os.environ
    assert settings.openrouter_management_key == _SENTINEL


async def test_run_worker_scrubs_the_key_before_building_the_runtime(monkeypatch):
    from types import SimpleNamespace

    from awf.service import worker as worker_mod

    monkeypatch.setenv(OPENROUTER_MANAGEMENT_KEY_ENV, _SENTINEL)
    seen: list[bool] = []

    class _Worker:
        async def run_once_and_drain(self) -> None:
            return None

    def _build(settings):
        seen.append(OPENROUTER_MANAGEMENT_KEY_ENV in os.environ)
        return SimpleNamespace(worker=_Worker(), engine=_Engine())

    async def _not_ahead(_engine):
        return None

    monkeypatch.setattr(worker_mod, "build_worker_runtime", _build)
    monkeypatch.setattr(worker_mod, "verify_database_schema_not_ahead", _not_ahead)
    service = resolve_service_settings(Settings(_env_file=None), environ={})

    await worker_mod.run_worker(service, once=True)

    assert seen == [False]
    assert service.openrouter_management_key == _SENTINEL
