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
from awf.db.enums import OperationStatus, OperationType
from awf.db.repositories import WorkspaceEventCreate
from awf.runtime.feedback_policy import unresolved_thread_counts
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
from awf.runtime.pr_monitor_runner import (
    sync_base_loop as _sync_base_loop,
)
from awf.runtime.pr_monitor_runner.constants import (
    _AUDIT_GIT_PUSH_EVENT,
    _CI_TRANSIENT_RERUN_FAILED_REASON,
    _CI_TRANSIENT_RERUN_REASON,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _ci_failure_payload,
    _ci_transient_rerun_attempt,
    _pending_review_feedback_count,
    _redact_and_truncate_forge_error,
)
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.loop_helpers import (
    _finish_cycle_for_terminal_pr,
    _post_workflow_scope_notification_best_effort,
)
from awf.runtime.pr_monitor_runner.loop_recovery_ops import (
    _finish_agent_service_recovery_failed_operation,
    _finish_agent_service_recovery_superseded_operation,
    _provider_recovery_operation_result_updates,
)
from awf.runtime.pr_monitor_runner.remote_ops import (
    _git_push_failure_outcome,
)
from awf.runtime.pr_monitor_runner.types import (
    ProviderRecoveryAuthError,
    ProviderRecoveryFallbackError,
    ProviderRecoveryRetryError,
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorAgentServiceRecoverySupersededError,
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
    unresolved_counts = unresolved_thread_counts(
        status.unresolved_inline_threads,
        status.outdated_unresolved_inline_threads,
    )
    _log.info(
        "monitor.action",
        workspace_id=workspace_id,
        pr_number=pr_number,
        iter=state.iter_count,
        action=type(action).__name__,
        head_sha=status.head_sha[:10],
        base_behind=status.base_behind_count,
        merge_state=(status.merge_state_status.value if status.merge_state_status else None),
        unresolved_threads=unresolved_counts["unresolved_threads"],
        unresolved_active_threads=unresolved_counts["unresolved_active_threads"],
        unresolved_outdated_threads=unresolved_counts["unresolved_outdated_threads"],
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
            "unresolved_threads": unresolved_counts["unresolved_threads"],
            "unresolved_active_threads": unresolved_counts["unresolved_active_threads"],
            "unresolved_outdated_threads": unresolved_counts["unresolved_outdated_threads"],
            "unresolved_reviews": unresolved_reviews,
            "review_feedback": review_feedback,
            "pending_review_feedback": pending_review_feedback,
            "blocking_reviews": len(status.blocking_reviews),
        },
    )

    # Clear awaiting-human attention as soon as the monitor resumes with a
    # non-human action. ``Merge`` owns its own attention handling because branch
    # protection can deterministically re-enter that arm every poll (#659).
    #
    # Comment repair may requeue ``AddressComments`` while waiting for an
    # operator-granted workflow token scope. The persisted marker skips this
    # attention clear for that blocked poll and must stay on ``state`` through
    # ``_run_fix_cycle`` so unpublished repairs are not abandoned. The marker is
    # cleared after the fix cycle (or immediately when ``decide()`` leaves
    # ``AddressComments``).
    awaiting_workflow_scope = state.awaiting_workflow_scope
    if not isinstance(action, AddressComments) and awaiting_workflow_scope:
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

    async def _finish_if_pr_terminal(operation: Any, push_result: Any) -> bool | None:
        """Finish the cycle when the just-run action outlived its PR (#910).

        Called by every agent-action arm right after its ``_run_*`` and BEFORE the
        paused/failed branches: a merged/closed PR makes the push, the ``blocked``
        pause, and the human ping moot, so the monitor runs the terminal handling
        ``decide()`` would return next poll.

        ``None`` means the action was not moot and the arm continues. Otherwise the
        cycle is over and this returns the *terminate sink's own result*: ``True``
        when this runner terminated the workspace, ``False`` when the sink refused
        the write because the runner had been superseded as the monitor owner.
        Reporting that refusal instead of an unconditional ``True`` keeps a
        superseded terminal cycle from presenting itself to ``run()`` as a
        completed terminal cycle whose state is safe to flush — the same seam the
        propagated ``monitor_writes_suppressed`` marker fences at every
        ``_persist_state`` (PRRT_kwDOSJAM6s6fsqcA).
        """
        moot = await _finish_cycle_for_terminal_pr(
            self,
            workspace_id=workspace_id,
            operation=operation,
            push_result=push_result,
            state=state,
            pr_number=pr_number,
            repo_url=repo_url,
            base_branch=base_branch,
            compose_project=compose_project,
            compose_file=compose_file,
        )
        if not moot:
            return None
        return not state.monitor_writes_suppressed

    async def _finish_if_pr_terminal_after_cleanup_error(
        operation: Any, *, context: str, operation_type: str
    ) -> bool | None:
        """Recheck live PR state before a cleanup-failure terminal fail (#910).

        ``ComposeExecCleanupError`` escapes the ``_run_*`` helpers as an exception,
        so it never reaches their post-action terminal guards nor the arm's
        ``_finish_if_pr_terminal`` call below the ``try`` — the handler recorded
        ``EXEC_PROCESS_CLEANUP_FAILED`` and terminally failed a workspace whose PR
        had merged or closed while the long action ran, instead of completing it as
        moot (``PRRT_kwDOSJAM6s6fvDbL``). Run the guard HERE, BEFORE the failure is
        recorded, so the operation's only outcome is the moot one and the cycle
        ends through the terminal handling ``decide()`` would return next poll.

        Fails OPEN exactly like every other #910 seam: ``None`` means the PR is
        still open (or the re-read could not run) and the caller records its
        cleanup failure unchanged.
        """
        moot_result = await self._post_action_pr_terminal_push_result_if_moot(
            workspace_id=workspace_id,
            pr_number=pr_number,
            context=context,
            operation_id=operation.operation_id if operation is not None else None,
            operation_type=operation_type,
            repo=repo,
        )
        if moot_result is None:
            return None
        return await _finish_if_pr_terminal(operation, moot_result)

    if isinstance(action, ShortCircuitCompleted):
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

        async def _publish_short_circuit_defer_signal() -> None:
            """Publish the terminal artifact once the ``completed`` write commits."""
            self._write_defer_signal(
                workspace_id=workspace_id,
                pr_number=pr_number,
                terminal_action="ShortCircuitCompleted",
                merged=True,
                status=status,
                state=state,
            )

        # The workspace-scoped writes are gated on the terminate sink's owner fence
        # — same seam as ``_finish_cycle_for_terminal_pr`` (PRRT_kwDOSJAM6s6flswY /
        # PRRT_kwDOSJAM6s6fsqcA). A runner that lost its monitor claim mid-cycle must
        # not publish a "monitor is done" defer signal (nor let ``run()``'s
        # post-``_execute`` persist flush its stale state) while the row is still
        # ``monitoring_pr`` under the live claimant, which re-derives the completion
        # from the merged PR on its own next poll (PRRT_kwDOSJAM6s6fsrlC). The gate is
        # the sink's transition commit, NOT its return: the callback runs there, ahead
        # of the cancellable target-branch reconcile + filesystem GC, so a cancellation
        # inside that cleanup cannot strand a completed workspace with no terminal
        # artifact that any later monitor would publish (PRRT_kwDOSJAM6s6fvDbP).
        if not await self._terminate_completed(
            workspace_id,
            pr_merge_sha=status.merge_commit_sha or status.head_sha,
            repo_url=repo_url,
            base_branch=base_branch,
            compose_project=compose_project,
            compose_file=compose_file,
            on_transition_committed=_publish_short_circuit_defer_signal,
        ):
            state.monitor_writes_suppressed = True
        return True

    if isinstance(action, Abort):
        # Same seam as the ``ShortCircuitCompleted`` arm above: the workspace-scoped
        # writes run AFTER the terminate sink and are gated on its owner fence, so a
        # runner that lost its monitor claim mid-cycle publishes neither the "monitor
        # is done" defer signal nor (via ``run()``'s post-``_execute`` persist) its
        # stale monitor state while the row is still ``monitoring_pr`` under the live
        # claimant (PRRT_kwDOSJAM6s6fsrlC).
        if await self._terminate_failed(
            workspace_id,
            message=f"monitor: abort ({action.reason.value})",
            reason_code=action.reason,
        ):
            self._write_defer_signal(
                workspace_id=workspace_id,
                pr_number=pr_number,
                terminal_action="Abort",
                merged=False,
                status=status,
                state=state,
            )
        else:
            state.monitor_writes_suppressed = True
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
        return await _sync_base_loop.handle_sync_base_action(
            self,
            workspace_id=workspace_id,
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
            finish_if_pr_terminal=_finish_if_pr_terminal,
            finish_if_pr_terminal_after_cleanup_error=(_finish_if_pr_terminal_after_cleanup_error),
        )

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
        except _MonitorAgentServiceRecoverySupersededError as exc:
            await _finish_agent_service_recovery_superseded_operation(
                self,
                operation,
                exc=exc,
                error_message=str(exc),
                extra_result={"failure_count": len(action.failures)},
            )
            raise
        except _MonitorAgentServiceRecoveryFailedError as exc:
            await _finish_agent_service_recovery_failed_operation(
                self,
                operation,
                exc=exc,
                error_message=str(exc),
                extra_result={"failure_count": len(action.failures)},
            )
            raise
        except ProviderRecoveryRetryError as exc:
            retry_result: dict[str, object] = {
                "status": "failed",
                "outcome": "provider_retry",
                "reason_code": "PROVIDER_OUTAGE",
                "failure_count": len(action.failures),
                "pushed": False,
            }
            retry_result.update(_provider_recovery_operation_result_updates(exc))
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.failed,
                result=retry_result,
                error_code="PROVIDER_OUTAGE",
                error_message="Provider recovery requested retry",
            )
            raise
        except ProviderRecoveryFallbackError as exc:
            fallback_result: dict[str, object] = {
                "status": "failed",
                "outcome": "provider_fallback",
                "reason_code": "PROVIDER_FALLBACK",
                "failure_count": len(action.failures),
                "pushed": False,
            }
            fallback_result.update(_provider_recovery_operation_result_updates(exc))
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.failed,
                result=fallback_result,
                error_code="PROVIDER_FALLBACK",
                error_message="Provider recovery triggered fallback",
            )
            raise
        except ProviderRecoveryAuthError as exc:
            auth_extra: dict[str, object] = {"failure_count": len(action.failures)}
            auth_extra.update(_provider_recovery_operation_result_updates(exc))
            await self._finish_provider_auth_failed_operation(
                operation,
                extra_result=auth_extra,
            )
            raise
        except ComposeExecCleanupError as exc:
            cleanup_terminal_result = await _finish_if_pr_terminal_after_cleanup_error(
                operation,
                context="ci_repair_cleanup_failure",
                operation_type=OperationType.ci_repair.value,
            )
            if cleanup_terminal_result is not None:
                return cleanup_terminal_result
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
        pr_terminal_result = await _finish_if_pr_terminal(operation, push_result)
        if pr_terminal_result is not None:
            return pr_terminal_result
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
                notification_moot = await _post_workflow_scope_notification_best_effort(
                    self,
                    workspace_id=workspace_id,
                    repo=repo,
                    pr_number=pr_number,
                    status=status,
                    state=state,
                    blocker_reason=push_result.error_message or push_result.reason_code,
                )
                if notification_moot is not None:
                    # The notification boundary's fresh read saw the PR go terminal
                    # between the pre-push guard and this push rejection, so the
                    # terminal fail below would mark a MERGED workspace failed. Run
                    # the moot completion path on that observation instead
                    # (PRRT_kwDOSJAM6s6fvGsp). ``operation`` is ``None``: this arm
                    # already finished it as ``failed`` above, and that push failure
                    # is a true audit record worth keeping.
                    #
                    # The moot envelope always carries the observation, so — unlike
                    # the post-``_run_*`` call above, which sees real pushes — this
                    # call can never report "not moot". Take the terminate sink's
                    # own result directly instead of guarding on an arc this call
                    # cannot produce; ``False`` still means the sink refused the
                    # write because this runner was superseded.
                    return bool(await _finish_if_pr_terminal(None, notification_moot))
            if push_result.terminal_monitor_failure or push_result.workflow_scope_required:
                await self._terminate_failed(
                    workspace_id,
                    message=push_result.error_message or push_result.reason_code,
                    reason_code=push_result.reason_code,
                    details=push_result.failure_evidence(),
                    failure_reason=push_result.failure_reason,
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
        except _MonitorAgentServiceRecoverySupersededError as exc:
            await _finish_agent_service_recovery_superseded_operation(
                self,
                operation,
                exc=exc,
                error_message=str(exc),
                extra_result={
                    "thread_count": len(action.threads),
                    "review_comment_count": len(action.review_comments),
                },
            )
            raise
        except _MonitorAgentServiceRecoveryFailedError as exc:
            await _finish_agent_service_recovery_failed_operation(
                self,
                operation,
                exc=exc,
                error_message=str(exc),
                extra_result={
                    "thread_count": len(action.threads),
                    "review_comment_count": len(action.review_comments),
                },
            )
            raise
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
            state.clear_awaiting_workflow_scope()
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
            state.clear_awaiting_workflow_scope()
            await self._finish_provider_auth_failed_operation(operation)
            raise
        except ComposeExecCleanupError as exc:
            state.clear_awaiting_workflow_scope()
            cleanup_terminal_result = await _finish_if_pr_terminal_after_cleanup_error(
                operation,
                context="comment_repair_cleanup_failure",
                operation_type=OperationType.comment_repair.value,
            )
            if cleanup_terminal_result is not None:
                return cleanup_terminal_result
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
        pr_terminal_result = await _finish_if_pr_terminal(operation, push_result)
        if pr_terminal_result is not None:
            return pr_terminal_result
        if push_result.paused_into_blocked:
            # A protected-scope violation paused the workspace into ``blocked``
            # for an operator decision (WS-2). The row already left
            # ``monitoring_pr`` (preserving the offending commit); end the monitor
            # cycle cleanly — do NOT terminally fail. Persist state so the
            # notification dedupe + preserved-commit marker survive a restart.
            state.clear_awaiting_workflow_scope()
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
                # ``awaiting_workflow_scope`` so the next poll's attention clear and
                # unpublished-repair abandon guards stay armed across requeues.
                state.mark_awaiting_workflow_scope()
                await self._set_workspace_attention(
                    workspace_id,
                    reason=push_result.error_message or push_result.reason_code,
                )
                # Unlike the two arms above, this one does not consume the helper's
                # terminal observation: it never terminally fails on a missing
                # workflow scope, so a PR that ended mid-push reaches its terminal
                # handling through the next poll's ``decide()`` short-circuit rather
                # than through a wrong ``failed`` write (PRRT_kwDOSJAM6s6fvGsp).
                await _post_workflow_scope_notification_best_effort(
                    self,
                    workspace_id=workspace_id,
                    repo=repo,
                    pr_number=pr_number,
                    status=status,
                    state=state,
                    blocker_reason=push_result.error_message or push_result.reason_code,
                )
            else:
                state.clear_awaiting_workflow_scope()
            if push_result.terminal_monitor_failure:
                state.clear_awaiting_workflow_scope()
                await self._terminate_failed(
                    workspace_id,
                    message=push_result.error_message or push_result.reason_code,
                    reason_code=push_result.reason_code,
                    details=push_result.failure_evidence(),
                    failure_reason=push_result.failure_reason,
                )
                return True
            state.iter_count += 1
            return False
        state.clear_awaiting_workflow_scope()
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
        except _MonitorAgentServiceRecoverySupersededError as exc:
            await _finish_agent_service_recovery_superseded_operation(
                self,
                operation,
                exc=exc,
                error_message=str(exc),
            )
            raise
        except _MonitorAgentServiceRecoveryFailedError as exc:
            await _finish_agent_service_recovery_failed_operation(
                self,
                operation,
                exc=exc,
                error_message=str(exc),
            )
            raise
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
            cleanup_terminal_result = await _finish_if_pr_terminal_after_cleanup_error(
                operation,
                context="operator_hint_cleanup_failure",
                operation_type=OperationType.comment_repair.value,
            )
            if cleanup_terminal_result is not None:
                return cleanup_terminal_result
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
        pr_terminal_result = await _finish_if_pr_terminal(operation, push_result)
        if pr_terminal_result is not None:
            return pr_terminal_result
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
                    failure_reason=push_result.failure_reason,
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
