"""Lock the reused checkout's worktree registration on local PR-monitor resume (#1133).

Workspaces created before AWF registered linked worktrees locked stay exposed to
an in-container ``git worktree prune`` until something locks them. A local
monitor resume reuses the kept checkout, so it locks an unlocked registration
under the mirror lock. Hosted resume goes through ``GitManager.ensure_worktree``,
which does the same.
"""

from __future__ import annotations

from typing import Any

from awf.node.git_manager_registration import lock_reused_registration


async def lock_resumed_worktree_registration(
    self: Any, *, workspace_id: str, repo_url: str
) -> None:
    """Lock the registration of ``workspace_id``'s checkout when AWF knows its mirror."""
    roots = self._trusted_git_roots_for_workspace(workspace_id=workspace_id, repo_url=repo_url)
    if roots is None:
        return
    await lock_reused_registration(
        mirror_path=roots[1],
        worktree_path=self._config.worktrees_root / workspace_id,
        workspace_id=workspace_id,
    )
