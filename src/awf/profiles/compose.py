"""Convert workspace profiles into compose-manager inputs."""

from __future__ import annotations

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
