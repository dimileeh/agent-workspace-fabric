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
    assert "githubcli-archive-keyring.gpg" in dockerfile
    assert "gh" in dockerfile
    assert "COPY src ./src" in dockerfile
    assert "COPY migrations ./migrations" in dockerfile
    assert "COPY docker/compose/workspace.base.yml.j2 ./docker/compose/workspace.base.yml.j2" in dockerfile
    assert "uv sync --frozen --extra dev" in dockerfile

    expected_work_dir = "${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}"
    expected_host_home = "${AWF_HOST_HOME:-${HOME}}"
    expected_auth_mounts = {
        f"{expected_host_home}/.config/gh:{expected_host_home}/.config/gh:ro",
        f"{expected_host_home}/.config/gcloud:{expected_host_home}/.config/gcloud:ro",
        f"{expected_host_home}/.gitconfig:{expected_host_home}/.gitconfig:ro",
        f"{expected_host_home}/.ssh:{expected_host_home}/.ssh:ro",
        f"{expected_host_home}/.codex:{expected_host_home}/.codex:ro",
        f"{expected_host_home}/.claude:{expected_host_home}/.claude:ro",
        f"{expected_host_home}/.claude.json:{expected_host_home}/.claude.json:ro",
        f"{expected_host_home}/.gemini:{expected_host_home}/.gemini:ro",
    }
    for service_name in ("api", "worker"):
        volumes = services[service_name]["volumes"]
        assert "../..:/app" not in volumes
        assert f"{expected_host_home}:{expected_host_home}:ro" not in volumes
        assert "/var/run/docker.sock:/var/run/docker.sock" in volumes
        assert "/run/host-services/ssh-auth.sock:/run/host-services/ssh-auth.sock" in volumes
        assert f"{expected_work_dir}:{expected_work_dir}" in volumes
        assert expected_auth_mounts.issubset(set(volumes))
        environment = services[service_name]["environment"]
        assert environment["AWF_API_BASE_URL"] == "http://api:8000"
        assert environment["AWF_API_TOKEN"] == "${AWF_API_TOKEN:-local-dev-token}"
        assert environment["AWF_DATABASE_URL"].startswith("postgresql+asyncpg://")
        assert "@postgres:5432/" in environment["AWF_DATABASE_URL"]
        assert environment["AWF_WORK_DIR"] == expected_work_dir
        assert environment["AWF_HOST_HOME"] == expected_host_home
        assert (
            environment["GOOGLE_APPLICATION_CREDENTIALS"] == "${GOOGLE_APPLICATION_CREDENTIALS:-}"
        )
        assert environment["SSH_AUTH_SOCK"] == "/run/host-services/ssh-auth.sock"

    assert "awf-work" not in data.get("volumes", {})
    migrate_command = services["migrate"]["command"]
    assert "alembic upgrade head" in migrate_command
    assert "uv run" not in migrate_command

    for service_name in ("api", "worker"):
        assert "uv run" not in services[service_name]["command"]
        depends_on = services[service_name]["depends_on"]
        assert depends_on["postgres"]["condition"] == "service_healthy"
        assert depends_on["migrate"]["condition"] == "service_completed_successfully"
