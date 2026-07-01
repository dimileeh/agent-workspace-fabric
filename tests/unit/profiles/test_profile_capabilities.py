"""Tests for the profile capability classifier (Phase 1 runtime-driver seam).

The classifier is a pure, cloud-neutral projection of a resolved
``WorkspaceProfile`` into public-safe capability flags so ``awf-cloud`` can
decide backend eligibility without Core embedding cloud product policy.
"""

from __future__ import annotations

import copy

import pytest

from awf.profiles.capabilities import (
    ProfileCapabilities,
    classify_profile_capabilities,
)
from awf.profiles.models import (
    DockerMode,
    ProfileDocker,
    ProfileService,
    WorkspaceProfile,
)


def _plain_profile() -> WorkspaceProfile:
    """Return a minimal profile with no Docker/privileged/ports/compose_files."""
    return WorkspaceProfile(name="plain")


@pytest.mark.unit
def test_plain_profile_is_autopilot_supported() -> None:
    """Verify plain profile is autopilot supported."""
    capabilities = classify_profile_capabilities(_plain_profile())

    assert capabilities == ProfileCapabilities(
        autopilot_supported=True,
        docker_required=False,
        privileged_required=False,
        unsupported_compose_feature=False,
    )


@pytest.mark.unit
def test_dind_mode_disqualifies_autopilot() -> None:
    """Verify dind mode disqualifies autopilot."""
    profile = WorkspaceProfile(name="dind", docker=ProfileDocker(mode=DockerMode.dind))

    capabilities = classify_profile_capabilities(profile)

    assert capabilities.docker_required is True
    assert capabilities.unsupported_compose_feature is True
    assert capabilities.autopilot_supported is False


@pytest.mark.unit
def test_privileged_service_disqualifies_autopilot() -> None:
    """Verify privileged service disqualifies autopilot."""
    service = ProfileService(name="daemon", image="example/daemon:1", privileged=True)
    profile = WorkspaceProfile(name="privileged", services=[service])

    capabilities = classify_profile_capabilities(profile)

    assert capabilities.privileged_required is True
    assert capabilities.docker_required is True
    assert capabilities.autopilot_supported is False


@pytest.mark.unit
def test_host_ports_disqualify_autopilot() -> None:
    """Verify host ports disqualify autopilot."""
    service = ProfileService(name="web", image="example/web:1", ports=[(8080, 80)])
    profile = WorkspaceProfile(name="host-ports", services=[service])

    capabilities = classify_profile_capabilities(profile)

    assert capabilities.unsupported_compose_feature is True
    assert capabilities.docker_required is True
    assert capabilities.autopilot_supported is False


@pytest.mark.unit
def test_compose_files_disqualify_autopilot_without_dind() -> None:
    """Verify compose_files disqualify autopilot without dind."""
    profile = WorkspaceProfile(
        name="compose",
        docker=ProfileDocker(mode=DockerMode.none, compose_files=["compose.yml"]),
    )

    capabilities = classify_profile_capabilities(profile)

    assert capabilities.unsupported_compose_feature is True
    assert capabilities.autopilot_supported is False
    # Compose-only orchestration with mode=none and no privileged/host-ports
    # does not by itself require a host Docker daemon for the eligibility
    # question (the cloud backend can run the Compose file).
    assert capabilities.docker_required is False
    assert capabilities.privileged_required is False


@pytest.mark.unit
def test_build_context_alone_is_autopilot_supported() -> None:
    """Verify build_context alone is autopilot supported."""
    service = ProfileService(name="app", build_context=".")
    profile = WorkspaceProfile(name="build", services=[service])

    capabilities = classify_profile_capabilities(profile)

    assert capabilities.autopilot_supported is True
    assert capabilities.docker_required is False
    assert capabilities.privileged_required is False
    assert capabilities.unsupported_compose_feature is False


@pytest.mark.unit
def test_classifier_is_pure_and_does_not_mutate_profile() -> None:
    """Verify classifier is pure and does not mutate profile."""
    profile = _plain_profile()
    snapshot = copy.deepcopy(profile.model_dump())

    first = classify_profile_capabilities(profile)
    second = classify_profile_capabilities(profile)

    assert first == second
    assert profile.model_dump() == snapshot
