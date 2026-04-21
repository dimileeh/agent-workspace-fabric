"""Workspace cleanup — tear down the compose stack, remove the worktree.

``cleanup(workspace)`` is idempotent so cron-style retry loops can call it
without having to check whether any specific artifact still exists. Errors
from individual cleanup steps are logged but don't abort the rest of the
teardown — one stuck container must not leak a git worktree.
"""

from __future__ import annotations

from pathlib import Path

from awf.common.logging import get_logger
from awf.node.compose_manager import ComposeManager, ComposeOperationError, WorkspaceComposeSpec
from awf.node.git_manager import GitManager, GitOperationError

_log = get_logger(__name__)


class WorkspaceCleaner:
    """Teardown coordinator. Holds the git + compose managers as dependencies."""

    def __init__(self, *, git: GitManager, compose: ComposeManager) -> None:
        self._git = git
        self._compose = compose

    async def cleanup(
        self,
        *,
        workspace_id: str,
        repo_url: str,
        worktree_host_path: Path | None = None,
    ) -> list[str]:
        """Best-effort cleanup. Returns list of failure-step names, empty on full success.

        The worktree host path is derived from ``workspace_id`` when not
        supplied. Callers may pass it explicitly if they've already computed it
        (avoids a second directory lookup).
        """
        failures: list[str] = []

        # Step 1: compose down -v (stops containers, removes volumes + network).
        spec = WorkspaceComposeSpec(
            workspace_id=workspace_id,
            worktree_host_path=worktree_host_path or Path("/dev/null"),
        )
        try:
            await self._compose.down(spec, remove_volumes=True)
        except ComposeOperationError as exc:
            _log.warning(
                "cleanup.compose_down_failed",
                workspace_id=workspace_id,
                reason_code=exc.reason_code,
                stderr=exc.stderr[:1000],
            )
            failures.append("compose_down")

        # Step 2: git worktree remove (idempotent already per GitManager).
        try:
            await self._git.remove_worktree(workspace_id=workspace_id, repo_url=repo_url)
        except GitOperationError as exc:
            _log.warning(
                "cleanup.git_remove_failed",
                workspace_id=workspace_id,
                reason_code=exc.reason_code,
                stderr=exc.stderr[:1000],
            )
            failures.append("worktree_remove")

        return failures
