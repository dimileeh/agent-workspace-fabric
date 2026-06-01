"""Git path parsing helpers for PR monitor runner repair flows."""

from __future__ import annotations

from awf.control.protected_file_diffs import (
    changed_paths_from_name_status_z as _parse_name_status_z,
)
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
)


def _changed_paths_from_porcelain(status_stdout: str) -> list[str]:
    """Extract changed paths from ``git status --porcelain`` output."""
    paths: list[str] = []
    for line in status_stdout.splitlines():
        if not line:
            continue
        if line.startswith("?? ") or (len(line) >= 4 and line[2] == " "):
            status = line[:2]
            path = line[3:]
        else:
            continue
        rename_paths = (
            _split_porcelain_rename_paths(path)
            if status[:1] in {"R", "C"} or status[1:2] in {"R", "C"}
            else None
        )
        if rename_paths:
            old_path, new_path = rename_paths
            paths.extend(
                [
                    _unquote_porcelain_path(old_path),
                    _unquote_porcelain_path(new_path),
                ]
            )
        else:
            paths.append(_unquote_porcelain_path(path))
    return list(dict.fromkeys(paths))


_PORCELAIN_C_ESCAPES = {
    "a": 0x07,
    "b": 0x08,
    "t": 0x09,
    "n": 0x0A,
    "v": 0x0B,
    "f": 0x0C,
    "r": 0x0D,
    '"': 0x22,
    "\\": 0x5C,
}


def _split_porcelain_rename_paths(path: str) -> tuple[str, str] | None:
    """Split porcelain rename paths on the separator outside C-quoted paths."""
    in_quote = False
    escaped = False
    for index, char in enumerate(path):
        if in_quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_quote = False
            continue

        if char == '"':
            in_quote = True
            continue

        if path.startswith(" -> ", index):
            return path[:index], path[index + 4 :]

    return None


def _unquote_porcelain_path(path: str) -> str:
    """Decode Git's C-quoted porcelain path form when present."""
    if len(path) < 2 or path[0] != '"' or path[-1] != '"':
        return path

    raw = bytearray()
    end = len(path) - 1
    i = 1
    while i < end:
        char = path[i]
        if char != "\\":
            raw.extend(char.encode("utf-8", "surrogateescape"))
            i += 1
            continue

        i += 1
        if i >= end:
            raw.append(ord("\\"))
            break

        escaped = path[i]
        if escaped in _PORCELAIN_C_ESCAPES:
            raw.append(_PORCELAIN_C_ESCAPES[escaped])
            i += 1
            continue

        if "0" <= escaped <= "7":
            j = i + 1
            while j < end and j < i + 3 and "0" <= path[j] <= "7":
                j += 1
            raw.append(int(path[i:j], 8))
            i = j
            continue

        raw.extend(escaped.encode("utf-8", "surrogateescape"))
        i += 1

    return bytes(raw).decode("utf-8", "surrogateescape")


def _porcelain_z_records(status_stdout: str) -> list[tuple[str, str, str | None]]:
    """Split `git status --porcelain -z` output into status/path record tuples."""
    records = status_stdout.split("\0")
    if records and records[-1] == "":
        records = records[:-1]
    parsed: list[tuple[str, str, str | None]] = []
    i = 0
    while i < len(records):
        record = records[i]
        i += 1
        if len(record) < 4 or record[2] != " ":
            continue
        status = record[:2]
        path = record[3:]
        original_path: str | None = None
        if (status[:1] in {"R", "C"} or status[1:2] in {"R", "C"}) and i < len(records):
            original_path = records[i]
            i += 1
        parsed.append((status, path, original_path))
    return parsed


def _changed_paths_from_porcelain_z(status_stdout: str) -> list[str]:
    """Extract changed paths from ``git status --porcelain -z`` output."""
    paths: list[str] = []
    for status, path, original_path in _porcelain_z_records(status_stdout):
        if status == "!!":
            continue
        if original_path:
            paths.extend([original_path, path])
        else:
            paths.append(path)
    return list(dict.fromkeys(paths))


def _changed_paths_from_name_status_z(diff_stdout: str) -> tuple[str, ...]:
    """Extract changed paths from ``git diff --name-status -z`` output."""
    try:
        return _parse_name_status_z(diff_stdout)
    except ValueError as exc:
        raise ProtectedScopeDiffError(str(exc)) from exc


def _changed_paths_from_name_only_z(diff_stdout: str) -> tuple[str, ...]:
    """Extract changed paths from ``git diff --name-only -z`` output."""
    parts = diff_stdout.split("\0")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    if any(part == "" for part in parts):
        raise ProtectedScopeDiffError("empty path in `--name-only -z` output")
    return tuple(dict.fromkeys(parts))


def _untracked_paths_from_porcelain(status_stdout: str) -> list[str]:
    """Extract untracked or ignored paths from ``git status --porcelain`` output."""
    paths: list[str] = []
    for line in status_stdout.splitlines():
        if not (line.startswith("?? ") or line.startswith("!! ")):
            continue
        paths.append(_unquote_porcelain_path(line[3:]))
    return list(dict.fromkeys(paths))


def _untracked_paths_from_porcelain_z(status_stdout: str) -> list[str]:
    """Extract untracked paths from ``git status --porcelain -z`` output."""
    return list(
        dict.fromkeys(
            path
            for status, path, _original_path in _porcelain_z_records(status_stdout)
            if status in {"??", "!!"}
        )
    )
