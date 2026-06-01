"""Utilities for constructing git commands inside worktree directories."""

from __future__ import annotations

from pathlib import Path

from awf.common.git_identity import git_safe_directory_config_args


def git_worktree_command(worktree_path: Path, *args: str) -> list[str]:
    """Build a git command vector scoped to a specific worktree."""
    return [
        "git",
        *git_safe_directory_config_args(worktree_path),
        "-C",
        str(worktree_path),
        *args,
    ]
