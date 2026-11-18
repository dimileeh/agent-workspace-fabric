"""Park when CI-repair operator-decision conflict undo fails.

A failed worktree reset/clean or hosted remote rewind on the conflict path must
not fail the workspace (``REPAIR_DIRTY_COMMIT_FAILED``). Instead AWF parks under
``CI_REPAIR_CONFLICT_ROLLBACK_PENDING``, keeps ``monitoring_pr``, and
``decide()`` returns that ``NotifyHuman`` before every other action until the
operator cancels and re-adopts. Only a ``NEEDS_HUMAN`` reason that contains
``conflicts with operator decision`` enters this path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from awf.runtime.monitor_state_keys import (
    _ci_repair_conflict_rollback_pending_key,
)
from awf.runtime.pr_monitor_runner.applied_operator_directives import (
    CI_REPAIR_OPERATOR_DECISION_CONFLICT,
    mark_ci_repair_operator_decision_conflict,
    needs_human_verdict_reason,
)
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult

if TYPE_CHECKING:
    from awf.runtime.pr_monitor import CheckFailure, MonitorState, NotifyHuman, PRStatus

CI_REPAIR_CONFLICT_ROLLBACK_PENDING = "CI_REPAIR_CONFLICT_ROLLBACK_PENDING"
_OPERATOR_DECISION_CONFLICT_MARKER = "conflicts with operator decision"


@dataclass(frozen=True, slots=True)
class CiRepairConflictRollbackPending:
    """Workspace-level park while a failed conflict undo still needs a human."""

    leftover_message: str


def operator_decision_conflict_reason(stdout: str) -> str | None:
    """Return NEEDS_HUMAN reason only when it is an operator-decision conflict."""
    reason = needs_human_verdict_reason(stdout)
    if reason is None:
        return None
    if _OPERATOR_DECISION_CONFLICT_MARKER not in reason:
        return None
    return reason


def conflict_rollback_pending_leftover_message(
    *,
    cause: str | None,
    published_remote_head: str | None,
    restore_ref: str,
) -> str:
    """Name what the failed undo left behind for the parked human message."""
    if cause == "hosted_remote_rollback_failed" and published_remote_head:
        return f"remote head {published_remote_head} not rewound to {restore_ref}"
    return "leftover worktree edits"


def conflict_rollback_pending_human_message(*, leftover_message: str) -> str:
    """Human-facing park message; exit is cancel + re-adopt (nothing clears it)."""
    return (
        f"CI repair operator-decision conflict; rollback pending: {leftover_message}. "
        "Cancel and re-adopt for a clean worktree; this park is not cleared automatically."
    )


def mark_ci_repair_conflict_rollback_pending(
    state: MonitorState,
    *,
    leftover_message: str,
) -> None:
    """Persist the workspace-level pending park (leftover message only)."""
    state.mark_addressed(
        _ci_repair_conflict_rollback_pending_key(),
        leftover_message,
    )


def ci_repair_conflict_rollback_pending(
    state: MonitorState,
) -> CiRepairConflictRollbackPending | None:
    """Return the unfinished conflict-rollback park for this workspace, if any."""
    raw = state.threads_addressed_ids.get(_ci_repair_conflict_rollback_pending_key())
    if raw is None:
        return None
    leftover = str(raw).strip()
    if not leftover:
        return None
    return CiRepairConflictRollbackPending(leftover_message=leftover)


def notify_human_for_conflict_rollback_pending(
    state: MonitorState,
) -> NotifyHuman | None:
    """Return NotifyHuman when the unfinished conflict rollback park remains."""
    from awf.runtime.pr_monitor import NotifyHuman

    pending = ci_repair_conflict_rollback_pending(state)
    if pending is None:
        return None
    return NotifyHuman(
        message=conflict_rollback_pending_human_message(leftover_message=pending.leftover_message),
        reason_code=CI_REPAIR_CONFLICT_ROLLBACK_PENDING,
    )


def parked_conflict_rollback_pending_result(
    *,
    leftover_message: str,
    conflict_reason: str,
    cause: str | None = None,
    restore_ref: str | None = None,
    published_remote_head: str | None = None,
    details: dict[str, Any] | None = None,
) -> _GitPushResult:
    """Build the parked_needs_human envelope for a failed conflict undo."""
    message = conflict_rollback_pending_human_message(leftover_message=leftover_message)
    result_details: dict[str, Any] = {
        "phase": "ci_repair_operator_decision_conflict",
        "operation_type": "ci_repair",
        "pushed": False,
        "conflict_reason": conflict_reason,
        "rollback_pending": {
            "leftover_message": leftover_message,
            "cause": cause,
            "restore_ref": restore_ref,
            "published_remote_head": published_remote_head,
        },
    }
    if details:
        result_details.update(details)
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr=message,
        reason_code=CI_REPAIR_CONFLICT_ROLLBACK_PENDING,
        parked_needs_human=True,
        details=result_details,
    )


async def handle_operator_decision_conflict_after_ci_repair(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    state: MonitorState,
    status: PRStatus | None,
    failures: tuple[CheckFailure, ...],
    conflict_stdout: str,
    operation_start_head: str | None,
    operation_start_last_push_sha: str | None,
) -> _GitPushResult | None:
    """Roll back conflict residue or park pending; None when not a conflict."""
    from awf.runtime.pr_monitor_runner import ci_ops

    conflict_reason = operator_decision_conflict_reason(conflict_stdout)
    if conflict_reason is None:
        return None

    conflict_head = status.head_sha if status is not None else (operation_start_head or "")
    published_hosted_head = await ci_ops._detect_hosted_ci_repair_conflict_published_head(
        self,
        worktree_path=worktree_path,
        state=state,
        restore_ref=operation_start_head,
        operation_start_last_push_sha=operation_start_last_push_sha,
    )
    rollback_result = await ci_ops._rollback_ci_fix_residue_before_provider_recovery(
        self,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        restore_ref=operation_start_head,
    )
    if rollback_result.ok and published_hosted_head is not None and operation_start_head:
        rollback_result = await ci_ops._rewind_hosted_ci_repair_conflict_remote(
            self,
            workspace_id=workspace_id,
            state=state,
            restore_ref=operation_start_head,
            published_remote_head=published_hosted_head,
            operation_start_last_push_sha=operation_start_last_push_sha,
        )
    if not rollback_result.ok:
        restore_ref = (operation_start_head or "").strip() or conflict_head
        leftover = conflict_rollback_pending_leftover_message(
            cause=rollback_result.cause,
            published_remote_head=published_hosted_head,
            restore_ref=restore_ref,
        )
        mark_ci_repair_conflict_rollback_pending(state, leftover_message=leftover)
        _log.warning(
            "monitor.ci_fix_operator_decision_conflict_rollback_pending",
            workspace_id=workspace_id,
            reason_code=CI_REPAIR_CONFLICT_ROLLBACK_PENDING,
            rollback_cause=rollback_result.cause,
            leftover_message=leftover,
        )
        return parked_conflict_rollback_pending_result(
            leftover_message=leftover,
            conflict_reason=conflict_reason,
            cause=rollback_result.cause,
            restore_ref=restore_ref,
            published_remote_head=published_hosted_head,
            details={
                "rollback_error": {
                    "message": rollback_result.message,
                    "cause": rollback_result.cause,
                },
            },
        )

    if conflict_head:
        mark_ci_repair_operator_decision_conflict(
            state,
            head_sha=conflict_head,
            failures=failures,
            reason=conflict_reason,
        )
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr=conflict_reason,
        reason_code=CI_REPAIR_OPERATOR_DECISION_CONFLICT,
        parked_needs_human=True,
    )
