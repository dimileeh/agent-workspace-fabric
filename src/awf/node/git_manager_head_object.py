"""HEAD object reachability probes for linked worktrees."""

from __future__ import annotations

import asyncio
from pathlib import Path

from awf.common.git_identity import git_safe_directory_config_args
from awf.common.logging import get_logger
from awf.node.git_manager_ownership import git_env_without_object_lookup_overrides

_log = get_logger(__name__)


async def verify_head_object_exists(worktree_path: Path) -> bool:
    """Return ``True`` when HEAD's commit object is reachable in the object database.

    Uses ``git cat-file -e HEAD^{commit}`` which exits 0 when the object exists
    and non-zero when the ref exists but the commit object is missing. Repository
    alternates are cleared before probing because they can make a shared mirror
    appear to contain objects that only exist in a workspace-private store.
    """
    if not _clear_repository_object_alternates(worktree_path):
        return False

    proc = await asyncio.create_subprocess_exec(
        "git",
        *git_safe_directory_config_args(worktree_path),
        "-C",
        str(worktree_path),
        "cat-file",
        "-e",
        "HEAD^{commit}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=git_env_without_object_lookup_overrides(),
    )
    await proc.communicate()
    assert proc.returncode is not None
    return proc.returncode == 0


def _clear_repository_object_alternates(worktree_path: Path) -> bool:
    alternates_path = _repository_alternates_path_for_worktree(worktree_path)
    if alternates_path is None:
        return True
    try:
        alternates_path.unlink()
    except FileNotFoundError:
        return True
    except OSError as exc:
        _log.warning(
            "git.repository_alternates_clear_failed",
            path=str(alternates_path),
            error=str(exc),
        )
        return False
    _log.warning("git.repository_alternates_cleared", path=str(alternates_path))
    return True


def _repository_alternates_path_for_worktree(worktree_path: Path) -> Path | None:
    # Late import avoids a cycle: ``git_manager`` re-exports this module.
    from awf.node.git_manager import mirror_path_for_worktree

    common_dir = mirror_path_for_worktree(worktree_path)
    if common_dir is not None:
        return common_dir / "objects" / "info" / "alternates"

    git_dir = worktree_path / ".git"
    if git_dir.is_dir():
        return git_dir / "objects" / "info" / "alternates"
    return None
