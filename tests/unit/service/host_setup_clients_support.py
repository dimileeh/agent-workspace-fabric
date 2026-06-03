"""Shared fixtures and fakes for the client MCP integration test parts."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from awf.host_setup.system_checks import CommandResult

_FIXED_NOW = datetime(2026, 6, 3, 12, 30, 45, tzinfo=UTC)
_ENV_FILE = "/srv/awf/docker/compose/.env"


def _now() -> datetime:
    return _FIXED_NOW


def _which_missing(_binary: str) -> str | None:
    return None


def _which_found(binary: str) -> str:
    return f"/usr/bin/{binary}"


class _FakeRunner:
    """Capturing fake ``CommandRunner`` for official-CLI assertions."""

    def __init__(self, result: CommandResult | None) -> None:
        self._result = result
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str]) -> CommandResult | None:
        self.calls.append(tuple(args))
        return self._result


def _never_run(args: Sequence[str]) -> CommandResult | None:
    raise AssertionError(f"runner must not be invoked, got {tuple(args)!r}")


def _claude_config_path(home: Path) -> Path:
    return home / ".claude.json"


def _codex_config_path(home: Path) -> Path:
    return home / ".codex" / "config.toml"


def _desired_args(env_file: str = _ENV_FILE) -> list[str]:
    return ["mcp", "serve", "--env-file", env_file]
