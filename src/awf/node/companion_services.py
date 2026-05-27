"""Runtime helpers for managed workspace companion services."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from awf.common.companions import companions_from_task_policy
from awf.node.compose_manager import CompanionService, ComposeService
from awf.node.git_manager import WorktreeLayout
from awf.profiles.models import DockerMode
from awf.profiles.resolver import ProfileResolutionError


@dataclass(frozen=True)
class WorkspaceCompanionSpec:
    """Normalized companion service request loaded from workspace task policy."""

    name: str
    repo_url: str
    base_branch: str
    build_context: str = "."
    dockerfile: str = "Dockerfile"
    env_file: str | None = None
    environment: tuple[tuple[str, str], ...] = ()
    depends_on: tuple[str, ...] = ()
    healthcheck_cmd: str | None = None
    ports: tuple[tuple[int, int], ...] = ()
    command: str | None = None
    volumes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class MaterializedCompanionService:
    """Companion service request plus its managed git worktree."""

    spec: WorkspaceCompanionSpec
    layout: WorktreeLayout


def companion_specs_from_task_policy(
    task_policy: Mapping[str, Any] | None,
) -> tuple[WorkspaceCompanionSpec, ...]:
    """Load companion specs from normalized workspace task policy."""
    return tuple(
        _companion_spec_from_mapping(item) for item in companions_from_task_policy(task_policy)
    )


def companion_service_from_materialized(
    companion: MaterializedCompanionService,
) -> CompanionService:
    """Convert a materialized companion checkout into a Compose service."""
    spec = companion.spec
    root = companion.layout.worktree_path
    return CompanionService(
        name=spec.name,
        build_context=_resolve_repo_path(spec.build_context, root=root),
        dockerfile=_resolve_repo_path(spec.dockerfile, root=root),
        env_file=(
            _resolve_repo_path(spec.env_file, root=root) if spec.env_file is not None else None
        ),
        environment=spec.environment,
        depends_on=spec.depends_on,
        healthcheck_cmd=spec.healthcheck_cmd,
        ports=spec.ports,
        command=spec.command,
        volumes=tuple(
            (_resolve_volume_source(source, root=root), target) for source, target in spec.volumes
        ),
    )


def validate_companion_service_graph(
    *,
    profile_services: tuple[ComposeService, ...],
    companions: tuple[MaterializedCompanionService, ...],
    docker_mode: DockerMode,
) -> None:
    """Validate companion/profile service names and dependency targets."""
    profile_names = {service.name for service in profile_services}
    companion_names = {companion.spec.name for companion in companions}
    collisions = sorted(profile_names & companion_names)
    if collisions:
        raise ProfileResolutionError(
            f"companion service name collides with profile service: {', '.join(collisions)}",
            reason_code="COMPANION_SERVICE_NAME_COLLISION",
        )

    known_names = set(profile_names) | set(companion_names)
    if docker_mode == DockerMode.dind:
        known_names.add("docker")

    unknown: list[str] = []
    for service in profile_services:
        unknown.extend(
            f"{service.name}->{dependency}"
            for dependency in service.depends_on
            if dependency not in known_names
        )
    for companion in companions:
        unknown.extend(
            f"{companion.spec.name}->{dependency}"
            for dependency in companion.spec.depends_on
            if dependency not in known_names
        )
    if unknown:
        raise ProfileResolutionError(
            f"unknown companion/profile service dependency target: {', '.join(sorted(unknown))}",
            reason_code="COMPANION_SERVICE_DEPENDENCY_UNKNOWN",
        )


def _companion_spec_from_mapping(item: Mapping[str, Any]) -> WorkspaceCompanionSpec:
    return WorkspaceCompanionSpec(
        name=str(item["name"]),
        repo_url=str(item["repo_url"]),
        base_branch=str(item["base_branch"]),
        build_context=str(item.get("build_context") or "."),
        dockerfile=str(item.get("dockerfile") or "Dockerfile"),
        env_file=(str(item["env_file"]) if item.get("env_file") is not None else None),
        environment=tuple(
            (str(key), str(value)) for key, value in _mapping_items(item.get("environment"))
        ),
        depends_on=tuple(
            str(value) for value in item.get("depends_on", []) if isinstance(value, str)
        ),
        healthcheck_cmd=(
            str(item["healthcheck_cmd"]) if item.get("healthcheck_cmd") is not None else None
        ),
        ports=tuple(_port_pair(value) for value in item.get("ports", [])),
        command=(str(item["command"]) if item.get("command") is not None else None),
        volumes=tuple(_volume_pair(value) for value in item.get("volumes", [])),
    )


def _mapping_items(value: object) -> tuple[tuple[object, object], ...]:
    if not isinstance(value, Mapping):
        return ()
    return tuple(value.items())


def _port_pair(value: object) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("companion port entries must be two-item sequences")
    return (int(value[0]), int(value[1]))


def _volume_pair(value: object) -> tuple[str, str]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("companion volume entries must be two-item sequences")
    return (str(value[0]), str(value[1]))


def _resolve_repo_path(value: str, *, root: Path) -> str:
    resolved_root = root.resolve()
    resolved = (resolved_root / value).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ValueError(f"companion path escapes managed worktree: {value!r}")
    return str(resolved)


def _resolve_volume_source(source: str, *, root: Path) -> str:
    if source.startswith(".") or "/" in source or "\\" in source:
        return _resolve_repo_path(source, root=root)
    return source
