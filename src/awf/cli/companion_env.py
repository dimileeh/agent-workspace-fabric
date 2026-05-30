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
    _ENVIRONMENT_KEY_PATTERN,
    _value_has_compose_interpolation,
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

    # Deep-copy companions so we don't mutate the caller's dicts
    result = [_shallow_copy_companion(c) for c in companions]

    # Apply env-from merges
    for comp_name, file_path_str in env_from:
        idx = name_to_index[comp_name]
        companion = result[idx]
        file_vars = parse_dotenv_file(Path(file_path_str))
        env = cast(dict[str, str], companion.get("environment") or {})
        skip_keys = _validate_env_keys(file_vars, comp_name)
        for key, value in file_vars.items():
            if key in skip_keys:
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
    """Map companion names to their index in the list."""
    index: dict[str, int] = {}
    for i, c in enumerate(companions):
        name = c.get("name")
        if name is not None:
            index[str(name)] = i
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
    (``_ENVIRONMENT_KEY_PATTERN`` and ``_value_has_compose_interpolation``).

    Warnings go to stderr with the key name ONLY — never the value.
    """
    skip: set[str] = set()
    for key in file_vars:
        if not _ENVIRONMENT_KEY_PATTERN.fullmatch(key):
            _warn(f"{comp_name!r}: skipping env key {key!r}: invalid key name pattern")
            skip.add(key)
            continue
        if len(key) > 256:
            _warn(f"{comp_name!r}: skipping env key {key!r}: key exceeds 256 characters")
            skip.add(key)
            continue
        value = file_vars[key]
        if _value_has_compose_interpolation(value):
            _warn(
                f"{comp_name!r}: skipping env key {key!r}: "
                f"value contains Docker Compose interpolation syntax"
            )
            skip.add(key)
    return skip


def _warn(message: str) -> None:
    """Print a warning to stderr."""
    print(f"warning: {message}", file=sys.stderr)


def _shallow_copy_companion(companion: dict[str, object]) -> dict[str, object]:
    """Shallow-copy a companion dict, deep-copying the environment."""
    result = dict(companion)
    env = companion.get("environment")
    result["environment"] = dict(cast(dict[str, str], env)) if env else {}
    return result
