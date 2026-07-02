"""Shared git subprocess helpers for salvage capture paths."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Protocol

from awf.common.git_identity import git_safe_directory_config_args

GIT_TIMEOUT_SECONDS = 30.0


class CompletedProcessLike(Protocol):
    """Protocol describing the small git subprocess result contract."""

    returncode: int
    stdout: str
    stderr: str


class SubprocessRun(Protocol):
    """Protocol for a subprocess runner used by salvage operations."""

    def __call__(  # pragma: no cover - Protocol method declaration only.
        self,
        args: list[str],
        *,
        check: bool,
        capture_output: bool,
        text: Literal[True],
        timeout: float,
        env: Mapping[str, str],
    ) -> CompletedProcessLike:
        """Execute subprocess command and return a captured result."""
        ...


class SalvageGitErrorFactory(Protocol):
    """Factory for salvage-specific git failure exceptions."""

    def __call__(
        self,
        *,
        reason_code: str,
        message: str,
        detail: dict[str, Any] | None = None,
    ) -> Exception:
        """Build a salvage git failure exception."""
        ...


def git_lines(value: str) -> list[str]:
    """Split git output into a sorted list of non-empty changed paths."""
    return sorted(line.strip() for line in value.splitlines() if line.strip())


def paths_from_name_status(value: str) -> list[str]:
    """Collect all old and new paths from ``git diff --name-status`` output."""
    paths: set[str] = set()
    for line in value.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split("\t")
        status = parts[0]
        if status.startswith(("R", "C")) and len(parts) >= 3:
            paths.add(parts[1])
            paths.add(parts[2])
        elif len(parts) >= 2:
            paths.add(parts[1])
    return sorted(paths)


def _timeout_output_text(output: str | bytes | None) -> str:
    """Normalize timeout-captured subprocess output to JSON-safe text."""
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output


def run_git(
    worktree: Path,
    args: list[str],
    *,
    run: SubprocessRun,
    env: Mapping[str, str],
    raise_error: SalvageGitErrorFactory,
    failure_reason: str,
    failure_context: str,
) -> CompletedProcessLike:
    """Run a git command for salvage with deterministic timeout and failure mapping."""
    try:
        result = run(
            ["git", *git_safe_directory_config_args(worktree), "-C", str(worktree), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        detail: dict[str, Any] = {"timeout_seconds": GIT_TIMEOUT_SECONDS}
        stdout = _timeout_output_text(exc.stdout)
        if stdout:
            detail["stdout"] = stdout[-2000:]
        stderr = _timeout_output_text(exc.stderr)
        if stderr:
            detail["stderr"] = stderr[-2000:]
        raise raise_error(
            reason_code=failure_reason,
            message=f"git {' '.join(args)} timed out during {failure_context}.",
            detail=detail,
        ) from exc
    if result.returncode != 0:
        raise raise_error(
            reason_code=failure_reason,
            message=f"git {' '.join(args)} failed during {failure_context}.",
            detail={
                "stdout": result.stdout[-2000:],
                "stderr": result.stderr[-2000:],
            },
        )
    return result
