"""Shared local service environment helpers."""

from __future__ import annotations

import os
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import cast

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
    r"(?<!\$)\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)(?=[}:?+\-])[^}]*\}|"
    r"(?<!\$)\$(?P<plain>[A-Za-z_][A-Za-z0-9_]*)"
)
_COMPOSE_INTERPOLATION_CACHE_MAX_SIZE = 32
_COMPOSE_INTERPOLATION_KEYS_CACHE_MISSING = object()
_COMPOSE_INTERPOLATION_KEYS_CACHE: OrderedDict[tuple[str, str, int], tuple[str, ...]] = (
    OrderedDict()
)
_COMPOSE_INTERPOLATION_KEYS_CACHE_LOCK = threading.Lock()
_COMPOSE_INTERPOLATION_KEYS_INFLIGHT: dict[tuple[str, str, int], threading.Event] = {}


def env_lookup(environ: Mapping[str, str], key: str) -> tuple[bool, str]:
    """Return whether an environment key is present using case-insensitive matching."""

    value = environ.get(key)
    if value is not None:
        return True, value
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
    return resolved


def cleared_docker_cli_client_keys(environ: Mapping[str, str]) -> frozenset[str]:
    """Return Docker CLI keys explicitly cleared by the service environment."""

    cleared_keys: set[str] = set()
    for key in _DOCKER_CLI_CLIENT_ENV_KEYS:
        found, value = env_lookup(environ, key)
        if not found or value:
            continue
        caller_found, caller_value = env_lookup(os.environ, key)
        if caller_found and caller_value:
            cleared_keys.add(key)
    return frozenset(cleared_keys)


def compose_cli_environ(environ: Mapping[str, str]) -> dict[str, str]:
    """Return resolved Compose CLI controls that affect stack selection."""

    resolved: dict[str, str] = {}
    for key in _COMPOSE_CLI_ENV_KEYS:
        found, value = env_lookup(environ, key)
        if found and value:
            resolved[key] = value
            continue
        if found:
            caller_found, caller_value = env_lookup(os.environ, key)
            if caller_found and caller_value:
                resolved[key] = ""
            continue
        # Compose CLI selectors are intentionally inherited from the caller
        # when the service env is silent; explicit service values override them,
        # and explicit blank service values above clear stale caller settings.
        caller_found, caller_value = env_lookup(os.environ, key)
        if caller_found and caller_value:
            resolved[key] = caller_value
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
        # Caller env wins over --env-file for interpolation. If it already
        # matches the resolved service value, a stale env-file value is harmless.
        if caller_found:
            if caller_value != value:
                resolved[key] = value
            continue
        # Without a caller value, Compose can read matching values from
        # --env-file; stale or missing env-file values need an explicit override.
        if not env_file_found or env_file_value != value:
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
        contents_bytes = compose_file.read_bytes()
    except OSError:
        return ()
    try:
        contents = contents_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return ()
    contents_digest = sha256(contents_bytes).hexdigest()
    return _cached_compose_interpolation_keys(
        str(compose_file),
        contents_digest,
        len(contents_bytes),
        contents,
    )


def _cached_compose_interpolation_keys(
    _compose_file: str,
    contents_digest: str,
    contents_size: int,
    contents: str,
) -> tuple[str, ...]:
    """Return cached Compose interpolation keys for one file version."""

    cache_key = (_compose_file, contents_digest, contents_size)
    while True:
        with _COMPOSE_INTERPOLATION_KEYS_CACHE_LOCK:
            cached = _COMPOSE_INTERPOLATION_KEYS_CACHE.get(
                cache_key, _COMPOSE_INTERPOLATION_KEYS_CACHE_MISSING
            )
            if cached is not _COMPOSE_INTERPOLATION_KEYS_CACHE_MISSING:
                _COMPOSE_INTERPOLATION_KEYS_CACHE.move_to_end(cache_key)
                return cast(tuple[str, ...], cached)
            inflight = _COMPOSE_INTERPOLATION_KEYS_INFLIGHT.get(cache_key)
            if inflight is None:
                inflight = threading.Event()
                _COMPOSE_INTERPOLATION_KEYS_INFLIGHT[cache_key] = inflight
                break
        inflight.wait()

    parsed = False
    try:
        keys = _parse_compose_interpolation_keys(contents)
        parsed = True
    finally:
        if not parsed:
            with _COMPOSE_INTERPOLATION_KEYS_CACHE_LOCK:
                if _COMPOSE_INTERPOLATION_KEYS_INFLIGHT.get(cache_key) is inflight:
                    del _COMPOSE_INTERPOLATION_KEYS_INFLIGHT[cache_key]
                inflight.set()

    with _COMPOSE_INTERPOLATION_KEYS_CACHE_LOCK:
        _COMPOSE_INTERPOLATION_KEYS_CACHE[cache_key] = keys
        if len(_COMPOSE_INTERPOLATION_KEYS_CACHE) > _COMPOSE_INTERPOLATION_CACHE_MAX_SIZE:
            _COMPOSE_INTERPOLATION_KEYS_CACHE.popitem(last=False)
        if _COMPOSE_INTERPOLATION_KEYS_INFLIGHT.get(cache_key) is inflight:
            del _COMPOSE_INTERPOLATION_KEYS_INFLIGHT[cache_key]
        inflight.set()
    return keys


def _parse_compose_interpolation_keys(contents: str) -> tuple[str, ...]:
    """Parse Compose YAML contents and collect interpolation variable names."""

    try:
        payload: object = yaml.safe_load(contents)
    except yaml.YAMLError:
        return ()
    collected_keys: set[str] = set()
    _collect_compose_interpolation_keys(payload, collected_keys)
    return tuple(sorted(collected_keys))


def _collect_compose_interpolation_keys(value: object, keys: set[str]) -> None:
    """Collect Compose interpolation variable names from nested YAML values."""

    if isinstance(value, str):
        for match in _COMPOSE_INTERPOLATION_PATTERN.finditer(value):
            key = match.group("braced") or match.group("plain")
            if key:
                keys.add(key)
        return
    if isinstance(value, Mapping):
        for nested_value in value.values():
            _collect_compose_interpolation_keys(nested_value, keys)
        return
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        for nested in value:
            _collect_compose_interpolation_keys(nested, keys)
