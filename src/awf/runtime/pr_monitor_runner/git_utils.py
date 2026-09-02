"""Utilities for constructing git commands inside worktree directories."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from awf.common.commands import AsyncCommandRunner, CommandResult
from awf.common.git_identity import git_safe_directory_config_args
from awf.node.git_manager import UNTRUSTED_NESTED_GIT_CONFIG_ARGS
from awf.runtime.worktree_writer_lock import (
    git_args_mutate_worktree,
    hold_exclusive_worktree_writer_lock,
)


def git_worktree_command(worktree_path: Path, *args: str) -> list[str]:
    """Build a git command vector scoped to a specific worktree."""
    return [
        "git",
        *git_safe_directory_config_args(worktree_path),
        "-C",
        str(worktree_path),
        *args,
    ]


def git_untrusted_nested_worktree_command(worktree_path: Path, *args: str) -> list[str]:
    """Build a git command vector for agent-controlled embedded repositories."""
    return [
        "git",
        *git_safe_directory_config_args(worktree_path),
        *UNTRUSTED_NESTED_GIT_CONFIG_ARGS,
        "-C",
        str(worktree_path),
        *args,
    ]


def git_untrusted_nested_pinned_worktree_command(
    git_dir: Path,
    worktree_path: Path,
    *args: str,
) -> list[str]:
    """Build a git command pinned to a nested repo's git-dir and work-tree."""
    return [
        "git",
        *git_safe_directory_config_args(worktree_path),
        *UNTRUSTED_NESTED_GIT_CONFIG_ARGS,
        "--git-dir",
        str(git_dir),
        "--work-tree",
        str(worktree_path),
        *args,
    ]


def git_untrusted_nested_snapshot_discovery_command(
    git_dir: Path,
    worktree_path: Path,
    *args: str,
) -> list[str]:
    """Build a discovery command using a validated config snapshot git-dir.

    Uses ``-C`` without ``--work-tree`` so snapshotted ``core.worktree`` still
    redirects ``rev-parse --show-toplevel`` (PRRT_kwDOSJAM6s6elv_p).
    """
    return [
        "git",
        *git_safe_directory_config_args(worktree_path),
        *UNTRUSTED_NESTED_GIT_CONFIG_ARGS,
        "--git-dir",
        str(git_dir),
        "-C",
        str(worktree_path),
        *args,
    ]


async def run_worktree_git(
    runner: AsyncCommandRunner,
    worktree_path: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
    timeout_seconds: float | None = None,
) -> CommandResult:
    """Run a scoped git command, serializing mutating worktree writes."""
    command = git_worktree_command(worktree_path, *args)
    if git_args_mutate_worktree(args):
        async with hold_exclusive_worktree_writer_lock(worktree_path):
            return await runner.run(command, env=env, timeout_seconds=timeout_seconds)
    return await runner.run(command, env=env, timeout_seconds=timeout_seconds)
