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
    """
    Build a Git command pinned to the specified nested repository and worktree.
    
    Parameters:
        git_dir (Path): Path to the nested repository's Git directory.
        worktree_path (Path): Path to the nested repository's worktree.
        *args (str): Additional Git arguments.
    
    Returns:
        list[str]: Command arguments for the configured Git invocation.
    """
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
    """
    Build a Git command for discovering a nested repository from a validated Git directory snapshot.
    
    Parameters:
    	git_dir (Path): Validated Git directory snapshot to use.
    	worktree_path (Path): Worktree path to scope the command to.
    
    Returns:
    	list[str]: Command arguments that preserve the worktree's `core.worktree` configuration.
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
    """
    Run a Git command scoped to a worktree, serializing operations that mutate it.
    
    Parameters:
    	worktree_path (Path): Path to the worktree in which to run the command
    	*args (str): Git command arguments
    
    Returns:
    	CommandResult: Result of the executed Git command
    """
    command = git_worktree_command(worktree_path, *args)
    if git_args_mutate_worktree(args):
        async with hold_exclusive_worktree_writer_lock(worktree_path):
            return await runner.run(command, env=env, timeout_seconds=timeout_seconds)
    return await runner.run(command, env=env, timeout_seconds=timeout_seconds)
