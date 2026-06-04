"""Legacy local service env-file migration helpers."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values

_ENV_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)(?P<suffix>\s*=.*)"
)
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_UNQUOTED_ENV_VALUE_RE = re.compile(r"[A-Za-z0-9_./:@%+=,\-]*")


@dataclass(frozen=True, kw_only=True)
class EnvMigrationResult:
    """Secret-free result for one legacy env migration attempt."""

    status: str
    canonical_env_file: Path
    legacy_env_file: Path
    imported_keys: tuple[str, ...] = ()
    conflict_keys: tuple[str, ...] = ()
    backup_path: Path | None = None
    created_env_file: bool = False

    def to_dict(self) -> dict[str, object]:
        """Return operator-visible metadata without raw env values."""
        payload: dict[str, object] = {
            "status": self.status,
            "canonical_env_file": str(self.canonical_env_file),
            "legacy_env_file": str(self.legacy_env_file),
            "imported_keys": list(self.imported_keys),
            "conflict_keys": list(self.conflict_keys),
            "created_env_file": self.created_env_file,
        }
        if self.backup_path is not None:
            payload["backup_path"] = str(self.backup_path)
        return payload


def migrate_legacy_compose_env_file(
    *,
    canonical_env_file: Path,
    env_example_file: Path,
    legacy_env_file: Path,
    now: Callable[[], datetime] | None = None,
) -> EnvMigrationResult:
    """Move legacy ``docker/compose/.env`` settings into root ``.env``.

    Only key names are returned. Raw values are written to the canonical env file
    and the legacy backup, never to the structured result.
    """

    canonical_env_file = canonical_env_file.expanduser()
    env_example_file = env_example_file.expanduser()
    legacy_env_file = legacy_env_file.expanduser()
    if not legacy_env_file.exists():
        return EnvMigrationResult(
            status="not_needed",
            canonical_env_file=canonical_env_file,
            legacy_env_file=legacy_env_file,
        )

    legacy_values = _dotenv_values_by_identity(legacy_env_file)
    if canonical_env_file.exists():
        created_env_file = False
        root_values = _dotenv_values_by_identity(canonical_env_file)
        imported_keys, conflict_keys = _append_missing_legacy_values(
            canonical_env_file,
            legacy_values=legacy_values,
            root_values=root_values,
        )
    else:
        created_env_file = True
        imported_keys, conflict_keys = _create_root_env_from_example_and_legacy(
            canonical_env_file,
            env_example_file=env_example_file,
            legacy_values=legacy_values,
        )

    backup_path = _backup_legacy_env_file(legacy_env_file, now=now)
    return EnvMigrationResult(
        status="migrated",
        canonical_env_file=canonical_env_file,
        legacy_env_file=legacy_env_file,
        imported_keys=tuple(sorted(imported_keys)),
        conflict_keys=tuple(sorted(conflict_keys)),
        backup_path=backup_path,
        created_env_file=created_env_file,
    )


def default_legacy_compose_env_file(canonical_env_file: Path) -> Path:
    """Return the legacy compose env path paired with a root env file."""
    expanded = canonical_env_file.expanduser()
    root = expanded.parent if expanded.is_absolute() else Path.cwd()
    return root / "docker" / "compose" / ".env"


def _dotenv_values_by_identity(path: Path) -> dict[str, tuple[str, str]]:
    values: dict[str, tuple[str, str]] = {}
    for key, value in dotenv_values(path).items():
        if value is None or not _ENV_NAME_RE.fullmatch(key):
            continue
        values[key.upper()] = (key, value)
    return values


def _create_root_env_from_example_and_legacy(
    canonical_env_file: Path,
    *,
    env_example_file: Path,
    legacy_values: dict[str, tuple[str, str]],
) -> tuple[list[str], list[str]]:
    template = env_example_file.read_text(encoding="utf-8") if env_example_file.exists() else ""
    template_lines = template.splitlines(keepends=True)
    emitted: set[str] = set()
    output: list[str] = []
    imported_keys: list[str] = []
    for line in template_lines:
        match = _ENV_ASSIGNMENT_RE.match(line)
        if match is None:
            output.append(line)
            continue
        identity = match.group("key").upper()
        emitted.add(identity)
        legacy = legacy_values.get(identity)
        if legacy is None:
            output.append(line)
            continue
        key, value = legacy
        output.append(f"{match.group('prefix')}{key}={_format_env_value(value)}\n")
        imported_keys.append(key)

    missing_keys = [legacy for identity, legacy in legacy_values.items() if identity not in emitted]
    if missing_keys:
        if output and not output[-1].endswith(("\n", "\r")):
            output[-1] = f"{output[-1]}\n"
        if output and output[-1].strip():
            output.append("\n")
        output.append("# Imported from legacy docker/compose/.env by AWF.\n")
        for key, value in missing_keys:
            output.append(f"{key}={_format_env_value(value)}\n")
            imported_keys.append(key)

    canonical_env_file.parent.mkdir(parents=True, exist_ok=True)
    canonical_env_file.write_text("".join(output), encoding="utf-8")
    canonical_env_file.chmod(0o600)
    return imported_keys, []


def _append_missing_legacy_values(
    canonical_env_file: Path,
    *,
    legacy_values: dict[str, tuple[str, str]],
    root_values: dict[str, tuple[str, str]],
) -> tuple[list[str], list[str]]:
    imported: list[str] = []
    conflicts: list[str] = []
    missing: list[tuple[str, str]] = []
    for identity, (key, value) in legacy_values.items():
        root = root_values.get(identity)
        if root is None:
            missing.append((key, value))
            imported.append(key)
            continue
        if root[1] != value:
            conflicts.append(root[0])

    if not missing:
        return imported, conflicts

    existing = canonical_env_file.read_text(encoding="utf-8")
    suffix: list[str] = []
    if existing and not existing.endswith(("\n", "\r")):
        suffix.append("\n")
    if existing and existing.strip():
        suffix.append("\n")
    suffix.append("# Imported from legacy docker/compose/.env by AWF.\n")
    for key, value in missing:
        suffix.append(f"{key}={_format_env_value(value)}\n")
    canonical_env_file.write_text(f"{existing}{''.join(suffix)}", encoding="utf-8")
    canonical_env_file.chmod(0o600)
    return imported, conflicts


def _format_env_value(value: str) -> str:
    if _UNQUOTED_ENV_VALUE_RE.fullmatch(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _backup_legacy_env_file(
    legacy_env_file: Path,
    *,
    now: Callable[[], datetime] | None,
) -> Path:
    timestamp = (now or (lambda: datetime.now(UTC)))().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = legacy_env_file.with_name(f".env.legacy-{timestamp}.bak")
    counter = 1
    while backup.exists():
        backup = legacy_env_file.with_name(f".env.legacy-{timestamp}.{counter}.bak")
        counter += 1
    legacy_env_file.replace(backup)
    return backup
