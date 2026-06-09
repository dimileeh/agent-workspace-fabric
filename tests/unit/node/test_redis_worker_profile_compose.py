"""Compose rendering contract for the generic Redis/app/worker fixture profile."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf.node.compose_manager import ComposeManager, WorkspaceComposeSpec
from awf.profiles.compose import profile_services
from awf.profiles.resolver import ProfileResolver

_FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "workspace_services" / "redis_worker_app"
)
_TEMPLATE = Path(__file__).resolve().parents[3] / "docker" / "compose" / "workspace.base.yml.j2"


@pytest.fixture
def manager(tmp_path: Path) -> ComposeManager:
    return ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)


def _rendered_profile_compose(manager: ComposeManager) -> dict[str, object]:
    assert _FIXTURE.is_dir(), "redis-worker workspace-services fixture is missing"
    profile = ProfileResolver().resolve(worktree_path=_FIXTURE, profile_ref="auto").profile
    spec = WorkspaceComposeSpec(
        workspace_id="ws_redis_worker",
        worktree_host_path=_FIXTURE,
        agent_runtime_image="python:3.12-alpine",
        postgres_password="unused-by-fixture",
        agent_environment=tuple(profile.runtime.environment.items()),
        services=profile_services(profile, base_path=_FIXTURE),
    )

    return yaml.safe_load(manager.render(spec).compose_file.read_text())


@pytest.mark.unit
def test_redis_worker_profile_renders_agent_and_services_on_workspace_network(
    manager: ComposeManager,
) -> None:
    parsed = _rendered_profile_compose(manager)
    services = parsed["services"]

    assert set(services) == {"agent", "redis", "app", "worker"}
    assert services["agent"]["networks"] == ["awf_net"]
    assert services["redis"]["networks"] == ["awf_net"]
    assert services["app"]["networks"] == ["awf_net"]
    assert services["worker"]["networks"] == ["awf_net"]


@pytest.mark.unit
def test_redis_worker_profile_renders_service_health_waits(
    manager: ComposeManager,
) -> None:
    parsed = _rendered_profile_compose(manager)
    services = parsed["services"]

    assert services["redis"]["healthcheck"]["test"] == ["CMD-SHELL", "redis-cli ping"]
    assert services["app"]["healthcheck"]["test"] == [
        "CMD-SHELL",
        "python /app/scripts/container_healthcheck.py app",
    ]
    assert services["worker"]["healthcheck"]["test"] == [
        "CMD-SHELL",
        "python /app/scripts/container_healthcheck.py worker",
    ]
    assert services["app"]["depends_on"] == {"redis": {"condition": "service_healthy"}}
    assert services["worker"]["depends_on"] == {"redis": {"condition": "service_healthy"}}
    assert services["agent"]["depends_on"] == {
        "redis": {"condition": "service_healthy"},
        "app": {"condition": "service_healthy"},
        "worker": {"condition": "service_healthy"},
    }


@pytest.mark.unit
def test_redis_worker_profile_renders_prefixed_named_volume_and_no_host_ports(
    manager: ComposeManager,
) -> None:
    parsed = _rendered_profile_compose(manager)
    services = parsed["services"]

    assert parsed["volumes"] == {"redis_data": {"name": "awf-ws_redis_worker-redis_data"}}
    assert services["redis"]["volumes"] == ["redis_data:/data"]
    assert "ports" not in services["redis"]
    assert "ports" not in services["app"]
    assert "ports" not in services["worker"]


@pytest.mark.unit
def test_redis_worker_profile_renders_worktree_local_build_contexts(
    manager: ComposeManager,
) -> None:
    parsed = _rendered_profile_compose(manager)
    services = parsed["services"]
    expected_build = {"context": str(_FIXTURE.resolve()), "dockerfile": "Dockerfile"}

    assert services["app"]["build"] == expected_build
    assert services["worker"]["build"] == expected_build
    assert all("aira" not in name.lower() for name in services)
    assert "aira" not in yaml.safe_dump(parsed["networks"]).lower()
    assert "aira" not in yaml.safe_dump(parsed["volumes"]).lower()
