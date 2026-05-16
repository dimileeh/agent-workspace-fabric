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

    assert {"postgres", "migrate", "api", "worker", "ollama-bridge"}.issubset(services)

    for service_name in ("migrate", "api", "worker"):
        assert services[service_name]["image"] == "awf-control-plane:local"
        assert services[service_name]["build"] == {
            "context": "../..",
            "dockerfile": "docker/control-plane.Dockerfile",
        }

    dockerfile = dockerfile_path.read_text()
    assert "ARG DOCKER_CE_CLI_VERSION=" in dockerfile
    assert '"docker-ce-cli=${DOCKER_CE_CLI_VERSION}"' in dockerfile
    assert "ARG DOCKER_COMPOSE_PLUGIN_VERSION=" in dockerfile
    assert '"docker-compose-plugin=${DOCKER_COMPOSE_PLUGIN_VERSION}"' in dockerfile
    assert "githubcli-archive-keyring.gpg" in dockerfile
    assert "gh" in dockerfile
    assert "COPY src ./src" in dockerfile
    assert "COPY migrations ./migrations" in dockerfile
    assert (
        "COPY docker/compose/workspace.base.yml.j2 ./docker/compose/workspace.base.yml.j2"
        in dockerfile
    )
    assert "uv sync --frozen --extra dev" in dockerfile

    expected_work_dir = "${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}"
    expected_host_home = "${AWF_HOST_HOME:-${HOME}}"
    expected_ssh_auth_sock_source = (
        "${AWF_HOST_SSH_AUTH_SOCK:-${SSH_AUTH_SOCK:-/run/host-services/ssh-auth.sock}}"
    )
    expected_ssh_auth_sock_target = "/run/host-services/ssh-auth.sock"
    expected_auth_mounts = {
        f"{expected_host_home}/.config/gh:{expected_host_home}/.config/gh:ro",
        f"{expected_host_home}/.config/gcloud:{expected_host_home}/.config/gcloud:ro",
        f"{expected_host_home}/.gitconfig:{expected_host_home}/.gitconfig:ro",
        f"{expected_host_home}/.ssh:{expected_host_home}/.ssh:ro",
        f"{expected_host_home}/.codex:{expected_host_home}/.codex:ro",
        f"{expected_host_home}/.claude:{expected_host_home}/.claude:ro",
        f"{expected_host_home}/.claude.json:{expected_host_home}/.claude.json:ro",
        f"{expected_host_home}/.gemini:{expected_host_home}/.gemini:ro",
        f"{expected_host_home}/.config/opencode:{expected_host_home}/.config/opencode:ro",
        f"{expected_host_home}/.ollama:{expected_host_home}/.ollama:ro",
    }
    for service_name in ("api", "worker"):
        volumes = services[service_name]["volumes"]
        assert "../..:/app" not in volumes
        assert f"{expected_host_home}:{expected_host_home}:ro" not in volumes
        assert services[service_name]["extra_hosts"] == ["host.docker.internal:host-gateway"]
        assert "/var/run/docker.sock:/var/run/docker.sock" in volumes
        assert f"{expected_ssh_auth_sock_source}:{expected_ssh_auth_sock_target}" in volumes
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
        assert environment["AWF_OPENCODE_OLLAMA_BASE_URL"] == "${AWF_OPENCODE_OLLAMA_BASE_URL:-}"
        assert environment["OLLAMA_HOST"] == "${OLLAMA_HOST:-}"
        assert environment["OLLAMA_API_KEY"] == "${OLLAMA_API_KEY:-}"
        assert environment["AWF_AGENT_IDLE_TIMEOUT_SECONDS"] == (
            "${AWF_AGENT_IDLE_TIMEOUT_SECONDS:-3600}"
        )
        assert environment["AWF_WORKSPACE_STEADY_CPU"] == "${AWF_WORKSPACE_STEADY_CPU:-3}"
        assert environment["AWF_WORKSPACE_STEADY_MEMORY_GB"] == (
            "${AWF_WORKSPACE_STEADY_MEMORY_GB:-10}"
        )
        assert environment["AWF_WORKSPACE_PEAK_CPU"] == "${AWF_WORKSPACE_PEAK_CPU:-6}"
        assert environment["AWF_WORKSPACE_PEAK_MEMORY_GB"] == (
            "${AWF_WORKSPACE_PEAK_MEMORY_GB:-16}"
        )
        assert environment["AWF_LOCAL_CAPACITY_CPU_CORES"] == ("${AWF_LOCAL_CAPACITY_CPU_CORES:-}")
        assert environment["AWF_LOCAL_CAPACITY_MEMORY_GB"] == ("${AWF_LOCAL_CAPACITY_MEMORY_GB:-}")
        assert environment["AWF_LOCAL_CAPACITY_DIND_SLOTS"] == (
            "${AWF_LOCAL_CAPACITY_DIND_SLOTS:-}"
        )
        assert environment["SSH_AUTH_SOCK"] == expected_ssh_auth_sock_target

    assert "awf-work" not in data.get("volumes", {})
    migrate_command = services["migrate"]["command"]
    assert "alembic upgrade head" in migrate_command
    assert "uv run" not in migrate_command

    for service_name in ("api", "worker"):
        assert "uv run" not in services[service_name]["command"]
        depends_on = services[service_name]["depends_on"]
        assert depends_on["postgres"]["condition"] == "service_healthy"
        assert depends_on["migrate"]["condition"] == "service_completed_successfully"

    bridge = services["ollama-bridge"]
    assert bridge["profiles"] == ["ollama-bridge"]
    assert bridge["image"] == "alpine/socat:1.8.0.3"
    assert bridge["network_mode"] == "host"
    assert bridge["command"] == [
        "TCP-LISTEN:${AWF_OLLAMA_BRIDGE_LISTEN_PORT:-11434},bind=${AWF_OLLAMA_BRIDGE_BIND_ADDRESS:-172.17.0.1},fork,reuseaddr",
        "TCP:${AWF_OLLAMA_BRIDGE_TARGET_HOST:-127.0.0.1}:${AWF_OLLAMA_BRIDGE_TARGET_PORT:-11434}",
    ]
