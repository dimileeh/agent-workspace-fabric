"""Static contract tests for the local AWF service Docker Compose stack."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.mark.integration
def test_local_service_compose_declares_control_plane_stack() -> None:
    compose_path = Path("docker/compose/local-service.yml")
    dockerfile_path = Path("docker/control-plane.Dockerfile")

    assert compose_path.exists()
    assert dockerfile_path.exists()
    data = yaml.safe_load(compose_path.read_text())
    services = data["services"]

    assert {"postgres", "migrate", "api", "worker"}.issubset(services)

    for service_name in ("migrate", "api", "worker"):
        assert services[service_name]["image"] == "awf-control-plane:local"
        assert services[service_name]["build"] == {
            "context": "../..",
            "dockerfile": "docker/control-plane.Dockerfile",
        }

    dockerfile = dockerfile_path.read_text()
    assert "docker-ce-cli" in dockerfile
    assert "docker-compose-plugin" in dockerfile

    for service_name in ("api", "worker"):
        volumes = services[service_name]["volumes"]
        assert "/var/run/docker.sock:/var/run/docker.sock" in volumes
        environment = services[service_name]["environment"]
        assert environment["AWF_API_BASE_URL"] == "http://api:8000"
        assert environment["AWF_API_TOKEN"] == "${AWF_API_TOKEN:-local-dev-token}"
        assert environment["AWF_DATABASE_URL"].startswith("postgresql+asyncpg://")
        assert "@postgres:5432/" in environment["AWF_DATABASE_URL"]
        assert environment["UV_PROJECT_ENVIRONMENT"] == "/tmp/awf-venv"
        assert environment["UV_LINK_MODE"] == "copy"

    migrate_command = services["migrate"]["command"]
    assert "alembic upgrade head" in migrate_command

    for service_name in ("api", "worker"):
        depends_on = services[service_name]["depends_on"]
        assert depends_on["postgres"]["condition"] == "service_healthy"
        assert depends_on["migrate"]["condition"] == "service_completed_successfully"
