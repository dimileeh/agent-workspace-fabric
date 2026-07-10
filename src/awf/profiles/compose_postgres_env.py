"""Compose Postgres password discovery for hosted profile-env redaction."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml

from awf.profiles.compose_env import (
    _COMPOSE_PASSTHROUGH,
    _compose_concrete_worker_password,
    _compose_environment_mapping,
    _compose_resolve_value,
    _ComposeEnvResolution,
)
from awf.service.environment import compose_env_file_values

_POSTGRES_SERVICE_ENV_NAMES = frozenset(
    {"POSTGRES_DB", "POSTGRES_HOST_AUTH_METHOD", "POSTGRES_PASSWORD", "POSTGRES_USER"}
)


def compose_postgres_service_hostnames(
    compose_file: Path, *, worker_env: Mapping[str, str] | None = None
) -> frozenset[str]:
    """Return Compose service names that are local Postgres sidecar hostnames."""
    try:
        payload = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        return frozenset()
    if not isinstance(payload, Mapping):
        return frozenset()
    services = payload.get("services")
    if not isinstance(services, Mapping):
        return frozenset()

    hostnames: set[str] = set()
    env = {} if worker_env is None else worker_env
    for service_name, service in services.items():
        if not isinstance(service_name, str) or not isinstance(service, Mapping):
            continue
        if _compose_service_is_postgres(service, compose_dir=compose_file.parent, worker_env=env):
            hostnames.add(service_name.lower())
    return frozenset(hostnames)


def try_compose_agent_env_and_postgres_passwords(
    compose_file: Path,
    *,
    worker_env: Mapping[str, str],
) -> tuple[dict[str, str] | None, frozenset[str]]:
    """Parse compose once, returning agent env and resolved DB passwords.

    ``POSTGRES_PASSWORD`` is collected from every service and from service
    ``env_file`` declarations, then resolved against ``worker_env`` so hosted
    profile-env redaction can drop rendered agent values that embed the same
    concrete password the local Compose container receives at stack launch.
    """
    try:
        payload = yaml.safe_load(compose_file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        return None, frozenset()
    if not isinstance(payload, Mapping):
        return None, frozenset()
    services = payload.get("services")
    if not isinstance(services, Mapping):
        return None, frozenset()
    agent = services.get("agent")
    agent_env = (
        _compose_environment_mapping(agent.get("environment"))
        if isinstance(agent, Mapping)
        else None
    )
    postgres_passwords: set[str] = set()
    for service in services.values():
        if not isinstance(service, Mapping):
            continue
        service_env = _compose_environment_mapping(service.get("environment"))
        collect_postgres_password(
            service_env.get("POSTGRES_PASSWORD"),
            postgres_passwords,
            worker_env=worker_env,
        )
        for env_file_path in compose_service_env_file_paths(
            service.get("env_file"),
            compose_dir=compose_file.parent,
        ):
            try:
                env_file_env = compose_env_file_values(env_file_path, environ=worker_env)
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            collect_postgres_password(
                env_file_env.get("POSTGRES_PASSWORD"),
                postgres_passwords,
                worker_env=worker_env,
            )
    return agent_env, frozenset(postgres_passwords)


def _compose_service_is_postgres(
    service: Mapping[object, object],
    *,
    compose_dir: Path | None = None,
    worker_env: Mapping[str, str],
) -> bool:
    image = service.get("image")
    if isinstance(image, str) and _compose_image_is_postgres(image):
        return True
    service_env = _compose_environment_mapping(service.get("environment"))
    if _POSTGRES_SERVICE_ENV_NAMES.intersection(service_env):
        return True
    for env_file_path in compose_service_env_file_paths(
        service.get("env_file"),
        compose_dir=compose_dir,
    ):
        try:
            env_file_env = compose_env_file_values(env_file_path, environ=worker_env)
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        if _POSTGRES_SERVICE_ENV_NAMES.intersection(env_file_env):
            return True
    return False


def _compose_image_is_postgres(image: str) -> bool:
    basename = image.split("@", 1)[0].rsplit("/", 1)[-1]
    repository = basename.split(":", 1)[0]
    return repository == "postgres"


def compose_service_env_file_paths(
    env_file: object, *, compose_dir: Path | None = None
) -> tuple[Path, ...]:
    """Return ``env_file`` paths declared on a compose service."""
    paths: list[Path] = []
    raw_paths: list[str] = []
    if isinstance(env_file, str):
        raw_paths.append(env_file)
    elif isinstance(env_file, list):
        for item in env_file:
            if isinstance(item, str):
                raw_paths.append(item)
            elif isinstance(item, Mapping):
                raw = item.get("path")
                if isinstance(raw, str):
                    raw_paths.append(raw)
    for raw in raw_paths:
        path = Path(raw)
        if not path.is_absolute() and compose_dir is not None:
            path = compose_dir / path
        paths.append(path)
    return tuple(paths)


def collect_postgres_password(
    raw_password: str | None,
    postgres_passwords: set[str],
    *,
    worker_env: Mapping[str, str],
) -> None:
    """Resolve and track one declared ``POSTGRES_PASSWORD`` for redaction."""
    if not raw_password:
        return
    if raw_password == _COMPOSE_PASSTHROUGH:
        resolved = worker_env.get("POSTGRES_PASSWORD")
        if resolved:
            postgres_passwords.add(resolved)
        return
    postgres_passwords.add(raw_password)
    resolved, resolution = _compose_resolve_value(raw_password, worker_env=worker_env)
    if resolution is _ComposeEnvResolution.LITERAL and resolved:
        postgres_passwords.add(resolved)
    elif resolution in (
        _ComposeEnvResolution.WORKER_RESOLVED_DEFAULTED,
        _ComposeEnvResolution.WORKER_RESOLVED_SLOT,
    ):
        concrete = _compose_concrete_worker_password(raw_password, worker_env=worker_env)
        if concrete:
            postgres_passwords.add(concrete)
