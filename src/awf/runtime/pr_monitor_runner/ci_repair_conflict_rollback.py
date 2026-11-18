"""Park + retry when CI-repair operator-decision conflict undo fails.

A failed worktree reset/clean or hosted remote rewind on the conflict path must
not fail the workspace (``REPAIR_DIRTY_COMMIT_FAILED``). Instead AWF parks under
``CI_REPAIR_CONFLICT_ROLLBACK_PENDING``, keeps ``monitoring_pr``, and retries the
same undo on each poll until it succeeds — then installs the normal
``CI_REPAIR_OPERATOR_DECISION_CONFLICT`` park. Only a ``NEEDS_HUMAN`` reason that
contains ``conflicts with operator decision`` enters this path.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from awf.runtime.monitor_state_keys import (
    _ci_repair_conflict_rollback_pending_key_prefix,
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
    """Persisted undo context while a conflict rollback is still pending."""

    conflict_reason: str
    restore_ref: str
    published_remote_head: str | None
    operation_start_last_push_sha: str | None
    leftover_message: str
    cause: str | None


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


def _conflict_failure_identity(failure: CheckFailure) -> tuple[str, str, str]:
    return (failure.run_id or "", failure.name, failure.conclusion)


def _pending_state_key(head_sha: str, failures: tuple[CheckFailure, ...]) -> str:
    signature = json.dumps(
        sorted(_conflict_failure_identity(failure) for failure in failures),
        separators=(",", ":"),
    )
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12]
    return f"{_ci_repair_conflict_rollback_pending_key_prefix()}{head_sha}:{digest}"


def mark_ci_repair_conflict_rollback_pending(
    state: MonitorState,
    *,
    head_sha: str,
    failures: tuple[CheckFailure, ...],
    pending: CiRepairConflictRollbackPending,
) -> None:
    """Persist a pending conflict undo for this head / failing-check signature."""
    payload = {
        "conflict_reason": pending.conflict_reason,
        "restore_ref": pending.restore_ref,
        "published_remote_head": pending.published_remote_head,
        "operation_start_last_push_sha": pending.operation_start_last_push_sha,
        "leftover_message": pending.leftover_message,
        "cause": pending.cause,
    }
    state.mark_addressed(
        _pending_state_key(head_sha, failures),
        json.dumps(payload, separators=(",", ":")),
    )


def clear_ci_repair_conflict_rollback_pending(
    state: MonitorState,
    *,
    head_sha: str | None = None,
    failures: tuple[CheckFailure, ...] | None = None,
) -> None:
    """Drop one pending park or every pending park when no signature is given."""
    if head_sha is not None and failures is not None:
        state.threads_addressed_ids.pop(_pending_state_key(head_sha, failures), None)
        return
    prefix = _ci_repair_conflict_rollback_pending_key_prefix()
    for key in [k for k in state.threads_addressed_ids if k.startswith(prefix)]:
        state.threads_addressed_ids.pop(key, None)


def ci_repair_conflict_rollback_pending(
    state: MonitorState,
    *,
    head_sha: str,
    failures: tuple[CheckFailure, ...],
) -> CiRepairConflictRollbackPending | None:
    """Return the pending undo payload for this head/signature, if any."""
    raw = state.threads_addressed_ids.get(_pending_state_key(head_sha, failures))
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    conflict_reason = payload.get("conflict_reason")
    restore_ref = payload.get("restore_ref")
    leftover_message = payload.get("leftover_message")
    if not isinstance(conflict_reason, str) or not conflict_reason.strip():
        return None
    if not isinstance(restore_ref, str) or not restore_ref.strip():
        return None
    if not isinstance(leftover_message, str) or not leftover_message.strip():
        return None
    published = payload.get("published_remote_head")
    start_push = payload.get("operation_start_last_push_sha")
    cause = payload.get("cause")
    return CiRepairConflictRollbackPending(
        conflict_reason=conflict_reason,
        restore_ref=restore_ref,
        published_remote_head=published if isinstance(published, str) else None,
        operation_start_last_push_sha=(start_push if isinstance(start_push, str) else None),
        leftover_message=leftover_message,
        cause=cause if isinstance(cause, str) else None,
    )


def parked_conflict_rollback_pending_result(
    *,
    pending: CiRepairConflictRollbackPending,
    conflict_reason: str,
    details: dict[str, Any] | None = None,
) -> _GitPushResult:
    """Build the parked_needs_human envelope for a failed conflict undo."""
    message = f"CI repair operator-decision conflict; rollback pending: {pending.leftover_message}"
    result_details: dict[str, Any] = {
        "phase": "ci_repair_operator_decision_conflict",
        "operation_type": "ci_repair",
        "pushed": False,
        "conflict_reason": conflict_reason,
        "rollback_pending": {
            "leftover_message": pending.leftover_message,
            "cause": pending.cause,
            "restore_ref": pending.restore_ref,
            "published_remote_head": pending.published_remote_head,
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
        pending = CiRepairConflictRollbackPending(
            conflict_reason=conflict_reason,
            restore_ref=restore_ref,
            published_remote_head=published_hosted_head,
            operation_start_last_push_sha=operation_start_last_push_sha,
            leftover_message=leftover,
            cause=rollback_result.cause,
        )
        if conflict_head:
            mark_ci_repair_conflict_rollback_pending(
                state,
                head_sha=conflict_head,
                failures=failures,
                pending=pending,
            )
        _log.warning(
            "monitor.ci_fix_operator_decision_conflict_rollback_pending",
            workspace_id=workspace_id,
            reason_code=CI_REPAIR_CONFLICT_ROLLBACK_PENDING,
            rollback_cause=rollback_result.cause,
            leftover_message=leftover,
        )
        return parked_conflict_rollback_pending_result(
            pending=pending,
            conflict_reason=conflict_reason,
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


async def retry_ci_repair_conflict_rollback_pending(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    state: MonitorState,
    status: PRStatus,
) -> NotifyHuman | None:
    """Retry a pending conflict undo; promote to normal conflict park on success.

    Returns an updated ``NotifyHuman`` when the park still applies (pending or
    promoted conflict). Returns ``None`` when there is no pending park for this
    head/signature.
    """
    from awf.runtime.pr_monitor import NotifyHuman
    from awf.runtime.pr_monitor_runner import ci_ops

    pending = ci_repair_conflict_rollback_pending(
        state,
        head_sha=status.head_sha,
        failures=status.ci_failures,
    )
    if pending is None:
        return None

    _log.info(
        "monitor.ci_fix_operator_decision_conflict_rollback_retry",
        workspace_id=workspace_id,
        reason_code=CI_REPAIR_CONFLICT_ROLLBACK_PENDING,
        rollback_cause=pending.cause,
        leftover_message=pending.leftover_message,
        restore_ref=pending.restore_ref,
        published_remote_head=pending.published_remote_head,
    )

    rollback_result = await ci_ops._rollback_ci_fix_residue_before_provider_recovery(
        self,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        restore_ref=pending.restore_ref,
    )
    if rollback_result.ok and pending.published_remote_head and pending.restore_ref:
        rollback_result = await ci_ops._rewind_hosted_ci_repair_conflict_remote(
            self,
            workspace_id=workspace_id,
            state=state,
            restore_ref=pending.restore_ref,
            published_remote_head=pending.published_remote_head,
            operation_start_last_push_sha=pending.operation_start_last_push_sha,
        )

    if not rollback_result.ok:
        leftover = conflict_rollback_pending_leftover_message(
            cause=rollback_result.cause,
            published_remote_head=pending.published_remote_head,
            restore_ref=pending.restore_ref,
        )
        refreshed = CiRepairConflictRollbackPending(
            conflict_reason=pending.conflict_reason,
            restore_ref=pending.restore_ref,
            published_remote_head=pending.published_remote_head,
            operation_start_last_push_sha=pending.operation_start_last_push_sha,
            leftover_message=leftover,
            cause=rollback_result.cause,
        )
        mark_ci_repair_conflict_rollback_pending(
            state,
            head_sha=status.head_sha,
            failures=status.ci_failures,
            pending=refreshed,
        )
        _log.warning(
            "monitor.ci_fix_operator_decision_conflict_rollback_pending",
            workspace_id=workspace_id,
            reason_code=CI_REPAIR_CONFLICT_ROLLBACK_PENDING,
            rollback_cause=rollback_result.cause,
            leftover_message=leftover,
        )
        return NotifyHuman(
            message=(f"CI repair operator-decision conflict; rollback pending: {leftover}"),
            reason_code=CI_REPAIR_CONFLICT_ROLLBACK_PENDING,
        )

    clear_ci_repair_conflict_rollback_pending(
        state,
        head_sha=status.head_sha,
        failures=status.ci_failures,
    )
    mark_ci_repair_operator_decision_conflict(
        state,
        head_sha=status.head_sha,
        failures=status.ci_failures,
        reason=pending.conflict_reason,
    )
    _log.info(
        "monitor.ci_fix_operator_decision_conflict_rollback_succeeded",
        workspace_id=workspace_id,
        reason_code=CI_REPAIR_OPERATOR_DECISION_CONFLICT,
        restore_ref=pending.restore_ref,
    )
    return NotifyHuman(
        message=pending.conflict_reason,
        reason_code=CI_REPAIR_OPERATOR_DECISION_CONFLICT,
    )
