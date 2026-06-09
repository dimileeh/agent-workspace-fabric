"""Shared git identity defaults for AWF-authored workspace commits."""

from __future__ import annotations

from pathlib import Path

DEFAULT_GIT_AUTHOR_NAME = "AWF Agent"
DEFAULT_GIT_AUTHOR_EMAIL = "awf@example.com"


def git_identity_config_args(
    *,
    name: str = DEFAULT_GIT_AUTHOR_NAME,
    email: str = DEFAULT_GIT_AUTHOR_EMAIL,
) -> list[str]:
    """Return ``git -c`` args that make commit identity explicit."""

    return ["-c", f"user.name={name}", "-c", f"user.email={email}"]


def git_safe_directory_config_args(worktree_path: str | Path) -> list[str]:
    """Return ``git -c`` args allowing AWF's managed worktree path."""

    return ["-c", f"safe.directory={worktree_path}"]
