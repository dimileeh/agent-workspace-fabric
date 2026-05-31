"""Operator doctor diagnostics for local AWF service readiness."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from awf.service.config import ServiceSettings


def _settings(
    tmp_path: Path,
    *,
    api_base_url: str = "http://localhost:8000",
    database_url: str = "postgresql+asyncpg://awf:pw@localhost:5433/awf",
    host_home: str | None = None,
    work_dir: str | None = None,
    api_token: str | None = None,
    github_token: str | None = None,
) -> ServiceSettings:
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir(exist_ok=True)
    work.mkdir(exist_ok=True)
    return ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url=api_base_url,
        database_url=database_url,
        docker_host=f"unix://{tmp_path / 'docker.sock'}",
        agent_runtime_image="awf-agent-runtime:latest",
        work_dir=str(work if work_dir is None else work_dir),
        api_token=api_token,
        github_token=github_token,
        worker_poll_interval_seconds=0.1,
        worker_max_concurrent_provisions=1,
        host_home=str(home if host_home is None else host_home),
    )


def _green_status() -> dict[str, object]:
    return {
        "service": "awf",
        "status": "ok",
        "checks": {
            "api": {"ok": True, "status": "ok", "version": "test"},
            "docker": {"ok": True, "status": "ok", "version": "27.0.3"},
            "disk": {
                "ok": True,
                "status": "ok",
                "reason": "SUFFICIENT_DISK",
                "free_bytes": 30_000_000_000,
                "threshold_bytes": 10_000_000_000,
            },
            "stranded_workspaces": {
                "ok": True,
                "status": "ok",
                "reason": "NO_STRANDED_WORKSPACES",
                "stranded_count": 0,
                "examples": [],
            },
            "orphan_resources": {
                "ok": True,
                "status": "ok",
                "reason": "NO_ORPHANS",
                "orphan_count": 0,
                "examples": [],
            },
            "network_posture": {
                "ok": True,
                "status": "ok",
                "reason": "NETWORK_POSTURE_NO_ACTIVE_OPEN",
                "active_counts_by_posture": {
                    "restricted": 1,
                    "offline": 0,
                    "open": 0,
                    "unknown": 0,
                },
                "open_examples": [],
            },
        },
        "agent_readiness": {
            "status": "ok",
            "strict_providers": [],
            "providers": {
                "github": {
                    "ok": True,
                    "status": "ok",
                    "reason": "GITHUB_AUTH_OK",
                    "message": "GitHub CLI auth is usable.",
                },
                "codex": {
                    "ok": True,
                    "status": "ok",
                    "reason": "CODEX_FILE_AUTH_PRESENT",
                    "message": "Codex auth files are visible.",
                },
                "claude_code": {
                    "ok": True,
                    "status": "ok",
                    "reason": "CLAUDE_FILE_AUTH_PRESENT",
                    "message": "Claude Code auth files are visible.",
                },
                "gemini": {
                    "ok": True,
                    "status": "ok",
                    "reason": "GEMINI_FILE_AUTH_PRESENT",
                    "message": "Gemini auth files are visible.",
                },
                "opencode": {
                    "ok": True,
                    "status": "ok",
                    "reason": "OPENCODE_FILE_AUTH_PRESENT",
                    "message": "OpenCode/Ollama auth is visible.",
                },
                "grok": {
                    "ok": True,
                    "status": "ok",
                    "reason": "GROK_ENV_AUTH_PRESENT",
                    "message": "Grok Build auth is visible.",
                },
            },
        },
    }


async def _green_collector(
    _settings: ServiceSettings,
    **_kwargs: object,
) -> dict[str, object]:
    return _green_status()


def _completed(stdout: str, *, returncode: int = 0, stderr: str = "") -> Any:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _worker_running(args: list[str], **_kwargs: object) -> Any:
    assert args[:2] == ["docker", "compose"]
    assert args[-6:] == [
        "-f",
        "docker/compose/local-service.yml",
        "ps",
        "worker",
        "--format",
        "json",
    ]
    return _completed('[{"Service":"worker","State":"running","Health":"healthy"}]')


def _connect_ok(_address: tuple[str, int], _timeout: float) -> Any:
    return SimpleNamespace(close=lambda: None)


def _diagnostics_by_id(report: Any) -> dict[str, dict[str, object]]:
    return {item["id"]: item for item in report.to_dict()["diagnostics"]}


@pytest.mark.unit
def test_doctor_green_report_covers_operator_diagnostics(tmp_path: Path) -> None:
    from awf.service.doctor import collect_doctor_report, render_doctor_pretty

    report = asyncio.run(
        collect_doctor_report(
            _settings(tmp_path),
            status_collector=_green_collector,
            run_subprocess=_worker_running,
            socket_connector=_connect_ok,
            environ={},
        )
    )

    payload = report.to_dict()
    diagnostics = _diagnostics_by_id(report)

    assert payload["service"] == "awf"
    assert payload["status"] == "ok"
    assert payload["summary"]["fail"] == 0
    assert {
        "docker",
        "api",
        "worker",
        "github",
        "provider.codex",
        "provider.claude_code",
        "provider.gemini",
        "provider.opencode",
        "provider.grok",
        "port.api",
        "port.db",
        "disk",
        "workspace_containers",
        "orphan_resources",
        "network_posture",
        "local_config",
    } <= set(diagnostics)
    assert diagnostics["docker"]["source"] == "checks.docker"
    assert diagnostics["worker"]["reason"] == "WORKER_RUNNING"
    assert diagnostics["port.api"]["metadata"] == {"host": "localhost", "port": 8000}
    assert diagnostics["port.api"]["message"] == "localhost:8000 is accepting connections."
    assert diagnostics["port.db"]["metadata"] == {"host": "localhost", "port": 5433}
    assert diagnostics["port.db"]["message"] == "localhost:5433 is accepting connections."
    assert "AWF doctor: ok" in render_doctor_pretty(report)
    assert "[ok] Docker:" in render_doctor_pretty(report)


@pytest.mark.unit
def test_doctor_port_diagnostics_reflect_configured_ports(tmp_path: Path) -> None:
    from awf.service.doctor import collect_doctor_report

    report = asyncio.run(
        collect_doctor_report(
            _settings(
                tmp_path,
                api_base_url="http://localhost:9100",
                database_url="postgresql+asyncpg://awf:pw@localhost:15433/awf",
            ),
            status_collector=_green_collector,
            run_subprocess=_worker_running,
            socket_connector=_connect_ok,
            environ={},
        )
    )

    diagnostics = _diagnostics_by_id(report)
    assert diagnostics["port.api"]["metadata"] == {"host": "localhost", "port": 9100}
    assert diagnostics["port.api"]["message"] == "localhost:9100 is accepting connections."
    assert diagnostics["port.db"]["metadata"] == {"host": "localhost", "port": 15433}
    assert diagnostics["port.db"]["message"] == "localhost:15433 is accepting connections."
    assert diagnostics["local_config"]["status"] == "ok"
    assert diagnostics["local_config"]["metadata"]["api_base_url"] == "http://localhost:9100"


@pytest.mark.unit
def test_doctor_worker_inspection_loads_local_compose_env_file(tmp_path: Path) -> None:
    from awf.service.doctor import collect_doctor_report

    compose_file = tmp_path / "docker" / "compose" / "local-service.yml"
    compose_file.parent.mkdir(parents=True)
    compose_file.write_text("services: {}\n", encoding="utf-8")
    compose_env_file = compose_file.parent / ".env"
    compose_env_file.write_text("AWF_POSTGRES_PASSWORD=from-compose-env\n", encoding="utf-8")
    calls: list[tuple[list[str], dict[str, str]]] = []

    def _run(args: list[str], **kwargs: object) -> Any:
        calls.append((args, dict(kwargs["env"])))  # type: ignore[arg-type]
        return _completed('[{"Service":"worker","State":"running","Health":"healthy"}]')

    report = asyncio.run(
        collect_doctor_report(
            _settings(tmp_path),
            status_collector=_green_collector,
            run_subprocess=_run,
            socket_connector=_connect_ok,
            environ={},
            compose_file=compose_file,
        )
    )

    args, env = calls[0]
    assert report.to_dict()["status"] == "ok"
    assert args[:4] == ["docker", "compose", "--env-file", str(compose_env_file)]
    assert env["AWF_POSTGRES_PASSWORD"] == "from-compose-env"


@pytest.mark.unit
def test_doctor_worker_inspection_uses_explicit_compose_env_file(tmp_path: Path) -> None:
    from awf.service.doctor import collect_doctor_report

    compose_file = tmp_path / "docker" / "compose" / "local-service.yml"
    compose_file.parent.mkdir(parents=True)
    compose_file.write_text("services: {}\n", encoding="utf-8")
    unrelated_env = compose_file.parent / ".env"
    unrelated_env.write_text("AWF_POSTGRES_PASSWORD=unrelated\n", encoding="utf-8")
    explicit_env_file = tmp_path / ".env"
    explicit_env_file.write_text("AWF_POSTGRES_PASSWORD=resolved-root-env\n", encoding="utf-8")
    calls: list[tuple[list[str], dict[str, str]]] = []

    def _run(args: list[str], **kwargs: object) -> Any:
        calls.append((args, dict(kwargs["env"])))  # type: ignore[arg-type]
        return _completed('[{"Service":"worker","State":"running","Health":"healthy"}]')

    report = asyncio.run(
        collect_doctor_report(
            _settings(tmp_path),
            status_collector=_green_collector,
            run_subprocess=_run,
            socket_connector=_connect_ok,
            environ={},
            compose_file=compose_file,
            compose_env_file=explicit_env_file,
        )
    )

    args, env = calls[0]
    assert report.to_dict()["status"] == "ok"
    assert args[:4] == ["docker", "compose", "--env-file", str(explicit_env_file)]
    assert env["AWF_POSTGRES_PASSWORD"] == "resolved-root-env"


@pytest.mark.unit
def test_doctor_worker_inspection_honors_explicit_null_compose_env_file(
    tmp_path: Path,
) -> None:
    from awf.service.doctor import collect_doctor_report

    compose_file = tmp_path / "docker" / "compose" / "local-service.yml"
    compose_file.parent.mkdir(parents=True)
    compose_file.write_text("services: {}\n", encoding="utf-8")
    adjacent_env = compose_file.parent / ".env"
    adjacent_env.write_text("AWF_POSTGRES_PASSWORD=untrusted\n", encoding="utf-8")
    calls: list[tuple[list[str], dict[str, str]]] = []

    def _run(args: list[str], **kwargs: object) -> Any:
        calls.append((args, dict(kwargs["env"])))  # type: ignore[arg-type]
        return _completed('[{"Service":"worker","State":"running","Health":"healthy"}]')

    report = asyncio.run(
        collect_doctor_report(
            _settings(tmp_path),
            status_collector=_green_collector,
            run_subprocess=_run,
            socket_connector=_connect_ok,
            environ={},
            compose_file=compose_file,
            compose_env_file=None,
        )
    )

    args, env = calls[0]
    assert report.to_dict()["status"] == "ok"
    assert args[:2] == ["docker", "compose"]
    assert "--env-file" not in args
    assert "AWF_POSTGRES_PASSWORD" not in env


@pytest.mark.unit
def test_doctor_local_compose_env_lookup_skips_duplicate_missing_candidates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import doctor

    monkeypatch.chdir(tmp_path)
    compose_file = Path("docker") / "compose" / "local-service.yml"
    assert doctor._local_service_compose_env_file(compose_file) is None  # noqa: SLF001


@pytest.mark.unit
def test_doctor_local_compose_env_lookup_accepts_absolute_configured_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import doctor

    env_file = tmp_path / ".env"
    env_file.write_text("AWF_POSTGRES_HOST_PORT=15433\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "LOCAL_SERVICE_COMPOSE_ENV_FILE", env_file)

    assert doctor._local_service_compose_env_file(tmp_path / "compose.yml") == env_file  # noqa: SLF001


@pytest.mark.unit
def test_doctor_warns_when_active_open_network_posture_is_visible(
    tmp_path: Path,
) -> None:
    from awf.service.doctor import collect_doctor_report, render_doctor_pretty

    status = _green_status()
    checks = status["checks"]
    assert isinstance(checks, dict)
    checks["network_posture"] = {
        "ok": True,
        "status": "warn",
        "reason": "NETWORK_POSTURE_OPEN_ACTIVE",
        "active_counts_by_posture": {
            "restricted": 0,
            "offline": 0,
            "open": 1,
            "unknown": 0,
        },
        "open_examples": [{"workspace_id": "ws_open", "status": "running", "pr_url": None}],
    }

    async def _collector(_settings: ServiceSettings, **_kwargs: object) -> dict[str, object]:
        return status

    report = asyncio.run(
        collect_doctor_report(
            _settings(tmp_path),
            status_collector=_collector,
            run_subprocess=_worker_running,
            socket_connector=_connect_ok,
            environ={},
        )
    )
    diagnostic = _diagnostics_by_id(report)["network_posture"]

    assert report.status == "warn"
    assert diagnostic["status"] == "warn"
    assert diagnostic["reason"] == "NETWORK_POSTURE_OPEN_ACTIVE"
    assert "unrestricted internet access" in diagnostic["message"]
    assert "[warn] Network Posture:" in render_doctor_pretty(report)


@pytest.mark.unit
def test_doctor_network_posture_metadata_surfaces_templates(tmp_path: Path) -> None:
    from awf.service.doctor import collect_doctor_report

    status = _green_status()
    checks = status["checks"]
    assert isinstance(checks, dict)
    checks["network_posture"] = {
        "ok": True,
        "status": "ok",
        "reason": "NETWORK_POSTURE_NO_ACTIVE_OPEN",
        "active_counts_by_posture": {
            "restricted": 2,
            "offline": 0,
            "open": 0,
            "unknown": 0,
        },
        "active_restricted_templates": ["github", "model_providers"],
        "deferred_enforcement_note": "Destination-level filtering is deferred.",
        "open_examples": [],
    }

    async def _collector(_settings: ServiceSettings, **_kwargs: object) -> dict[str, object]:
        return status

    report = asyncio.run(
        collect_doctor_report(
            _settings(tmp_path),
            status_collector=_collector,
            run_subprocess=_worker_running,
            socket_connector=_connect_ok,
            environ={},
        )
    )
    diagnostic = _diagnostics_by_id(report)["network_posture"]

    assert diagnostic["status"] == "ok"
    assert diagnostic["reason"] == "NETWORK_POSTURE_NO_ACTIVE_OPEN"
    assert diagnostic["metadata"]["active_restricted_templates"] == ["github", "model_providers"]
    assert "deferred" in str(diagnostic["metadata"]["deferred_enforcement_note"])


@pytest.mark.unit
@pytest.mark.parametrize(
    ("provider_name", "expected_reason", "expected_message"),
    [
        ("codex", "CODEX_AUTH_OK", "Codex auth is usable for agent workspaces."),
        ("claude_code", "CLAUDE_CODE_AUTH_OK", "Claude Code auth is usable for agent workspaces."),
        ("gemini", "GEMINI_AUTH_OK", "Gemini auth is usable for agent workspaces."),
        ("opencode", "OPENCODE_AUTH_OK", "OpenCode/Ollama auth is usable for agent workspaces."),
        ("grok", "GROK_AUTH_OK", "Grok Build auth is usable for agent workspaces."),
    ],
)
def test_doctor_maps_provider_ok_fallback_reasons_to_operator_output(
    tmp_path: Path,
    provider_name: str,
    expected_reason: str,
    expected_message: str,
) -> None:
    from awf.service.doctor import collect_doctor_report

    status = _green_status()
    readiness = status["agent_readiness"]
    assert isinstance(readiness, dict)
    providers = readiness["providers"]
    assert isinstance(providers, dict)
    providers[provider_name] = {"ok": True, "status": "ok"}

    async def _collector(_settings: ServiceSettings, **_kwargs: object) -> dict[str, object]:
        return status

    report = asyncio.run(
        collect_doctor_report(
            _settings(tmp_path),
            status_collector=_collector,
            run_subprocess=_worker_running,
            socket_connector=_connect_ok,
            environ={},
        )
    )
    diagnostic = _diagnostics_by_id(report)[f"provider.{provider_name}"]

    assert diagnostic["reason"] == expected_reason
    assert diagnostic["message"] == expected_message
    assert diagnostic["action"] == "No action required."


@pytest.mark.unit
def test_doctor_maps_plain_language_failures(tmp_path: Path) -> None:
    from awf.service.doctor import collect_doctor_report

    failing_status = _green_status()
    checks = failing_status["checks"]
    assert isinstance(checks, dict)
    checks["docker"] = {
        "ok": False,
        "status": "fail",
        "reason": "DOCKER_DAEMON_UNREACHABLE",
        "detail": "Cannot connect to Docker",
    }
    checks["api"] = {
        "ok": False,
        "status": "fail",
        "reason": "API_UNREACHABLE",
        "detail": "connection refused",
    }
    checks["disk"] = {
        "ok": False,
        "status": "fail",
        "reason": "INSUFFICIENT_DISK",
        "free_bytes": 100,
        "threshold_bytes": 200,
    }
    checks["stranded_workspaces"] = {
        "ok": False,
        "status": "fail",
        "reason": "STRANDED_WORKSPACES_PRESENT",
        "stranded_count": 2,
        "examples": [{"workspace_id": "ws_stale", "container": "awf-ws-stale-worker"}],
    }
    checks["orphan_resources"] = {
        "ok": False,
        "status": "fail",
        "reason": "ORPHAN_RESOURCES_PRESENT",
        "orphan_count": 1,
        "examples": [{"kind": "container", "name": "awf-ws-old-api"}],
    }
    readiness = failing_status["agent_readiness"]
    assert isinstance(readiness, dict)
    providers = readiness["providers"]
    assert isinstance(providers, dict)
    providers["github"] = {
        "ok": False,
        "status": "fail",
        "reason": "GITHUB_AUTH_UNUSABLE",
        "message": "GitHub CLI auth is not usable.",
        "action": "Run gh auth login.",
    }
    providers["codex"] = {
        "ok": False,
        "status": "fail",
        "reason": "CODEX_AUTH_MISSING",
        "message": "No Codex auth signal was visible.",
    }
    providers["claude_code"] = {
        "ok": False,
        "status": "fail",
        "reason": "CLAUDE_AUTH_MISSING",
        "message": "No Claude Code auth signal was visible.",
    }
    providers["gemini"] = {
        "ok": False,
        "status": "fail",
        "reason": "GEMINI_AUTH_MISSING",
        "message": "No Gemini auth signal was visible.",
    }
    providers["opencode"] = {
        "ok": False,
        "status": "fail",
        "reason": "OPENCODE_OLLAMA_AUTH_MISSING",
        "message": "No OpenCode/Ollama auth signal was visible.",
    }
    providers["grok"] = {
        "ok": False,
        "status": "fail",
        "reason": "GROK_AUTH_MISSING",
        "message": "No Grok Build auth signal was visible.",
    }

    async def _collector(_settings: ServiceSettings, **_kwargs: object) -> dict[str, object]:
        return failing_status

    def _worker_exited(_args: list[str], **_kwargs: object) -> Any:
        return _completed('[{"Service":"worker","State":"exited","ExitCode":1}]')

    def _connect_closed(address: tuple[str, int], _timeout: float) -> Any:
        raise OSError(f"{address[0]}:{address[1]} refused")

    report = asyncio.run(
        collect_doctor_report(
            _settings(tmp_path),
            status_collector=_collector,
            run_subprocess=_worker_exited,
            socket_connector=_connect_closed,
            environ={},
        )
    )
    diagnostics = _diagnostics_by_id(report)

    assert report.to_dict()["status"] == "fail"
    assert (
        diagnostics["docker"]["message"] == "Docker is installed but the daemon is not reachable."
    )
    assert diagnostics["docker"]["action"] == "Start Docker Desktop or verify AWF_DOCKER_HOST."
    assert diagnostics["api"]["message"].startswith("AWF API is not reachable")
    assert diagnostics["worker"]["reason"] == "WORKER_CONTAINER_EXITED"
    assert diagnostics["github"]["reason"] == "GITHUB_AUTH_UNUSABLE"
    assert diagnostics["provider.codex"]["action"].startswith("Mount ~/.codex")
    assert diagnostics["provider.claude_code"]["reason"] == "CLAUDE_AUTH_MISSING"
    assert diagnostics["provider.gemini"]["reason"] == "GEMINI_AUTH_MISSING"
    assert diagnostics["provider.opencode"]["reason"] == "OPENCODE_OLLAMA_AUTH_MISSING"
    assert diagnostics["provider.grok"]["reason"] == "GROK_AUTH_MISSING"
    assert diagnostics["port.api"]["reason"] == "PORT_CLOSED"
    assert diagnostics["port.db"]["reason"] == "PORT_CLOSED"
    assert diagnostics["disk"]["message"] == "Free disk is below the configured AWF threshold."
    assert diagnostics["workspace_containers"]["reason"] == "STRANDED_WORKSPACES_PRESENT"
    assert diagnostics["orphan_resources"]["reason"] == "ORPHAN_RESOURCES_PRESENT"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("reason", "expected_message", "expected_action"),
    [
        (
            "DOCKER_CLI_NOT_FOUND",
            "Docker CLI is not installed or is not on PATH.",
            "Install Docker Desktop or make the docker CLI available to the AWF service environment.",
        ),
        (
            "DOCKER_SOCKET_UNREACHABLE",
            "Docker socket is not reachable.",
            "Start Docker Desktop or verify AWF_DOCKER_HOST.",
        ),
    ],
)
def test_doctor_maps_docker_availability_reasons_to_operator_output(
    tmp_path: Path,
    reason: str,
    expected_message: str,
    expected_action: str,
) -> None:
    from awf.service.doctor import collect_doctor_report, render_doctor_pretty

    status = _green_status()
    checks = status["checks"]
    assert isinstance(checks, dict)
    checks["docker"] = {
        "ok": False,
        "status": "fail",
        "reason": reason,
        "detail": "low-level Docker failure",
    }

    async def _collector(_settings: ServiceSettings, **_kwargs: object) -> dict[str, object]:
        return status

    report = asyncio.run(
        collect_doctor_report(
            _settings(tmp_path),
            status_collector=_collector,
            run_subprocess=_worker_running,
            socket_connector=_connect_ok,
            environ={},
        )
    )

    docker = _diagnostics_by_id(report)["docker"]
    pretty = render_doctor_pretty(report)

    assert docker["reason"] == reason
    assert docker["message"] == expected_message
    assert docker["action"] == expected_action
    assert f"reason: {reason}" in pretty
    assert f"action: {expected_action}" in pretty


@pytest.mark.unit
@pytest.mark.parametrize(
    ("reason", "expected_message", "expected_action"),
    [
        (
            "GITHUB_TOKEN_ENV_MISSING",
            "No service-visible GitHub token was found.",
            "Set AWF_GITHUB_TOKEN from `gh auth token` before starting the service.",
        ),
        (
            "GITHUB_CLI_NOT_FOUND",
            "GitHub token is present, but the gh CLI is not installed.",
            "Install gh in the service image or rebuild the local service image.",
        ),
    ],
)
def test_doctor_maps_github_readiness_reasons_to_operator_output(
    tmp_path: Path,
    reason: str,
    expected_message: str,
    expected_action: str,
) -> None:
    from awf.service.doctor import collect_doctor_report, render_doctor_pretty

    status = _green_status()
    readiness = status["agent_readiness"]
    assert isinstance(readiness, dict)
    providers = readiness["providers"]
    assert isinstance(providers, dict)
    providers["github"] = {
        "ok": False,
        "status": "fail",
        "reason": reason,
    }

    async def _collector(_settings: ServiceSettings, **_kwargs: object) -> dict[str, object]:
        return status

    report = asyncio.run(
        collect_doctor_report(
            _settings(tmp_path),
            status_collector=_collector,
            run_subprocess=_worker_running,
            socket_connector=_connect_ok,
            environ={},
        )
    )

    github = _diagnostics_by_id(report)["github"]
    pretty = render_doctor_pretty(report)

    assert github["reason"] == reason
    assert github["message"] == expected_message
    assert github["action"] == expected_action
    assert f"reason: {reason}" in pretty
    assert f"action: {expected_action}" in pretty


@pytest.mark.unit
@pytest.mark.parametrize(
    ("worker_stdout", "expected_reason"),
    [
        ("[]", "WORKER_CONTAINER_MISSING"),
        ('{"Service":"worker","State":"created"}', "WORKER_CONTAINER_NOT_RUNNING"),
        ("not-json", "WORKER_STATUS_UNPARSEABLE"),
    ],
)
def test_doctor_worker_reachability_reasons(
    tmp_path: Path,
    worker_stdout: str,
    expected_reason: str,
) -> None:
    from awf.service.doctor import collect_doctor_report

    def _worker_status(_args: list[str], **_kwargs: object) -> Any:
        return _completed(worker_stdout)

    report = asyncio.run(
        collect_doctor_report(
            _settings(tmp_path),
            status_collector=_green_collector,
            run_subprocess=_worker_status,
            socket_connector=_connect_ok,
            environ={},
        )
    )

    assert _diagnostics_by_id(report)["worker"]["reason"] == expected_reason


@pytest.mark.unit
def test_doctor_reports_local_config_issues(tmp_path: Path) -> None:
    from awf.service.doctor import collect_doctor_report

    missing_home = tmp_path / "missing-home"
    missing_work_parent = tmp_path / "missing" / "work"

    report = asyncio.run(
        collect_doctor_report(
            _settings(
                tmp_path,
                api_base_url="not a url",
                database_url="postgresql://user:db-secret@host:bad/db",
                host_home=str(missing_home),
                work_dir=str(missing_work_parent),
            ),
            status_collector=_green_collector,
            run_subprocess=_worker_running,
            socket_connector=_connect_ok,
            environ={},
        )
    )

    config = _diagnostics_by_id(report)["local_config"]

    assert config["status"] == "fail"
    assert config["reason"] == "LOCAL_CONFIG_INVALID"
    assert (
        config["message"] == "Local AWF configuration has issues that block reliable service use."
    )
    assert config["metadata"]["issue_count"] == 4
    serialized = json.dumps(report.to_dict(), sort_keys=True)
    assert "db-secret" not in serialized


@pytest.mark.unit
def test_doctor_output_redacts_secrets_from_pretty_and_json(tmp_path: Path) -> None:
    from awf.service.doctor import collect_doctor_report, render_doctor_pretty

    api_secret = "awf-api-doctor-secret"
    openai_secret = "sk-proj-doctorsecret123456"
    github_secret = "ghp_doctorsecret123456"
    anthropic_secret = "sk-ant-doctorsecret123456"
    db_secret = "doctor-db-secret"
    status = _green_status()
    checks = status["checks"]
    assert isinstance(checks, dict)
    checks["api"] = {
        "ok": False,
        "status": "fail",
        "reason": "API_UNREACHABLE",
        "detail": (
            f"api_token={api_secret} token={openai_secret} "
            f"db=postgresql://awf:{db_secret}@localhost/awf"
        ),
    }
    readiness = status["agent_readiness"]
    assert isinstance(readiness, dict)
    providers = readiness["providers"]
    assert isinstance(providers, dict)
    providers["github"] = {
        "ok": False,
        "status": "fail",
        "reason": "GITHUB_AUTH_UNUSABLE",
        "message": f"bad token {github_secret}",
        "detail": f"anthropic={anthropic_secret}",
    }

    async def _collector(_settings: ServiceSettings, **_kwargs: object) -> dict[str, object]:
        return status

    settings = _settings(
        tmp_path,
        database_url=f"postgresql+asyncpg://awf:{db_secret}@localhost:5433/awf",
        api_token=api_secret,
        github_token=github_secret,
    )
    report = asyncio.run(
        collect_doctor_report(
            settings,
            status_collector=_collector,
            run_subprocess=_worker_running,
            socket_connector=_connect_ok,
            environ={
                "OPENAI_API_KEY": openai_secret,
                "ANTHROPIC_API_KEY": anthropic_secret,
                "AWF_GITHUB_TOKEN": github_secret,
            },
        )
    )

    pretty = render_doctor_pretty(report)
    serialized = json.dumps(report.to_dict(), sort_keys=True)

    for secret in (api_secret, openai_secret, github_secret, anthropic_secret, db_secret):
        assert secret not in pretty
        assert secret not in serialized
    assert "<redacted>" in serialized


@pytest.mark.unit
def test_doctor_handles_status_collection_failure_as_diagnostic(tmp_path: Path) -> None:
    from awf.service.doctor import collect_doctor_report

    github_secret = "ghp_collectorsecret123456"

    async def _collector(_settings: ServiceSettings, **_kwargs: object) -> dict[str, object]:
        raise RuntimeError(f"status failed with {github_secret}")

    report = asyncio.run(
        collect_doctor_report(
            _settings(tmp_path, github_token=github_secret),
            status_collector=_collector,
            run_subprocess=_worker_running,
            socket_connector=_connect_ok,
            environ={"AWF_GITHUB_TOKEN": github_secret},
        )
    )
    diagnostics = _diagnostics_by_id(report)

    assert report.to_dict()["status"] == "fail"
    assert diagnostics["api"]["reason"] == "SERVICE_STATUS_COLLECTION_FAILED"
    assert github_secret not in json.dumps(report.to_dict(), sort_keys=True)


@pytest.mark.unit
def test_doctor_warns_when_only_non_strict_provider_credentials_are_missing(
    tmp_path: Path,
) -> None:
    from awf.service.doctor import collect_doctor_report

    status = _green_status()
    readiness = status["agent_readiness"]
    assert isinstance(readiness, dict)
    providers = readiness["providers"]
    assert isinstance(providers, dict)
    providers["codex"] = {
        "ok": False,
        "status": "warn",
        "reason": "CODEX_AUTH_MISSING",
        "message": "No Codex auth signal was visible.",
    }

    async def _collector(_settings: ServiceSettings, **_kwargs: object) -> dict[str, object]:
        return status

    report = asyncio.run(
        collect_doctor_report(
            _settings(tmp_path),
            status_collector=_collector,
            run_subprocess=_worker_running,
            socket_connector=_connect_ok,
            environ={},
        )
    )

    assert report.to_dict()["status"] == "warn"
    assert _diagnostics_by_id(report)["provider.codex"]["status"] == "warn"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("runner_kind", "expected_reason"),
    [
        ("missing_docker", "DOCKER_CLI_NOT_FOUND"),
        ("timeout", "WORKER_STATUS_UNAVAILABLE"),
        ("oserror", "WORKER_STATUS_UNAVAILABLE"),
        ("compose_failed", "WORKER_STATUS_UNAVAILABLE"),
        ("unhealthy", "WORKER_UNHEALTHY"),
        ("non_worker_records", "WORKER_CONTAINER_MISSING"),
    ],
)
def test_doctor_worker_status_error_branches(
    tmp_path: Path,
    runner_kind: str,
    expected_reason: str,
) -> None:
    from awf.service.doctor import collect_doctor_report

    def _worker_status(args: list[str], **_kwargs: object) -> Any:
        if runner_kind == "missing_docker":
            raise FileNotFoundError("docker")
        if runner_kind == "timeout":
            raise subprocess.TimeoutExpired(cmd=args, timeout=5)
        if runner_kind == "oserror":
            raise OSError("socket unavailable")
        if runner_kind == "compose_failed":
            return _completed("", returncode=2, stderr="compose failed")
        if runner_kind == "unhealthy":
            return _completed('[{"Service":"worker","State":"running","Health":"unhealthy"}]')
        return _completed(
            '{"Service":"api","Name":"awf-api","State":"running"}\n'
            '{"Service":"sidecar","Name":"awf-sidecar","State":"created"}'
        )

    report = asyncio.run(
        collect_doctor_report(
            _settings(tmp_path),
            status_collector=_green_collector,
            run_subprocess=_worker_status,
            socket_connector=_connect_ok,
            environ={},
        )
    )

    assert _diagnostics_by_id(report)["worker"]["reason"] == expected_reason


@pytest.mark.unit
def test_doctor_helper_fallbacks_and_endpoint_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import doctor as doctor_mod

    quiet_report = doctor_mod.DoctorReport(
        service="awf",
        status="ok",
        diagnostics=(
            doctor_mod.DoctorDiagnostic(
                id="quiet",
                label="Quiet",
                status="ok",
                reason="QUIET",
                message="Quiet check passed.",
                action="",
                source="test",
            ),
        ),
    )
    assert "action:" not in doctor_mod.render_doctor_pretty(quiet_report)
    assert doctor_mod._reason_text("FUTURE_OK", label="Future", status="ok").message == (
        "Future check passed."
    )
    assert (
        doctor_mod._reason_text(
            "FUTURE_SKIP",
            label="Future",
            status="skipped",
        ).action
        == "Fix prerequisite checks first."
    )
    assert doctor_mod._reason_text("FUTURE_FAIL", label="Future", status="fail").message == (
        "Future check reported fail."
    )
    assert (
        doctor_mod._reason_text(
            "PORT_OPEN",
            label="Port",
            status="ok",
            context={"endpoint": "localhost:8000"},
        ).message
        == "localhost:8000 is accepting connections."
    )

    assert doctor_mod._status_from_check({"status": "fail"}) == "fail"
    assert doctor_mod._status_from_check({"status": "unknown"}) == "warn"
    assert doctor_mod._status_from_check({}) == "skipped"
    assert doctor_mod._status_from_provider({"ok": True}) == "ok"
    assert doctor_mod._status_from_provider({"ok": False}) == "warn"
    assert doctor_mod._status_from_provider({}) == "skipped"
    assert doctor_mod._mapping("not-a-mapping") == {}
    assert doctor_mod._optional_text(None, frozenset()) is None

    assert doctor_mod._parse_compose_ps("") == []
    assert doctor_mod._parse_compose_ps('{"Service":"worker"}') == [{"Service": "worker"}]
    assert doctor_mod._parse_compose_ps('"not-an-object"') is None
    assert doctor_mod._parse_compose_ps('["not-an-object"]') is None
    assert doctor_mod._parse_compose_ps("1\n2") is None
    assert doctor_mod._parse_compose_ps('{"Service":"api"}\nnot-json') is None

    fallback = {"Service": "api", "Name": "awf-api"}
    assert doctor_mod._worker_record([fallback]) is None
    assert doctor_mod._record_text({}, "missing") == ""
    assert doctor_mod._exit_code_nonzero("not-int") is True

    assert doctor_mod._api_endpoint("https://example.test") == ("example.test", 443)
    assert "Invalid IPv6 URL" in doctor_mod._api_endpoint("http://[::1")
    assert "Port could not be cast" in doctor_mod._api_endpoint("http://localhost:bad")
    assert doctor_mod._database_endpoint("postgresql+asyncpg://awf:awf_dev@localhost:5433/awf") == (
        "localhost",
        5433,
    )
    bad_db = doctor_mod._database_endpoint("postgresql://user:secret-db@host:bad/db")
    assert "secret-db" not in bad_db

    class _UrlWithBadPort:
        host = "db.local"

        @property
        def port(self) -> int:
            raise ValueError("bad port")

    monkeypatch.setattr(doctor_mod, "make_url", lambda _url: _UrlWithBadPort())
    assert "bad port" in doctor_mod._database_endpoint("postgresql://db.local/awf")

    def _raises(_path: Path) -> bool:
        raise OSError("permission denied")

    assert doctor_mod._safe_path_exists(Path("/blocked"), path_exists=_raises) is False
    assert doctor_mod._safe_path_is_dir(Path("/blocked"), path_is_dir=_raises) is False

    class _SecretObject:
        def __str__(self) -> str:
            return "opaque-secret"

    assert doctor_mod._redact_value(_SecretObject(), frozenset({"opaque-secret"})) == ("<redacted>")


@pytest.mark.unit
def test_doctor_default_adapters_are_thin_wrappers(monkeypatch: pytest.MonkeyPatch) -> None:
    from awf.service import doctor as doctor_mod

    socket_calls: list[tuple[tuple[str, int], float]] = []

    def _create_connection(address: tuple[str, int], timeout: float) -> Any:
        socket_calls.append((address, timeout))
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(doctor_mod.socket, "create_connection", _create_connection)
    connection = doctor_mod._socket_connect(("localhost", 8000), 0.25)
    connection.close()

    subprocess_calls: list[tuple[list[str], dict[str, object]]] = []

    def _run(args: list[str], **kwargs: object) -> Any:
        subprocess_calls.append((args, kwargs))
        return _completed("ok\n")

    monkeypatch.setattr(doctor_mod.subprocess, "run", _run)
    result = doctor_mod._run_subprocess(
        ["docker", "info"],
        check=False,
        capture_output=True,
        text=True,
        timeout=1.0,
        env={"DOCKER_HOST": "unix:///tmp/docker.sock"},
    )

    assert socket_calls == [(("localhost", 8000), 0.25)]
    assert result.stdout == "ok\n"
    assert subprocess_calls[0][0] == ["docker", "info"]


@pytest.mark.unit
def test_doctor_secret_collection_redacts_api_and_generic_secret_env(tmp_path: Path) -> None:
    from awf.service import doctor as doctor_mod

    settings = ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url="postgresql+asyncpg://awf:pw@localhost:5433/awf",
        docker_host=f"unix://{tmp_path / 'docker.sock'}",
        agent_runtime_image="awf-agent-runtime:latest",
        work_dir=str(tmp_path / "work"),
        api_token="api-secret",
        github_token=None,
        worker_poll_interval_seconds=0.1,
        worker_max_concurrent_provisions=1,
        host_home=str(tmp_path / "home"),
    )
    secrets = doctor_mod._secret_values(
        settings,
        {
            "CUSTOM_PASSWORD": "custom-secret",
            "NORMAL_ENV": "visible",
        },
    )

    assert doctor_mod._redact_text("api-secret custom-secret visible", secrets) == (
        "<redacted> <redacted> visible"
    )
