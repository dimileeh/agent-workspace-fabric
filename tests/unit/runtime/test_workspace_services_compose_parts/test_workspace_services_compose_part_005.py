"""No-Docker compose coverage for profile-declared workspace services (part 5).

Shared fixtures/helpers live in
``test_workspace_services_compose_part_001``; this part imports them. Split
from the original monolithic module to stay under the first-party file line
limit (see ``test_core_decomposition_maintainability``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from awf.node.compose_manager import ComposeManager, WorkspaceComposeSpec
from awf.node.git_manager import WorktreeLayout
from awf.node.stack_launcher import ComposeStackLauncher, WorkspaceStackLaunchRequest
from awf.profiles.compose import (
    agent_environment_with_legacy_host_auth,
    agent_exec_env_passthrough,
    filter_hosted_env_passthrough_names,
)
from tests.unit.runtime.test_workspace_services_compose_parts import (
    test_workspace_services_compose_part_001 as _part_001,
)

_FIXTURE = _part_001._FIXTURE
_POSTGRES_FIXTURE = _part_001._POSTGRES_FIXTURE
_NODE_BROWSER_FIXTURE = _part_001._NODE_BROWSER_FIXTURE
_TEMPLATE = _part_001._TEMPLATE
_RecordingCompose = _part_001._RecordingCompose
_load_profile = _part_001._load_profile
_load_postgres_profile = _part_001._load_postgres_profile
_load_node_browser_profile = _part_001._load_node_browser_profile
_clear_host_auth = _part_001._clear_host_auth


@pytest.mark.unit
def test_agent_exec_env_passthrough_parses_compose_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exec-time passthrough reads/parses the compose file exactly once."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {"OPENAI_API_KEY": "${OPENAI_API_KEY}"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    import awf.profiles.compose as compose_module

    real_parse = compose_module._try_agent_environment_from_compose_file
    parse_calls = 0

    def _counting_parse(path: Path) -> dict[str, str] | None:
        nonlocal parse_calls
        parse_calls += 1
        return real_parse(path)

    monkeypatch.setattr(compose_module, "_try_agent_environment_from_compose_file", _counting_parse)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-worker")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-worker")

    passthrough = agent_exec_env_passthrough(compose_file=compose_file)

    assert parse_calls == 1
    assert "OPENAI_API_KEY" not in passthrough
    assert "ANTHROPIC_API_KEY" in passthrough


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

    assert set(parsed["services"]) == {"agent", "app", "clarification", "redis"}
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

    assert set(parsed["services"]) == {"agent", "app", "browser", "clarification"}
    assert "docker" not in parsed["services"]

    clarification = parsed["services"]["clarification"]
    assert clarification["profiles"] == ["awf-clarification"]
    assert clarification["networks"] == ["clarification_egress_net"]

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


@pytest.mark.unit
def test_filter_hosted_env_passthrough_names_carries_empty_bare_reference_override(
    tmp_path: Path,
) -> None:
    """A same-name bare reference with an empty worker value stays an empty override."""
    from awf.profiles.compose import literal_profile_env_from_compose

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "OPENAI_API_KEY": "${OPENAI_API_KEY}",
                            "PLAIN_EMPTY": "$PLAIN_EMPTY",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    worker_env = {"OPENAI_API_KEY": "", "PLAIN_EMPTY": ""}

    profile_env = dict(literal_profile_env_from_compose(compose_file, worker_env=worker_env))
    filtered = filter_hosted_env_passthrough_names(
        ("OPENAI_API_KEY", "PLAIN_EMPTY"),
        compose_file=compose_file,
        worker_env=worker_env,
    )

    assert profile_env["OPENAI_API_KEY"] == ""
    assert profile_env["PLAIN_EMPTY"] == ""
    assert "OPENAI_API_KEY" not in filtered
    assert "PLAIN_EMPTY" not in filtered
