"""Path and policy helper functions for PR monitor repair flows."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from awf.control.quality_gates import QualityGateViolation
from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError


def _changed_paths_from_name_only_z(diff_stdout: str | bytes) -> tuple[str, ...]:
    """Extract changed paths from ``git diff --name-only -z`` output.

    Prefer raw ``bytes`` when available: ``AsyncioSubprocessRunner`` decodes
    ``CommandResult.stdout`` with ``errors="replace"``, which rewrites legal Git
    pathnames that contain invalid UTF-8 and breaks later ``ls-tree`` lookups.
    Byte records are decoded with ``surrogateescape`` so argv round-trips restore
    the original pathname bytes on Unix.
    """
    if isinstance(diff_stdout, bytes):
        if not diff_stdout:
            return ()
        if b"\0" not in diff_stdout:
            raise ProtectedScopeDiffError(
                "expected NUL-delimited output from `git diff --name-only -z`"
            )
        if not diff_stdout.endswith(b"\0"):
            raise ProtectedScopeDiffError(
                "truncated `--name-only -z` output: missing terminating NUL"
            )
        byte_parts = diff_stdout.split(b"\0")[:-1]
        if any(part == b"" for part in byte_parts):
            raise ProtectedScopeDiffError("empty path in `--name-only -z` output")
        return tuple(
            dict.fromkeys(part.decode("utf-8", errors="surrogateescape") for part in byte_parts)
        )
    if not diff_stdout:
        return ()
    if "\0" not in diff_stdout:
        raise ProtectedScopeDiffError(
            "expected NUL-delimited output from `git diff --name-only -z`"
        )
    if not diff_stdout.endswith("\0"):
        raise ProtectedScopeDiffError("truncated `--name-only -z` output: missing terminating NUL")
    text_parts = diff_stdout.split("\0")[:-1]
    if any(part == "" for part in text_parts):
        raise ProtectedScopeDiffError("empty path in `--name-only -z` output")
    return tuple(dict.fromkeys(text_parts))


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


def _supply_chain_policy_blocked_message(reason_codes: Iterable[str]) -> str:
    codes = list(dict.fromkeys(reason_codes))
    suffix = f": {', '.join(codes)}" if codes else "."
    return f"Supply-chain policy blocked PR monitor publication{suffix}"
