"""Path and policy helper functions for PR monitor repair flows."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from awf.control.protected_file_diffs import (
    changed_paths_from_name_status_z as _parse_name_status_z,
)
from awf.control.quality_gates import QualityGateViolation
from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError


def _changed_paths_from_porcelain(status_stdout: str) -> list[str]:
    """Extract changed paths from ``git status --porcelain`` output."""
    paths: list[str] = []
    for line in status_stdout.splitlines():
        if not line:
            continue
        if line.startswith("?? ") or (len(line) >= 4 and line[2] == " "):
            path = line[3:]
        else:
            continue
        if " -> " in path:
            old_path, new_path = path.split(" -> ", 1)
            paths.extend([old_path, new_path])
        else:
            paths.append(path)
    return list(dict.fromkeys(paths))


def _porcelain_z_records(status_stdout: str) -> list[tuple[str, str, str | None]]:
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
    if not diff_stdout:
        return ()
    if "\0" not in diff_stdout:
        raise ProtectedScopeDiffError(
            "expected NUL-delimited output from `git diff --name-only -z`"
        )
    if not diff_stdout.endswith("\0"):
        raise ProtectedScopeDiffError("truncated `--name-only -z` output: missing terminating NUL")
    parts = diff_stdout.split("\0")
    parts = parts[:-1]
    if any(part == "" for part in parts):
        raise ProtectedScopeDiffError("empty path in `--name-only -z` output")
    return tuple(dict.fromkeys(parts))


def _quality_gate_violation_paths(violations: Sequence[QualityGateViolation]) -> list[str]:
    return list(dict.fromkeys(violation.path for violation in violations))


def _read_worktree_text(path: Path, *, display_path: str | None = None) -> str:
    label = display_path or str(path)
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ProtectedScopeDiffError(
            f"Could not read protected worktree file {label!r} as UTF-8 for classification"
        ) from exc
    except OSError as exc:
        raise ProtectedScopeDiffError(
            f"Could not read protected worktree file {label!r} for classification: {exc}"
        ) from exc


def _untracked_paths_from_porcelain(status_stdout: str) -> list[str]:
    """Extract untracked paths from ``git status --porcelain`` output."""
    paths: list[str] = []
    for line in status_stdout.splitlines():
        if not line.startswith("?? "):
            continue
        paths.append(line[3:])
    return list(dict.fromkeys(paths))


def _untracked_paths_from_porcelain_z(status_stdout: str) -> list[str]:
    """Extract untracked paths from ``git status --porcelain -z`` output."""
    return list(
        dict.fromkeys(
            path
            for status, path, _original_path in _porcelain_z_records(status_stdout)
            if status == "??"
        )
    )


def _supply_chain_policy_blocked_message(reason_codes: Iterable[str]) -> str:
    codes = list(dict.fromkeys(reason_codes))
    suffix = f": {', '.join(codes)}" if codes else "."
    return f"Supply-chain policy blocked PR monitor publication{suffix}"
