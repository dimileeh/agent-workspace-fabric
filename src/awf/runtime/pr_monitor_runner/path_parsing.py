"""Git path parsing helpers for PR monitor runner repair flows."""

from __future__ import annotations

from awf.control.protected_file_diffs import (
    changed_paths_from_name_status_z as _parse_name_status_z,
)
from awf.runtime.git_porcelain import (
    changed_paths_from_porcelain as _shared_changed_paths_from_porcelain,
)
from awf.runtime.git_porcelain import split_porcelain_rename_paths, unquote_porcelain_path
from awf.runtime.git_porcelain import (
    untracked_paths_from_porcelain as _shared_untracked_paths_from_porcelain,
)
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
)

_split_porcelain_rename_paths = split_porcelain_rename_paths
_unquote_porcelain_path = unquote_porcelain_path


def _changed_paths_from_porcelain(status_stdout: str) -> list[str]:
    """Extract changed paths from ``git status --porcelain`` output."""
    return list(_shared_changed_paths_from_porcelain(status_stdout))


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
    """Extract untracked paths from ``git status --porcelain`` output."""
    return list(_shared_untracked_paths_from_porcelain(status_stdout))


def _untracked_paths_from_porcelain_z(status_stdout: str) -> list[str]:
    """Extract untracked paths from ``git status --porcelain -z`` output."""
    return list(
        dict.fromkeys(
            path
            for status, path, _original_path in _porcelain_z_records(status_stdout)
            if status == "??"
        )
    )
