"""Pull request monitor decision loop.

Mechanically extracted from the original orchestrator; behavior is unchanged.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, cast

from awf.common.compose_exec import (
    EXEC_PROCESS_CLEANUP_FAILED,
    ComposeExecCleanupError,
    cleanup_failure_message,
)
from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import (
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
    Merge,
    MonitorAction,
    MonitorState,
    NotifyHuman,
    PRStatus,
    ReportCiFailure,
    RerunTransientCI,
    ShortCircuitCompleted,
    SyncBase,
    WaitForCI,
    WaitForTransientCI,
    _ci_transient_rerun_count,
    _ci_transient_rerun_state_key,
    _record_ci_transient_infra_wait,
)
from awf.runtime.pr_monitor_runner import (
    merge_loop as _merge_loop,
)
from awf.runtime.pr_monitor_runner import (
    notify_human_loop as _notify_human_loop,
)
from awf.runtime.pr_monitor_runner.constants import (
    _AUDIT_GIT_PUSH_EVENT,
    _CI_TRANSIENT_RERUN_FAILED_REASON,
    _CI_TRANSIENT_RERUN_REASON,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _ci_failure_payload,
    _ci_transient_rerun_attempt,
    _clear_transient_base_fetch_retry_state,
    _pending_review_feedback_count,
    _redact_and_truncate_forge_error,
)
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.loop_helpers import (
    _post_workflow_scope_notification_best_effort,
)
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

    # Clear the awaiting-human attention flag the moment the monitor resumes with
    # a resuming action: ``decide()`` returns exactly one action per poll, so
    # WaitForCI / AddressComments / SyncBase / terminal completes+aborts each mean
    # the external blocker is gone. ``NotifyHuman`` is excluded because it IS the
    # human-block action. ``Merge`` is excluded too: ``handle_merge_action`` owns
    # the attention flag for that arm because a deterministic merge rejection
    # (branch protection / restrictions) falls back to notifying a human *without*
    # recording a sticky blocker, so ``decide()`` keeps returning ``Merge`` every
    # poll. Clearing here first would null the persisted episode start before the
    # merge loop re-sets it, defeating the repo-side COALESCE and resetting
    # ``awaiting_human_since`` to ``now`` each cycle — the operator's "awaiting
    # human for N" timer would never age (#659). ``handle_merge_action`` (for
    # ``Merge``) and the manual-ready ``NotifyHuman`` handoff below instead clear
    # any stale flag themselves right before each of their non-human gate waits
    # (merge queue, reviewer settle, initial review grace) so a *resolved*
    # ``NotifyHuman`` episode does not keep surfacing "awaiting human" while the
    # monitor merely waits on a non-human gate; their deterministic-rejection arms
    # re-set attention directly, and the pre-merge settle is deliberately left
    # untouched so a branch-protection rejection that re-sets attention every poll
    # does not flicker the signal. The ``IS NOT NULL`` guard makes
    # the repo update a no-op when the flag is already clear, so this per-poll clear
    # never churns the row — but the guarded ``UPDATE`` still round-trips once per
    # poll. We keep it unconditional rather than inferring "already clear" from
    # in-process state: ``awaiting_human_since`` is persisted, so a monitor restart
    # between the ``NotifyHuman`` set and this clear would otherwise strand a stale
    # attention signal. The round-trip is negligible beside the operation/state/
    # audit writes each poll already performs.
    #
    # ``AddressComments`` is the one resuming action that can itself be a human
    # wait: a comment-repair push rejected for a missing ``workflow`` token scope
    # requeues ``AddressComments`` and keeps the row in ``monitoring_pr`` (it does
    # NOT terminally fail like the sync-base / CI-repair workflow-scope arms),
    # waiting on an operator to grant the scope. That arm persists
    # ``awaiting_workflow_scope`` so this poll knows the previous one ended on that
    # wait; honor it by skipping the clear (the attention set by the prior poll
    # survives — ``set_workspace_attention`` COALESCEs the episode start, so it
    # stays stable across the requeued polls). Reset the marker first so the moment
    # a poll is no longer workflow-scope-blocked (push succeeds, or ``decide``
    # returns a different action) we fall back to the normal resume clear next
    # cycle — at most one extra poll of "awaiting human" after the block resolves.
    awaiting_workflow_scope = state.awaiting_workflow_scope
    state.clear_awaiting_workflow_scope()
    # The merge-block attention marker only makes sense while ``decide()`` stays on
    # the ``Merge`` arm (the branch-protection fallback that sets it keeps
    # ``decide()`` returning ``Merge``). The moment ``decide()`` returns any other
    # action the branch-protection retry context is over, so drop the marker — a
    # later ``Merge`` poll's non-human gate wait must then clear a now-stale
    # ``NotifyHuman`` episode normally instead of preserving it
    # (PRRT_kwDOSJAM6s6LXscz). ``handle_merge_action`` re-sets it each poll the
    # branch-protection block persists.
    if not isinstance(action, Merge):
        state.clear_merge_block_attention()
    if not isinstance(action, (NotifyHuman, Merge)) and not awaiting_workflow_scope:
        await self._clear_workspace_attention(workspace_id)

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
            reason={
                "pending_checks": "CI checks are still pending.",
                "awaiting_required_checks": "Required CI has not started on the new head yet.",
            }.get(action.reason, "GitHub has not reported a stable mergeable state."),
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
                pr_head_sha=status.head_sha,
                base_branch=base_branch,
                remote_branch=remote_branch,
                remote_push_url=remote_push_url,
                compose_project=compose_project,
                compose_file=compose_file,
                operation_id=operation.operation_id if operation is not None else None,
                operation_type=OperationType.sync_base.value,
                monitor_log=monitor_log,
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
        if push_result.paused_into_blocked:
            # A protected-scope violation in the base-conflict resolution commit
            # paused the workspace into ``blocked`` for an operator decision
            # (WS-2). The row already left ``monitoring_pr`` (preserving the
            # offending commit); end the monitor cycle cleanly — do NOT terminally
            # fail. Persist state so the notification dedupe + preserved-commit
            # marker survive a restart.
            await self._persist_state(workspace_id, state)
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.succeeded,
                result={
                    "status": "succeeded",
                    "outcome": "protected_scope_paused",
                    "reason_code": push_result.reason_code,
                    "pushed": False,
                },
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
            if push_result.workflow_scope_required:
                await _post_workflow_scope_notification_best_effort(
                    self,
                    workspace_id=workspace_id,
                    repo=repo,
                    pr_number=pr_number,
                    status=status,
                    state=state,
                    blocker_reason=push_result.error_message or push_result.reason_code,
                )
            if push_result.terminal_monitor_failure or push_result.workflow_scope_required:
                await self._terminate_failed(
                    workspace_id,
                    message=push_result.error_message or push_result.reason_code,
                    reason_code=push_result.reason_code,
                    details=push_result.failure_evidence(),
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
                "rerunning failed CI jobs before invoking an agent."
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
                except ForgeClientError:
                    # A Bitbucket forge raises ``BitbucketClientError`` here (e.g.
                    # ``BITBUCKET_PIPELINE_NOT_RERUNNABLE`` for a custom/manual
                    # pipeline target it cannot reconstruct). Catch it alongside
                    # the GitHub error so it is recorded as a failed transient
                    # rerun below instead of escaping ``_execute`` to the runner's
                    # non-transient handler, which would ``_terminate_failed`` the
                    # workspace permanently rather than logging the limitation and
                    # continuing to poll.
                    failed_run_id = run_id
                    raise
                accepted_run_ids.append(run_id)
        except ForgeClientError as exc:
            error_message = _redact_and_truncate_forge_error(str(exc))
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
                        "The CI provider accepted at least one failed-job "
                        "rerun; waiting before the next PR status poll so the "
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
                "The CI provider accepted the failed-job rerun; waiting "
                "before the next PR status poll so the rollup can leave its "
                "stale failure snapshot."
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

    if isinstance(action, WaitForTransientCI):
        failures_payload = [_ci_failure_payload(failure) for failure in action.failures]
        run_ids = tuple(
            dict.fromkeys(failure.run_id for failure in action.failures if failure.run_id)
        )
        rerun_signature = _ci_transient_rerun_state_key(status.head_sha, action.failures)
        recorded_wait_count, first_seen_at = _record_ci_transient_infra_wait(
            state,
            head_sha=status.head_sha,
            failures=action.failures,
        )
        elapsed_seconds = max(time.time() - first_seen_at, 0.0)
        rerun_attempts = _ci_transient_rerun_count(
            state,
            head_sha=status.head_sha,
            failures=action.failures,
            legacy_failures=status.ci_failures,
        )
        infra_wait_payload: dict[str, object] = {
            "wait_count": recorded_wait_count,
            "wait_seconds": action.wait_seconds,
            "elapsed_seconds": elapsed_seconds,
            "rerun_attempts": rerun_attempts,
            "failures": failures_payload,
            "run_ids": list(run_ids),
            "head_sha": status.head_sha,
        }
        await self._persist_state(workspace_id, state)
        await self._write_monitor_log(
            monitor_log,
            {
                "event": "monitor.ci_transient_infra_waiting",
                "workspace_id": workspace_id,
                "pr_number": pr_number,
                "reason_code": _CI_TRANSIENT_RERUN_REASON,
                **infra_wait_payload,
            },
        )
        await self._append_workspace_events(
            workspace_id=workspace_id,
            events=[
                WorkspaceEventCreate(
                    event_type="workspace.monitor_ci_transient_infra_waiting",
                    reason_code=_CI_TRANSIENT_RERUN_REASON,
                    payload=infra_wait_payload,
                )
            ],
        )
        await self._sleep_with_monitor_state_operation(
            workspace_id=workspace_id,
            action="ci_transient_infra_wait",
            requested_action="wait_for_transient_ci",
            reason=(
                "CI failure still matches transient infrastructure signatures "
                "after deterministic reruns; waiting before human escalation."
            ),
            reason_code=_CI_TRANSIENT_RERUN_REASON,
            pr_number=pr_number,
            status=status,
            base_branch=base_branch,
            remote_branch=remote_branch,
            wait_seconds=action.wait_seconds,
            monitor_log=monitor_log,
            extra_payload=infra_wait_payload,
            extra_identity=(rerun_signature, recorded_wait_count, *run_ids, "infra_wait"),
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
        if push_result.paused_into_blocked:
            # A protected-scope violation in the CI-repair commit paused the
            # workspace into ``blocked`` for an operator decision (WS-2). The row
            # already left ``monitoring_pr`` (preserving the offending commit); end
            # the monitor cycle cleanly — do NOT terminally fail. Persist state so
            # the notification dedupe + preserved-commit marker survive a restart.
            await self._persist_state(workspace_id, state)
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.succeeded,
                result={
                    "status": "succeeded",
                    "outcome": "protected_scope_paused",
                    "reason_code": push_result.reason_code,
                    "failure_count": len(action.failures),
                    "pushed": False,
                },
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
            if push_result.workflow_scope_required:
                await _post_workflow_scope_notification_best_effort(
                    self,
                    workspace_id=workspace_id,
                    repo=repo,
                    pr_number=pr_number,
                    status=status,
                    state=state,
                    blocker_reason=push_result.error_message or push_result.reason_code,
                )
            if push_result.terminal_monitor_failure or push_result.workflow_scope_required:
                await self._terminate_failed(
                    workspace_id,
                    message=push_result.error_message or push_result.reason_code,
                    reason_code=push_result.reason_code,
                    details=push_result.failure_evidence(),
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
        if push_result.paused_into_blocked:
            # A protected-scope violation paused the workspace into ``blocked``
            # for an operator decision (WS-2). The row already left
            # ``monitoring_pr`` (preserving the offending commit); end the monitor
            # cycle cleanly — do NOT terminally fail. Persist state so the
            # notification dedupe + preserved-commit marker survive a restart.
            await self._persist_state(workspace_id, state)
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.succeeded,
                result={
                    "status": "succeeded",
                    "outcome": "protected_scope_paused",
                    "reason_code": push_result.reason_code,
                    "thread_count": len(action.threads),
                    "review_comment_count": len(action.review_comments),
                    "pushed": False,
                },
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
            if push_result.workflow_scope_required:
                # Unlike sync-base / CI-repair, this arm does NOT terminally fail
                # on a missing ``workflow`` token scope — it requeues
                # ``AddressComments`` and stays in ``monitoring_pr`` while an
                # operator grants the scope, so the direct PR comment below is the
                # only human escalation. Surface that wait as a first-class
                # attention signal, in parity with the ``NotifyHuman`` touch-point
                # and the merge-loop direct notifications, and persist
                # ``awaiting_workflow_scope`` so the top-of-poll resume clear keeps
                # the flag set across the requeued polls.
                state.mark_awaiting_workflow_scope()
                await self._set_workspace_attention(
                    workspace_id,
                    reason=push_result.error_message or push_result.reason_code,
                )
                await _post_workflow_scope_notification_best_effort(
                    self,
                    workspace_id=workspace_id,
                    repo=repo,
                    pr_number=pr_number,
                    status=status,
                    state=state,
                    blocker_reason=push_result.error_message or push_result.reason_code,
                )
            if push_result.terminal_monitor_failure:
                await self._terminate_failed(
                    workspace_id,
                    message=push_result.error_message or push_result.reason_code,
                    reason_code=push_result.reason_code,
                    details=push_result.failure_evidence(),
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
        if push_result.paused_into_blocked:
            # A directive-revert / grant resume that still trips the protected
            # gate re-paused the workspace into ``blocked`` (WS-2 §2 re-block).
            # End the cycle cleanly without terminally failing.
            await self._persist_state(workspace_id, state)
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.succeeded,
                result={
                    "status": "succeeded",
                    "outcome": "protected_scope_paused",
                    "reason_code": push_result.reason_code,
                    "pushed": False,
                },
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
                    details=push_result.failure_evidence(),
                )
                return True
            state.iter_count += 1
            return False
        if push_result.pushed or (
            not push_result.pushed
            and (
                state.pending_operator_hint is None
                or state.pending_operator_hint.status in {"needs_human", "agent_failed"}
            )
        ):
            # Persist terminal, processed no-op, or pushed hint status before
            # returning to the outer loop so a restart cannot re-run the same
            # hint as pending.
            await self._persist_state(workspace_id, state)
        if push_result.pushed:
            outcome = "operator_hint_pushed"
        elif state.pending_operator_hint is None:
            outcome = "operator_hint_processed"
        elif (
            state.pending_operator_hint is not None
            and state.pending_operator_hint.status == "agent_failed"
        ):
            outcome = "operator_hint_agent_failed"
        else:
            outcome = "operator_hint_needs_human"
        await self._finish_monitor_operation(
            operation,
            status=OperationStatus.succeeded,
            result={
                "status": "succeeded",
                "outcome": outcome,
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
        return await _notify_human_loop.handle_notify_human_action(
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
            compose_project=compose_project,
            compose_file=compose_file,
            monitor_log=monitor_log,
        )

    # If we got here the MonitorAction union gained a variant without
    # a dispatch arm — fail loudly so tests catch it.
    raise RuntimeError(f"unhandled monitor action: {action!r}")  # pragma: no cover
