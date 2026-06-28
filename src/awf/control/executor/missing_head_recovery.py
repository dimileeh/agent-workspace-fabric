"""Missing-HEAD recovery helpers for executor cleanup-failure paths.

After a Compose ``exec`` cleanup failure (a tagged process that outlived its
invocation), the worktree HEAD object can be left missing. These helpers run
the shared ``_recover_missing_git_head_or_mark_failed`` recovery before the
outer failure path marks the workspace failed, mirroring the delegation shape
of :mod:`awf.control.executor.mirror_hooks_repair`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from awf.common.compose_exec import ComposeExecCleanupError
from awf.db.enums import WorkspaceStatus


async def recover_missing_head_after_cleanup_failure(
    exc: ComposeExecCleanupError,
    *,
    executor: Any,
    workspace_id: str,
    worktree_path: Path,
    base_commit: str | None,
    branch_name: str,
    task_tag: str | None,
    stage: str,
    verify_head_object_exists_fn: Callable[[Path], Awaitable[bool]],
    from_status: WorkspaceStatus = WorkspaceStatus.running,
    owned_paths: list[str] | None = None,
    execution_owner_id: str | None = None,
    verify_post_agent_commit: bool = False,
) -> bool:
    """Recover a missing worktree HEAD before the outer failure path runs.

    Returns ``True`` when HEAD is already present or was recovered (and, for
    ``verify_post_agent_commit``, the recovered post-agent commit verified);
    ``False`` when recovery is unavailable or failed, leaving the caller to
    mark the workspace failed. ``mark_failed_on_failure`` is always ``False``
    here so the caller owns the terminal transition.
    """
    if await verify_head_object_exists_fn(worktree_path):
        return True
    recover_missing_head = getattr(executor, "_recover_missing_git_head_or_mark_failed", None)
    if recover_missing_head is None:
        return False
    recovered = await recover_missing_head(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        base_commit=base_commit,
        branch_name=branch_name,
        from_status=from_status,
        stage=stage,
        error=exc,
        task_tag=task_tag,
        mark_failed_on_failure=False,
    )
    if not verify_post_agent_commit:
        return bool(recovered)
    if not recovered:
        return False
    recovered_commit_verified = await executor._verify_recovered_post_agent_commit_or_mark_failed(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        base_commit=base_commit,
        owned_paths=list(owned_paths or []),
        expected_status=from_status,
        execution_owner_id=execution_owner_id,
        mark_failed_on_failure=False,
    )
    return bool(recovered_commit_verified)
