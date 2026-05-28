"""Runtime helpers for managed workspace companion services."""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from awf.common.companions import (
    companion_volume_source_is_repo_relative,
    companions_from_task_policy,
)
from awf.node.compose_manager import CompanionService, ComposeService
from awf.node.git_manager import WorktreeLayout
from awf.profiles.models import DockerMode
from awf.profiles.resolver import ProfileResolutionError


@dataclass(frozen=True)
class WorkspaceCompanionSpec:
    """Normalized companion service request loaded from workspace task policy."""

    name: str
    repo_url: str
    base_branch: str | None = None
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


CompanionGraphInput = WorkspaceCompanionSpec | MaterializedCompanionService


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
    build_context = _resolve_repo_path(spec.build_context, root=root)
    return CompanionService(
        name=spec.name,
        build_context=build_context,
        dockerfile=_resolve_companion_dockerfile(
            spec.dockerfile,
            build_context=Path(build_context),
            root=root,
        ),
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
    companions: tuple[CompanionGraphInput, ...],
    docker_mode: DockerMode,
) -> None:
    """Validate companion/profile service names, dependency targets, and cycles."""
    profile_names = {service.name for service in profile_services}
    duplicate_companion_names = _duplicate_companion_service_names(companions)
    if duplicate_companion_names:
        raise ProfileResolutionError(
            f"duplicate companion service name: {', '.join(duplicate_companion_names)}",
            reason_code="COMPANION_SERVICE_NAME_COLLISION",
        )

    companion_names = {_companion_graph_spec(companion).name for companion in companions}
    collisions = sorted(profile_names & companion_names)
    if collisions:
        raise ProfileResolutionError(
            f"companion service name collides with profile service: {', '.join(collisions)}",
            reason_code="COMPANION_SERVICE_NAME_COLLISION",
        )

    duplicate_host_ports = _duplicate_companion_profile_host_ports(
        profile_services=profile_services,
        companions=companions,
    )
    if duplicate_host_ports:
        raise ProfileResolutionError(
            f"duplicate companion/profile host port {'; '.join(duplicate_host_ports)}",
            reason_code="COMPANION_SERVICE_HOST_PORT_COLLISION",
        )

    known_names = set(profile_names) | set(companion_names)
    known_names.add("agent")
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
        spec = _companion_graph_spec(companion)
        unknown.extend(
            f"{spec.name}->{dependency}"
            for dependency in spec.depends_on
            if dependency not in known_names
        )
    if unknown:
        raise ProfileResolutionError(
            f"unknown companion/profile service dependency target: {', '.join(sorted(unknown))}",
            reason_code="COMPANION_SERVICE_DEPENDENCY_UNKNOWN",
        )

    cycle = _companion_service_dependency_cycle(
        profile_services=profile_services,
        companions=companions,
    )
    if cycle is not None:
        raise ProfileResolutionError(
            f"circular companion/profile service dependency: {'->'.join(cycle)}",
            reason_code="COMPANION_SERVICE_DEPENDENCY_CYCLE",
        )

    healthy_targets = {service.name for service in profile_services if service.healthcheck_cmd} | {
        spec.name
        for companion in companions
        for spec in (_companion_graph_spec(companion),)
        if spec.healthcheck_cmd
    }
    if docker_mode == DockerMode.dind:
        healthy_targets.add("docker")

    unhealthy: list[str] = []
    for service in profile_services:
        unhealthy.extend(
            f"{service.name}->{dependency}"
            for dependency in service.depends_on
            if dependency not in healthy_targets
        )
    for companion in companions:
        spec = _companion_graph_spec(companion)
        unhealthy.extend(
            f"{spec.name}->{dependency}"
            for dependency in spec.depends_on
            if dependency not in healthy_targets
        )
    if unhealthy:
        raise ProfileResolutionError(
            "companion/profile service dependency target has no healthcheck: "
            f"{', '.join(sorted(unhealthy))}",
            reason_code="COMPANION_SERVICE_DEPENDENCY_UNHEALTHY",
        )


def _companion_service_dependency_cycle(
    *,
    profile_services: tuple[ComposeService, ...],
    companions: tuple[CompanionGraphInput, ...],
) -> tuple[str, ...] | None:
    """Return a dependency cycle after public graph validation verifies names."""
    graph: dict[str, tuple[str, ...]] = {
        service.name: tuple(service.depends_on) for service in profile_services
    }
    for companion in companions:
        spec = _companion_graph_spec(companion)
        graph[spec.name] = tuple(spec.depends_on)
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []
    stack_indexes: dict[str, int] = {}

    def visit(name: str) -> tuple[str, ...] | None:
        if name in visited:
            return None
        if name in visiting:
            return (*stack[stack_indexes[name] :], name)

        visiting.add(name)
        stack_indexes[name] = len(stack)
        stack.append(name)
        for dependency in graph[name]:
            if dependency not in graph:
                continue
            cycle = visit(dependency)
            if cycle is not None:
                return cycle
        stack.pop()
        del stack_indexes[name]
        visiting.remove(name)
        visited.add(name)
        return None

    for service_name in graph:
        cycle = visit(service_name)
        if cycle is not None:
            return cycle
    return None


def _duplicate_companion_service_names(
    companions: tuple[CompanionGraphInput, ...],
) -> list[str]:
    return sorted(
        name
        for name, count in Counter(
            _companion_graph_spec(companion).name for companion in companions
        ).items()
        if count > 1
    )


def _duplicate_companion_profile_host_ports(
    *,
    profile_services: tuple[ComposeService, ...],
    companions: tuple[CompanionGraphInput, ...],
) -> list[str]:
    host_port_owners: dict[int, list[str]] = {}
    for service in profile_services:
        for _, host_port in service.ports:
            host_port_owners.setdefault(host_port, []).append(service.name)
    for companion in companions:
        spec = _companion_graph_spec(companion)
        for _, host_port in spec.ports:
            host_port_owners.setdefault(host_port, []).append(spec.name)
    return [
        f"{host_port} used by {', '.join(owners)}"
        for host_port, owners in sorted(host_port_owners.items())
        if len(owners) > 1
    ]


def _companion_graph_spec(companion: CompanionGraphInput) -> WorkspaceCompanionSpec:
    if isinstance(companion, WorkspaceCompanionSpec):
        return companion
    return companion.spec


def _companion_spec_from_mapping(item: Mapping[str, Any]) -> WorkspaceCompanionSpec:
    base_branch = item.get("base_branch")
    return WorkspaceCompanionSpec(
        name=str(item["name"]),
        repo_url=str(item["repo_url"]),
        base_branch=str(base_branch) if base_branch is not None else None,
        build_context=str(item.get("build_context") or "."),
        dockerfile=str(item.get("dockerfile") or "Dockerfile"),
        env_file=(str(item["env_file"]) if item.get("env_file") is not None else None),
        environment=tuple(
            (str(key), str(value)) for key, value in _mapping_items(item.get("environment"))
        ),
        depends_on=tuple(
            str(value)
            for value in _depends_on_items_or_empty(item.get("depends_on"))
            if isinstance(value, str)
        ),
        healthcheck_cmd=(
            str(item["healthcheck_cmd"]) if item.get("healthcheck_cmd") is not None else None
        ),
        ports=tuple(_port_pair(value) for value in _sequence_items_or_empty(item.get("ports"))),
        command=(str(item["command"]) if item.get("command") is not None else None),
        volumes=tuple(
            _volume_pair(value) for value in _sequence_items_or_empty(item.get("volumes"))
        ),
    )


def _mapping_items(value: object) -> tuple[tuple[object, object], ...]:
    if not isinstance(value, Mapping):
        return ()
    return tuple(value.items())


def _sequence_items_or_empty(value: Any) -> Any:
    if value is None:
        return ()
    return value


def _depends_on_items_or_empty(value: Any) -> Any:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return value


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


def _resolve_companion_dockerfile(value: str, *, build_context: Path, root: Path) -> str:
    if value == "Dockerfile":
        return value

    resolved = Path(_resolve_repo_path(value, root=root))
    return os.path.relpath(resolved, start=build_context)


def _resolve_volume_source(source: str, *, root: Path) -> str:
    if companion_volume_source_is_repo_relative(source):
        return _resolve_repo_path(source, root=root)
    return source
