from __future__ import annotations

from pathlib import Path

import pytest


def _control_plane_dockerfile() -> str:
    return Path("docker/control-plane.Dockerfile").read_text(encoding="utf-8")


@pytest.mark.unit
def test_control_plane_installs_docker_cli_from_official_apt_repository() -> None:
    dockerfile = _control_plane_dockerfile()

    assert "download.docker.com/linux/debian" in dockerfile
    assert "docker.asc" in dockerfile
    assert "ARG DOCKER_CE_CLI_VERSION=" in dockerfile
    assert '"docker-ce-cli=${DOCKER_CE_CLI_VERSION}"' in dockerfile


@pytest.mark.unit
def test_control_plane_installs_docker_compose_plugin() -> None:
    dockerfile = _control_plane_dockerfile()

    assert "ARG DOCKER_COMPOSE_PLUGIN_VERSION=" in dockerfile
    assert '"docker-compose-plugin=${DOCKER_COMPOSE_PLUGIN_VERSION}"' in dockerfile


@pytest.mark.unit
def test_control_plane_installs_pinned_docker_buildx_plugin() -> None:
    dockerfile = _control_plane_dockerfile()

    assert "ARG DOCKER_BUILDX_PLUGIN_VERSION=" in dockerfile
    assert "ARG DOCKER_BUILDX_PLUGIN_VERSION=latest" not in dockerfile
    assert '"docker-buildx-plugin=${DOCKER_BUILDX_PLUGIN_VERSION}"' in dockerfile
    assert "docker buildx version" in dockerfile
