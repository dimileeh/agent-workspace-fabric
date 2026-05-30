"""Tests for the ``ProfileDocker`` workspace Docker configuration model."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from awf.profiles.models import DockerMode, ProfileDocker
from awf.profiles.registry import docker_compose_profile


@pytest.mark.unit
def test_profile_docker_dind_image_defaults_to_official_image() -> None:
    assert ProfileDocker().dind_image == "docker:27-dind"
    assert docker_compose_profile().docker.dind_image == "docker:27-dind"


@pytest.mark.unit
def test_profile_docker_dind_image_accepts_override() -> None:
    docker = ProfileDocker(mode=DockerMode.dind, dind_image="ghcr.io/example/dind:buildx")
    assert docker.dind_image == "ghcr.io/example/dind:buildx"


@pytest.mark.unit
def test_profile_docker_rejects_empty_dind_image() -> None:
    with pytest.raises(ValidationError):
        ProfileDocker(dind_image="")
