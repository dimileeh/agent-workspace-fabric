"""CLI-side companion env merge for --companion-env-from / --companion-env-exclude.

This module handles reading .env files, merging their values into companion
payloads, applying excludes, and validating keys/values against AWF's companion
env contract. It is purely client-side — the API server never sees file paths.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import cast

from awf.api.schemas_companions import (
    ENV_KEY_MAX_LENGTH,
    ENVIRONMENT_KEY_PATTERN,
    value_has_compose_interpolation,
)
from awf.cli.env_file import parse_dotenv_file


def merge_companion_env(
    companions: list[dict[str, object]],
    *,
    env_from: list[tuple[str, str]],
    env_exclude: list[tuple[str, set[str]]],
) -> list[dict[str, object]]:
    """Merge .env files into companion payloads and apply exclude lists.

    Parameters
    ----------
    companions:
        The list of companion dicts (as parsed from --companion-json).
        Each must have a ``name`` key.
    env_from:
        List of (companion_name, file_path) pairs from --companion-env-from.
        file_path is already expanded.
    env_exclude:
        List of (companion_name, excluded_keys) pairs from
        --companion-env-exclude.

    Returns
    -------
    A new list of companion dicts with merged environment blocks.

    Raises
    ------
    ValueError
        If --companion-env-from or --companion-env-exclude names a companion
        not present.
    FileNotFoundError
        If --companion-env-from points to a non-existent file.
    PermissionError
        If --companion-env-from points to an unreadable file.
    """
    name_to_index = _build_name_index(companions)

    # Validate that every companion's environment is a dict (or absent).
    # JSON values like strings, arrays, booleans, or numbers are not valid
    # environment mappings and would crash merge/exclude operations.
    for comp in companions:
        env = comp.get("environment")
        if env is not None and not isinstance(env, dict):
            name = comp.get("name", "<unknown>")
            raise ValueError(
                f"companion {name!r} has a non-object 'environment' field "
                f"(expected a JSON object/mapping, got {type(env).__name__}). "
                f"Each companion's 'environment' must be a key-value mapping "
                f'like {{"KEY": "value"}}, not a scalar or array.'
            )

    # Validate that environment_secrets is also a dict (or absent).
    # A truthy non-dict (string, array) would crash .keys() during merge.
    for comp in companions:
        secrets = comp.get("environment_secrets")
        if secrets is not None and not isinstance(secrets, dict):
            name = comp.get("name", "<unknown>")
            raise ValueError(
                f"companion {name!r} has a non-object 'environment_secrets' field "
                f"(expected a JSON object/mapping, got {type(secrets).__name__}). "
                "Each companion's 'environment_secrets' must be a key-value mapping "
                'like {"KEY": {"value_from": "..."}}, not a scalar or array.'
            )

    # Validate env-from targets exist and files are accessible
    for comp_name, file_path_str in env_from:
        _require_companion(comp_name, name_to_index, "--companion-env-from")
        file_path = Path(file_path_str)
        if not file_path.is_file():
            raise FileNotFoundError(
                f"--companion-env-from file not found for companion "
                f"{comp_name!r}: {file_path_str!r}"
            )
        # Check readability upfront to give a clear message instead of
        # an unhandled PermissionError traceback from parse_dotenv_file.
        if not os.access(file_path, os.R_OK):
            raise PermissionError(
                f"--companion-env-from file is unreadable (permission denied) "
                f"for companion {comp_name!r}: {file_path_str!r}"
            )

    # Validate env-exclude targets exist
    for comp_name, _keys in env_exclude:
        _require_companion(comp_name, name_to_index, "--companion-env-exclude")

    # Shallow-copy companions, deep-copying only the environment dict,
    # so that env mutations do not propagate back to the caller's dicts.
    # Only inject environment: {} for companions that will be mutated;
    # untargeted companions pass through unchanged.
    targeted = {name for name, _ in env_from} | {name for name, _ in env_exclude}
    result = [
        _shallow_copy_companion(
            c,
            always_include_env=str(c.get("name", "")).strip() in targeted,
        )
        for c in companions
    ]

    # Apply env-from merges
    for comp_name, file_path_str in env_from:
        idx = name_to_index[comp_name]
        companion = result[idx]
        file_vars = parse_dotenv_file(Path(file_path_str))
        env = cast(dict[str, str], companion.get("environment") or {})
        skip_keys = _validate_env_keys(file_vars, comp_name)
        secret_keys = set(
            cast(dict[str, object], companion.get("environment_secrets") or {}).keys()
        )
        overlap_keys = (set(file_vars.keys()) & secret_keys) - skip_keys
        for key in sorted(overlap_keys):
            _warn(
                f"{comp_name!r}: skipping env key {key!r}: already declared in environment_secrets"
            )
        for key, value in file_vars.items():
            if key in skip_keys:
                continue
            if key in secret_keys:
                continue
            if key not in env:
                env[key] = value
        companion["environment"] = env

    # Apply env-exclude
    for comp_name, keys in env_exclude:
        idx = name_to_index[comp_name]
        companion = result[idx]
        env = cast(dict[str, str], companion.get("environment") or {})
        for key in keys:
            env.pop(key, None)
        companion["environment"] = env

    return result


def _build_name_index(companions: list[dict[str, object]]) -> dict[str, int]:
    """Map companion names to their index in the list.

    Names are stripped of leading/trailing whitespace so that companions
    loaded from ``--companion-json`` (where Pydantic may normalise whitespace)
    match the stripped names produced by ``--companion-env-from`` and
    ``--companion-env-exclude``.
    """
    index: dict[str, int] = {}
    for i, c in enumerate(companions):
        name = c.get("name")
        if name is not None:
            index[str(name).strip()] = i
    return index


def _require_companion(
    name: str,
    name_to_index: dict[str, int],
    flag: str,
) -> None:
    """Raise ValueError if name doesn't correspond to any companion."""
    if name not in name_to_index:
        raise ValueError(
            f"{flag} names companion {name!r}, but no companion with that "
            f"name appears in any --companion-json"
        )


def _validate_env_keys(
    file_vars: dict[str, str],
    comp_name: str,
) -> set[str]:
    """Validate env keys and values, warn and return the set of keys to skip.

    Uses the same patterns as the server-side companion env validation
    (``ENVIRONMENT_KEY_PATTERN`` and ``value_has_compose_interpolation``).

    Warnings go to stderr with the key name ONLY — never the value.
    """
    skip: set[str] = set()
    for key in file_vars:
        if not ENVIRONMENT_KEY_PATTERN.fullmatch(key):
            _warn(f"{comp_name!r}: skipping env key {key!r}: invalid key name pattern")
            skip.add(key)
            continue
        if len(key) > ENV_KEY_MAX_LENGTH:
            _warn(
                f"{comp_name!r}: skipping env key {key!r}: key exceeds {ENV_KEY_MAX_LENGTH} characters"
            )
            skip.add(key)
            continue
        value = file_vars[key]
        if value_has_compose_interpolation(value):
            _warn(
                f"{comp_name!r}: skipping env key {key!r}: "
                f"value contains Docker Compose interpolation syntax"
            )
            skip.add(key)
    return skip


def _warn(message: str) -> None:
    """Print a warning to stderr."""
    print(f"warning: {message}", file=sys.stderr)


def _shallow_copy_companion(
    companion: dict[str, object],
    *,
    always_include_env: bool = True,
) -> dict[str, object]:
    """Shallow-copy a companion dict, isolating the environment mapping.

    When *always_include_env* is True (default), the result always has an
    ``environment`` key — an empty dict ``{}`` is substituted when the
    original had no ``environment`` field.  This is needed for companions
    that will receive env-from / env-exclude mutations so that downstream
    code can rely on the key existing.

    When *always_include_env* is False, companions without an
    ``environment`` field are left unchanged (no ``environment: {}``
    injected), preserving the original payload shape for untargeted
    companions.
    """
    result = dict(companion)
    env = companion.get("environment")
    if env is not None and not isinstance(env, dict):
        name = companion.get("name", "<unknown>")
        raise ValueError(
            f"companion {name!r} has a non-object 'environment' field "
            f"(expected a JSON object/mapping, got {type(env).__name__}). "
            f"Each companion's 'environment' must be a key-value mapping "
            f'like {{"KEY": "value"}}, not a scalar or array.'
        )
    if env is not None:
        result["environment"] = dict(cast(dict[str, str], env))
    elif always_include_env:
        result["environment"] = {}
    return result
