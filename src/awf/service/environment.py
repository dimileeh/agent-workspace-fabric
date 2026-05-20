"""Shared local service environment helpers."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import dotenv_values

_COMPOSE_CLI_ENV_KEYS = ("COMPOSE_PROFILES", "COMPOSE_PROJECT_NAME")
_DOCKER_CLI_CLIENT_ENV_KEYS = (
    "DOCKER_API_VERSION",
    "DOCKER_CERT_PATH",
    "DOCKER_CONFIG",
    "DOCKER_CONTEXT",
    "DOCKER_TLS",
    "DOCKER_TLS_VERIFY",
)
_COMPOSE_INTERPOLATION_PATTERN = re.compile(
    r"(?<!\$)\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)|"
    r"(?<!\$)\$(?P<plain>[A-Za-z_][A-Za-z0-9_]*)"
)


def env_lookup(environ: Mapping[str, str], key: str) -> tuple[bool, str]:
    """Return whether an environment key is present using case-insensitive matching."""

    wanted = key.upper()
    for existing, value in environ.items():
        if existing.upper() == wanted:
            return True, value
    return False, ""


def non_empty_env_value(environ: Mapping[str, str], key: str) -> str | None:
    """Look up a case-insensitive environment value and ignore empty strings."""

    found, value = env_lookup(environ, key)
    if found and value:
        return value
    return None


def docker_cli_client_environ(environ: Mapping[str, str]) -> dict[str, str]:
    """Return resolved Docker CLI controls needed to reach the selected daemon."""

    resolved: dict[str, str] = {}
    for key in _DOCKER_CLI_CLIENT_ENV_KEYS:
        found, value = env_lookup(environ, key)
        if found and value:
            resolved[key] = value
            continue
        caller_found, caller_value = env_lookup(os.environ, key)
        # Service env explicitly cleared the key; zero out the caller's value so
        # it does not bleed into the subprocess env via dict(os.environ).
        if found and caller_found and caller_value:
            resolved[key] = ""
    return resolved


def compose_cli_environ(environ: Mapping[str, str]) -> dict[str, str]:
    """Return resolved Compose CLI controls that affect stack selection."""

    resolved: dict[str, str] = {}
    for key in _COMPOSE_CLI_ENV_KEYS:
        found, value = env_lookup(environ, key)
        if found and value:
            resolved[key] = value
            continue
        caller_found, caller_value = env_lookup(os.environ, key)
        if found and caller_found and caller_value:
            resolved[key] = ""
    return resolved


def compose_interpolation_environ(
    environ: Mapping[str, str],
    *,
    compose_file: Path,
    compose_env_file: Path | None,
) -> dict[str, str]:
    """Return resolved service values Docker Compose still interpolates."""

    resolved: dict[str, str] = {}
    env_file_values = compose_env_file_values(compose_env_file)
    for key in compose_interpolation_keys(compose_file):
        found, value = env_lookup(environ, key)
        if not found:
            continue
        caller_found, caller_value = env_lookup(os.environ, key)
        env_file_found, env_file_value = env_lookup(env_file_values, key)
        # Equal values from the caller environment or the Compose env file can
        # stay out of this override map because the Docker subprocess env starts
        # from dict(os.environ) and the compose command also receives
        # compose_env_file via --env-file.
        # A stale caller value must be overridden because it wins over --env-file.
        caller_override_needed = caller_found and caller_value != value
        # A stale --env-file value must be overridden by the resolved service value.
        env_file_override_needed = env_file_found and env_file_value != value
        # Service-env-only values need an explicit subprocess env entry for interpolation.
        service_env_only = not caller_found and not env_file_found
        if caller_override_needed or env_file_override_needed or service_env_only:
            resolved[key] = value
    return resolved


def compose_env_file_values(compose_env_file: Path | None) -> dict[str, str]:
    """Return parsed Compose env-file values, omitting unset entries."""

    if compose_env_file is None or not compose_env_file.exists():
        return {}
    return {
        key: value for key, value in dotenv_values(compose_env_file).items() if value is not None
    }


def compose_interpolation_keys(compose_file: Path) -> tuple[str, ...]:
    """Return Compose interpolation variable names referenced by the YAML file."""

    compose_file = compose_file.expanduser().resolve()
    try:
        contents = compose_file.read_text(encoding="utf-8")
    except OSError:
        return ()
    return _cached_compose_interpolation_keys(str(compose_file), contents)


@lru_cache(maxsize=32)
def _cached_compose_interpolation_keys(
    _compose_file: str,
    contents: str,
) -> tuple[str, ...]:
    """Return cached Compose interpolation keys for one file version."""

    try:
        payload: object = yaml.safe_load(contents)
    except yaml.YAMLError:
        return ()

    keys: set[str] = set()
    _collect_compose_interpolation_keys(payload, keys)
    return tuple(sorted(keys))


def _collect_compose_interpolation_keys(value: object, keys: set[str]) -> None:
    """Collect Compose interpolation variable names from nested YAML values."""

    if isinstance(value, str):
        for match in _COMPOSE_INTERPOLATION_PATTERN.finditer(value):
            key = match.group("braced") or match.group("plain")
            if key:
                keys.add(key)
        return
    if isinstance(value, Mapping):
        for nested_key, nested_value in value.items():
            _collect_compose_interpolation_keys(nested_key, keys)
            _collect_compose_interpolation_keys(nested_value, keys)
        return
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        for nested in value:
            _collect_compose_interpolation_keys(nested, keys)
