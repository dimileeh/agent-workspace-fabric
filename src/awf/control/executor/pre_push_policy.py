"""Pre-push policy checks for executor validation output."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from awf.control.executor.quality_gates import _log
from awf.db.enums import FailureReason, WorkspaceStatus


async def run_pre_push_policy_checks(
    executor: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    base_commit: str,
    owned_paths: list[str],
    execution_owner_id: str | None,
    repair_mirror_hooks_path_or_mark_failed: Callable[..., Awaitable[bool | str]],
) -> bool:
    """Run committed-output policy checks.

    Returns True when the caller should stop execution because a guard already
    handled the workspace state.
    """
    # The committed-output gates below diff ``base..HEAD`` in the worktree. If
    # the worktree vanished during validation/repair the diff would fail and the
    # empty-net-diff branch of the plan-only gate would mislabel the disappearance
    # as a terminal PLAN_ONLY_OUTPUT agent failure. Surface the missing worktree
    # as WORKTREE_MISSING (infrastructure) first so the reason code reflects the
    # real cause, mirroring the worktree guard at the push step below.
    if not await executor._ensure_worktree_available(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        expected=WorkspaceStatus.validating,
        action="pre_push_policy_check",
    ):
        return True
    if not await repair_mirror_hooks_path_or_mark_failed(
        failure_stage="before post-validation policy checks",
        failure_from_status=WorkspaceStatus.validating,
    ):
        return True
    try:
        if await executor._fail_if_plan_only_committed_output(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            base_commit=base_commit,
            expected_status=WorkspaceStatus.validating,
        ):
            return True
        if await executor._fail_if_protected_quality_gate_committed_output(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            base_commit=base_commit,
            owned_paths=owned_paths,
            expected_status=WorkspaceStatus.validating,
            execution_owner_id=execution_owner_id,
        ):
            return True
    except Exception as exc:
        _log.exception("executor.pre_push_policy_check_failed", workspace_id=workspace_id)
        await executor._mark_failed(
            workspace_id=workspace_id,
            from_status=WorkspaceStatus.validating,
            failure_reason=FailureReason.infrastructure_failure,
            message=f"pre-push policy check failed: {exc!r}"[:2000],
        )
        return True
    return False
