"""Service-oriented CLI and local service runtime tests."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from awf.cli.main import app
from awf.service.readiness import CoreReadinessCheck, CoreReadinessReport
from awf.service.target_branch_monitor import (
    TargetBranchMonitorResult,
    TargetBranchMonitorStatus,
)

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
def test_service_readiness_emits_json_scorecard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import awf.service.config as config_module
    import awf.service.readiness as readiness_module
    from awf.service import bootstrap as bootstrap_mod

    settings = SimpleNamespace(service_name="awf-local")
    report = CoreReadinessReport(
        status="ok",
        checks=(
            CoreReadinessCheck(
                name="service_status",
                status="ok",
                reason_code="SERVICE_STATUS_OK",
                message="service dependencies are ready",
                evidence={"status": "ok"},
            ),
        ),
        next_actions=(),
    )
    calls: list[dict[str, object]] = []

    async def _collect(**kwargs: object) -> CoreReadinessReport:
        calls.append(kwargs)
        return report

    monkeypatch.setattr(readiness_module, "collect_core_readiness_report", _collect)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    monkeypatch.setattr(
        config_module,
        "resolve_service_settings",
        lambda *_args, **_kwargs: settings,
    )
    monkeypatch.setattr(config_module, "local_service_environ", lambda **_kwargs: os.environ)

    result = _runner.invoke(
        app,
        [
            "service",
            "readiness",
            "--format",
            "json",
            "--demo-path",
            str(tmp_path),
            "--failure-window-hours",
            "12",
            "--slo-window-hours",
            "168",
            "--provider",
            "codex",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["summary"] == {"ok": 1, "warn": 0, "fail": 0}
    assert payload["checks"][0]["name"] == "service_status"
    assert calls == [
        {
            "settings": settings,
            "demo_path": tmp_path,
            "failure_window_hours": 12,
            "slo_window_hours": 168,
            "strict_providers": frozenset({"codex"}),
            "provider_environ": os.environ,
            "environ": os.environ,
            "compose_file": Path("docker/compose/local-service.yml"),
            "compose_env_file": None,
            "allow_generic_failures": False,
            "allow_slo_breach": False,
        }
    ]


@pytest.mark.unit
def test_service_readiness_resolves_settings_from_compose_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import awf.service.readiness as readiness_module
    from awf.service import bootstrap as bootstrap_mod

    workspace_root = tmp_path / "workspace"
    compose = workspace_root / "docker" / "compose"
    compose.mkdir(parents=True)
    compose_file = compose / "local-service.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    database_url = "postgresql+asyncpg://awf:compose-secret@db.internal:5432/awf"
    docker_host = f"unix://{tmp_path / 'docker.sock'}"
    api_base_url = "http://api.internal:9000"
    github_token = "ghp_compose_token"
    (compose / ".env").write_text(
        "\n".join(
            [
                f"AWF_DATABASE_URL={database_url}",
                f"AWF_DOCKER_HOST={docker_host}",
                f"AWF_API_BASE_URL={api_base_url}",
                f"AWF_GITHUB_TOKEN={github_token}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    project_subdir = workspace_root / "project"
    project_subdir.mkdir()
    monkeypatch.chdir(project_subdir)
    for key in (
        "AWF_DATABASE_URL",
        "AWF_DOCKER_HOST",
        "AWF_API_BASE_URL",
        "AWF_GITHUB_TOKEN",
        "AWF_POSTGRES_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: workspace_root)
    captured: dict[str, object] = {}

    async def _collect(**kwargs: object) -> CoreReadinessReport:
        captured.update(kwargs)
        return CoreReadinessReport(
            status="ok",
            checks=(
                CoreReadinessCheck(
                    name="service_status",
                    status="ok",
                    reason_code="SERVICE_STATUS_OK",
                    message="service dependencies are ready",
                    evidence={"status": "ok"},
                ),
            ),
        )

    monkeypatch.setattr(readiness_module, "collect_core_readiness_report", _collect)

    result = _runner.invoke(app, ["service", "readiness", "--format", "json"])

    assert result.exit_code == 0, result.output
    settings = captured["settings"]
    assert settings.database_url == database_url
    assert settings.docker_host == docker_host
    assert settings.api_base_url == api_base_url
    assert settings.github_token == github_token
    provider_environ = captured["provider_environ"]
    assert isinstance(provider_environ, dict)
    assert provider_environ["AWF_DATABASE_URL"] == database_url
    assert provider_environ["AWF_DOCKER_HOST"] == docker_host
    assert provider_environ["AWF_API_BASE_URL"] == api_base_url
    assert provider_environ["AWF_GITHUB_TOKEN"] == github_token
    assert provider_environ["AWF_POSTGRES_PASSWORD"] == "compose-secret"
    assert captured["environ"] is provider_environ
    assert captured["compose_file"] == compose_file
    assert captured["compose_env_file"] == compose / ".env"


@pytest.mark.unit
def test_service_readiness_ignores_compose_env_without_verified_source_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import awf.service.readiness as readiness_module
    from awf.service import bootstrap as bootstrap_mod

    database_url = "postgresql+asyncpg://awf:compose-secret@db.internal:5432/awf"
    _write_non_source_compose_env(
        tmp_path,
        f"AWF_DATABASE_URL={database_url}\nAWF_API_BASE_URL=http://api.internal:9000\n",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    for key in ("AWF_DATABASE_URL", "AWF_API_BASE_URL", "AWF_POSTGRES_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    captured: dict[str, object] = {}

    async def _collect(**kwargs: object) -> CoreReadinessReport:
        captured.update(kwargs)
        return CoreReadinessReport(
            status="ok",
            checks=(
                CoreReadinessCheck(
                    name="service_status",
                    status="ok",
                    reason_code="SERVICE_STATUS_OK",
                    message="service dependencies are ready",
                    evidence={"status": "ok"},
                ),
            ),
        )

    monkeypatch.setattr(readiness_module, "collect_core_readiness_report", _collect)

    result = _runner.invoke(app, ["service", "readiness", "--format", "json"])

    assert result.exit_code == 0, result.output
    settings = captured["settings"]
    assert settings.database_url != database_url
    assert captured["compose_env_file"] is None
    provider_environ = captured["provider_environ"]
    assert isinstance(provider_environ, dict)
    assert "AWF_DATABASE_URL" not in provider_environ
    assert "AWF_POSTGRES_PASSWORD" not in provider_environ


@pytest.mark.unit
def test_service_readiness_exits_nonzero_when_scorecard_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import awf.service.config as config_module
    import awf.service.readiness as readiness_module
    from awf.service import bootstrap as bootstrap_mod

    async def _collect(**_kwargs: object) -> CoreReadinessReport:
        return CoreReadinessReport(
            status="fail",
            checks=(
                CoreReadinessCheck(
                    name="recent_failure_taxonomy",
                    status="fail",
                    reason_code="GENERIC_FAILURE_REASON_BLOCKS_RELEASE",
                    message="recent failures include generic reason codes",
                    evidence={"workspace_ids": ["ws_unknown"]},
                ),
            ),
            next_actions=("Classify generic recent workspace failures.",),
        )

    monkeypatch.setattr(readiness_module, "collect_core_readiness_report", _collect)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    monkeypatch.setattr(
        config_module,
        "resolve_service_settings",
        lambda *_args, **_kwargs: SimpleNamespace(service_name="awf-local"),
    )

    result = _runner.invoke(app, ["service", "readiness", "--format", "json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "fail"
    assert payload["checks"][0]["reason_code"] == "GENERIC_FAILURE_REASON_BLOCKS_RELEASE"


@pytest.mark.unit
def test_service_readiness_pretty_labels_release_gate_and_summarizes_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import awf.service.config as config_module
    import awf.service.readiness as readiness_module
    from awf.service import bootstrap as bootstrap_mod

    async def _collect(**_kwargs: object) -> CoreReadinessReport:
        return CoreReadinessReport(
            status="fail",
            checks=(
                CoreReadinessCheck(
                    name="service_status",
                    status="ok",
                    reason_code="SERVICE_STATUS_OK",
                    message="service dependencies are ready",
                    evidence={"checks": {"database": {"ok": True}}},
                ),
                CoreReadinessCheck(
                    name="prd_slo_thresholds",
                    status="fail",
                    reason_code="PRD_SLO_THRESHOLDS_FAILED",
                    message="rolling PRD SLO thresholds are below Core release criteria",
                    evidence={
                        "since_hours": 168,
                        "breaches": {
                            "workspace_creation_success_rate": {
                                "actual": 0.9,
                                "threshold": {"operator": ">=", "value": 0.98},
                            }
                        },
                    },
                ),
            ),
            next_actions=("Collect more successful workspace evidence.",),
        )

    monkeypatch.setattr(readiness_module, "collect_core_readiness_report", _collect)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    monkeypatch.setattr(
        config_module,
        "resolve_service_settings",
        lambda *_args, **_kwargs: SimpleNamespace(service_name="awf-local"),
    )
    monkeypatch.setattr(config_module, "local_service_environ", lambda **_kwargs: os.environ)

    result = _runner.invoke(app, ["service", "readiness", "--format", "pretty"])

    assert result.exit_code == 1, result.output
    assert "AWF Core release readiness: fail" in result.stdout
    assert "local service health" in result.stdout
    assert "[fail] prd_slo_thresholds" in result.stdout
    assert "workspace_creation_success_rate: 90.0% >= 98.0%" in result.stdout
    assert "checks[0]." not in result.stdout


@pytest.mark.unit
def test_service_release_readiness_alias_matches_readiness_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import awf.service.config as config_module
    import awf.service.readiness as readiness_module
    from awf.service import bootstrap as bootstrap_mod

    async def _collect(**_kwargs: object) -> CoreReadinessReport:
        return CoreReadinessReport(
            status="ok",
            checks=(
                CoreReadinessCheck(
                    name="service_status",
                    status="ok",
                    reason_code="SERVICE_STATUS_OK",
                    message="service dependencies are ready",
                    evidence={"status": "ok"},
                ),
            ),
        )

    monkeypatch.setattr(readiness_module, "collect_core_readiness_report", _collect)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    monkeypatch.setattr(
        config_module,
        "resolve_service_settings",
        lambda *_args, **_kwargs: SimpleNamespace(service_name="awf-local"),
    )
    monkeypatch.setattr(config_module, "local_service_environ", lambda **_kwargs: os.environ)

    result = _runner.invoke(app, ["service", "release-readiness", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["checks"][0]["reason_code"] == "SERVICE_STATUS_OK"


@pytest.mark.unit
@pytest.mark.usefixtures("_default_local_service_compose_file")
def test_service_logs_defaults_to_tail_api_and_worker_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = str(Path("docker/compose/local-service.yml").resolve())
    calls: list[tuple[list[str], dict[str, object]]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args, returncode=0, stdout="api log\nworker log\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", _run)

    result = _runner.invoke(app, ["service", "logs"])

    assert result.exit_code == 0, result.output
    assert result.stdout == "api log\nworker log\n"
    assert calls == [
        (
            [
                "docker",
                "compose",
                "-f",
                compose_file,
                "logs",
                "--tail",
                "100",
                "api",
                "worker",
            ],
            {"check": False, "capture_output": True, "text": True},
        )
    ]


@pytest.mark.unit
def test_service_logs_passes_source_checkout_compose_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import bootstrap as bootstrap_mod

    workspace_root = tmp_path / "workspace"
    compose = workspace_root / "docker" / "compose"
    compose.mkdir(parents=True)
    compose_file = compose / "local-service.yml"
    compose_env = compose / ".env"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    compose_env.write_text("AWF_API_TOKEN=from-compose-env\n", encoding="utf-8")
    project_subdir = workspace_root / "project"
    project_subdir.mkdir()
    calls: list[list[str]] = []

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.chdir(project_subdir)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: workspace_root)
    monkeypatch.setattr(subprocess, "run", _run)

    result = _runner.invoke(app, ["service", "logs"])

    assert result.exit_code == 0, result.output
    assert calls == [
        [
            "docker",
            "compose",
            "--env-file",
            str(compose_env),
            "-f",
            str(compose_file),
            "logs",
            "--tail",
            "100",
            "api",
            "worker",
        ]
    ]


@pytest.mark.unit
def test_service_logs_reuses_resolved_asset_root_for_compose_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import bootstrap as bootstrap_mod

    workspace_root = tmp_path / "workspace"
    compose = workspace_root / "docker" / "compose"
    compose.mkdir(parents=True)
    compose_file = compose / "local-service.yml"
    compose_env = compose / ".env"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    compose_env.write_text("AWF_API_TOKEN=from-compose-env\n", encoding="utf-8")
    project_subdir = workspace_root / "project"
    project_subdir.mkdir()
    root_lookups = 0

    def _asset_root() -> Path:
        nonlocal root_lookups
        root_lookups += 1
        return workspace_root

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.chdir(project_subdir)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", _asset_root)
    monkeypatch.setattr(subprocess, "run", _run)

    result = _runner.invoke(app, ["service", "logs", "--service", "api"])

    assert result.exit_code == 0, result.output
    assert root_lookups == 1


@pytest.mark.unit
def test_service_logs_ignores_compose_env_without_verified_source_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import bootstrap as bootstrap_mod

    _write_non_source_compose_env(tmp_path, "AWF_API_TOKEN=from-compose-env\n")
    calls: list[list[str]] = []

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    monkeypatch.setattr(subprocess, "run", _run)

    result = _runner.invoke(app, ["service", "logs", "--service", "worker"])

    assert result.exit_code == 0, result.output
    assert calls == [
        [
            "docker",
            "compose",
            "-f",
            str((tmp_path / "docker" / "compose" / "local-service.yml").resolve()),
            "logs",
            "--tail",
            "100",
            "worker",
        ]
    ]


@pytest.mark.unit
def test_service_logs_ignores_ancestor_compose_env_without_source_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import bootstrap as bootstrap_mod

    project_root = tmp_path / "project"
    _write_non_source_compose_env(project_root, "AWF_API_TOKEN=from-ancestor\n")
    project_subdir = project_root / "subdir"
    project_subdir.mkdir()
    calls: list[list[str]] = []

    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.chdir(project_subdir)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    monkeypatch.setattr(subprocess, "run", _run)

    result = _runner.invoke(app, ["service", "logs", "--service", "worker"])

    assert result.exit_code == 0, result.output
    assert calls == [
        [
            "docker",
            "compose",
            "-f",
            str((project_root / "docker" / "compose" / "local-service.yml").resolve()),
            "logs",
            "--tail",
            "100",
            "worker",
        ]
    ]


@pytest.mark.unit
def test_service_logs_mirrors_compose_awf_docker_host_into_subprocess_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import bootstrap as bootstrap_mod

    workspace_root = tmp_path / "workspace"
    compose = workspace_root / "docker" / "compose"
    compose.mkdir(parents=True)
    compose_file = compose / "local-service.yml"
    compose_env = compose / ".env"
    docker_host = f"unix://{tmp_path / 'docker.sock'}"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    compose_env.write_text(f"AWF_DOCKER_HOST={docker_host}\n", encoding="utf-8")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.chdir(workspace_root)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: workspace_root)
    monkeypatch.delenv("AWF_DOCKER_HOST", raising=False)
    monkeypatch.setenv("DOCKER_HOST", "unix:///stale-docker.sock")
    monkeypatch.setattr(subprocess, "run", _run)

    result = _runner.invoke(app, ["service", "logs", "--service", "api"])

    assert result.exit_code == 0, result.output
    args, kwargs = calls[0]
    assert args == [
        "docker",
        "compose",
        "--env-file",
        str(compose_env),
        "-f",
        str(compose_file),
        "logs",
        "--tail",
        "100",
        "api",
    ]
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert env["DOCKER_HOST"] == docker_host
    assert "AWF_DOCKER_HOST" not in env


@pytest.mark.unit
def test_service_logs_omits_root_env_file_when_compose_env_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import bootstrap as bootstrap_mod

    workspace_root = tmp_path / "workspace"
    compose = workspace_root / "docker" / "compose"
    compose.mkdir(parents=True)
    compose_file = compose / "local-service.yml"
    root_env = workspace_root / ".env"
    docker_host = f"unix://{tmp_path / 'docker.sock'}"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    root_env.write_text(f"AWF_DOCKER_HOST={docker_host}\n", encoding="utf-8")
    calls: list[tuple[list[str], dict[str, object]]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.chdir(workspace_root)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: workspace_root)
    monkeypatch.delenv("AWF_DOCKER_HOST", raising=False)
    monkeypatch.setattr(subprocess, "run", _run)

    result = _runner.invoke(app, ["service", "logs", "--service", "worker"])

    assert result.exit_code == 0, result.output
    assert calls[0][0] == [
        "docker",
        "compose",
        "-f",
        str(compose_file),
        "logs",
        "--tail",
        "100",
        "worker",
    ]
    env = calls[0][1]["env"]
    assert isinstance(env, dict)
    assert env["DOCKER_HOST"] == docker_host
    assert "AWF_DOCKER_HOST" not in env


@pytest.mark.unit
@pytest.mark.usefixtures("_default_local_service_compose_file")
def test_service_logs_accepts_repeated_service_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = str(Path("docker/compose/local-service.yml").resolve())
    calls: list[list[str]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)

    result = _runner.invoke(
        app,
        [
            "service",
            "logs",
            "--tail",
            "25",
            "--service",
            "postgres",
            "--service",
            "migrate",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [
        [
            "docker",
            "compose",
            "-f",
            compose_file,
            "logs",
            "--tail",
            "25",
            "postgres",
            "migrate",
        ]
    ]


@pytest.mark.unit
@pytest.mark.usefixtures("_default_local_service_compose_file")
def test_service_logs_follow_streams_without_capturing_subprocess_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose_file = str(Path("docker/compose/local-service.yml").resolve())
    calls: list[tuple[list[str], dict[str, object]]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, returncode=0, stdout=None, stderr=None)

    monkeypatch.setattr(subprocess, "run", _run)

    result = _runner.invoke(app, ["service", "logs", "--follow", "--service", "worker"])

    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert calls == [
        (
            [
                "docker",
                "compose",
                "-f",
                compose_file,
                "logs",
                "--tail",
                "100",
                "--follow",
                "worker",
            ],
            {"check": False, "capture_output": False, "text": True},
        )
    ]


@pytest.mark.unit
@pytest.mark.parametrize("returncode", [130, -2])
def test_service_logs_follow_ignores_subprocess_interrupt_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
) -> None:
    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode=returncode, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)

    result = _runner.invoke(app, ["service", "logs", "--follow"])

    assert result.exit_code == 0, result.output
    assert _combined_output(result) == ""


@pytest.mark.unit
def test_service_logs_follow_keyboard_interrupt_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise KeyboardInterrupt

    monkeypatch.setattr(subprocess, "run", _run)

    result = _runner.invoke(app, ["service", "logs", "--follow"])

    assert result.exit_code == 0, result.output
    assert _combined_output(result) == ""


@pytest.mark.unit
def test_service_logs_docker_compose_failure_is_clean_typer_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args,
            returncode=17,
            stdout="",
            stderr='service "api" is not running\n',
        )

    monkeypatch.setattr(subprocess, "run", _run)

    result = _runner.invoke(app, ["service", "logs"])

    output = _combined_output(result)
    assert result.exit_code == 1
    assert 'error: docker compose logs failed (exit 17): service "api" is not running' in output
    assert "Traceback" not in output


@pytest.mark.unit
def test_service_bootstrap_cli_invokes_helper_and_emits_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import config as config_mod
    from awf.service.bootstrap import ServiceBootstrapResult

    settings = object()
    captured: dict[str, object] = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda *_args, **_kwargs: settings)

    async def _bootstrap(received: object, **kwargs: object) -> ServiceBootstrapResult:
        captured["settings"] = received
        captured.update(kwargs)
        return ServiceBootstrapResult(
            stages=(),
            service_status={"service": "awf", "status": "ok", "checks": {}},
        )

    monkeypatch.setattr(bootstrap_mod, "run_service_bootstrap", _bootstrap)

    result = _runner.invoke(app, ["service", "bootstrap"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["service_status"]["status"] == "ok"
    assert captured["settings"] is settings
    assert captured["env_file"] is None
    options = captured["options"]
    assert options.timeout_seconds == 180
    assert options.poll_interval_seconds == 2
    assert options.skip_agent_runtime_build is False
    assert options.strict_providers == frozenset()


@pytest.mark.unit
def test_service_bootstrap_cli_resolves_settings_from_compose_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service.bootstrap import ServiceBootstrapResult

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

    monkeypatch.chdir(workspace_root)
    monkeypatch.delenv("AWF_DATABASE_URL", raising=False)
    monkeypatch.delenv("AWF_DOCKER_HOST", raising=False)
    monkeypatch.delenv("AWF_API_BASE_URL", raising=False)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: workspace_root)
    captured: dict[str, object] = {}

    async def _bootstrap(settings: object, **kwargs: object) -> ServiceBootstrapResult:
        captured["settings"] = settings
        captured.update(kwargs)
        return ServiceBootstrapResult(
            stages=(),
            service_status={"service": "awf", "status": "ok", "checks": {}},
        )

    monkeypatch.setattr(bootstrap_mod, "run_service_bootstrap", _bootstrap)

    result = _runner.invoke(app, ["service", "bootstrap"])

    assert result.exit_code == 0, result.output
    settings = captured["settings"]
    assert settings.database_url == database_url
    assert settings.docker_host == docker_host
    assert settings.api_base_url == api_base_url
    assert captured["compose_file"] == compose / "local-service.yml"
    assert captured["env_file"] == compose / ".env"
    assert "provider_environ" not in captured
    service_environ = captured["service_environ"]
    assert service_environ["AWF_DATABASE_URL"] == database_url
    assert service_environ["AWF_DOCKER_HOST"] == docker_host
    assert service_environ["AWF_API_BASE_URL"] == api_base_url


@pytest.mark.unit
def test_service_bootstrap_cli_ignores_compose_env_without_verified_source_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service.bootstrap import ServiceBootstrapResult

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

    async def _bootstrap(settings: object, **kwargs: object) -> ServiceBootstrapResult:
        captured["settings"] = settings
        captured.update(kwargs)
        return ServiceBootstrapResult(
            stages=(),
            service_status={"service": "awf", "status": "ok", "checks": {}},
        )

    monkeypatch.setattr(bootstrap_mod, "run_service_bootstrap", _bootstrap)

    result = _runner.invoke(app, ["service", "bootstrap"])

    assert result.exit_code == 0, result.output
    settings = captured["settings"]
    assert settings.database_url != database_url
    assert settings.docker_host != docker_host
    assert settings.api_base_url != api_base_url
    assert captured["compose_file"] == Path("docker/compose/local-service.yml")
    assert captured["env_file"] is None
    service_environ = captured["service_environ"]
    assert "AWF_DATABASE_URL" not in service_environ
    assert "AWF_POSTGRES_PASSWORD" not in service_environ


@pytest.mark.unit
def test_service_bootstrap_cli_resolves_settings_from_existing_root_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service.bootstrap import ServiceBootstrapResult

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

    monkeypatch.chdir(workspace_root)
    for key in ("AWF_DATABASE_URL", "AWF_DOCKER_HOST", "AWF_API_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: workspace_root)
    captured: dict[str, object] = {}

    async def _bootstrap(settings: object, **kwargs: object) -> ServiceBootstrapResult:
        captured["settings"] = settings
        captured.update(kwargs)
        return ServiceBootstrapResult(
            stages=(),
            service_status={"service": "awf", "status": "ok", "checks": {}},
        )

    monkeypatch.setattr(bootstrap_mod, "run_service_bootstrap", _bootstrap)

    result = _runner.invoke(app, ["service", "bootstrap"])

    assert result.exit_code == 0, result.output
    settings = captured["settings"]
    assert settings.database_url == database_url
    assert settings.docker_host == docker_host
    assert settings.api_base_url == api_base_url
    assert captured["compose_file"] == compose / "local-service.yml"
    assert captured["env_file"] is None
    assert "provider_environ" not in captured
    service_environ = captured["service_environ"]
    assert service_environ["AWF_DATABASE_URL"] == database_url
    assert service_environ["AWF_DOCKER_HOST"] == docker_host
    assert service_environ["AWF_API_BASE_URL"] == api_base_url


@pytest.mark.unit
def test_service_bootstrap_cli_pretty_output_uses_existing_emitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import config as config_mod
    from awf.service.bootstrap import ServiceBootstrapResult

    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda *_args, **_kwargs: object())

    async def _bootstrap(_settings: object, **_kwargs: object) -> ServiceBootstrapResult:
        return ServiceBootstrapResult(
            stages=(),
            service_status={"service": "awf", "status": "ok", "checks": {}},
        )

    monkeypatch.setattr(bootstrap_mod, "run_service_bootstrap", _bootstrap)

    result = _runner.invoke(app, ["service", "bootstrap", "--format", "pretty"])

    assert result.exit_code == 0, result.output
    assert "status: ok" in result.stdout
    assert "service_status.status: ok" in result.stdout


@pytest.mark.unit
def test_service_bootstrap_cli_passes_strict_provider_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import config as config_mod
    from awf.service.bootstrap import ServiceBootstrapResult

    captured: dict[str, object] = {}
    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda *_args, **_kwargs: object())

    async def _bootstrap(_settings: object, **kwargs: object) -> ServiceBootstrapResult:
        captured.update(kwargs)
        return ServiceBootstrapResult(
            stages=(),
            service_status={"service": "awf", "status": "ok", "checks": {}},
        )

    monkeypatch.setattr(bootstrap_mod, "run_service_bootstrap", _bootstrap)

    result = _runner.invoke(
        app,
        [
            "service",
            "bootstrap",
            "--provider",
            "github",
            "--provider",
            "opencode",
        ],
    )

    assert result.exit_code == 0, result.output
    assert captured["options"].strict_providers == frozenset({"github", "opencode"})


@pytest.mark.unit
def test_service_bootstrap_cli_helper_failures_exit_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import bootstrap as bootstrap_mod
    from awf.service import config as config_mod
    from awf.service.bootstrap import ServiceBootstrapError

    monkeypatch.setattr(bootstrap_mod, "get_bootstrap_asset_root", lambda: None)
    monkeypatch.setattr(config_mod, "resolve_service_settings", lambda *_args, **_kwargs: object())

    async def _bootstrap(_settings: object, **_kwargs: object) -> object:
        raise ServiceBootstrapError(
            reason_code="SERVICE_BOOTSTRAP_TIMEOUT",
            message="timed out waiting for service readiness",
            last_status={"status": "fail", "checks": {"api": {"reason": "API_UNREACHABLE"}}},
        )

    monkeypatch.setattr(bootstrap_mod, "run_service_bootstrap", _bootstrap)

    result = _runner.invoke(app, ["service", "bootstrap"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "SERVICE_BOOTSTRAP_TIMEOUT"
    assert payload["last_status"]["status"] == "fail"
    assert "Traceback" not in _combined_output(result)


@pytest.mark.unit
def test_service_bootstrap_cli_rejects_unknown_provider_without_traceback() -> None:
    result = _runner.invoke(app, ["service", "bootstrap", "--provider", "bogus"])

    output = _combined_output(result)
    assert result.exit_code == 2
    assert "error: unknown provider(s): bogus" in output
    assert "Traceback" not in output


@pytest.mark.unit
def test_service_reconcile_target_invokes_target_branch_monitor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from awf.service import target_branch_monitor

    calls: list[dict[str, object]] = []

    async def _fake_reconcile_once(**kwargs: object) -> TargetBranchMonitorResult:
        calls.append(kwargs)
        return TargetBranchMonitorResult(
            repo_url=str(kwargs["repo_url"]),
            branch=str(kwargs["branch"]),
            checkout_path=tmp_path / "checkout",
            status=TargetBranchMonitorStatus.clean,
            resolver_results=(),
        )

    monkeypatch.setattr(
        target_branch_monitor,
        "run_target_branch_reconcile_once",
        _fake_reconcile_once,
    )

    result = _runner.invoke(
        app,
        [
            "service",
            "reconcile-target",
            "--repo-url",
            "git@github.com:example/repo.git",
            "--branch",
            "development",
            "--work-dir",
            str(tmp_path / "state"),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "clean"
    assert calls[0]["repo_url"] == "git@github.com:example/repo.git"
    assert calls[0]["branch"] == "development"
    assert calls[0]["work_dir"] == (tmp_path / "state").resolve()


@pytest.mark.unit
def test_readme_documents_service_logs_command() -> None:
    readme = Path("docs/CLI_REFERENCE.md").read_text()

    assert "awf service logs" in readme
    assert "docker/compose/local-service.yml" in readme
    assert "--tail" in readme
    assert "--service worker" in readme
    assert "--follow" in readme


@pytest.mark.unit
def test_readme_documents_service_bootstrap_command() -> None:
    """Verify concepts docs keep the service bootstrap command discoverable."""
    readme = Path("docs/CONCEPTS.md").read_text()

    assert "awf service bootstrap" in readme
    assert "uv run --python 3.12 --extra dev awf service bootstrap" in readme
    assert "uv run --python 3.12 --extra dev awf service status --format pretty" in readme
    assert (
        "docker compose --env-file docker/compose/.env "
        "-f docker/compose/local-service.yml up --build" in readme
    )
    assert "safe to re-run" in readme
    assert "docker/compose/.env" in readme


@pytest.mark.unit
@pytest.mark.parametrize(
    "doc_path",
    [
        Path("docs/QUICKSTART.md"),
        Path("docs/GETTING_STARTED.md"),
    ],
)
def test_readme_documents_compose_env_bootstrap_path(doc_path: Path) -> None:
    """Verify onboarding docs mention the compose env bootstrap target."""
    document = doc_path.read_text(encoding="utf-8")

    assert "docker/compose/.env" in document
    assert "wrote .env" not in document


@pytest.mark.unit
def test_readme_documents_optional_ollama_bridge_profile() -> None:
    readme = Path("docs/CONCEPTS.md").read_text()

    assert "COMPOSE_PROFILES=ollama-bridge" in readme
    assert "AWF_OLLAMA_BRIDGE_BIND_ADDRESS" in readme
    assert "Linux-only" in readme


@pytest.mark.unit
def test_readme_documents_control_plane_postgres_backup_restore() -> None:
    readme = Path("docs/CONCEPTS.md").read_text()

    assert "### Control-Plane Postgres Backup And Restore" in readme
    assert "AWF control-plane database" in readme
    assert "workspace or project databases" in readme
    assert "docker compose -f docker/compose/local-service.yml exec -T postgres" in readme
    assert "pg_dump" in readme
    assert "pg_restore" in readme
    assert "docker compose -f docker/compose/local-service.yml stop api worker" in readme
    assert "before restore" in readme.lower()


@pytest.mark.unit
def test_readme_documents_local_service_upgrade_and_image_versioning() -> None:
    readme = Path("docs/CONCEPTS.md").read_text()

    assert "### Local Service Image Versioning" in readme
    assert "### Local Service Upgrade" in readme
    assert "awf-control-plane:local" in readme
    assert "docker compose -f docker/compose/local-service.yml build" in readme
    assert "uv run --python 3.12 --extra dev awf service bootstrap" in readme
    assert "docker build -t awf-agent-runtime:latest" in readme
    assert "docker image inspect awf-control-plane:local" in readme
    assert "docker image inspect awf-agent-runtime:latest" in readme
    assert "migrate" in readme


@pytest.mark.unit
def test_readme_documents_rollback_and_migration_handling() -> None:
    readme = Path("docs/CONCEPTS.md").read_text()

    assert "### Local Service Rollback" in readme
    assert "pre-upgrade backup" in readme
    assert "image rollback" in readme
    assert "database migration rollback" in readme
    assert "awf service logs --service migrate" in readme
    assert "do not delete the Postgres volume" in readme
    assert "backup" in readme


@pytest.mark.unit
def test_readme_documents_local_disaster_recovery() -> None:
    readme = Path("docs/CONCEPTS.md").read_text()

    assert "### Local Disaster Recovery" in readme
    assert "docker compose -f docker/compose/local-service.yml down --remove-orphans" in readme
    assert "${AWF_HOST_WORK_DIR}" in readme
    assert "quarantine" in readme.lower()
    assert "logs, artifacts, backups, and auth" in readme
    assert "Postgres volume" in readme
    assert "uv run --python 3.12 --extra dev awf service status --format pretty" in readme


@pytest.mark.unit
def test_getting_started_documents_service_worker_as_canonical_executor() -> None:
    readme = Path("docs/GETTING_STARTED.md").read_text()
    normalized_readme = " ".join(readme.split())

    assert "Use `awf workspace create`" in normalized_readme
    assert "PostgreSQL control-plane DB" in normalized_readme
    assert "service worker is the normal always-on executor" in normalized_readme.lower()


@pytest.mark.unit
def test_readme_documents_service_gc_command() -> None:
    readme = Path("docs/CLI_REFERENCE.md").read_text()

    assert "awf service gc" in readme
    assert "--execute" in readme
    assert "--min-age-hours" in readme
    assert "dry-run" in readme.lower()
    assert "control-plane database" in readme
    assert "log streams" in readme
