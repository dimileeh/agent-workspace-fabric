"""Pull request monitor decision loop.

Mechanically extracted from the original orchestrator; behavior is unchanged.
"""

from __future__ import annotations

import time
from dataclasses import (
    replace,
)
from pathlib import Path
from typing import Any, cast

from awf.common.compose_exec import (
    EXEC_PROCESS_CLEANUP_FAILED,
    ComposeExecCleanupError,
    cleanup_failure_message,
)
from awf.common.github_client import (
    GitHubClientError,
    RepoRef,
)
from awf.db.enums import (
    OperationStatus,
    OperationType,
)
from awf.db.repositories import WorkspaceEventCreate
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.pr_monitor import (
    Abort,
    AddressComments,
    AddressOperatorHint,
    MonitorAction,
    MonitorState,
    NotifyHuman,
    PRStatus,
    ReportCiFailure,
    RerunTransientCI,
    ShortCircuitCompleted,
    SyncBase,
    WaitForCI,
    _ci_transient_rerun_count,
    _ci_transient_rerun_state_key,
)
from awf.runtime.pr_monitor_runner import merge_loop as _merge_loop
from awf.runtime.pr_monitor_runner.constants import (
    _AUDIT_GIT_PUSH_EVENT,
    _CI_TRANSIENT_RERUN_FAILED_REASON,
    _CI_TRANSIENT_RERUN_REASON,
)
from awf.runtime.pr_monitor_runner.gates import (
    _NonCheckReviewerSettleDecision,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _ci_failure_payload,
    _ci_transient_rerun_attempt,
    _clear_transient_base_fetch_retry_state,
    _gate_requires_validation_recovery,
    _is_manual_ready_handoff,
    _non_check_reviewer_settle_decision,
    _non_check_reviewer_settle_wait_operation_context,
    _notify_human_reason,
    _pending_review_feedback_count,
    _redact_and_truncate_github_error,
)
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.remote_ops import (
    _git_push_failure_outcome,
)
from awf.runtime.pr_monitor_runner.types import (
    BaseFetchError,
    ProviderRecoveryAuthError,
    ProviderRecoveryFallbackError,
    ProviderRecoveryRetryError,
)


async def _execute(
    self: Any,
    *,
    action: MonitorAction,
    workspace_id: str,
    repo_url: str,
    repo: RepoRef,
    pr_number: int,
    status: PRStatus,
    state: MonitorState,
    base_branch: str,
    remote_branch: str,
    compose_project: str,
    compose_file: Path,
    monitor_log: WorkspaceLogSink | None,
    remote_push_url: str | None = None,
) -> bool:
    """Execute one monitor action.

    Returns True iff the monitor reached a terminal state.
    """
    # One structured log line per iteration, BEFORE any side effect —
    # operators grepping logs need to see which arm the decision
    # core chose without having to correlate gh / git calls
    # downstream. Regression guard for PR 342: the monitor ran 200+
    # iterations silently because only handoff_to_pr_monitor and
    # compose_teardown_ok fired.
    review_feedback = len(status.unresolved_review_comments)
    pending_review_feedback = _pending_review_feedback_count(status, state)
    unresolved_reviews = pending_review_feedback
    _log.info(
        "monitor.action",
        workspace_id=workspace_id,
        pr_number=pr_number,
        iter=state.iter_count,
        action=type(action).__name__,
        head_sha=status.head_sha[:10],
        base_behind=status.base_behind_count,
        merge_state=(status.merge_state_status.value if status.merge_state_status else None),
        unresolved_threads=len(status.unresolved_inline_threads),
        # Keep the historical field name, but report only review feedback
        # still needing attention. The raw retained inbox count lives in
        # ``review_feedback``.
        unresolved_reviews=unresolved_reviews,
        review_feedback=review_feedback,
        pending_review_feedback=pending_review_feedback,
        blocking_reviews=len(status.blocking_reviews),
    )
    await self._write_monitor_log(
        monitor_log,
        {
            "event": "monitor.action",
            "workspace_id": workspace_id,
            "pr_number": pr_number,
            "iter": state.iter_count,
            "action": type(action).__name__,
            "head_sha": status.head_sha,
            "base_behind": status.base_behind_count,
            "merge_state": (status.merge_state_status.value if status.merge_state_status else None),
            "unresolved_threads": len(status.unresolved_inline_threads),
            "unresolved_reviews": unresolved_reviews,
            "review_feedback": review_feedback,
            "pending_review_feedback": pending_review_feedback,
            "blocking_reviews": len(status.blocking_reviews),
        },
    )

    if isinstance(action, ShortCircuitCompleted):
        self._write_defer_signal(
            workspace_id=workspace_id,
            pr_number=pr_number,
            terminal_action="ShortCircuitCompleted",
            merged=True,
            status=status,
            state=state,
        )
        await self._record_monitor_state_operation(
            workspace_id=workspace_id,
            action="completed",
            requested_action="complete",
            reason="PR was already completed upstream.",
            reason_code="SHORT_CIRCUIT_COMPLETED",
            pr_number=pr_number,
            status=status,
            base_branch=base_branch,
            remote_branch=remote_branch,
            result={"status": "succeeded", "outcome": "already_completed"},
            monitor_log=monitor_log,
        )
        await self._terminate_completed(
            workspace_id,
            pr_merge_sha=status.merge_commit_sha or status.head_sha,
            repo_url=repo_url,
            base_branch=base_branch,
            compose_project=compose_project,
            compose_file=compose_file,
        )
        return True

    if isinstance(action, Abort):
        self._write_defer_signal(
            workspace_id=workspace_id,
            pr_number=pr_number,
            terminal_action="Abort",
            merged=False,
            status=status,
            state=state,
        )
        await self._terminate_failed(
            workspace_id,
            message=f"monitor: abort ({action.reason.value})",
            reason_code=action.reason,
        )
        return True

    if isinstance(action, WaitForCI):
        emitted_stale_warning = await self._record_stale_pending_check_warnings(
            workspace_id=workspace_id,
            status=status,
            state=state,
            monitor_log=monitor_log,
        )
        if emitted_stale_warning:
            await self._persist_state(workspace_id, state)
        await self._sleep_with_monitor_state_operation(
            workspace_id=workspace_id,
            action="check_wait",
            requested_action="wait_for_ci",
            reason=(
                "CI checks are still pending."
                if action.reason == "pending_checks"
                else "GitHub has not reported a stable mergeable state."
            ),
            reason_code="CHECK_WAIT",
            pr_number=pr_number,
            status=status,
            base_branch=base_branch,
            remote_branch=remote_branch,
            wait_seconds=self._config.poll_interval_seconds,
            monitor_log=monitor_log,
            extra_payload={
                "wait_reason": action.reason,
                "check_state": status.check_state.value,
                "merge_state": (
                    status.merge_state_status.value if status.merge_state_status else None
                ),
            },
            extra_identity=(action.reason,),
        )
        return False

    if isinstance(action, SyncBase):
        operation = await self._begin_monitor_operation(
            workspace_id=workspace_id,
            operation_type=OperationType.sync_base,
            action="sync_base",
            requested_action="sync_base",
            reason="PR branch is behind the target branch.",
            reason_code="SYNC_BASE",
            pr_number=pr_number,
            status=status,
            base_branch=base_branch,
            remote_branch=remote_branch,
            monitor_log=monitor_log,
            extra_identity=(state.iter_count,),
        )
        try:
            push_result = await self._run_sync_base(
                workspace_id=workspace_id,
                state=state,
                repo=repo,
                pr_number=pr_number,
                base_branch=base_branch,
                remote_branch=remote_branch,
                remote_push_url=remote_push_url,
                compose_project=compose_project,
                compose_file=compose_file,
            )
            _clear_transient_base_fetch_retry_state(state, context="sync_base")
        except ProviderRecoveryRetryError:
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.failed,
                result={
                    "status": "failed",
                    "outcome": "provider_retry",
                    "reason_code": "PROVIDER_OUTAGE",
                    "pushed": False,
                },
                error_code="PROVIDER_OUTAGE",
                error_message="Provider recovery requested retry",
            )
            raise
        except ProviderRecoveryFallbackError:
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.failed,
                result={
                    "status": "failed",
                    "outcome": "provider_fallback",
                    "reason_code": "PROVIDER_FALLBACK",
                    "pushed": False,
                },
                error_code="PROVIDER_FALLBACK",
                error_message="Provider recovery triggered fallback",
            )
            raise
        except ProviderRecoveryAuthError:
            await self._finish_provider_auth_failed_operation(operation)
            raise
        except BaseFetchError as exc:
            base_fetch_result = await self._wait_after_transient_base_fetch_error(
                exc,
                workspace_id=workspace_id,
                pr_number=pr_number,
                context="sync_base",
                state=state,
                monitor_log=monitor_log,
            )
            if base_fetch_result.retry:
                await self._finish_monitor_operation(
                    operation,
                    status=OperationStatus.failed,
                    result={
                        "status": "retrying",
                        "outcome": "transient_base_fetch_error",
                        "reason_code": base_fetch_result.reason_code,
                        "pushed": False,
                    },
                    error_code=base_fetch_result.reason_code,
                    error_message=str(exc),
                )
                return False
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.failed,
                result={
                    "status": "failed",
                    "outcome": "base_fetch_failed",
                    "reason_code": base_fetch_result.reason_code,
                    "pushed": False,
                },
                error_code=base_fetch_result.reason_code,
                error_message=str(exc),
            )
            await self._terminate_failed(
                workspace_id,
                message=f"monitor: could not refresh base branch: {exc}"[:2000],
                reason_code=base_fetch_result.reason_code,
            )
            return True
        except ComposeExecCleanupError as exc:
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.failed,
                result={
                    "status": "failed",
                    "reason_code": EXEC_PROCESS_CLEANUP_FAILED,
                },
                error_code=EXEC_PROCESS_CLEANUP_FAILED,
                error_message=cleanup_failure_message(exc),
            )
            await self._terminate_failed(
                workspace_id,
                message=cleanup_failure_message(exc),
                reason_code=EXEC_PROCESS_CLEANUP_FAILED,
            )
            return True
        if push_result.failed:
            reason_code = push_result.reason_code
            outcome = _git_push_failure_outcome(push_result)
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.failed,
                result={
                    "status": "failed",
                    "outcome": outcome,
                    "reason_code": reason_code,
                    "pushed": False,
                },
                error_code=reason_code,
                error_message=push_result.error_message,
            )
            await self._record_pr_monitor_audit_event(
                workspace_id=workspace_id,
                event_type=_AUDIT_GIT_PUSH_EVENT,
                action="sync_base_push",
                outcome="failed",
                reason_code=reason_code,
                pr_number=pr_number,
                status=status,
                base_branch=base_branch,
                remote_branch=remote_branch,
                operation_id=operation.operation_id if operation is not None else None,
                operation_type=OperationType.sync_base.value,
                monitor_log=monitor_log,
                evidence=push_result.failure_evidence(),
            )
            if push_result.terminal_monitor_failure:
                await self._terminate_failed(
                    workspace_id,
                    message=push_result.error_message or push_result.reason_code,
                    reason_code=push_result.reason_code,
                )
                return True
            self._record_sync_base_progress(
                state=state,
                status=status,
                push_result=push_result,
            )
            state.iter_count += 1
            return False
        await self._finish_monitor_operation(
            operation,
            status=OperationStatus.succeeded,
            result={
                "status": "succeeded",
                "outcome": "base_synced",
                "pushed": push_result.pushed,
            },
        )
        self._record_sync_base_progress(
            state=state,
            status=status,
            push_result=push_result,
        )
        await self._record_pr_monitor_audit_event(
            workspace_id=workspace_id,
            event_type=_AUDIT_GIT_PUSH_EVENT,
            action="sync_base_push",
            outcome="succeeded",
            reason_code="SYNC_BASE",
            pr_number=pr_number,
            status=status,
            base_branch=base_branch,
            remote_branch=remote_branch,
            operation_id=operation.operation_id if operation is not None else None,
            operation_type=OperationType.sync_base.value,
            monitor_log=monitor_log,
        )
        if push_result.pushed:
            # SyncBase merged ``origin/<base>`` into the workspace branch and
            # pushed. Without this call, the ``STALE_TARGET_ADVANCED`` row
            # the staleness service wrote when target first advanced stays
            # ``status=active, resolved_at=null`` with ``blocks_merge=true``
            # — gating every subsequent merge attempt even though the
            # monitor's own ``base_behind`` check is back to 0. Advance the
            # candidate's validation base to the SHA we just merged in and
            # refresh staleness so the resolution propagates atomically.
            await self._refresh_staleness_after_sync_base(
                workspace_id=workspace_id,
                base_branch=base_branch,
            )
        state.iter_count += 1
        return False

    if isinstance(action, RerunTransientCI):
        attempt = (
            _ci_transient_rerun_count(
                state,
                head_sha=status.head_sha,
                failures=action.failures,
                legacy_failures=status.ci_failures,
            )
            + 1
        )
        run_ids = tuple(
            dict.fromkeys(failure.run_id for failure in action.failures if failure.run_id)
        )
        if not run_ids:
            return cast(
                bool,
                await self._execute(
                    action=ReportCiFailure(failures=action.failures),
                    workspace_id=workspace_id,
                    repo_url=repo_url,
                    repo=repo,
                    pr_number=pr_number,
                    status=status,
                    state=state,
                    base_branch=base_branch,
                    remote_branch=remote_branch,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    monitor_log=monitor_log,
                    remote_push_url=remote_push_url,
                ),
            )
        failures_payload = [_ci_failure_payload(failure) for failure in action.failures]
        rerun_signature = _ci_transient_rerun_state_key(status.head_sha, action.failures)
        operation = await self._begin_monitor_operation(
            workspace_id=workspace_id,
            operation_type=OperationType.ci_repair,
            action="ci_transient_rerun",
            requested_action="rerun_failed_ci",
            reason=(
                "CI failure logs matched transient infrastructure signatures; "
                "rerunning failed GitHub jobs before invoking an agent."
            ),
            reason_code=_CI_TRANSIENT_RERUN_REASON,
            pr_number=pr_number,
            status=status,
            base_branch=base_branch,
            remote_branch=remote_branch,
            monitor_log=monitor_log,
            extra_payload={
                "attempt": attempt,
                "failures": failures_payload,
                "run_ids": list(run_ids),
            },
            extra_identity=(rerun_signature, attempt, *run_ids),
        )
        event_payload: dict[str, object] = {
            "attempt": attempt,
            "failures": failures_payload,
            "run_ids": list(run_ids),
            "head_sha": status.head_sha,
        }
        recorded_attempt = _ci_transient_rerun_attempt(
            state,
            head_sha=status.head_sha,
            failures=action.failures,
            legacy_failures=status.ci_failures,
        )
        event_payload["attempt"] = recorded_attempt
        await self._persist_state(workspace_id, state)
        accepted_run_ids: list[str] = []
        failed_run_id: str | None = None
        try:
            for run_id in run_ids:
                try:
                    await self._deps.gh.rerun_failed_workflow_jobs(repo=repo, run_id=run_id)
                except GitHubClientError:
                    failed_run_id = run_id
                    raise
                accepted_run_ids.append(run_id)
        except GitHubClientError as exc:
            error_message = _redact_and_truncate_github_error(str(exc))
            if accepted_run_ids:
                partial_event_payload = {
                    **event_payload,
                    "accepted_run_ids": accepted_run_ids,
                    "failed_run_id": failed_run_id,
                    "error": error_message,
                }
                await self._finish_monitor_operation(
                    operation,
                    status=OperationStatus.succeeded,
                    result={
                        "status": "succeeded",
                        "outcome": "ci_transient_rerun_partially_requested",
                        "reason_code": _CI_TRANSIENT_RERUN_REASON,
                        **partial_event_payload,
                    },
                )
                await self._write_monitor_log(
                    monitor_log,
                    {
                        "event": "monitor.ci_transient_rerun_partially_requested",
                        "workspace_id": workspace_id,
                        "pr_number": pr_number,
                        "reason_code": _CI_TRANSIENT_RERUN_REASON,
                        **partial_event_payload,
                    },
                )
                await self._append_workspace_events(
                    workspace_id=workspace_id,
                    events=[
                        WorkspaceEventCreate(
                            event_type=("workspace.monitor_ci_transient_rerun_partially_requested"),
                            reason_code=_CI_TRANSIENT_RERUN_REASON,
                            payload=partial_event_payload,
                        )
                    ],
                )
                await self._sleep_with_monitor_state_operation(
                    workspace_id=workspace_id,
                    action="ci_transient_rerun_wait",
                    requested_action="wait_for_rerun_rollup",
                    reason=(
                        "GitHub accepted at least one failed-job rerun; "
                        "waiting before the next PR status poll so the "
                        "rollup can leave its stale failure snapshot."
                    ),
                    reason_code=_CI_TRANSIENT_RERUN_REASON,
                    pr_number=pr_number,
                    status=status,
                    base_branch=base_branch,
                    remote_branch=remote_branch,
                    wait_seconds=self._config.poll_interval_seconds,
                    monitor_log=monitor_log,
                    extra_payload=partial_event_payload,
                    extra_identity=(
                        rerun_signature,
                        attempt,
                        *accepted_run_ids,
                        "partial_wait",
                    ),
                )
                state.iter_count += 1
                return False
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.failed,
                result={
                    "status": "failed",
                    "outcome": "ci_transient_rerun_failed",
                    "reason_code": _CI_TRANSIENT_RERUN_FAILED_REASON,
                    **event_payload,
                },
                error_code=_CI_TRANSIENT_RERUN_FAILED_REASON,
                error_message=error_message,
            )
            await self._write_monitor_log(
                monitor_log,
                {
                    "event": "monitor.ci_transient_rerun_failed",
                    "workspace_id": workspace_id,
                    "pr_number": pr_number,
                    "reason_code": _CI_TRANSIENT_RERUN_FAILED_REASON,
                    "error": error_message,
                    **event_payload,
                },
            )
            await self._append_workspace_events(
                workspace_id=workspace_id,
                events=[
                    WorkspaceEventCreate(
                        event_type="workspace.monitor_ci_transient_rerun_failed",
                        reason_code=_CI_TRANSIENT_RERUN_FAILED_REASON,
                        payload={**event_payload, "error": error_message},
                    )
                ],
            )
            state.iter_count += 1
            return False

        await self._finish_monitor_operation(
            operation,
            status=OperationStatus.succeeded,
            result={
                "status": "succeeded",
                "outcome": "ci_transient_rerun_requested",
                "reason_code": _CI_TRANSIENT_RERUN_REASON,
                **event_payload,
            },
        )
        await self._write_monitor_log(
            monitor_log,
            {
                "event": "monitor.ci_transient_rerun_requested",
                "workspace_id": workspace_id,
                "pr_number": pr_number,
                "reason_code": _CI_TRANSIENT_RERUN_REASON,
                **event_payload,
            },
        )
        await self._append_workspace_events(
            workspace_id=workspace_id,
            events=[
                WorkspaceEventCreate(
                    event_type="workspace.monitor_ci_transient_rerun_requested",
                    reason_code=_CI_TRANSIENT_RERUN_REASON,
                    payload=event_payload,
                )
            ],
        )
        await self._sleep_with_monitor_state_operation(
            workspace_id=workspace_id,
            action="ci_transient_rerun_wait",
            requested_action="wait_for_rerun_rollup",
            reason=(
                "GitHub accepted the failed-job rerun; waiting before the "
                "next PR status poll so the rollup can leave its stale "
                "failure snapshot."
            ),
            reason_code=_CI_TRANSIENT_RERUN_REASON,
            pr_number=pr_number,
            status=status,
            base_branch=base_branch,
            remote_branch=remote_branch,
            wait_seconds=self._config.poll_interval_seconds,
            monitor_log=monitor_log,
            extra_payload=event_payload,
            extra_identity=(rerun_signature, attempt, *run_ids, "wait"),
        )
        state.iter_count += 1
        return False

    if isinstance(action, ReportCiFailure):
        operation = await self._begin_monitor_operation(
            workspace_id=workspace_id,
            operation_type=OperationType.ci_repair,
            action="ci_repair",
            requested_action="fix_ci",
            reason="CI checks failed and recovery was dispatched.",
            reason_code="CI_REPAIR",
            pr_number=pr_number,
            status=status,
            base_branch=base_branch,
            remote_branch=remote_branch,
            monitor_log=monitor_log,
            extra_payload={
                "failures": [_ci_failure_payload(failure) for failure in action.failures]
            },
            extra_identity=tuple(failure.name for failure in action.failures),
        )
        try:
            push_result = await self._run_ci_fix(
                repo=repo,
                pr_number=pr_number,
                failures=action.failures,
                compose_project=compose_project,
                compose_file=compose_file,
                workspace_id=workspace_id,
                remote_branch=remote_branch,
                remote_push_url=remote_push_url,
                status=status,
                state=state,
                base_branch=base_branch,
                operation_id=operation.operation_id if operation is not None else None,
                operation_type=OperationType.ci_repair.value,
                monitor_log=monitor_log,
            )
        except ProviderRecoveryRetryError:
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.failed,
                result={
                    "status": "failed",
                    "outcome": "provider_retry",
                    "reason_code": "PROVIDER_OUTAGE",
                    "failure_count": len(action.failures),
                    "pushed": False,
                },
                error_code="PROVIDER_OUTAGE",
                error_message="Provider recovery requested retry",
            )
            raise
        except ProviderRecoveryFallbackError:
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.failed,
                result={
                    "status": "failed",
                    "outcome": "provider_fallback",
                    "reason_code": "PROVIDER_FALLBACK",
                    "failure_count": len(action.failures),
                    "pushed": False,
                },
                error_code="PROVIDER_FALLBACK",
                error_message="Provider recovery triggered fallback",
            )
            raise
        except ProviderRecoveryAuthError:
            await self._finish_provider_auth_failed_operation(
                operation,
                extra_result={"failure_count": len(action.failures)},
            )
            raise
        except ComposeExecCleanupError as exc:
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.failed,
                result={
                    "status": "failed",
                    "reason_code": EXEC_PROCESS_CLEANUP_FAILED,
                },
                error_code=EXEC_PROCESS_CLEANUP_FAILED,
                error_message=cleanup_failure_message(exc),
            )
            await self._terminate_failed(
                workspace_id,
                message=cleanup_failure_message(exc),
                reason_code=EXEC_PROCESS_CLEANUP_FAILED,
            )
            return True
        if push_result.failed:
            reason_code = push_result.reason_code
            outcome = _git_push_failure_outcome(push_result)
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.failed,
                result={
                    "status": "failed",
                    "outcome": outcome,
                    "reason_code": reason_code,
                    "failure_count": len(action.failures),
                    "pushed": False,
                    "failure_evidence": push_result.failure_evidence(),
                },
                error_code=reason_code,
                error_message=push_result.error_message,
            )
            await self._record_pr_monitor_audit_event(
                workspace_id=workspace_id,
                event_type=_AUDIT_GIT_PUSH_EVENT,
                action="ci_repair_push",
                outcome="failed",
                reason_code=reason_code,
                pr_number=pr_number,
                status=status,
                base_branch=base_branch,
                remote_branch=remote_branch,
                operation_id=operation.operation_id if operation is not None else None,
                operation_type=OperationType.ci_repair.value,
                monitor_log=monitor_log,
                evidence=push_result.failure_evidence(),
            )
            if push_result.terminal_monitor_failure:
                await self._terminate_failed(
                    workspace_id,
                    message=push_result.error_message or push_result.reason_code,
                    reason_code=push_result.reason_code,
                )
                return True
            state.iter_count += 1
            return False
        await self._finish_monitor_operation(
            operation,
            status=OperationStatus.succeeded,
            result={
                "status": "succeeded",
                "outcome": "ci_repair_pushed",
                "failure_count": len(action.failures),
                "pushed": push_result.pushed,
            },
        )
        await self._record_pr_monitor_audit_event(
            workspace_id=workspace_id,
            event_type=_AUDIT_GIT_PUSH_EVENT,
            action="ci_repair_push",
            outcome="succeeded",
            reason_code="CI_REPAIR",
            pr_number=pr_number,
            status=status,
            base_branch=base_branch,
            remote_branch=remote_branch,
            operation_id=operation.operation_id if operation is not None else None,
            operation_type=OperationType.ci_repair.value,
            monitor_log=monitor_log,
        )
        state.iter_count += 1
        return False

    if isinstance(action, AddressComments):
        operation = await self._begin_monitor_operation(
            workspace_id=workspace_id,
            operation_type=OperationType.comment_repair,
            action="comment_repair",
            requested_action="address_comments",
            reason="Unresolved PR review comments required repair.",
            reason_code="COMMENT_REPAIR",
            pr_number=pr_number,
            status=status,
            base_branch=base_branch,
            remote_branch=remote_branch,
            monitor_log=monitor_log,
            extra_payload={
                "thread_count": len(action.threads),
                "review_comment_count": len(action.review_comments),
                "thread_ids": [thread.thread_id for thread in action.threads],
                "review_comment_ids": [comment.comment_id for comment in action.review_comments],
            },
            extra_identity=(
                *(thread.thread_id for thread in action.threads),
                *(comment.comment_id for comment in action.review_comments),
            ),
        )
        try:
            push_result = await self._run_fix_cycle(
                workspace_id=workspace_id,
                repo=repo,
                pr_number=pr_number,
                pr_head_sha=status.head_sha,
                initial_threads=action.threads,
                initial_reviews=action.review_comments,
                state=state,
                base_branch=base_branch,
                remote_branch=remote_branch,
                remote_push_url=remote_push_url,
                compose_project=compose_project,
                compose_file=compose_file,
                monitor_log=monitor_log,
                operation_id=operation.operation_id if operation is not None else None,
                operation_type=OperationType.comment_repair.value,
            )
        except ProviderRecoveryRetryError:
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.failed,
                result={
                    "status": "failed",
                    "outcome": "provider_retry",
                    "reason_code": "PROVIDER_OUTAGE",
                    "pushed": False,
                },
                error_code="PROVIDER_OUTAGE",
                error_message="Provider recovery requested retry",
            )
            raise
        except ProviderRecoveryFallbackError:
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.failed,
                result={
                    "status": "failed",
                    "outcome": "provider_fallback",
                    "reason_code": "PROVIDER_FALLBACK",
                    "pushed": False,
                },
                error_code="PROVIDER_FALLBACK",
                error_message="Provider recovery triggered fallback",
            )
            raise
        except ProviderRecoveryAuthError:
            await self._finish_provider_auth_failed_operation(operation)
            raise
        except ComposeExecCleanupError as exc:
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.failed,
                result={
                    "status": "failed",
                    "reason_code": EXEC_PROCESS_CLEANUP_FAILED,
                },
                error_code=EXEC_PROCESS_CLEANUP_FAILED,
                error_message=cleanup_failure_message(exc),
            )
            await self._terminate_failed(
                workspace_id,
                message=cleanup_failure_message(exc),
                reason_code=EXEC_PROCESS_CLEANUP_FAILED,
            )
            return True
        if push_result.failed:
            reason_code = push_result.reason_code
            outcome = _git_push_failure_outcome(push_result)
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.failed,
                result={
                    "status": "failed",
                    "outcome": outcome,
                    "reason_code": reason_code,
                    "thread_count": len(action.threads),
                    "review_comment_count": len(action.review_comments),
                    "pushed": False,
                    "failure_evidence": push_result.failure_evidence(),
                },
                error_code=reason_code,
                error_message=push_result.error_message,
            )
            if push_result.terminal_monitor_failure:
                await self._terminate_failed(
                    workspace_id,
                    message=push_result.error_message or push_result.reason_code,
                    reason_code=push_result.reason_code,
                )
                return True
            state.iter_count += 1
            return False
        await self._finish_monitor_operation(
            operation,
            status=OperationStatus.succeeded,
            result={
                "status": "succeeded",
                "outcome": "comments_addressed",
                "thread_count": len(action.threads),
                "review_comment_count": len(action.review_comments),
                "pushed": push_result.pushed,
            },
        )
        state.iter_count += 1
        return False

    if isinstance(action, AddressOperatorHint):
        operation = await self._begin_monitor_operation(
            workspace_id=workspace_id,
            operation_type=OperationType.comment_repair,
            action="operator_hint_repair",
            requested_action="address_operator_hint",
            reason="Operator remonitor hint required repair before merge.",
            reason_code=action.hint.reason_code,
            pr_number=pr_number,
            status=status,
            base_branch=base_branch,
            remote_branch=remote_branch,
            monitor_log=monitor_log,
            extra_payload={
                "operator_hint_operation_id": action.hint.operation_id,
                "operator_hint_status": action.hint.status,
            },
            extra_identity=(action.hint.operation_id, action.hint.reason),
        )
        try:
            push_result = await self._run_operator_hint_cycle(
                workspace_id=workspace_id,
                repo=repo,
                pr_number=pr_number,
                pr_head_sha=status.head_sha,
                hint=action.hint,
                state=state,
                base_branch=base_branch,
                remote_branch=remote_branch,
                remote_push_url=remote_push_url,
                compose_project=compose_project,
                compose_file=compose_file,
                _monitor_log=monitor_log,
                _operation_id=operation.operation_id if operation is not None else None,
                _operation_type=OperationType.comment_repair.value,
            )
        except ProviderRecoveryRetryError:
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.failed,
                result={
                    "status": "failed",
                    "outcome": "provider_retry",
                    "reason_code": "PROVIDER_OUTAGE",
                    "pushed": False,
                },
                error_code="PROVIDER_OUTAGE",
                error_message="Provider recovery requested retry",
            )
            raise
        except ProviderRecoveryFallbackError:
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.failed,
                result={
                    "status": "failed",
                    "outcome": "provider_fallback",
                    "reason_code": "PROVIDER_FALLBACK",
                    "pushed": False,
                },
                error_code="PROVIDER_FALLBACK",
                error_message="Provider recovery triggered fallback",
            )
            raise
        except ProviderRecoveryAuthError:
            await self._finish_provider_auth_failed_operation(operation)
            raise
        except ComposeExecCleanupError as exc:
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.failed,
                result={
                    "status": "failed",
                    "reason_code": EXEC_PROCESS_CLEANUP_FAILED,
                },
                error_code=EXEC_PROCESS_CLEANUP_FAILED,
                error_message=cleanup_failure_message(exc),
            )
            await self._terminate_failed(
                workspace_id,
                message=cleanup_failure_message(exc),
                reason_code=EXEC_PROCESS_CLEANUP_FAILED,
            )
            return True
        if push_result.failed:
            reason_code = push_result.reason_code
            outcome = _git_push_failure_outcome(push_result)
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.failed,
                result={
                    "status": "failed",
                    "outcome": outcome,
                    "reason_code": reason_code,
                    "pushed": False,
                    "failure_evidence": push_result.failure_evidence(),
                },
                error_code=reason_code,
                error_message=push_result.error_message,
            )
            if push_result.terminal_monitor_failure:
                await self._persist_state(workspace_id, state)
                await self._terminate_failed(
                    workspace_id,
                    message=push_result.error_message or push_result.reason_code,
                    reason_code=push_result.reason_code,
                )
                return True
            state.iter_count += 1
            return False
        if (
            not push_result.pushed
            and state.pending_operator_hint is not None
            and state.pending_operator_hint.status in {"needs_human", "agent_failed"}
        ):
            # Persist terminal hint status before returning to the outer loop so
            # a restart cannot re-run the same blocked operator hint as pending.
            await self._persist_state(workspace_id, state)
        await self._finish_monitor_operation(
            operation,
            status=OperationStatus.succeeded,
            result={
                "status": "succeeded",
                "outcome": (
                    "operator_hint_pushed" if push_result.pushed else "operator_hint_needs_human"
                ),
                "pushed": push_result.pushed,
            },
        )
        state.iter_count += 1
        return False

    merge_result = await _merge_loop.handle_merge_action(
        self,
        action=action,
        workspace_id=workspace_id,
        repo_url=repo_url,
        repo=repo,
        pr_number=pr_number,
        status=status,
        state=state,
        base_branch=base_branch,
        remote_branch=remote_branch,
        remote_push_url=remote_push_url,
        compose_project=compose_project,
        compose_file=compose_file,
        monitor_log=monitor_log,
    )
    if merge_result is not None:
        return merge_result

    if isinstance(action, NotifyHuman):
        if _is_manual_ready_handoff(action, status, state, self._config):
            policy_blocked = await self._refresh_scope_policy_for_merge(
                workspace_id=workspace_id,
                changed_paths=status.changed_paths,
            )
            if policy_blocked:
                action = NotifyHuman(
                    message=(
                        "OUT_OF_SCOPE_CHANGE: changed files outside declared "
                        "owned_paths require an operator scope decision."
                    )
                )
            else:
                queue_blockers = await self._merge_queue_blockers_for_workspace(workspace_id)
                if queue_blockers:
                    await self._wait_for_merge_queue(
                        blockers=queue_blockers,
                        workspace_id=workspace_id,
                        repo_url=repo_url,
                        base_branch=base_branch,
                        pr_number=pr_number,
                        status=status,
                        state=state,
                        monitor_log=monitor_log,
                    )
                    return False

                merge_gate = await self._merge_gate_with_legacy_head_support(
                    workspace_id,
                    check_policy=True,
                    current_head_sha=status.head_sha,
                )
                manual_ready_handled = None
                settle_config = replace(self._config, auto_merge=True)
                notify_settle_decision: _NonCheckReviewerSettleDecision | None = None
                if _gate_requires_validation_recovery(merge_gate):
                    notify_settle_decision = _non_check_reviewer_settle_decision(
                        status,
                        state,
                        settle_config,
                        pr_number=pr_number,
                        now=time.monotonic(),
                    )
                    await self._record_non_check_reviewer_settle_decision(
                        decision=notify_settle_decision,
                        workspace_id=workspace_id,
                        pr_number=pr_number,
                        status=status,
                        monitor_log=monitor_log,
                    )
                    if notify_settle_decision.wait_seconds > 0:
                        settle_operation_context = (
                            _non_check_reviewer_settle_wait_operation_context(
                                settle_config,
                                notify_settle_decision,
                            )
                        )
                        await self._sleep_with_monitor_state_operation(
                            workspace_id=workspace_id,
                            action="reviewer_settle_wait",
                            requested_action="validate",
                            reason=(
                                "Waiting for configured non-check reviewers to settle "
                                "before final validation."
                            ),
                            reason_code="NON_CHECK_REVIEWER_SETTLE",
                            pr_number=pr_number,
                            status=status,
                            base_branch=base_branch,
                            remote_branch=remote_branch,
                            wait_seconds=notify_settle_decision.wait_seconds,
                            monitor_log=monitor_log,
                            extra_payload=settle_operation_context.extra_payload,
                            extra_identity=settle_operation_context.extra_identity,
                        )
                        return False

                    manual_ready_handled = await self._handle_merge_gate_blocker(
                        gate=merge_gate,
                        workspace_id=workspace_id,
                        repo_url=repo_url,
                        repo=repo,
                        pr_number=pr_number,
                        status=status,
                        state=state,
                        base_branch=base_branch,
                        remote_branch=remote_branch,
                        compose_project=compose_project,
                        compose_file=compose_file,
                        monitor_log=monitor_log,
                        skip_initial_review_grace=(
                            self._config.non_check_reviewer_settle_seconds > 0
                        ),
                    )

                if manual_ready_handled is not None:
                    return cast(bool, manual_ready_handled)

                if notify_settle_decision is None:
                    notify_settle_decision = _non_check_reviewer_settle_decision(
                        status,
                        state,
                        settle_config,
                        pr_number=pr_number,
                        now=time.monotonic(),
                    )
                await self._record_non_check_reviewer_settle_decision(
                    decision=notify_settle_decision,
                    workspace_id=workspace_id,
                    pr_number=pr_number,
                    status=status,
                    monitor_log=monitor_log,
                )
                if notify_settle_decision.wait_seconds > 0:
                    settle_operation_context = _non_check_reviewer_settle_wait_operation_context(
                        settle_config,
                        notify_settle_decision,
                    )
                    await self._sleep_with_monitor_state_operation(
                        workspace_id=workspace_id,
                        action="reviewer_settle_wait",
                        requested_action="notify_human",
                        reason=(
                            "Waiting for configured non-check reviewers to settle "
                            "before human ready notification."
                        ),
                        reason_code="NON_CHECK_REVIEWER_SETTLE",
                        pr_number=pr_number,
                        status=status,
                        base_branch=base_branch,
                        remote_branch=remote_branch,
                        wait_seconds=notify_settle_decision.wait_seconds,
                        monitor_log=monitor_log,
                        extra_payload=settle_operation_context.extra_payload,
                        extra_identity=settle_operation_context.extra_identity,
                    )
                    return False

        operation = await self._begin_monitor_operation(
            workspace_id=workspace_id,
            operation_type=OperationType.human_wait,
            action="human_wait",
            requested_action="notify_human",
            reason=action.message or _notify_human_reason(status, state),
            reason_code="HUMAN_WAIT",
            pr_number=pr_number,
            status=status,
            base_branch=base_branch,
            remote_branch=remote_branch,
            monitor_log=monitor_log,
            extra_identity=(action.message or "", state.iter_count),
        )
        try:
            await self._post_human_notification_once(
                repo=repo,
                pr_number=pr_number,
                status=status,
                state=state,
                blocker_reason=action.message,
            )
        except GitHubClientError as exc:
            if await self._wait_after_transient_github_error(
                exc,
                workspace_id=workspace_id,
                pr_number=pr_number,
                context="post_human_notification",
                monitor_log=monitor_log,
            ):
                await self._finish_monitor_operation(
                    operation,
                    status=OperationStatus.failed,
                    result={
                        "status": "failed",
                        "outcome": "transient_github_error",
                        "reason_code": "GITHUB_TRANSIENT_ERROR",
                    },
                    error_code="GITHUB_TRANSIENT_ERROR",
                    error_message=str(exc),
                )
                return False
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.failed,
                result={
                    "status": "failed",
                    "outcome": "github_error",
                    "reason_code": "GITHUB_ERROR",
                },
                error_code="GITHUB_ERROR",
                error_message=str(exc),
            )
            raise
        await self._deps.sleep(self._config.poll_interval_seconds)
        await self._finish_monitor_operation(
            operation,
            status=OperationStatus.succeeded,
            result={
                "status": "succeeded",
                "outcome": "human_notification_posted",
                "slept_seconds": self._config.poll_interval_seconds,
            },
        )
        return False

    # If we got here the MonitorAction union gained a variant without
    # a dispatch arm — fail loudly so tests catch it.
    raise RuntimeError(f"unhandled monitor action: {action!r}")  # pragma: no cover
