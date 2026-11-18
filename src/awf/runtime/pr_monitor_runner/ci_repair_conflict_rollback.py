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
    # Original failing-check signature for the conflict park after promote.
    # Live ``status.ci_failures`` on a later poll may describe the published
    # repair head (new run ids / different checks) and must not re-key the park
    # under restore_ref (PRRT_kwDOSJAM6s64y_gv).
    park_failures: tuple[CheckFailure, ...] = ()


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
    """Human-facing park message, including the cancel/re-adopt recovery path."""
    return (
        f"CI repair operator-decision conflict; rollback pending: {leftover_message}. "
        "If retries keep failing, cancel and re-adopt for a clean worktree."
    )


def _conflict_failure_identity(failure: CheckFailure) -> tuple[str, str, str]:
    return (failure.run_id or "", failure.name, failure.conclusion)


def _serialize_park_failures(failures: tuple[CheckFailure, ...]) -> list[dict[str, str | None]]:
    """Persist identity fields needed to re-key the conflict park on promote."""
    return [
        {
            "name": failure.name,
            "conclusion": failure.conclusion,
            "log_excerpt": failure.log_excerpt,
            "run_id": failure.run_id,
        }
        for failure in failures
    ]


def _parse_park_failures(raw: object) -> tuple[CheckFailure, ...]:
    """Reconstruct park failures from a persisted pending payload."""
    from awf.runtime.pr_monitor import CheckFailure as _CheckFailure

    if not isinstance(raw, list):
        return ()
    parsed: list[CheckFailure] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        conclusion = item.get("conclusion")
        log_excerpt = item.get("log_excerpt")
        if not isinstance(name, str) or not isinstance(conclusion, str):
            continue
        if not isinstance(log_excerpt, str):
            log_excerpt = ""
        run_id = item.get("run_id")
        parsed.append(
            _CheckFailure(
                name=name,
                conclusion=conclusion,
                log_excerpt=log_excerpt,
                run_id=run_id if isinstance(run_id, str) else None,
            )
        )
    return tuple(parsed)


def conflict_park_lookup_head(
    *,
    status_head_sha: str,
    published_remote_head: str | None,
) -> str:
    """Return the head SHA next poll will observe for park lookup.

    When a hosted rewind fails, the remote still carries the published repair
    SHA while the repair-cycle ``status.head_sha`` remains the pre-repair
    snapshot. Park under the published SHA so the next poll (and retry) finds
    the pending entry instead of ReportCiFailure / merging the unre wound
    conflict (PRRT_kwDOSJAM6s64yp29).
    """
    published = (published_remote_head or "").strip()
    if published:
        return published
    return status_head_sha


def live_head_matches_pending_rollback(
    pending: CiRepairConflictRollbackPending,
    *,
    live_head_sha: str,
) -> bool:
    """True when the live PR head is one of the heads this undo is about.

    Workspace-wide pending lookup still gates agents after unrelated head
    drift, but a hard reset to ``restore_ref`` is only safe when the live head
    is ``restore_ref`` or ``published_remote_head``. Resetting on a third SHA
    can clear the pending gate, park the conflict under ``restore_ref``, and
    leave the next poll on the drifted head free to launch agents
    (PRRT_kwDOSJAM6s64zKdm).
    """
    live = (live_head_sha or "").strip().lower()
    if not live:
        return False
    allowed: set[str] = set()
    restore = (pending.restore_ref or "").strip().lower()
    if restore:
        allowed.add(restore)
    published = (pending.published_remote_head or "").strip().lower()
    if published:
        allowed.add(published)
    return live in allowed


def _pending_state_key(head_sha: str) -> str:
    """Build the pending-undo key for one PR head (CI signature independent).

    Unfinished rollbacks must stay reachable after the live head's failing-check
    signature drifts (new run ids on the published repair, empty while checks
    restart, or green). Key by head only; ``failures`` is ignored on lookup
    (PRRT_kwDOSJAM6s64yqJ9).
    """
    return f"{_ci_repair_conflict_rollback_pending_key_prefix()}{head_sha}"


def _legacy_pending_state_key(head_sha: str, failures: tuple[CheckFailure, ...]) -> str:
    """Pre-signature-independent key retained so in-flight parks still resolve."""
    signature = json.dumps(
        sorted(_conflict_failure_identity(failure) for failure in failures),
        separators=(",", ":"),
    )
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:12]
    return f"{_ci_repair_conflict_rollback_pending_key_prefix()}{head_sha}:{digest}"


def _parse_pending_payload(raw: object) -> CiRepairConflictRollbackPending | None:
    if isinstance(raw, dict):
        payload: object = raw
    elif isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    else:
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
        park_failures=_parse_park_failures(payload.get("park_failures")),
    )


def _pending_keys_for_head(state: MonitorState, head_sha: str) -> list[str]:
    """Return stored pending keys for ``head_sha`` (head-only and legacy digest)."""
    prefix = _ci_repair_conflict_rollback_pending_key_prefix()
    head_prefix = f"{prefix}{head_sha}"
    keys: list[str] = []
    for key in state.threads_addressed_ids:
        if key == head_prefix or key.startswith(f"{head_prefix}:"):
            keys.append(key)
    return keys


def mark_ci_repair_conflict_rollback_pending(
    state: MonitorState,
    *,
    head_sha: str,
    failures: tuple[CheckFailure, ...],
    pending: CiRepairConflictRollbackPending,
) -> None:
    """Persist a pending conflict undo for this head (CI signature independent)."""
    del failures  # Kept for call-site compatibility; keying ignores CI signature.
    park_failures = pending.park_failures
    payload = {
        "conflict_reason": pending.conflict_reason,
        "restore_ref": pending.restore_ref,
        "published_remote_head": pending.published_remote_head,
        "operation_start_last_push_sha": pending.operation_start_last_push_sha,
        "leftover_message": pending.leftover_message,
        "cause": pending.cause,
        "park_failures": _serialize_park_failures(park_failures),
    }
    # Drop any legacy head:digest keys for this head so lookup stays unambiguous.
    for key in _pending_keys_for_head(state, head_sha):
        state.threads_addressed_ids.pop(key, None)
    state.mark_addressed(
        _pending_state_key(head_sha),
        json.dumps(payload, separators=(",", ":")),
    )


def clear_ci_repair_conflict_rollback_pending(
    state: MonitorState,
    *,
    head_sha: str | None = None,
    failures: tuple[CheckFailure, ...] | None = None,
) -> None:
    """Drop one pending park (by head) or every pending park when head is omitted."""
    if head_sha is not None:
        for key in _pending_keys_for_head(state, head_sha):
            state.threads_addressed_ids.pop(key, None)
        if failures is not None:
            # Also drop the exact legacy key in case head-prefix scan missed it.
            state.threads_addressed_ids.pop(_legacy_pending_state_key(head_sha, failures), None)
        return
    prefix = _ci_repair_conflict_rollback_pending_key_prefix()
    for key in [k for k in state.threads_addressed_ids if k.startswith(prefix)]:
        state.threads_addressed_ids.pop(key, None)


def ci_repair_conflict_rollback_pending(
    state: MonitorState,
    *,
    head_sha: str = "",
    failures: tuple[CheckFailure, ...] = (),
) -> CiRepairConflictRollbackPending | None:
    """Return any unfinished conflict-rollback park for this workspace.

    Lookup is by reserved prefix only: head SHA and failing-check signature can
    both change while leftovers remain (hosted: the published head *is* the next
    head). Matching any pending park keeps retries and agent gates reachable.
    ``head_sha`` / ``failures`` are kept for call-site compatibility only.
    """
    del head_sha, failures
    prefix = _ci_repair_conflict_rollback_pending_key_prefix()
    for key, raw in list(state.threads_addressed_ids.items()):
        if not key.startswith(prefix):
            continue
        pending = _parse_pending_payload(raw)
        if pending is not None:
            return pending
    return None


def notify_human_for_conflict_rollback_pending(
    state: MonitorState,
    *,
    head_sha: str = "",
    failures: tuple[CheckFailure, ...] = (),
) -> NotifyHuman | None:
    """Return NotifyHuman when any unfinished conflict rollback park remains."""
    from awf.runtime.pr_monitor import NotifyHuman

    pending = ci_repair_conflict_rollback_pending(
        state,
        head_sha=head_sha,
        failures=failures,
    )
    if pending is None:
        return None
    return NotifyHuman(
        message=conflict_rollback_pending_human_message(leftover_message=pending.leftover_message),
        reason_code=CI_REPAIR_CONFLICT_ROLLBACK_PENDING,
    )


def parked_conflict_rollback_pending_result(
    *,
    pending: CiRepairConflictRollbackPending,
    conflict_reason: str,
    details: dict[str, Any] | None = None,
) -> _GitPushResult:
    """Build the parked_needs_human envelope for a failed conflict undo."""
    message = conflict_rollback_pending_human_message(leftover_message=pending.leftover_message)
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
            park_failures=failures,
        )
        park_head = conflict_park_lookup_head(
            status_head_sha=conflict_head,
            published_remote_head=published_hosted_head,
        )
        if park_head:
            mark_ci_repair_conflict_rollback_pending(
                state,
                head_sha=park_head,
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


async def divert_work_slot_for_conflict_rollback_pending(
    self: Any,
    *,
    action: object,
    work_slot_action_types: tuple[type, ...],
    workspace_id: str,
    worktree_path: Path,
    state: MonitorState,
    status: PRStatus,
) -> NotifyHuman | None:
    """Retry unfinished conflict undo before any agent-launching monitor action.

    While any pending park exists for this workspace, work-slot actions
    (AddressComments, AddressOperatorHint, ReportCiFailure, SyncBase) must not
    launch an agent. Retry the rollback first; if it still fails (or promotes to
    the normal conflict park), return the NotifyHuman the caller should divert
    to. Returns ``None`` when there is no pending park.
    """
    if not isinstance(action, work_slot_action_types):
        return None
    if ci_repair_conflict_rollback_pending(state) is None:
        return None
    return await retry_ci_repair_conflict_rollback_pending(
        self,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        state=state,
        status=status,
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
    promoted conflict). Returns ``None`` when there is no pending park.
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

    # Gate stays up for any pending park, but destructive undo only runs when
    # the live head is still restore_ref or the unre wound published repair.
    if not live_head_matches_pending_rollback(pending, live_head_sha=status.head_sha):
        _log.warning(
            "monitor.ci_fix_operator_decision_conflict_rollback_deferred_unrelated_head",
            workspace_id=workspace_id,
            reason_code=CI_REPAIR_CONFLICT_ROLLBACK_PENDING,
            live_head_sha=status.head_sha,
            restore_ref=pending.restore_ref,
            published_remote_head=pending.published_remote_head,
            leftover_message=pending.leftover_message,
        )
        return NotifyHuman(
            message=conflict_rollback_pending_human_message(
                leftover_message=pending.leftover_message
            ),
            reason_code=CI_REPAIR_CONFLICT_ROLLBACK_PENDING,
        )

    # Always re-key under the live head so retries stay reachable if CI drifts.
    park_head = (
        conflict_park_lookup_head(
            status_head_sha=status.head_sha,
            published_remote_head=pending.published_remote_head,
        )
        or status.head_sha
    )

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
            park_failures=pending.park_failures,
        )
        # Clear every unfinished park, then rewrite under the live head.
        clear_ci_repair_conflict_rollback_pending(state)
        mark_ci_repair_conflict_rollback_pending(
            state,
            head_sha=park_head,
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
            message=conflict_rollback_pending_human_message(leftover_message=leftover),
            reason_code=CI_REPAIR_CONFLICT_ROLLBACK_PENDING,
        )

    clear_ci_repair_conflict_rollback_pending(state)
    # Remote was rewound to restore_ref; next poll observes that SHA, not the
    # published head still recorded on this poll's status snapshot.
    promote_head = (pending.restore_ref or "").strip() or status.head_sha
    # Prefer the signature captured when the conflict parked — live
    # status.ci_failures may describe the published repair head after drift.
    promote_failures = pending.park_failures or status.ci_failures
    mark_ci_repair_operator_decision_conflict(
        state,
        head_sha=promote_head,
        failures=promote_failures,
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
