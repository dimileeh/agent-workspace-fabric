from __future__ import annotations

from pathlib import Path

import pytest


def _agent_runtime_dockerfile() -> str:
    return Path("docker/agent-runtime.Dockerfile").read_text(encoding="utf-8")


@pytest.mark.unit
def test_agent_runtime_installs_github_cli_from_official_apt_repository() -> None:
    dockerfile = _agent_runtime_dockerfile()

    assert "cli.github.com/packages" in dockerfile
    assert "githubcli-archive-keyring.gpg" in dockerfile
    assert "gh=${GH_VERSION}" in dockerfile
    assert "gh --version" in dockerfile


@pytest.mark.unit
def test_agent_runtime_installs_docker_cli_from_official_apt_repository() -> None:
    dockerfile = _agent_runtime_dockerfile()

    assert "download.docker.com/linux/debian" in dockerfile
    assert "docker.asc" in dockerfile
    assert "ARG DOCKER_CE_CLI_VERSION=" in dockerfile
    assert '"docker-ce-cli=${DOCKER_CE_CLI_VERSION}"' in dockerfile
    assert "docker --version" in dockerfile


@pytest.mark.unit
def test_agent_runtime_installs_docker_compose_plugin() -> None:
    dockerfile = _agent_runtime_dockerfile()

    assert "ARG DOCKER_COMPOSE_PLUGIN_VERSION=" in dockerfile
    assert '"docker-compose-plugin=${DOCKER_COMPOSE_PLUGIN_VERSION}"' in dockerfile
    assert "docker compose version" in dockerfile


@pytest.mark.unit
def test_readme_notes_agent_runtime_rebuild_for_docker_tooling_changes() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    start = readme.index("### Build the Agent Runtime Image")
    end = readme.index("### Configure Environment")
    section = readme[start:end]

    assert "Docker CLI" in section
    assert "Docker Compose plugin" in section
    assert "rebuild" in section.lower()
    assert "docker build -t awf-agent-runtime:latest" in section
