"""Shared Git loaders for protected quality-gate file diffs."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from awf.common.commands import AsyncCommandRunner
from awf.common.git_identity import git_safe_directory_config_args
from awf.control.quality_gates import (
    ProtectedFileDiff,
    diff_classified_protected_paths,
)


async def git_show_text(
    runner: AsyncCommandRunner,
    *,
    worktree_path: Path,
    refspec: str,
) -> str | None:
    """Return `git show` text, treating missing paths as absent content."""
    result = await runner.run(
        [
            "git",
            *git_safe_directory_config_args(worktree_path),
            "-C",
            str(worktree_path),
            "show",
            refspec,
        ]
    )
    if result.ok:
        return result.stdout
    error_text = (result.stderr or result.stdout or "").lower()
    if "path " in error_text and (
        "does not exist" in error_text or "exists on disk, but not in" in error_text
    ):
        return None
    details = (result.stderr or result.stdout or "<no output>").strip()
    raise RuntimeError(f"git show failed for {refspec!r} in {worktree_path}: {details}")


async def protected_file_diffs_for_committed_paths(
    runner: AsyncCommandRunner,
    *,
    worktree_path: Path,
    base_ref: str,
    changed_paths: Sequence[str],
) -> dict[str, ProtectedFileDiff]:
    """Load old/new content for committed protected files changed since `base_ref`."""
    diffs: dict[str, ProtectedFileDiff] = {}
    for path in diff_classified_protected_paths(changed_paths):
        old_text = await git_show_text(
            runner,
            worktree_path=worktree_path,
            refspec=f"{base_ref}:{path}",
        )
        new_text = await git_show_text(
            runner,
            worktree_path=worktree_path,
            refspec=f"HEAD:{path}",
        )
        diffs[path] = ProtectedFileDiff(
            path=path,
            old_text=old_text,
            new_text=new_text,
        )
    return diffs
