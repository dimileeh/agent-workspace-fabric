"""Service-oriented CLI and local service runtime tests."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from awf.cli.main import app
from awf.common.config import Settings

_runner = CliRunner()


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
        database_url="sqlite+aiosqlite:///./awf.db",
    )

    settings = resolve_service_settings(base, environ={})
    payload = service_config_payload(settings)
    rendered = json.dumps(payload)

    assert settings.database_url == DEFAULT_LOCAL_SERVICE_DATABASE_URL
    assert payload["database_url"].startswith("postgresql+asyncpg://awf:")
    assert "awf_dev" not in payload["database_url"]
    assert payload["api_token"] == "<redacted>"
    assert payload["github_token"] == "<redacted>"
    assert "api-secret" not in rendered
    assert "ghp_secret" not in rendered


@pytest.mark.unit
def test_service_mode_uses_postgres_when_database_env_unset() -> None:
    from awf.service.config import DEFAULT_LOCAL_SERVICE_DATABASE_URL, resolve_service_settings

    base = Settings(_env_file=None)

    service_settings = resolve_service_settings(base, environ={})

    assert base.database_url.startswith("sqlite+aiosqlite")
    assert service_settings.database_url == DEFAULT_LOCAL_SERVICE_DATABASE_URL
    assert service_settings.database_url.startswith("postgresql+asyncpg://")


@pytest.mark.unit
def test_service_mode_preserves_explicit_sqlite_for_throwaway_runs(tmp_path: Path) -> None:
    from awf.service.config import resolve_service_settings

    sqlite_url = f"sqlite+aiosqlite:///{tmp_path / 'throwaway.db'}"
    base = Settings(_env_file=None, database_url=sqlite_url)

    service_settings = resolve_service_settings(base, environ={"AWF_DATABASE_URL": sqlite_url})

    assert service_settings.database_url == sqlite_url


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
    )

    status = asyncio.run(
        collect_service_status(
            settings,
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_run_subprocess,
            socket_exists=lambda _path: True,
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
    )

    status = asyncio.run(
        collect_service_status(
            settings,
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_run_subprocess,
            socket_exists=lambda _path: True,
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
    )

    status = asyncio.run(
        collect_service_status(
            settings,
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_run_subprocess,
            socket_exists=lambda _path: False,
        )
    )

    assert status["status"] == "fail"
    assert status["checks"]["api"]["reason"] == "API_UNREACHABLE"
    assert status["checks"]["db"]["reason"] == "DB_CONNECTION_FAILED"
    assert status["checks"]["docker"]["reason"] == "DOCKER_SOCKET_UNREACHABLE"
    assert status["checks"]["agent_runtime_image"]["reason"] == "AGENT_RUNTIME_IMAGE_MISSING"


@pytest.mark.unit
def test_worker_entrypoint_wires_control_worker_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import worker as worker_mod
    from awf.service.config import ServiceSettings

    created: dict[str, Any] = {}

    class _Engine:
        async def dispose(self) -> None:
            created["disposed"] = True

    class _GitManager:
        def __init__(self, work_dir: Path) -> None:
            created["git_work_dir"] = work_dir

    class _ComposeManager:
        def __init__(self, *, work_dir: Path, template_path: Path) -> None:
            created["compose_work_dir"] = work_dir
            created["compose_template_path"] = template_path

    class _ComposeStackLauncher:
        def __init__(self, *, compose: object, agent_runtime_image: str) -> None:
            created["stack_compose"] = compose
            created["stack_agent_runtime_image"] = agent_runtime_image

    class _Provisioner:
        def __init__(
            self,
            *,
            session_factory: object,
            git: object,
            stack_launcher: object,
            config: object,
        ) -> None:
            created["provisioner_session_factory"] = session_factory
            created["provisioner_git"] = git
            created["provisioner_stack_launcher"] = stack_launcher
            created["provisioner_config"] = config

    class _ControlWorker:
        def __init__(self, *, session_factory: object, provisioner: object, config: object) -> None:
            created["worker_session_factory"] = session_factory
            created["worker_provisioner"] = provisioner
            created["worker_config"] = config

        async def run_once(self) -> int:
            created["run_once"] = True
            return 0

    engine = _Engine()
    session_factory = object()

    def _make_engine(url: str) -> _Engine:
        created["db_url"] = url
        return engine

    def _make_session_factory(eng: _Engine) -> object:
        created["session_engine"] = eng
        return session_factory

    monkeypatch.setattr(worker_mod, "make_engine", _make_engine)
    monkeypatch.setattr(
        worker_mod,
        "make_session_factory",
        _make_session_factory,
    )
    monkeypatch.setattr(worker_mod, "GitManager", _GitManager)
    monkeypatch.setattr(worker_mod, "ComposeManager", _ComposeManager, raising=False)
    monkeypatch.setattr(worker_mod, "ComposeStackLauncher", _ComposeStackLauncher, raising=False)
    monkeypatch.setattr(worker_mod, "Provisioner", _Provisioner)
    monkeypatch.setattr(worker_mod, "ControlWorker", _ControlWorker)

    host_work_dir = (tmp_path / "awf-work").resolve()
    settings = ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url="postgresql+asyncpg://awf:pw@localhost:5433/awf",
        docker_host="unix:///var/run/docker.sock",
        agent_runtime_image="custom-agent-runtime:dev",
        work_dir=str(host_work_dir),
        api_token=None,
        github_token=None,
        worker_poll_interval_seconds=0.25,
        worker_max_concurrent_provisions=2,
        node_id="node-1",
    )

    asyncio.run(worker_mod.run_worker(settings, once=True))

    assert created["db_url"] == settings.database_url
    assert created["session_engine"] is engine
    assert created["git_work_dir"] == host_work_dir / "git"
    assert created["compose_work_dir"] == host_work_dir / "compose"
    assert created["compose_template_path"].name == "workspace.base.yml.j2"
    assert created["stack_compose"].__class__ is _ComposeManager
    assert created["stack_agent_runtime_image"] == "custom-agent-runtime:dev"
    assert created["provisioner_session_factory"] is session_factory
    assert created["worker_session_factory"] is session_factory
    assert created["provisioner_stack_launcher"].__class__ is _ComposeStackLauncher
    assert created["provisioner_config"].node_id == "node-1"
    assert created["worker_config"].poll_interval_seconds == 0.25
    assert created["worker_config"].max_concurrent_provisions == 2
    assert created["run_once"] is True
    assert created["disposed"] is True
