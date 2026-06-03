"""Service-oriented CLI and local service runtime tests."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from awf.cli.main import app
from awf.common.config import Settings

_runner = CliRunner()
_POSTGRES_TEST_URL = "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
_DOCKER_COMPOSE_CALLER_ENV_KEYS = frozenset(
    {
        "AWF_DOCKER_HOST",
        "COMPOSE_PROFILES",
        "COMPOSE_PROJECT_NAME",
        "DOCKER_API_VERSION",
        "DOCKER_CERT_PATH",
        "DOCKER_CONFIG",
        "DOCKER_CONTEXT",
        "DOCKER_HOST",
        "DOCKER_TLS",
        "DOCKER_TLS_VERIFY",
    }
)


def _combined_output(result: Any) -> str:
    return f"{result.stdout}{getattr(result, 'stderr', '')}"


class _FakeDiskUsage:
    total = 20 * 1024 * 1024 * 1024
    used = 1 * 1024 * 1024 * 1024
    free = 19 * 1024 * 1024 * 1024


def _ok_disk_usage(_path: Path) -> _FakeDiskUsage:
    return _FakeDiskUsage()


def _clear_docker_compose_caller_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.upper() in _DOCKER_COMPOSE_CALLER_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def _default_local_service_compose_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import bootstrap as bootstrap_mod

    compose_file = tmp_path / "docker" / "compose" / "local-service.yml"
    compose_file.parent.mkdir(parents=True)
    compose_file.write_text("services: {}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    _clear_docker_compose_caller_env(monkeypatch)


def _write_non_source_compose_env(tmp_path: Path, contents: str) -> Path:
    profile_dir = tmp_path / ".awf"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "workspace.yml").write_text("version: 1\n", encoding="utf-8")
    compose_env = tmp_path / "docker" / "compose" / ".env"
    compose_env.parent.mkdir(parents=True)
    (compose_env.parent / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    compose_env.write_text(contents, encoding="utf-8")
    return compose_env


@pytest.mark.unit
def test_service_config_uses_postgres_default_and_redacts_secrets() -> None:
    from awf.service.config import (
        DEFAULT_LOCAL_SERVICE_DATABASE_URL,
        resolve_service_settings,
        service_config_payload,
    )

    base = Settings(
        _env_file=None,
        api_token="api-secret",
        github_token="ghp_secret",
        database_url=_POSTGRES_TEST_URL,
        worker_max_concurrent_executions=5,
    )

    settings = resolve_service_settings(base, environ={})
    payload = service_config_payload(settings)
    rendered = json.dumps(payload)

    assert settings.database_url == DEFAULT_LOCAL_SERVICE_DATABASE_URL
    assert settings.worker_max_concurrent_executions == 5
    assert payload["database_url"].startswith("postgresql+asyncpg://awf:")
    assert "awf_dev" not in payload["database_url"]
    assert payload["api_token"] == "<redacted>"
    assert payload["github_token"] == "<redacted>"
    assert "api-secret" not in rendered
    assert "ghp_secret" not in rendered


@pytest.mark.unit
def test_service_config_carries_host_home_for_service_auth_mounts(tmp_path: Path) -> None:
    from awf.service.config import resolve_service_settings

    host_home = tmp_path / "host-home"
    base = Settings(_env_file=None, host_home=str(host_home))

    settings = resolve_service_settings(base, environ={"AWF_HOST_HOME": str(host_home)})

    assert settings.host_home == str(host_home)


@pytest.mark.unit
def test_service_mode_uses_postgres_when_database_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service.config import DEFAULT_LOCAL_SERVICE_DATABASE_URL, resolve_service_settings

    monkeypatch.delenv("AWF_DATABASE_URL", raising=False)
    base = Settings(_env_file=None)

    service_settings = resolve_service_settings(base, environ={})

    assert base.database_url.startswith("postgresql+asyncpg://")
    assert service_settings.database_url == DEFAULT_LOCAL_SERVICE_DATABASE_URL
    assert service_settings.database_url.startswith("postgresql+asyncpg://")


@pytest.mark.unit
def test_service_mode_preserves_explicit_postgres_url() -> None:
    from awf.service.config import resolve_service_settings

    explicit_url = "postgresql+asyncpg://awf:awf_dev@db.internal:5432/awf"
    base = Settings(_env_file=None, database_url=explicit_url)

    service_settings = resolve_service_settings(base, environ={"AWF_DATABASE_URL": explicit_url})

    assert service_settings.database_url == explicit_url


@pytest.mark.unit
def test_service_config_command_prints_redacted_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWF_DATABASE_URL", raising=False)
    monkeypatch.setenv("AWF_API_TOKEN", "api-secret")
    monkeypatch.setenv("AWF_GITHUB_TOKEN", "ghp_secret")

    result = _runner.invoke(app, ["service", "config"])

    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["database_url"].startswith("postgresql+asyncpg://")
    assert body["api_token"] == "<redacted>"
    assert body["github_token"] == "<redacted>"
    assert "api-secret" not in result.stdout
    assert "ghp_secret" not in result.stdout


@pytest.mark.unit
def test_service_status_resolves_settings_from_compose_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import status as status_mod
    from awf.service.config import ServiceSettings

    compose_env = tmp_path / "docker" / "compose" / ".env"
    compose_env.parent.mkdir(parents=True)
    (compose_env.parent / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    compose_database_url = "postgresql+asyncpg://awf:compose-secret@compose-db:5432/awf"
    compose_api_base_url = "http://compose-api:8123"
    compose_env.write_text(
        "\n".join(
            [
                f"AWF_DATABASE_URL={compose_database_url}",
                f"AWF_API_BASE_URL={compose_api_base_url}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: tmp_path)
    for key in ("AWF_DATABASE_URL", "AWF_API_BASE_URL", "AWF_POSTGRES_PASSWORD"):
        monkeypatch.delenv(key, raising=False)

    captured: dict[str, object] = {}

    async def _collect(settings: object, **kwargs: object) -> dict[str, object]:
        captured["settings"] = settings
        captured.update(kwargs)
        return {"service": "awf", "status": "ok", "checks": {}}

    monkeypatch.setattr(status_mod, "collect_service_status", _collect)

    result = _runner.invoke(app, ["service", "status", "--format", "json"])

    assert result.exit_code == 0, result.output
    settings = captured["settings"]
    assert isinstance(settings, ServiceSettings)
    assert settings.database_url == compose_database_url
    assert settings.api_base_url == compose_api_base_url
    assert captured["compose_file"] == compose_env.parent / "local-service.yml"
    assert captured["compose_env_file"] == compose_env
    provider_environ = captured["provider_environ"]
    assert isinstance(provider_environ, dict)
    assert provider_environ["AWF_DATABASE_URL"] == compose_database_url
    assert provider_environ["AWF_POSTGRES_PASSWORD"] == "compose-secret"


@pytest.mark.unit
def test_service_status_ignores_compose_env_without_verified_source_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import status as status_mod
    from awf.service.config import ServiceSettings

    compose_database_url = "postgresql+asyncpg://awf:compose-secret@compose-db:5432/awf"
    compose_api_base_url = "http://compose-api:8123"
    _write_non_source_compose_env(
        tmp_path,
        "\n".join(
            [
                f"AWF_DATABASE_URL={compose_database_url}",
                f"AWF_API_BASE_URL={compose_api_base_url}",
            ]
        )
        + "\n",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    for key in ("AWF_DATABASE_URL", "AWF_API_BASE_URL", "AWF_POSTGRES_PASSWORD"):
        monkeypatch.delenv(key, raising=False)

    captured: dict[str, object] = {}

    async def _collect(settings: object, **kwargs: object) -> dict[str, object]:
        captured["settings"] = settings
        captured.update(kwargs)
        return {"service": "awf", "status": "ok", "checks": {}}

    monkeypatch.setattr(status_mod, "collect_service_status", _collect)

    result = _runner.invoke(app, ["service", "status", "--format", "json"])

    assert result.exit_code == 0, result.output
    settings = captured["settings"]
    assert isinstance(settings, ServiceSettings)
    assert settings.database_url != compose_database_url
    assert settings.api_base_url != compose_api_base_url
    assert captured["compose_file"] == Path("docker/compose/local-service.yml")
    assert captured["compose_env_file"] is None
    provider_environ = captured["provider_environ"]
    assert isinstance(provider_environ, dict)
    assert "AWF_DATABASE_URL" not in provider_environ
    assert "AWF_POSTGRES_PASSWORD" not in provider_environ


@pytest.mark.unit
def test_service_status_resolves_settings_from_existing_root_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import status as status_mod
    from awf.service.config import ServiceSettings

    compose = tmp_path / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    root_database_url = "postgresql+asyncpg://awf:root-secret@root-db:5432/awf"
    root_api_base_url = "http://root-api:8123"
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                f"AWF_DATABASE_URL={root_database_url}",
                f"AWF_API_BASE_URL={root_api_base_url}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: tmp_path)
    for key in ("AWF_DATABASE_URL", "AWF_API_BASE_URL", "AWF_POSTGRES_PASSWORD"):
        monkeypatch.delenv(key, raising=False)

    captured: dict[str, object] = {}

    async def _collect(settings: object, **kwargs: object) -> dict[str, object]:
        captured["settings"] = settings
        captured.update(kwargs)
        return {"service": "awf", "status": "ok", "checks": {}}

    monkeypatch.setattr(status_mod, "collect_service_status", _collect)

    result = _runner.invoke(app, ["service", "status", "--format", "json"])

    assert result.exit_code == 0, result.output
    settings = captured["settings"]
    assert isinstance(settings, ServiceSettings)
    assert settings.database_url == root_database_url
    assert settings.api_base_url == root_api_base_url
    assert captured["compose_file"] == compose / "local-service.yml"
    assert captured["compose_env_file"] is None
    provider_environ = captured["provider_environ"]
    assert isinstance(provider_environ, dict)
    assert provider_environ["AWF_DATABASE_URL"] == root_database_url
    assert provider_environ["AWF_POSTGRES_PASSWORD"] == "root-secret"


@pytest.mark.unit
def test_service_status_uses_mocked_api_db_docker_and_image_checks(tmp_path: Path) -> None:
    from awf.service.config import ServiceSettings
    from awf.service.status import collect_service_status

    api_calls: list[str] = []
    subprocess_calls: list[list[str]] = []

    class _Response:
        status_code = 200

        def json(self) -> dict[str, str]:
            return {"status": "ok", "version": "test-version"}

        def raise_for_status(self) -> None:
            return None

    async def _api_get(url: str, *, timeout: float) -> _Response:
        api_calls.append(url)
        return _Response()

    async def _db_probe(database_url: str) -> dict[str, Any]:
        assert database_url == "postgresql+asyncpg://awf:pw@localhost:5433/awf"
        return {"ok": True, "status": "ok"}

    def _run_subprocess(args: list[str], **_kwargs: object) -> Any:
        subprocess_calls.append(args)
        if args[:2] == ["docker", "info"]:
            return type("Completed", (), {"returncode": 0, "stdout": "27.0.3\n", "stderr": ""})()
        if args[:3] == ["docker", "image", "inspect"]:
            return type(
                "Completed",
                (),
                {"returncode": 0, "stdout": "sha256:deadbeef\n", "stderr": ""},
            )()
        if args[:3] == ["docker", "ps", "-a"]:
            return type(
                "Completed",
                (),
                {"returncode": 0, "stdout": "", "stderr": ""},
            )()
        raise AssertionError(f"unexpected subprocess call: {args}")

    settings = ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url="postgresql+asyncpg://awf:pw@localhost:5433/awf",
        docker_host=f"unix://{tmp_path / 'docker.sock'}",
        agent_runtime_image="awf-agent-runtime:latest",
        work_dir=str(tmp_path / "work"),
        api_token=None,
        github_token=None,
        worker_poll_interval_seconds=0.1,
        worker_max_concurrent_provisions=1,
        host_home=str(tmp_path / "home"),
    )

    status = asyncio.run(
        collect_service_status(
            settings,
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_run_subprocess,
            socket_exists=lambda _path: True,
            disk_usage=_ok_disk_usage,
            provider_environ={},
        )
    )

    assert status["status"] == "ok"
    assert api_calls == ["http://localhost:8000/healthz"]
    assert ["docker", "info", "--format", "{{.ServerVersion}}"] in subprocess_calls
    assert [
        "docker",
        "image",
        "inspect",
        "awf-agent-runtime:latest",
        "--format",
        "{{.Id}}",
    ] in subprocess_calls


@pytest.mark.unit
def test_service_status_runs_dependency_checks_concurrently(tmp_path: Path) -> None:
    from awf.service.config import ServiceSettings
    from awf.service.status import collect_service_status

    started = {
        "api": threading.Event(),
        "docker": threading.Event(),
        "image": threading.Event(),
    }
    release = threading.Event()

    class _Response:
        status_code = 200

        def json(self) -> dict[str, str]:
            return {"status": "ok"}

        def raise_for_status(self) -> None:
            return None

    def _wait_for_release(name: str) -> None:
        started[name].set()
        if not release.wait(timeout=2):
            raise AssertionError(f"{name} check was not released")

    async def _api_get(url: str, *, timeout: float) -> _Response:
        started["api"].set()
        if not await asyncio.to_thread(release.wait, 2):
            raise AssertionError("api check was not released")
        return _Response()

    async def _db_probe(database_url: str) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 2
        while not all(event.is_set() for event in started.values()):
            if loop.time() >= deadline:
                missing = sorted(name for name, event in started.items() if not event.is_set())
                raise AssertionError(f"checks did not run concurrently: {missing}")
            await asyncio.sleep(0.01)
        release.set()
        return {"ok": True, "status": "ok"}

    def _run_subprocess(args: list[str], **_kwargs: object) -> Any:
        if args[:2] == ["docker", "info"]:
            _wait_for_release("docker")
            return type("Completed", (), {"returncode": 0, "stdout": "27.0.3\n", "stderr": ""})()
        if args[:3] == ["docker", "image", "inspect"]:
            _wait_for_release("image")
            return type(
                "Completed",
                (),
                {"returncode": 0, "stdout": "sha256:deadbeef\n", "stderr": ""},
            )()
        if args[:3] == ["docker", "ps", "-a"]:
            return type(
                "Completed",
                (),
                {"returncode": 0, "stdout": "", "stderr": ""},
            )()
        raise AssertionError(f"unexpected subprocess call: {args}")

    settings = ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url="postgresql+asyncpg://awf:pw@localhost:5433/awf",
        docker_host=f"unix://{tmp_path / 'docker.sock'}",
        agent_runtime_image="awf-agent-runtime:latest",
        work_dir=str(tmp_path / "work"),
        api_token=None,
        github_token=None,
        worker_poll_interval_seconds=0.1,
        worker_max_concurrent_provisions=1,
        host_home=str(tmp_path / "home"),
    )

    status = asyncio.run(
        collect_service_status(
            settings,
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_run_subprocess,
            socket_exists=lambda _path: True,
            disk_usage=_ok_disk_usage,
            provider_environ={},
        )
    )

    assert status["status"] == "ok"


@pytest.mark.unit
def test_service_status_reports_failures_from_mocked_checks(tmp_path: Path) -> None:
    from awf.service.config import ServiceSettings
    from awf.service.status import collect_service_status

    async def _api_get(url: str, *, timeout: float) -> Any:
        raise httpx.ConnectError("connection refused")

    async def _db_probe(database_url: str) -> dict[str, Any]:
        return {
            "ok": False,
            "status": "fail",
            "reason": "DB_CONNECTION_FAILED",
            "detail": "connection refused",
        }

    def _run_subprocess(args: list[str], **_kwargs: object) -> Any:
        return type(
            "Completed",
            (),
            {"returncode": 1, "stdout": "", "stderr": "Cannot connect to Docker\n"},
        )()

    settings = ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url="postgresql+asyncpg://awf:pw@localhost:5433/awf",
        docker_host=f"unix://{tmp_path / 'missing.sock'}",
        agent_runtime_image="awf-agent-runtime:latest",
        work_dir=str(tmp_path / "work"),
        api_token=None,
        github_token=None,
        worker_poll_interval_seconds=0.1,
        worker_max_concurrent_provisions=1,
        host_home=str(tmp_path / "home"),
    )

    status = asyncio.run(
        collect_service_status(
            settings,
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_run_subprocess,
            socket_exists=lambda _path: False,
            disk_usage=_ok_disk_usage,
            provider_environ={},
        )
    )

    assert status["status"] == "fail"
    assert status["checks"]["api"]["reason"] == "API_UNREACHABLE"
    assert status["checks"]["db"]["reason"] == "DB_CONNECTION_FAILED"
    assert status["checks"]["docker"]["reason"] == "DOCKER_SOCKET_UNREACHABLE"
    assert status["checks"]["agent_runtime_image"]["reason"] == "AGENT_RUNTIME_IMAGE_MISSING"


@pytest.mark.unit
def test_service_status_pretty_output_includes_disk_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import config as config_mod
    from awf.service import status as status_mod

    settings = object()
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda *_args, **_kwargs: settings)
    monkeypatch.setattr(config_mod, "local_service_environ", lambda **_kwargs: os.environ)

    async def _collect(received: object, **_kwargs: object) -> dict[str, object]:
        assert received is settings
        return {
            "service": "awf",
            "status": "ok",
            "checks": {
                "disk": {
                    "ok": True,
                    "status": "ok",
                    "reason": "SUFFICIENT_DISK",
                    "free_bytes": 300,
                    "threshold_bytes": 200,
                }
            },
        }

    monkeypatch.setattr(status_mod, "collect_service_status", _collect)

    result = _runner.invoke(app, ["service", "status", "--format", "pretty"])

    assert result.exit_code == 0, result.output
    assert "checks.disk.free_bytes: 300" in result.stdout
    assert "checks.disk.threshold_bytes: 200" in result.stdout


@pytest.mark.unit
def test_service_status_pretty_output_includes_network_posture_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import config as config_mod
    from awf.service import status as status_mod

    settings = object()
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda *_args, **_kwargs: settings)

    async def _collect(received: object, **_kwargs: object) -> dict[str, object]:
        assert received is settings
        return {
            "service": "awf",
            "status": "ok",
            "checks": {
                "network_posture": {
                    "ok": True,
                    "status": "warn",
                    "reason": "NETWORK_POSTURE_OPEN_ACTIVE",
                    "active_counts_by_posture": {
                        "restricted": 2,
                        "offline": 0,
                        "open": 1,
                        "unknown": 0,
                    },
                    "open_examples": [
                        {
                            "workspace_id": "ws_open",
                            "status": "running",
                            "pr_url": None,
                        }
                    ],
                }
            },
        }

    monkeypatch.setattr(status_mod, "collect_service_status", _collect)

    result = _runner.invoke(app, ["service", "status", "--format", "pretty"])

    assert result.exit_code == 0, result.output
    assert "checks.network_posture.reason: NETWORK_POSTURE_OPEN_ACTIVE" in result.stdout
    assert "checks.network_posture.active_counts_by_posture.open: 1" in result.stdout
    assert "checks.network_posture.open_examples[0].workspace_id: ws_open" in result.stdout


@pytest.mark.unit
def test_service_status_pretty_output_includes_provider_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import config as config_mod
    from awf.service import status as status_mod

    settings = object()
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda *_args, **_kwargs: settings)

    async def _collect(received: object, **kwargs: object) -> dict[str, object]:
        assert received is settings
        assert kwargs["strict_providers"] == set()
        return {
            "service": "awf",
            "status": "ok",
            "checks": {},
            "agent_readiness": {
                "status": "ok",
                "strict_providers": [],
                "providers": {
                    "github": {
                        "ok": False,
                        "status": "warn",
                        "reason": "GITHUB_TOKEN_ENV_MISSING",
                    }
                },
            },
        }

    monkeypatch.setattr(status_mod, "collect_service_status", _collect)

    result = _runner.invoke(app, ["service", "status", "--format", "pretty"])

    assert result.exit_code == 0, result.output
    assert "agent_readiness.providers.github.reason: GITHUB_TOKEN_ENV_MISSING" in result.stdout


@pytest.mark.unit
def test_service_status_provider_option_requests_strict_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import config as config_mod
    from awf.service import status as status_mod

    settings = object()
    service_env = {"AWF_GITHUB_TOKEN": "ghp_compose_token"}
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda *_args, **_kwargs: settings)
    monkeypatch.setattr(config_mod, "local_service_environ", lambda **_kwargs: service_env)

    async def _collect(received: object, **kwargs: object) -> dict[str, object]:
        assert received is settings
        assert kwargs["strict_providers"] == {"github"}
        assert kwargs["provider_environ"] is service_env
        return {
            "service": "awf",
            "status": "fail",
            "checks": {},
            "agent_readiness": {
                "status": "fail",
                "strict_providers": ["github"],
                "providers": {
                    "github": {
                        "ok": False,
                        "status": "fail",
                        "reason": "GITHUB_TOKEN_ENV_MISSING",
                    }
                },
            },
        }

    monkeypatch.setattr(status_mod, "collect_service_status", _collect)

    result = _runner.invoke(
        app,
        ["service", "status", "--provider", "github", "--format", "pretty"],
    )

    assert result.exit_code == 1, result.output
    assert "agent_readiness.providers.github.status: fail" in result.stdout
    assert "GITHUB_TOKEN_ENV_MISSING" in result.stdout


@pytest.mark.unit
def test_service_status_provider_option_accepts_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import config as config_mod
    from awf.service import status as status_mod

    settings = object()
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda *_args, **_kwargs: settings)

    async def _collect(received: object, **kwargs: object) -> dict[str, object]:
        assert received is settings
        assert kwargs["strict_providers"] == {"codex"}
        return {
            "service": "awf",
            "status": "fail",
            "checks": {},
            "agent_readiness": {
                "status": "fail",
                "strict_providers": ["codex"],
                "security": {
                    "status": "warning",
                    "warning_count": 1,
                    "providers_with_warnings": ["codex"],
                    "reason_codes": ["CODEX_AUTH_MISSING"],
                },
                "providers": {
                    "codex": {
                        "ok": False,
                        "status": "fail",
                        "reason": "CODEX_AUTH_MISSING",
                    }
                },
            },
        }

    monkeypatch.setattr(status_mod, "collect_service_status", _collect)

    result = _runner.invoke(
        app,
        ["service", "status", "--provider", "codex", "--format", "pretty"],
    )

    assert result.exit_code == 1, result.output
    assert "agent_readiness.providers.codex.status: fail" in result.stdout
    assert "agent_readiness.security.reason_codes: ['CODEX_AUTH_MISSING']" in result.stdout


@pytest.mark.unit
def test_service_doctor_defaults_to_pretty_output_and_zero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import config as config_mod
    from awf.service import doctor as doctor_mod

    settings = object()
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda *_args, **_kwargs: settings)
    monkeypatch.setattr(config_mod, "local_service_environ", lambda **_kwargs: os.environ)

    report = SimpleNamespace(
        status="ok",
        to_dict=lambda: {
            "service": "awf",
            "status": "ok",
            "summary": {"ok": 1, "warn": 0, "fail": 0},
            "diagnostics": [
                {
                    "id": "docker",
                    "label": "Docker",
                    "status": "ok",
                    "reason": "DOCKER_OK",
                    "message": "Docker daemon is reachable.",
                    "action": "No action required.",
                    "source": "checks.docker",
                    "metadata": {},
                }
            ],
        },
    )

    async def _collect(received: object, **kwargs: object) -> object:
        assert received is settings
        assert kwargs["strict_providers"] == set()
        assert kwargs["provider_environ"] is os.environ
        assert kwargs["environ"] is os.environ
        return report

    monkeypatch.setattr(doctor_mod, "collect_doctor_report", _collect)
    monkeypatch.setattr(doctor_mod, "render_doctor_pretty", lambda _report: "AWF doctor: ok\n")

    result = _runner.invoke(app, ["service", "doctor"])

    assert result.exit_code == 0, result.output
    assert result.stdout == "AWF doctor: ok\n"


@pytest.mark.unit
def test_service_doctor_resolves_settings_from_compose_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import doctor as doctor_mod

    workspace_root = tmp_path / "workspace"
    compose = workspace_root / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    database_url = "postgresql+asyncpg://awf:compose-secret@db.internal:5432/awf"
    docker_host = f"unix://{tmp_path / 'docker.sock'}"
    api_base_url = "http://api.internal:9000"
    (compose / ".env").write_text(
        "\n".join(
            [
                f"AWF_DATABASE_URL={database_url}",
                f"AWF_DOCKER_HOST={docker_host}",
                f"AWF_API_BASE_URL={api_base_url}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    project_subdir = workspace_root / "project"
    project_subdir.mkdir()
    monkeypatch.chdir(project_subdir)
    monkeypatch.delenv("AWF_DATABASE_URL", raising=False)
    monkeypatch.delenv("AWF_DOCKER_HOST", raising=False)
    monkeypatch.delenv("AWF_API_BASE_URL", raising=False)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: workspace_root)
    captured: dict[str, object] = {}
    report = SimpleNamespace(
        status="ok",
        to_dict=lambda: {
            "service": "awf",
            "status": "ok",
            "summary": {"ok": 1, "warn": 0, "fail": 0},
            "diagnostics": [],
        },
    )

    async def _collect(settings: object, **kwargs: object) -> object:
        captured["settings"] = settings
        captured.update(kwargs)
        return report

    monkeypatch.setattr(doctor_mod, "collect_doctor_report", _collect)

    result = _runner.invoke(app, ["service", "doctor", "--format", "json"])

    assert result.exit_code == 0, result.output
    settings = captured["settings"]
    assert settings.database_url == database_url
    assert settings.docker_host == docker_host
    assert settings.api_base_url == api_base_url
    provider_environ = captured["provider_environ"]
    assert provider_environ["AWF_DATABASE_URL"] == database_url
    assert provider_environ["AWF_DOCKER_HOST"] == docker_host
    assert provider_environ["AWF_API_BASE_URL"] == api_base_url
    assert captured["environ"] is provider_environ
    assert captured["compose_file"] == workspace_root / "docker" / "compose" / "local-service.yml"
    assert captured["compose_env_file"] == compose / ".env"


@pytest.mark.unit
def test_service_doctor_ignores_compose_env_without_verified_source_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import doctor as doctor_mod

    database_url = "postgresql+asyncpg://awf:compose-secret@db.internal:5432/awf"
    docker_host = f"unix://{tmp_path / 'docker.sock'}"
    api_base_url = "http://api.internal:9000"
    _write_non_source_compose_env(
        tmp_path,
        "\n".join(
            [
                f"AWF_DATABASE_URL={database_url}",
                f"AWF_DOCKER_HOST={docker_host}",
                f"AWF_API_BASE_URL={api_base_url}",
            ]
        )
        + "\n",
    )

    monkeypatch.chdir(tmp_path)
    for key in ("AWF_DATABASE_URL", "AWF_DOCKER_HOST", "AWF_API_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    captured: dict[str, object] = {}
    report = SimpleNamespace(
        status="ok",
        to_dict=lambda: {
            "service": "awf",
            "status": "ok",
            "summary": {"ok": 1, "warn": 0, "fail": 0},
            "diagnostics": [],
        },
    )

    async def _collect(settings: object, **kwargs: object) -> object:
        captured["settings"] = settings
        captured.update(kwargs)
        return report

    monkeypatch.setattr(doctor_mod, "collect_doctor_report", _collect)

    result = _runner.invoke(app, ["service", "doctor", "--format", "json"])

    assert result.exit_code == 0, result.output
    settings = captured["settings"]
    assert settings.database_url != database_url
    assert settings.docker_host != docker_host
    assert settings.api_base_url != api_base_url
    assert captured["compose_env_file"] is None
    provider_environ = captured["provider_environ"]
    assert "AWF_DATABASE_URL" not in provider_environ
    assert "AWF_POSTGRES_PASSWORD" not in provider_environ


@pytest.mark.unit
def test_service_doctor_resolves_settings_from_existing_root_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import doctor as doctor_mod

    workspace_root = tmp_path / "workspace"
    compose = workspace_root / "docker" / "compose"
    compose.mkdir(parents=True)
    (compose / "local-service.yml").write_text("services: {}\n", encoding="utf-8")
    root_env = workspace_root / ".env"
    database_url = "postgresql+asyncpg://awf:root-secret@root-db:5432/awf"
    docker_host = f"unix://{tmp_path / 'docker.sock'}"
    api_base_url = "http://root-api:8123"
    root_env.write_text(
        "\n".join(
            [
                f"AWF_DATABASE_URL={database_url}",
                f"AWF_DOCKER_HOST={docker_host}",
                f"AWF_API_BASE_URL={api_base_url}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    project_subdir = workspace_root / "project"
    project_subdir.mkdir()
    monkeypatch.chdir(project_subdir)
    for key in ("AWF_DATABASE_URL", "AWF_DOCKER_HOST", "AWF_API_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: workspace_root)
    captured: dict[str, object] = {}
    report = SimpleNamespace(
        status="ok",
        to_dict=lambda: {
            "service": "awf",
            "status": "ok",
            "summary": {"ok": 1, "warn": 0, "fail": 0},
            "diagnostics": [],
        },
    )

    async def _collect(settings: object, **kwargs: object) -> object:
        captured["settings"] = settings
        captured.update(kwargs)
        return report

    monkeypatch.setattr(doctor_mod, "collect_doctor_report", _collect)

    result = _runner.invoke(app, ["service", "doctor", "--format", "json"])

    assert result.exit_code == 0, result.output
    settings = captured["settings"]
    assert settings.database_url == database_url
    assert settings.docker_host == docker_host
    assert settings.api_base_url == api_base_url
    provider_environ = captured["provider_environ"]
    assert provider_environ["AWF_DATABASE_URL"] == database_url
    assert provider_environ["AWF_DOCKER_HOST"] == docker_host
    assert provider_environ["AWF_API_BASE_URL"] == api_base_url
    assert captured["environ"] is provider_environ
    assert captured["compose_file"] == workspace_root / "docker" / "compose" / "local-service.yml"
    assert captured["compose_env_file"] is None
