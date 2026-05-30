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


@pytest.mark.unit
def test_profile_docker_accepts_canonical_image_references() -> None:
    # Registry/host/digest forms must keep working — the guard is a grammar, not a denylist.
    for ref in (
        "docker:27-dind",
        "ghcr.io/example/dind:buildx",
        "registry.example.com:5000/team/dind:latest",
        "docker@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    ):
        assert ProfileDocker(dind_image=ref).dind_image == ref


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        'docker:27-dind"\nprivileged: true',  # YAML scalar breakout + key injection
        'docker:27-dind"',  # bare embedded quote closes the scalar
        "docker 27-dind",  # space is illegal in an image reference
        "-docker:27-dind",  # must start with an alphanumeric
    ],
)
def test_profile_docker_rejects_yaml_injection_in_dind_image(payload: str) -> None:
    # dind_image is interpolated unescaped into image: "{{ s.image }}" in the
    # compose Jinja template (autoescape=False); reject illegal characters at
    # deserialization so a malicious profile cannot inject compose YAML.
    with pytest.raises(ValidationError):
        ProfileDocker(dind_image=payload)
