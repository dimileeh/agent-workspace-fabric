"""DinD compose rendering regression tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf.node.compose_manager import ComposeManager
from awf.profiles.registry import docker_compose_profile
from tests.unit.node.test_compose_manager import _TEMPLATE, _spec


@pytest.fixture
def manager(tmp_path: Path) -> ComposeManager:
    """Provide a compose manager rooted in the test temp directory."""
    return ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)


class TestRenderDinD:
    @pytest.mark.unit
    def test_dind_profile_adds_docker_daemon(self, manager: ComposeManager, tmp_path: Path) -> None:
        """DinD mode injects the managed Docker daemon service."""
        profile = docker_compose_profile()
        spec = _spec(
            tmp_path,
            docker_mode=profile.docker.mode.value,
            agent_environment=tuple(profile.runtime.environment.items()),
        )
        parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())
        assert parsed["services"]["docker"]["image"] == "docker:27-dind"
        assert parsed["services"]["docker"]["privileged"] is True
        assert parsed["services"]["docker"]["environment"]["DOCKER_TLS_CERTDIR"] == ""
        assert parsed["services"]["docker"]["healthcheck"]["test"] == [
            "CMD-SHELL",
            "docker info >/dev/null 2>&1",
        ]
        assert parsed["services"]["agent"]["environment"]["DOCKER_HOST"] == "tcp://docker:2375"
        assert parsed["services"]["agent"]["depends_on"] == {
            "docker": {"condition": "service_healthy"}
        }
        assert parsed["volumes"]["dind_data"]["name"] == "awf-ws_test123-dind_data"

    @pytest.mark.unit
    def test_dind_daemon_uses_configured_dind_image(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """DinD mode uses the configured daemon image."""
        spec = _spec(
            tmp_path,
            docker_mode="dind",
            dind_image="ghcr.io/example/dind:buildx",
        )
        parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())
        assert parsed["services"]["docker"]["image"] == "ghcr.io/example/dind:buildx"

    @pytest.mark.unit
    def test_dind_mode_sets_default_agent_docker_host(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """DinD mode supplies the default agent Docker host."""
        parsed = yaml.safe_load(
            manager.render(_spec(tmp_path, docker_mode="dind")).compose_file.read_text()
        )

        assert parsed["services"]["agent"]["environment"]["DOCKER_HOST"] == "tcp://docker:2375"

    @pytest.mark.unit
    def test_explicit_agent_docker_host_is_preserved(
        self, manager: ComposeManager, tmp_path: Path
    ) -> None:
        """Explicit agent Docker host values override the DinD default."""
        parsed = yaml.safe_load(
            manager.render(
                _spec(
                    tmp_path,
                    docker_mode="dind",
                    agent_environment=(("DOCKER_HOST", "tcp://custom-docker:2375"),),
                )
            ).compose_file.read_text()
        )

        assert (
            parsed["services"]["agent"]["environment"]["DOCKER_HOST"] == "tcp://custom-docker:2375"
        )
