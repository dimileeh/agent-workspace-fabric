"""No-Docker compose coverage for profile-declared workspace services."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from awf.node.compose_manager import ComposeManager, ComposeProjectPaths, WorkspaceComposeSpec
from awf.node.git_manager import WorktreeLayout
from awf.node.stack_launcher import ComposeStackLauncher, WorkspaceStackLaunchRequest
from awf.profiles.compose import (
    AGENT_AUTH_ENV_VARS,
    agent_environment_with_github_token,
    agent_environment_with_legacy_host_auth,
    profile_agent_environment,
    profile_services,
    resolve_app_endpoints,
)
from awf.profiles.models import WorkspaceProfile
from awf.profiles.resolver import ProfileResolver

_FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "workspace_services" / "dockerized_app"
)
_POSTGRES_FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "workspace_services" / "python_postgres_app"
)
_NODE_BROWSER_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "workspace_services"
    / "node_next_browser_app"
)
_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


class _RecordingCompose:
    def __init__(self) -> None:
        self.specs: list[WorkspaceComposeSpec] = []
        self.waits: list[bool] = []

    async def up(
        self,
        spec: WorkspaceComposeSpec,
        *,
        wait: bool = True,
        on_compose_up_started: Any | None = None,
    ) -> ComposeProjectPaths:
        if on_compose_up_started is not None:
            await on_compose_up_started()
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


def _load_node_browser_profile() -> WorkspaceProfile:
    assert _NODE_BROWSER_FIXTURE.is_dir(), "node browser workspace-services fixture is missing"
    return (
        ProfileResolver()
        .resolve(
            worktree_path=_NODE_BROWSER_FIXTURE,
            profile_ref="auto",
        )
        .profile
    )


def _load_awf_self_profile() -> tuple[Path, WorkspaceProfile]:
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root, ProfileResolver().resolve(worktree_path=repo_root, profile_ref="auto").profile


def _clear_host_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*AGENT_AUTH_ENV_VARS, "AWF_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.unit
def test_resolve_app_endpoints_generates_deterministic_internal_urls() -> None:
    endpoints = {
        endpoint["name"]: endpoint
        for endpoint in resolve_app_endpoints(_load_node_browser_profile())
    }

    assert endpoints == {
        "app": {
            "name": "app",
            "service": "app",
            "scheme": "http",
            "port": 3000,
            "path": "/",
            "internal_url": "http://app:3000/",
            "visibility": "agent",
            "health": {
                "path": "/healthz",
                "method": "GET",
                "expected_status": 200,
                "internal_url": "http://app:3000/healthz",
            },
        },
        "browser_validation": {
            "name": "browser_validation",
            "service": "browser",
            "scheme": "http",
            "port": 9323,
            "path": "/validate",
            "internal_url": "http://browser:9323/validate",
            "visibility": "validation",
            "health": {
                "path": "/healthz",
                "method": "GET",
                "expected_status": 200,
                "internal_url": "http://browser:9323/healthz",
            },
        },
        "operator_notes": {
            "name": "operator_notes",
            "service": "app",
            "scheme": "http",
            "port": 3000,
            "path": "/operator",
            "internal_url": "http://app:3000/operator",
            "visibility": "console",
            "health": None,
        },
    }


@pytest.mark.unit
def test_profile_agent_environment_exposes_only_agent_and_validation_app_endpoints() -> None:
    env = dict(profile_agent_environment(_load_node_browser_profile()))

    assert env["AWF_APP_ENDPOINT_APP_URL"] == "http://app:3000/"
    assert env["AWF_APP_ENDPOINT_BROWSER_VALIDATION_URL"] == ("http://browser:9323/validate")
    assert "AWF_APP_ENDPOINT_OPERATOR_NOTES_URL" not in env

    endpoints = json.loads(env["AWF_APP_ENDPOINTS_JSON"])
    assert [endpoint["name"] for endpoint in endpoints] == ["app", "browser_validation"]
    assert endpoints[0]["internal_url"] == "http://app:3000/"
    assert endpoints[1]["health"]["internal_url"] == "http://browser:9323/healthz"


@pytest.mark.unit
def test_awf_self_profile_renders_workspace_local_test_postgres(
    tmp_path: Path,
) -> None:
    repo_root, profile = _load_awf_self_profile()
    manager = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
    paths = manager.render(
        WorkspaceComposeSpec(
            workspace_id="ws_awf_self",
            worktree_host_path=repo_root,
            postgres_password="workspace-secret",
            agent_environment=profile_agent_environment(profile),
            services=profile_services(profile, base_path=repo_root),
        )
    )

    rendered = paths.compose_file.read_text(encoding="utf-8")
    parsed = yaml.safe_load(rendered)
    agent = parsed["services"]["agent"]
    postgres = parsed["services"]["postgres"]

    assert agent["environment"]["AWF_DATABASE_URL"] == (
        "postgresql+asyncpg://awf:workspace-secret@postgres:5432/awf"
    )
    assert agent["environment"]["AWF_TEST_DATABASE_URL"] == (
        "postgresql+asyncpg://awf:workspace-secret@postgres:5432/awf"
    )
    assert "host.docker.internal:5433" not in rendered
    assert agent["depends_on"] == {"postgres": {"condition": "service_healthy"}}
    assert postgres["image"] == "postgres:16-alpine"
    assert postgres["environment"] == {
        "POSTGRES_DB": "awf",
        "POSTGRES_PASSWORD": "workspace-secret",
        "POSTGRES_USER": "awf",
    }
    assert "ports" not in postgres
    assert parsed["volumes"]["postgres_data"]["name"] == "awf-ws_awf_self-postgres_data"


@pytest.mark.unit
def test_github_token_placeholder_preserves_profile_supplied_agent_env() -> None:
    env = agent_environment_with_github_token(
        (("GH_TOKEN", "${WORKSPACE_GH_TOKEN}"),),
        host_env={"AWF_GITHUB_TOKEN": "ghp_host_secret"},
    )

    assert env == (
        ("GH_TOKEN", "${WORKSPACE_GH_TOKEN}"),
        ("GITHUB_TOKEN", "${AWF_GITHUB_TOKEN}"),
    )


@pytest.mark.unit
def test_profile_ollama_host_suppresses_worker_base_url_placeholder() -> None:
    # The profile owns the daemon by declaring only the lower-precedence
    # OLLAMA_HOST. A stale higher-precedence AWF_OPENCODE_OLLAMA_BASE_URL in the
    # worker env must NOT be injected, or the agent's OpenCode launcher would
    # talk to a different daemon than AWF's preflight readied.
    env = agent_environment_with_legacy_host_auth(
        (("OLLAMA_HOST", "http://ollama.profile:11434"),),
        host_env={"AWF_OPENCODE_OLLAMA_BASE_URL": "http://stale.worker:11434/v1"},
    )

    assert env == (("OLLAMA_HOST", "http://ollama.profile:11434"),)


@pytest.mark.unit
def test_profile_base_url_still_allows_worker_ollama_host_placeholder() -> None:
    # The profile declares the highest-precedence key; the lower-precedence
    # OLLAMA_HOST cannot shadow it, so injecting the worker value is harmless.
    env = agent_environment_with_legacy_host_auth(
        (("AWF_OPENCODE_OLLAMA_BASE_URL", "http://ollama.profile:11434/v1"),),
        host_env={"OLLAMA_HOST": "http://worker:11434"},
    )

    assert env == (
        ("AWF_OPENCODE_OLLAMA_BASE_URL", "http://ollama.profile:11434/v1"),
        ("OLLAMA_HOST", "${OLLAMA_HOST}"),
    )


@pytest.mark.unit
def test_worker_ollama_base_url_injected_when_profile_declares_none() -> None:
    # No profile-declared Ollama key — the worker base URL flows through as a
    # placeholder unchanged (the pre-existing behavior).
    env = agent_environment_with_legacy_host_auth(
        (),
        host_env={"AWF_OPENCODE_OLLAMA_BASE_URL": "http://worker:11434/v1"},
    )

    assert env == (("AWF_OPENCODE_OLLAMA_BASE_URL", "${AWF_OPENCODE_OLLAMA_BASE_URL}"),)


@pytest.mark.unit
def test_opencode_bash_timeout_env_reaches_agent_as_placeholder() -> None:
    env = agent_environment_with_legacy_host_auth(
        (),
        host_env={"OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS": "600000"},
    )

    assert env == (
        (
            "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS",
            "${OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS}",
        ),
    )


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


async def _launched_node_browser_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> WorkspaceComposeSpec:
    _clear_host_auth(monkeypatch)
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="node:22-bookworm-slim",
    )
    layout = WorktreeLayout(
        mirror_path=tmp_path / "mirror.git",
        worktree_path=_NODE_BROWSER_FIXTURE,
        branch_name="awf/ws-node-browser",
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_node_browser",
            layout=layout,
            profile=_load_node_browser_profile(),
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
            'python -c "import urllib.request; '
            "urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5).read()\""
        ),
    ]
    assert app["networks"] == ["awf_net"]
    assert "ports" not in app

    postgres = parsed["services"]["postgres"]
    assert postgres["image"] == "postgres:16-alpine"
    assert postgres["environment"] == {
        "POSTGRES_DB": "awf",
        "POSTGRES_HOST_AUTH_METHOD": "trust",
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


@pytest.mark.unit
async def test_stack_launcher_builds_node_next_browser_profile_service_spec_from_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = await _launched_node_browser_spec(tmp_path, monkeypatch)

    assert spec.workspace_id == "ws_node_browser"
    assert spec.worktree_host_path == _NODE_BROWSER_FIXTURE
    assert spec.agent_runtime_image == "node:22-bookworm-slim"
    assert spec.docker_mode == "none"
    agent_environment = dict(spec.agent_environment)
    assert agent_environment["APP_BASE_URL"] == "http://app:3000"
    assert agent_environment["BROWSER_VALIDATE_URL"] == "http://browser:9323/validate"
    assert agent_environment["AWF_APP_ENDPOINT_APP_URL"] == "http://app:3000/"
    assert agent_environment["AWF_APP_ENDPOINT_BROWSER_VALIDATION_URL"] == (
        "http://browser:9323/validate"
    )

    services = {service.name: service for service in spec.services}
    assert set(services) == {"app", "browser"}
    assert services["app"].build_context == str(_NODE_BROWSER_FIXTURE.resolve())
    assert services["app"].environment == (("PORT", "3000"),)
    assert services["app"].depends_on == ()
    assert services["browser"].build_context == str(_NODE_BROWSER_FIXTURE.resolve())
    assert services["browser"].dockerfile == "Dockerfile.playwright"
    assert services["browser"].depends_on == ("app",)


@pytest.mark.unit
async def test_rendered_node_next_browser_compose_expresses_browser_validation_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = await _launched_node_browser_spec(tmp_path, monkeypatch)
    manager = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)

    parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())

    assert set(parsed["services"]) == {"agent", "app", "browser"}
    assert "docker" not in parsed["services"]

    app = parsed["services"]["app"]
    assert app["build"] == {
        "context": str(_NODE_BROWSER_FIXTURE.resolve()),
        "dockerfile": "Dockerfile",
    }
    assert app["environment"] == {"PORT": "3000"}
    assert app["healthcheck"]["test"] == [
        "CMD-SHELL",
        "node /app/scripts/container-healthcheck.mjs http://127.0.0.1:3000/healthz ok",
    ]
    assert app["command"] == "node /app/server.mjs"
    assert app["networks"] == ["awf_net"]
    assert "ports" not in app

    browser = parsed["services"]["browser"]
    assert browser["build"] == {
        "context": str(_NODE_BROWSER_FIXTURE.resolve()),
        "dockerfile": "Dockerfile.playwright",
    }
    assert browser["environment"] == {
        "APP_BASE_URL": "http://app:3000",
        "PORT": "9323",
    }
    assert browser["depends_on"] == {"app": {"condition": "service_healthy"}}
    assert browser["healthcheck"]["test"] == [
        "CMD-SHELL",
        "node /app/scripts/container-healthcheck.mjs http://127.0.0.1:9323/healthz ok",
    ]
    assert browser["command"] == "node /app/browser/validator-server.mjs"
    assert browser["networks"] == ["awf_net"]
    assert "ports" not in browser

    agent = parsed["services"]["agent"]
    assert agent["environment"]["APP_BASE_URL"] == "http://app:3000"
    assert agent["environment"]["BROWSER_VALIDATE_URL"] == "http://browser:9323/validate"
    assert agent["environment"]["AWF_APP_ENDPOINT_APP_URL"] == "http://app:3000/"
    assert agent["environment"]["AWF_APP_ENDPOINT_BROWSER_VALIDATION_URL"] == (
        "http://browser:9323/validate"
    )
    assert [
        endpoint["name"] for endpoint in json.loads(agent["environment"]["AWF_APP_ENDPOINTS_JSON"])
    ] == ["app", "browser_validation"]
    assert agent["depends_on"] == {
        "app": {"condition": "service_healthy"},
        "browser": {"condition": "service_healthy"},
    }
    assert parsed["volumes"] == {}
    assert parsed["networks"]["awf_net"]["name"] == "awf-ws_node_browser-net"
