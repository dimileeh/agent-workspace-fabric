"""CLI-side .env file parsing for --companion-env-from.

This module provides a minimal, well-documented .env parser that supports
exactly what AWF needs — no more, no less. It is intentionally separate from
the service-layer ``dotenv_values`` usage so that the contract is explicit
and testable.

Supported .env syntax
----------------------
- ``KEY=value`` lines (first ``=`` separates key from value)
- ``#`` comment lines (entire line is ignored)
- Blank lines (ignored)
- Surrounding double quotes: ``KEY="value with spaces"``
- Surrounding single quotes: ``KEY='value with spaces'``
- Leading ``export `` prefix: ``export KEY=value``

Explicitly NOT supported
------------------------
- Multi-line values
- Variable substitution (``${VAR}`` or ``$VAR``) within the file
- ``source`` directives
- Inline comments (``#`` after a value is part of the value)
- ``.env.local`` / cascading file resolution
"""

from __future__ import annotations

import os
from pathlib import Path


def parse_dotenv_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a flat key→value mapping.

    Raises FileNotFoundError if the file does not exist.
    Raises PermissionError if the file exists but is unreadable.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"--companion-env-from file not found: {path!r}") from exc
    except PermissionError as exc:
        raise PermissionError(
            f"--companion-env-from file is unreadable (permission denied): {path!r}"
        ) from exc
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        eq_index = line.find("=")
        if eq_index < 1:
            continue
        key = line[:eq_index].strip()
        raw_value = line[eq_index + 1 :].strip()
        if not key:
            continue
        value = _strip_quotes(raw_value)
        result[key] = value
    return result


def _strip_quotes(value: str) -> str:
    """Remove surrounding single or double quotes from a value."""
    if len(value) >= 2 and (
        (value.startswith('"') and value.endswith('"'))
        or (value.startswith("'") and value.endswith("'"))
    ):
        return value[1:-1]
    return value


def parse_env_from_arg(arg: str) -> tuple[str, str]:
    """Parse a ``--companion-env-from`` argument of the form ``name=path``.

    Expands ``~`` and environment variables in the path portion.

    Raises ``ValueError`` with an actionable message on malformed input.
    """
    if "=" not in arg:
        raise ValueError(
            f"--companion-env-from argument is malformed (expected name=path): {arg!r}"
        )
    name, _, path = arg.partition("=")
    name = name.strip()
    path = path.strip()
    if not name:
        raise ValueError(f"--companion-env-from argument is malformed (empty name): {arg!r}")
    if not path:
        raise ValueError(f"--companion-env-from argument is malformed (empty path): {arg!r}")
    expanded_path = Path(os.path.expandvars(path)).expanduser()
    return name, str(expanded_path)


def parse_env_exclude_arg(arg: str) -> tuple[str, set[str]]:
    """Parse a ``--companion-env-exclude`` argument of the form ``name=KEY1,KEY2,...``.

    Raises ``ValueError`` with an actionable message on malformed input.
    """
    if "=" not in arg:
        raise ValueError(
            f"--companion-env-exclude argument is malformed (expected name=KEYS): {arg!r}"
        )
    name, _, keys_str = arg.partition("=")
    name = name.strip()
    keys_str = keys_str.strip()
    if not name:
        raise ValueError(f"--companion-env-exclude argument is malformed (empty name): {arg!r}")
    if not keys_str:
        raise ValueError(f"--companion-env-exclude argument is malformed (empty keys): {arg!r}")
    keys = {k.strip() for k in keys_str.split(",") if k.strip()}
    return name, keys
