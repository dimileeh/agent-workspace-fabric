"""Profile capability classification (Phase 1 runtime-driver seam).

A pure, cloud-neutral projection of a resolved :class:`WorkspaceProfile` into
public-safe capability flags. ``awf-cloud`` consumes these flags to decide
whether a profile can run on a cloud Kubernetes backend; Core itself never
embeds cloud product policy here.

The classifier performs no I/O and reads no secrets. It is a read-only view of
the resolved profile and does not alter provisioning, compose rendering, or the
local runtime driver.
"""

from __future__ import annotations

from dataclasses import dataclass

from awf.profiles.models import DockerMode, WorkspaceProfile


@dataclass(frozen=True)
class ProfileCapabilities:
    """Public-safe capability flags derived from a resolved workspace profile."""

    autopilot_supported: bool
    docker_required: bool
    privileged_required: bool
    unsupported_compose_feature: bool


def classify_profile_capabilities(profile: WorkspaceProfile) -> ProfileCapabilities:
    """Classify a resolved profile into cloud-neutral capability flags.

    Rules (cloud-neutral, public-safe):

    - ``docker_required`` is True when ``profile.docker.mode != none`` OR any
      service declares ``privileged`` OR any service declares host ``ports``.
    - ``privileged_required`` is True when any ``ProfileService.privileged`` is
      True.
    - ``unsupported_compose_feature`` is True when
      ``profile.docker.compose_files`` is non-empty OR any service declares
      host ``ports`` OR ``profile.docker.mode == dind``.
    - ``autopilot_supported`` is ``not (docker_required or
      privileged_required or unsupported_compose_feature)``.

    The function is pure: the same input always yields the same output and the
    profile is never mutated.
    """
    has_docker_mode = profile.docker.mode != DockerMode.none
    has_dind_mode = profile.docker.mode == DockerMode.dind
    has_compose_files = bool(profile.docker.compose_files)
    has_privileged_service = any(service.privileged for service in profile.services)
    has_host_ports = any(bool(service.ports) for service in profile.services)

    docker_required = has_docker_mode or has_privileged_service or has_host_ports
    privileged_required = has_privileged_service
    unsupported_compose_feature = has_compose_files or has_host_ports or has_dind_mode
    autopilot_supported = not (
        docker_required or privileged_required or unsupported_compose_feature
    )

    return ProfileCapabilities(
        autopilot_supported=autopilot_supported,
        docker_required=docker_required,
        privileged_required=privileged_required,
        unsupported_compose_feature=unsupported_compose_feature,
    )
