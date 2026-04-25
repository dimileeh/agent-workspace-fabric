"""Static contract tests for the local AWF service Docker Compose stack."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.mark.integration
def test_local_service_compose_declares_control_plane_stack() -> None:
    compose_path = Path("docker/compose/local-service.yml")

    assert compose_path.exists()
    data = yaml.safe_load(compose_path.read_text())
    services = data["services"]

    assert {"postgres", "migrate", "api", "worker"}.issubset(services)

    for service_name in ("api", "worker"):
        volumes = services[service_name]["volumes"]
        assert "/var/run/docker.sock:/var/run/docker.sock" in volumes
        environment = services[service_name]["environment"]
        assert environment["AWF_DATABASE_URL"].startswith("postgresql+asyncpg://")
        assert "@postgres:5432/" in environment["AWF_DATABASE_URL"]

    migrate_command = services["migrate"]["command"]
    assert "alembic upgrade head" in migrate_command

    for service_name in ("api", "worker"):
        depends_on = services[service_name]["depends_on"]
        assert depends_on["postgres"]["condition"] == "service_healthy"
        assert depends_on["migrate"]["condition"] == "service_completed_successfully"
