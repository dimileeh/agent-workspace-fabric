"""Convert workspace profiles into compose-manager inputs."""

from __future__ import annotations

import os
from collections.abc import Mapping

from awf.node.compose_manager import ComposeService
from awf.profiles.models import WorkspaceProfile


def profile_services(profile: WorkspaceProfile) -> tuple[ComposeService, ...]:
    return tuple(
        ComposeService(
            name=s.name,
            image=s.image,
            build_context=s.build_context,
            dockerfile=s.dockerfile,
            env_file=s.env_file,
            environment=tuple(s.environment.items()),
            depends_on=tuple(s.depends_on),
            healthcheck_cmd=s.healthcheck_cmd,
            ports=tuple(s.ports),
            command=s.command,
            volumes=tuple(s.volumes),
            privileged=s.privileged,
        )
        for s in profile.services
    )


def profile_agent_environment(profile: WorkspaceProfile) -> tuple[tuple[str, str], ...]:
    return tuple(profile.runtime.environment.items())


def agent_environment_with_github_token(
    base_environment: tuple[tuple[str, str], ...],
    *,
    host_env: Mapping[str, str] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Expose AWF's GitHub token to agent containers via Compose placeholders."""
    source_env = os.environ if host_env is None else host_env
    if not source_env.get("AWF_GITHUB_TOKEN"):
        return base_environment

    merged: list[tuple[str, str]] = list(base_environment)
    existing = {key for key, _ in merged}
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if name not in existing:
            merged.append((name, "${AWF_GITHUB_TOKEN}"))
            existing.add(name)
    return tuple(merged)
