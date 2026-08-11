"""Pinned Git command helpers for isolated review-comment re-asks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from awf.common.git_identity import git_safe_directory_config_args
from awf.node.git_manager import git_env_without_object_lookup_overrides
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command


def _pinned_linked_worktree_command(
    worktree_path: Path,
    source_git_dir: Path,
    *args: str,
) -> list[str]:
    """Build a command that ignores the primary checkout's mutable `.git` file."""
    return [
        "git",
        *git_safe_directory_config_args(worktree_path),
        "--git-dir",
        str(source_git_dir),
        "--work-tree",
        str(worktree_path),
        *args,
    ]


def _pinned_git_dir_command(git_dir: Path, *args: str) -> list[str]:
    """Build a command against source Git metadata that validation pinned."""
    return ["git", "--git-dir", str(git_dir), *args]


def _reask_source_mirror_command(
    worktree_path: Path,
    source_mirror: Path | None,
    *args: str,
) -> list[str]:
    """Build a source-mirror command without consulting the source `.git` when pinned."""
    if source_mirror is not None:
        return _pinned_git_dir_command(source_mirror, *args)
    return git_worktree_command(worktree_path, *args)


async def _rev_parse_pinned_reask_source_head(
    runner: Any,
    source_git_dir: Path,
    *,
    timeout_seconds: float,
) -> str | None:
    """Resolve a source HEAD through its pinned linked-worktree admin directory."""
    result = await runner._deps.runner.run(
        _pinned_git_dir_command(source_git_dir, "rev-parse", "HEAD"),
        timeout_seconds=timeout_seconds,
        env=git_env_without_object_lookup_overrides(),
    )
    if not result.ok:
        return None
    return result.stdout.strip() or None
