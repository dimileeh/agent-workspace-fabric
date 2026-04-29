"""No-Docker compose coverage for profile-declared workspace services."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf.node.compose_manager import ComposeManager, ComposeProjectPaths, WorkspaceComposeSpec
from awf.node.git_manager import WorktreeLayout
from awf.node.stack_launcher import ComposeStackLauncher, WorkspaceStackLaunchRequest
from awf.profiles.compose import AGENT_AUTH_ENV_VARS
from awf.profiles.models import WorkspaceProfile
from awf.profiles.resolver import ProfileResolver

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "workspace_services" / "dockerized_app"
_POSTGRES_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "workspace_services"
    / "python_postgres_app"
)
_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


class _RecordingCompose:
    def __init__(self) -> None:
        self.specs: list[WorkspaceComposeSpec] = []
        self.waits: list[bool] = []

    async def up(self, spec: WorkspaceComposeSpec, *, wait: bool = True) -> ComposeProjectPaths:
        self.specs.append(spec)
        self.waits.append(wait)
        return ComposeProjectPaths(
            project_dir=Path("/tmp/awf-compose/ws_services"),
            compose_file=Path("/tmp/awf-compose/ws_services/compose.yml"),
        )


def _load_profile() -> WorkspaceProfile:
    assert _FIXTURE.is_dir(), "workspace-services fixture is missing"
    return ProfileResolver().resolve(worktree_path=_FIXTURE, profile_ref="auto").profile


def _load_postgres_profile() -> WorkspaceProfile:
    assert _POSTGRES_FIXTURE.is_dir(), "python-postgres workspace-services fixture is missing"
    return ProfileResolver().resolve(worktree_path=_POSTGRES_FIXTURE, profile_ref="auto").profile


def _clear_host_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*AGENT_AUTH_ENV_VARS, "AWF_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)


async def _launched_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> WorkspaceComposeSpec:
    _clear_host_auth(monkeypatch)
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="awf-agent-runtime:test",
    )
    layout = WorktreeLayout(
        mirror_path=tmp_path / "mirror.git",
        worktree_path=_FIXTURE,
        branch_name="awf/ws-services",
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_services",
            layout=layout,
            profile=_load_profile(),
        )
    )

    assert compose.waits == [True]
    return compose.specs[0]


async def _launched_postgres_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> WorkspaceComposeSpec:
    _clear_host_auth(monkeypatch)
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="python:3.12-alpine",
    )
    layout = WorktreeLayout(
        mirror_path=tmp_path / "mirror.git",
        worktree_path=_POSTGRES_FIXTURE,
        branch_name="awf/ws-python-postgres",
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_python_pg",
            layout=layout,
            profile=_load_postgres_profile(),
        )
    )

    assert compose.waits == [True]
    return compose.specs[0]


@pytest.mark.unit
async def test_stack_launcher_builds_profile_service_spec_from_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = await _launched_spec(tmp_path, monkeypatch)

    assert spec.workspace_id == "ws_services"
    assert spec.worktree_host_path == _FIXTURE
    assert spec.agent_runtime_image == "awf-agent-runtime:test"
    assert spec.docker_mode == "none"
    assert dict(spec.agent_environment) == {
        "APP_BASE_URL": "http://app:8080",
        "CACHE_URL": "redis://redis:6379/0",
    }
    assert spec.git_name == "AWF Agent"
    assert spec.git_email == "awf@example.com"
    assert spec.auth_mounts[0].source == str(tmp_path / "mirror.git")
    assert spec.auth_mounts[0].mode == "rw"

    services = {service.name: service for service in spec.services}
    assert set(services) == {"app", "redis"}
    assert services["app"].build_context == str(_FIXTURE.resolve())
    assert services["app"].env_file == str((_FIXTURE / "app.env").resolve())
    assert services["app"].depends_on == ("redis",)
    assert services["redis"].image == "redis:7-alpine"


@pytest.mark.unit
async def test_rendered_workspace_services_compose_expresses_sidecar_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = await _launched_spec(tmp_path, monkeypatch)
    manager = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)

    parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())

    assert set(parsed["services"]) == {"agent", "app", "redis"}
    assert "docker" not in parsed["services"]

    app = parsed["services"]["app"]
    assert app["build"] == {
        "context": str(_FIXTURE.resolve()),
        "dockerfile": "Dockerfile",
    }
    assert app["env_file"] == [str((_FIXTURE / "app.env").resolve())]
    assert app["environment"] == {
        "CACHE_URL": "redis://redis:6379/0",
        "PORT": "8080",
    }
    assert app["depends_on"] == {"redis": {"condition": "service_healthy"}}
    assert app["healthcheck"]["test"] == [
        "CMD-SHELL",
        "wget -qO- http://127.0.0.1:8080/healthz >/dev/null",
    ]
    assert app["ports"] == ["18080:8080"]
    assert app["networks"] == ["awf_net"]

    redis = parsed["services"]["redis"]
    assert redis["image"] == "redis:7-alpine"
    assert redis["environment"] == {"REDIS_PORT": "6379"}
    assert redis["healthcheck"]["test"] == ["CMD-SHELL", "redis-cli ping"]
    assert redis["ports"] == ["16379:6379"]
    assert redis["networks"] == ["awf_net"]

    agent = parsed["services"]["agent"]
    assert agent["environment"]["APP_BASE_URL"] == "http://app:8080"
    assert agent["environment"]["CACHE_URL"] == "redis://redis:6379/0"
    assert agent["depends_on"] == {
        "app": {"condition": "service_healthy"},
        "redis": {"condition": "service_healthy"},
    }
    assert parsed["networks"]["awf_net"]["name"] == "awf-ws_services-net"


@pytest.mark.unit
async def test_stack_launcher_builds_python_postgres_profile_service_spec_from_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = await _launched_postgres_spec(tmp_path, monkeypatch)

    assert spec.workspace_id == "ws_python_pg"
    assert spec.worktree_host_path == _POSTGRES_FIXTURE
    assert spec.agent_runtime_image == "python:3.12-alpine"
    assert spec.docker_mode == "none"
    assert dict(spec.agent_environment) == {
        "APP_BASE_URL": "http://app:8080",
        "DATABASE_URL": "postgresql://awf:${AWF_POSTGRES_PASSWORD}@postgres:5432/awf",
    }

    services = {service.name: service for service in spec.services}
    assert set(services) == {"app", "postgres"}
    assert services["app"].build_context == str(_POSTGRES_FIXTURE.resolve())
    assert services["app"].depends_on == ("postgres",)
    assert services["app"].environment == (
        ("DATABASE_URL", "postgresql://awf:${AWF_POSTGRES_PASSWORD}@postgres:5432/awf"),
        ("PORT", "8080"),
    )
    assert services["postgres"].image == "postgres:16-alpine"
    assert services["postgres"].volumes == (("postgres_data", "/var/lib/postgresql/data"),)


@pytest.mark.unit
async def test_rendered_python_postgres_compose_expresses_db_backed_service_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = await _launched_postgres_spec(tmp_path, monkeypatch)
    spec = WorkspaceComposeSpec(
        workspace_id=spec.workspace_id,
        worktree_host_path=spec.worktree_host_path,
        agent_runtime_image=spec.agent_runtime_image,
        agent_environment=spec.agent_environment,
        docker_mode=spec.docker_mode,
        postgres_password="deterministic-postgres-password",
        auth_mounts=spec.auth_mounts,
        git_name=spec.git_name,
        git_email=spec.git_email,
        services=spec.services,
    )
    manager = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)

    parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())

    assert set(parsed["services"]) == {"agent", "app", "postgres"}
    assert "docker" not in parsed["services"]

    app = parsed["services"]["app"]
    assert app["build"] == {
        "context": str(_POSTGRES_FIXTURE.resolve()),
        "dockerfile": "Dockerfile",
    }
    assert app["environment"] == {
        "DATABASE_URL": "postgresql://awf:deterministic-postgres-password@postgres:5432/awf",
        "PORT": "8080",
    }
    assert app["depends_on"] == {"postgres": {"condition": "service_healthy"}}
    assert app["healthcheck"]["test"] == [
        "CMD-SHELL",
        (
            "python -c \"import urllib.request; "
            "urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5).read()\""
        ),
    ]
    assert app["networks"] == ["awf_net"]
    assert "ports" not in app

    postgres = parsed["services"]["postgres"]
    assert postgres["image"] == "postgres:16-alpine"
    assert postgres["environment"] == {
        "POSTGRES_DB": "awf",
        "POSTGRES_PASSWORD": "deterministic-postgres-password",
        "POSTGRES_USER": "awf",
    }
    assert postgres["healthcheck"]["test"] == ["CMD-SHELL", "pg_isready -U awf -d awf"]
    assert postgres["volumes"] == ["postgres_data:/var/lib/postgresql/data"]
    assert postgres["networks"] == ["awf_net"]
    assert "ports" not in postgres

    agent = parsed["services"]["agent"]
    assert agent["environment"]["APP_BASE_URL"] == "http://app:8080"
    assert (
        agent["environment"]["DATABASE_URL"]
        == "postgresql://awf:deterministic-postgres-password@postgres:5432/awf"
    )
    assert agent["depends_on"] == {
        "app": {"condition": "service_healthy"},
        "postgres": {"condition": "service_healthy"},
    }
    assert parsed["volumes"]["postgres_data"]["name"] == "awf-ws_python_pg-postgres_data"
    assert parsed["networks"]["awf_net"]["name"] == "awf-ws_python_pg-net"
