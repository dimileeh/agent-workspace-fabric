"""Merge-method attempt primitives for the PR monitor merge loop.

Extracted from ``merge_loop`` so that module stays within the first-party
file-line guardrail (``tests/unit/test_core_decomposition_maintainability.py``).
Behavior is unchanged: the symbols are imported back into ``merge_loop`` and
the public test surface (``test_pr_monitor_merge_method_classifier``) still
imports ``_MergeAttemptOutcome`` / ``_MergeAttemptResult`` from here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from awf.common.bitbucket_client import (
    BITBUCKET_MERGE_IN_PROGRESS,
    BITBUCKET_MERGE_TASK_TIMEOUT,
    BitbucketClientError,
)
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.enums import (
    OperationStatus,
    OperationType,
)
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.monitor_state_keys import _merge_method_blocked_key
from awf.runtime.pr_monitor import (
    MonitorState,
    PRStatus,
)
from awf.runtime.pr_monitor_runner.constants import (
    _AUDIT_MERGE_ATTEMPT_EVENT,
    _AUDIT_MERGE_RESULT_EVENT,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _redact_and_truncate_forge_error,
)
from awf.runtime.pr_monitor_runner.merge_methods import (
    _MERGE_METHOD_MISMATCH_REASON,
    _merge_completion_marker,
    _merge_method_mismatch_message,
    _merge_method_rejection_method,
)

# Bitbucket reason codes whose merge attempt is still in flight server-side rather
# than a deterministic failure: the 409 already-in-progress signal and the exhausted
# async-merge poll budget. Both are cancelled (never failed) so a later
# ``fetch_pr_status`` observing the eventual MERGED state can complete the workspace
# without contradicting a permanently-failed operation or a spurious notification.
_BITBUCKET_IN_FLIGHT_MERGE_REASON_CODES = frozenset(
    {BITBUCKET_MERGE_IN_PROGRESS, BITBUCKET_MERGE_TASK_TIMEOUT}
)


class _MergeAttemptOutcome(StrEnum):
    """Terminal categories for one merge-method attempt."""

    SUCCESS = "success"
    RETRY_NEXT_METHOD = "retry_next_method"
    METHOD_BLOCKER = "method_blocker"
    BLOCKER = "blocker"


@dataclass(frozen=True)
class _MergeAttemptResult:
    """Structured result from trying one explicit merge method."""

    outcome: _MergeAttemptOutcome
    merge_sha: str | None = None
    blocker: GitHubClientError | BitbucketClientError | None = None
    notification_reason: str | None = None

    def __post_init__(self) -> None:
        """Enforce the state value required to suppress repeat merge attempts."""
        if self.outcome is _MergeAttemptOutcome.METHOD_BLOCKER and not self.notification_reason:
            raise ValueError("method-blocker merge attempt result requires a notification reason")

    @property
    def method_blocker_notification_reason(self) -> str:
        """Return the non-empty reason required for METHOD_BLOCKER outcomes."""
        if self.outcome is not _MergeAttemptOutcome.METHOD_BLOCKER:
            raise RuntimeError("merge attempt result is not a method blocker")
        if not self.notification_reason:  # pragma: no cover
            raise RuntimeError("method-blocker merge attempt result has no reason")
        return self.notification_reason


async def _record_empty_effective_merge_methods_blocker(
    self: Any,
    *,
    workspace_id: str,
    pr_number: int,
    merge_status: PRStatus,
    base_branch: str,
    remote_branch: str,
    monitor_log: WorkspaceLogSink | None,
    notification_reason: str,
) -> None:
    """Record operator evidence when policy intersection leaves no merge method."""
    merge_operation = await self._begin_monitor_state_operation(
        workspace_id=workspace_id,
        action="merge",
        requested_action="merge",
        reason=notification_reason,
        reason_code=_MERGE_METHOD_MISMATCH_REASON,
        pr_number=pr_number,
        status=merge_status,
        base_branch=base_branch,
        remote_branch=remote_branch,
        monitor_log=monitor_log,
    )
    operation_id = merge_operation.operation_id if merge_operation is not None else None
    await self._record_pr_monitor_audit_event(
        workspace_id=workspace_id,
        event_type=_AUDIT_MERGE_ATTEMPT_EVENT,
        action="merge",
        outcome="blocked",
        reason_code=_MERGE_METHOD_MISMATCH_REASON,
        pr_number=pr_number,
        status=merge_status,
        base_branch=base_branch,
        remote_branch=remote_branch,
        operation_id=operation_id,
        operation_type=OperationType.monitor_state.value,
        monitor_log=monitor_log,
        evidence={
            "operation": "resolve_effective_merge_methods",
            "effective_methods": [],
        },
    )
    await self._finish_monitor_operation(
        merge_operation,
        status=OperationStatus.failed,
        result={
            "status": "failed",
            "outcome": "merge_method_mismatch",
            "reason_code": _MERGE_METHOD_MISMATCH_REASON,
            "effective_methods": [],
        },
        error_code=_MERGE_METHOD_MISMATCH_REASON,
        error_message=notification_reason,
    )


async def _attempt_merge_method(
    self: Any,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    merge_method: str,
    merge_status: PRStatus,
    base_branch: str,
    remote_branch: str,
    monitor_log: WorkspaceLogSink | None,
    state: MonitorState,
    effective_methods: tuple[str, ...],
    attempt_index: int,
) -> _MergeAttemptResult:
    """Try one explicit merge method and classify the result for the merge loop."""
    merge_operation = await self._begin_monitor_state_operation(
        workspace_id=workspace_id,
        action="merge",
        requested_action="merge",
        reason="Merging PR after all monitor gates passed.",
        reason_code="MERGE",
        pr_number=pr_number,
        status=merge_status,
        base_branch=base_branch,
        remote_branch=remote_branch,
        monitor_log=monitor_log,
    )
    operation_id = merge_operation.operation_id if merge_operation is not None else None
    await self._record_pr_monitor_audit_event(
        workspace_id=workspace_id,
        event_type=_AUDIT_MERGE_ATTEMPT_EVENT,
        action="merge",
        outcome="attempted",
        reason_code="MERGE",
        pr_number=pr_number,
        status=merge_status,
        base_branch=base_branch,
        remote_branch=remote_branch,
        operation_id=operation_id,
        operation_type=OperationType.monitor_state.value,
        monitor_log=monitor_log,
    )
    try:
        merge_sha = await self._deps.gh.merge_pr(
            repo=repo,
            pr_number=pr_number,
            method=merge_method,
        )
    except GitHubClientError as exc:
        rejected_method = _merge_method_rejection_method(exc)
        is_confirmed_method_rejection = rejected_method == merge_method
        await self._finish_monitor_operation(
            merge_operation,
            status=OperationStatus.failed,
            result={
                "status": "failed",
                "outcome": "github_merge_failed",
                "reason_code": "GITHUB_MERGE_FAILED",
            },
            error_code="GITHUB_MERGE_FAILED",
            error_message=str(exc),
        )
        await self._record_pr_monitor_audit_event(
            workspace_id=workspace_id,
            event_type=_AUDIT_MERGE_RESULT_EVENT,
            action="merge",
            outcome="failed",
            reason_code="GITHUB_MERGE_FAILED",
            pr_number=pr_number,
            status=merge_status,
            base_branch=base_branch,
            remote_branch=remote_branch,
            operation_id=operation_id,
            operation_type=OperationType.monitor_state.value,
            monitor_log=monitor_log,
            evidence={
                "operation": "merge_pr",
                "merge_method": merge_method,
                "error_message": str(exc),
            },
        )
        has_remaining_alternative = attempt_index < len(effective_methods) - 1
        if has_remaining_alternative and is_confirmed_method_rejection:
            return _MergeAttemptResult(_MergeAttemptOutcome.RETRY_NEXT_METHOD)
        if is_confirmed_method_rejection:
            notification_reason = _merge_method_mismatch_message(
                base_branch=base_branch,
                attempted_method=merge_method,
                effective_methods=effective_methods,
                detail=" ".join(_redact_and_truncate_forge_error(exc.stderr).split())[:240] or None,
            )
            state.mark_addressed(
                _merge_method_blocked_key(
                    pr_number=merge_status.number,
                    head_sha=merge_status.head_sha,
                ),
                notification_reason,
            )
            await self._persist_state(workspace_id, state)
            return _MergeAttemptResult(
                _MergeAttemptOutcome.METHOD_BLOCKER,
                notification_reason=notification_reason,
            )
        return _MergeAttemptResult(_MergeAttemptOutcome.BLOCKER, blocker=exc)
    except BitbucketClientError as exc:
        # Bitbucket workspaces merge through ``BitbucketClient.merge_pr``, which
        # raises ``BitbucketClientError`` (not ``GitHubClientError``) on a
        # deterministic merge failure — branch restrictions, unresolved tasks,
        # missing approvals, or a permanent 4xx. Without this arm the error
        # escapes ``_attempt_merge_method`` and terminates the workspace at the
        # runner's outer catch, whereas the GitHub arm classifies the same shape
        # of failure as a merge blocker that notifies a human and keeps polling.
        # Mirror that: finish the operation as failed, record the audit event,
        # and return a BLOCKER so the merge-blocker arm waits on transient blips
        # and otherwise notifies-and-keeps-polling. Bitbucket carries no
        # per-method rejection signal to retry alternative methods against, so
        # every fault is a generic blocker.
        #
        # The in-flight reason codes (``BITBUCKET_MERGE_IN_PROGRESS`` — the 409
        # raised when a prior async merge is already running — and
        # ``BITBUCKET_MERGE_TASK_TIMEOUT`` — the async-merge poll budget exhausted
        # while the task was still PENDING) are the exception: both are transient,
        # so the merge-blocker arm's ``_wait_after_transient_bitbucket_error`` keeps
        # the monitor polling and ``fetch_pr_status`` later observes the original
        # merge's MERGED state, terminating the workspace *successfully*. Recording
        # a permanent ``failed`` operation here would leave an inconsistent audit
        # trail (operation "merge failed" vs. workspace "completed"), so for these
        # reason codes we do not fail the operation. We must still drive it to a
        # terminal state, though: ``_attempt_merge_method`` already created a
        # *running* monitor-state operation for this attempt, and if the in-flight
        # merge completes before the next loop re-enters ``Merge`` the monitor
        # takes ``ShortCircuitCompleted`` — which records its own separate
        # operation and never finishes this one. Leaving it ``running`` would
        # orphan it indefinitely and pollute active-operation/recovery state.
        # Cancel it instead (neither failed nor succeeded — this attempt was
        # superseded by the still-running merge) so it is terminal without
        # contradicting the eventual completion. Unlike GitHub, Bitbucket has a
        # transient blocker that later succeeds, so this case is unique to it.
        if exc.reason_code not in _BITBUCKET_IN_FLIGHT_MERGE_REASON_CODES:
            # Forward ``exc.reason_code`` as the primary audit field rather than a
            # flat ``BITBUCKET_MERGE_FAILED`` so the specific diagnostic code
            # (e.g. ``BITBUCKET_MERGE_METHOD_UNSUPPORTED`` from an unmappable merge
            # method) survives end-to-end in the operation record and audit event.
            # An operator inspecting events sees the real cause without parsing the
            # prose ``error_message``.
            await self._finish_monitor_operation(
                merge_operation,
                status=OperationStatus.failed,
                result={
                    "status": "failed",
                    "outcome": "bitbucket_merge_failed",
                    "reason_code": exc.reason_code,
                },
                error_code=exc.reason_code,
                error_message=str(exc),
            )
            await self._record_pr_monitor_audit_event(
                workspace_id=workspace_id,
                event_type=_AUDIT_MERGE_RESULT_EVENT,
                action="merge",
                outcome="failed",
                reason_code=exc.reason_code,
                pr_number=pr_number,
                status=merge_status,
                base_branch=base_branch,
                remote_branch=remote_branch,
                operation_id=operation_id,
                operation_type=OperationType.monitor_state.value,
                monitor_log=monitor_log,
                evidence={
                    "operation": "merge_pr",
                    "merge_method": merge_method,
                    "error_message": str(exc),
                },
            )
        else:
            await self._finish_monitor_operation(
                merge_operation,
                status=OperationStatus.cancelled,
                result={
                    "status": "cancelled",
                    "outcome": "bitbucket_merge_in_progress",
                    "reason_code": exc.reason_code,
                },
            )
            # Record an audit breadcrumb for the cancellation. Every other merge
            # arm — GitHub failure, Bitbucket deterministic failure, success —
            # emits a ``merge_result`` event; without one here a long-running
            # async merge that spans several poll cycles produces a chain of
            # silently cancelled operations, leaving operators unable to tell
            # "superseded by a still-running merge" from an unexplained
            # cancellation. The ``cancelled`` outcome + the in-flight reason code
            # (``BITBUCKET_MERGE_IN_PROGRESS`` or ``BITBUCKET_MERGE_TASK_TIMEOUT``)
            # keep that distinction in the audit trail.
            await self._record_pr_monitor_audit_event(
                workspace_id=workspace_id,
                event_type=_AUDIT_MERGE_RESULT_EVENT,
                action="merge",
                outcome="cancelled",
                reason_code=exc.reason_code,
                pr_number=pr_number,
                status=merge_status,
                base_branch=base_branch,
                remote_branch=remote_branch,
                operation_id=operation_id,
                operation_type=OperationType.monitor_state.value,
                monitor_log=monitor_log,
                evidence={
                    "operation": "merge_pr",
                    "merge_method": merge_method,
                    "error_message": str(exc),
                },
            )
        return _MergeAttemptResult(_MergeAttemptOutcome.BLOCKER, blocker=exc)

    merge_marker = _merge_completion_marker(
        merge_sha=merge_sha,
        head_sha=merge_status.head_sha,
    )
    await self._finish_monitor_operation(
        merge_operation,
        status=OperationStatus.succeeded,
        result={
            "status": "succeeded",
            "outcome": "merged",
            "merge_sha": merge_marker,
        },
    )
    await self._record_pr_monitor_audit_event(
        workspace_id=workspace_id,
        event_type=_AUDIT_MERGE_RESULT_EVENT,
        action="merge",
        outcome="succeeded",
        reason_code="MERGE",
        pr_number=pr_number,
        status=merge_status,
        base_branch=base_branch,
        remote_branch=remote_branch,
        operation_id=operation_id,
        operation_type=OperationType.monitor_state.value,
        monitor_log=monitor_log,
        evidence={
            "merge_sha": merge_marker,
        },
    )
    return _MergeAttemptResult(_MergeAttemptOutcome.SUCCESS, merge_sha=merge_marker)
