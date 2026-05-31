"""Merge action branch for the PR monitor decision loop."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, cast

from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.enums import (
    OperationStatus,
    OperationType,
)
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.pr_monitor import (
    Merge,
    MonitorAction,
    MonitorState,
    NotifyHuman,
    PRStatus,
    decide,
)
from awf.runtime.pr_monitor_operations import MonitorOperationHandle
from awf.runtime.pr_monitor_runner.constants import (
    _AUDIT_MERGE_ATTEMPT_EVENT,
    _AUDIT_MERGE_RESULT_EVENT,
    _GIT_BASE_BEHIND_FAILED_REASON,
)
from awf.runtime.pr_monitor_runner.gates import (
    _MergeGateResult,
    _NonCheckReviewerSettleDecision,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _clear_transient_base_fetch_retry_state,
    _gate_requires_validation_recovery,
    _initial_review_grace_wait_seconds,
    _merge_gate_blocks,
    _merge_rejection_reason,
    _non_check_reviewer_settle_decision,
    _non_check_reviewer_settle_wait_operation_context,
    _pending_review_feedback_count,
    _redact_and_truncate_github_error,
)
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.types import (
    BaseBehindCountError,
    BaseFetchError,
)
from awf.service.merge_queue import MergeQueueBlocker


async def handle_merge_action(
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
    remote_push_url: str | None,
    compose_project: str,
    compose_file: Path,
    monitor_log: WorkspaceLogSink | None,
) -> bool | None:
    if isinstance(action, Merge):
        merge_gate = await self._merge_gate_with_legacy_head_support(
            workspace_id,
            current_head_sha=status.head_sha,
        )
        pending_validation_gate = (
            merge_gate if _gate_requires_validation_recovery(merge_gate) else None
        )
        if pending_validation_gate is None:
            handled = await self._handle_merge_gate_blocker(
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
            )
            if handled is not None:
                return cast(bool | None, handled)
        elif await self._recovery_dispatch_status_is_stale(workspace_id):
            return True

        policy_blocked = await self._refresh_scope_policy_for_merge(
            workspace_id=workspace_id,
            changed_paths=status.changed_paths,
        )
        if policy_blocked:
            return cast(
                bool | None,
                await self._execute(
                    action=NotifyHuman(
                        message=(
                            "OUT_OF_SCOPE_CHANGE: changed files outside declared "
                            "owned_paths require an operator scope decision."
                        )
                    ),
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
                ),
            )

        merge_gate = await self._merge_gate_with_legacy_head_support(
            workspace_id,
            check_policy=True,
            current_head_sha=status.head_sha,
        )
        pending_validation_gate = (
            merge_gate if _gate_requires_validation_recovery(merge_gate) else None
        )
        if pending_validation_gate is None:
            handled = await self._handle_merge_gate_blocker(
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
            )
            if handled is not None:
                return cast(bool | None, handled)

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

        settle_decision = _non_check_reviewer_settle_decision(
            status,
            state,
            self._config,
            pr_number=pr_number,
            now=time.monotonic(),
        )
        await self._record_non_check_reviewer_settle_decision(
            decision=settle_decision,
            workspace_id=workspace_id,
            pr_number=pr_number,
            status=status,
            monitor_log=monitor_log,
        )
        if settle_decision.wait_seconds > 0:
            requested_action = "validate" if pending_validation_gate is not None else "merge"
            settle_operation_context = _non_check_reviewer_settle_wait_operation_context(
                self._config,
                settle_decision,
            )
            await self._sleep_with_monitor_state_operation(
                workspace_id=workspace_id,
                action="reviewer_settle_wait",
                requested_action=requested_action,
                reason=(
                    "Waiting for configured non-check reviewers to settle before final validation."
                    if pending_validation_gate is not None
                    else "Waiting for configured non-check reviewers to settle."
                ),
                reason_code="NON_CHECK_REVIEWER_SETTLE",
                pr_number=pr_number,
                status=status,
                base_branch=base_branch,
                remote_branch=remote_branch,
                wait_seconds=settle_decision.wait_seconds,
                monitor_log=monitor_log,
                extra_payload=settle_operation_context.extra_payload,
                extra_identity=settle_operation_context.extra_identity,
            )
            return False

        if pending_validation_gate is not None:
            handled = await self._handle_merge_gate_blocker(
                gate=pending_validation_gate,
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
                skip_initial_review_grace=(self._config.non_check_reviewer_settle_seconds > 0),
            )
            if handled is not None:
                return cast(bool | None, handled)

        await self._record_monitor_state_operation(
            workspace_id=workspace_id,
            action="merge_ready",
            requested_action="merge",
            reason="Comments, checks, freshness, policy, and queue gates are clean.",
            reason_code="MERGE_READY",
            pr_number=pr_number,
            status=status,
            base_branch=base_branch,
            remote_branch=remote_branch,
            result={"status": "succeeded", "outcome": "ready_to_merge"},
            monitor_log=monitor_log,
        )

        await self._record_merge_coordination_event(
            "monitor.merge_critical_section_waiting",
            monitor_log=monitor_log,
            workspace_id=workspace_id,
            repo_url=repo_url,
            base_branch=base_branch,
            pr_number=pr_number,
            status=status,
        )
        fresh_action: MonitorAction | None = None
        fresh_status: PRStatus | None = None
        merge_sha: str | None = None
        merge_blocker: GitHubClientError | None = None
        merge_operation: MonitorOperationHandle | None = None
        recheck_error: GitHubClientError | None = None
        recheck_base_error: BaseFetchError | None = None
        recheck_behind_error: BaseBehindCountError | None = None
        merge_status = status
        queue_blockers_after_lock: list[MergeQueueBlocker] = []
        merge_gate_after_lock: _MergeGateResult | None = None
        settle_recheck_decision: _NonCheckReviewerSettleDecision | None = None
        settle_recheck_performed = False
        initial_grace_recheck_wait_seconds = 0.0
        operator_state_refreshed = False
        pre_merge_status_refreshed = False

        async def _refresh_operator_state_for_merge(*, event_name: str) -> bool:
            nonlocal fresh_action, fresh_status, operator_state_refreshed
            if fresh_action is not None:
                return False
            changed = await self._refresh_operator_state_from_workspace(
                workspace_id,
                state,
            )
            if not changed:
                return False
            operator_state_refreshed = True
            checked_action = decide(merge_status, state, self._config)
            if not isinstance(checked_action, Merge):
                fresh_action = checked_action
                fresh_status = merge_status
                _log.info(
                    event_name,
                    workspace_id=workspace_id,
                    pr_number=pr_number,
                    original_action="Merge",
                    fresh_action=type(checked_action).__name__,
                    head_sha=merge_status.head_sha[:10],
                )
            return True

        async def _recheck_non_check_reviewer_settle() -> None:
            nonlocal settle_recheck_decision, settle_recheck_performed
            settle_recheck_performed = True
            checked_settle_decision = _non_check_reviewer_settle_decision(
                merge_status,
                state,
                self._config,
                pr_number=pr_number,
                now=time.monotonic(),
            )
            await self._record_non_check_reviewer_settle_decision(
                decision=checked_settle_decision,
                workspace_id=workspace_id,
                pr_number=pr_number,
                status=merge_status,
                monitor_log=monitor_log,
            )
            if checked_settle_decision.wait_seconds > 0:
                settle_recheck_decision = checked_settle_decision

        async with self._merge_coordinator.serialized_merge(
            repo_url=repo_url,
            base_branch=base_branch,
        ):
            await self._record_merge_coordination_event(
                "monitor.merge_critical_section_entered",
                monitor_log=monitor_log,
                workspace_id=workspace_id,
                repo_url=repo_url,
                base_branch=base_branch,
                pr_number=pr_number,
                status=merge_status,
            )
            if self._config.pre_merge_settle_seconds > 0:
                wait_seconds = self._config.pre_merge_settle_seconds
                await self._record_pre_merge_settle_event(
                    "monitor.pre_merge_settle_started",
                    monitor_log=monitor_log,
                    workspace_id=workspace_id,
                    repo_url=repo_url,
                    base_branch=base_branch,
                    pr_number=pr_number,
                    status=merge_status,
                    wait_seconds=wait_seconds,
                )
                settle_started_at = time.monotonic()
                await self._deps.sleep(wait_seconds)
                await self._record_pre_merge_settle_event(
                    "monitor.pre_merge_settle_completed",
                    monitor_log=monitor_log,
                    workspace_id=workspace_id,
                    repo_url=repo_url,
                    base_branch=base_branch,
                    pr_number=pr_number,
                    status=merge_status,
                    wait_seconds=wait_seconds,
                    elapsed_seconds=max(time.monotonic() - settle_started_at, 0.0),
                )
                try:
                    checked_status = await self._fetch_status_for_decision(
                        repo=repo,
                        pr_number=pr_number,
                        workspace_id=workspace_id,
                        base_branch=base_branch,
                    )
                except GitHubClientError as exc:
                    recheck_error = exc
                except BaseFetchError as exc:
                    recheck_base_error = exc
                except BaseBehindCountError as exc:
                    recheck_behind_error = exc
                else:
                    pre_merge_state_changed = _clear_transient_base_fetch_retry_state(
                        state,
                        context="pre_merge_recheck",
                    )
                    if await self._refresh_pr_feedback_resolution_state(
                        workspace_id=workspace_id,
                        repo=repo,
                        pr_number=pr_number,
                        status=checked_status,
                        state=state,
                    ):
                        pre_merge_state_changed = True
                    if pre_merge_state_changed:
                        await self._persist_state(workspace_id, state)
                    checked_action = decide(checked_status, state, self._config)
                    if not isinstance(checked_action, Merge):
                        fresh_action = checked_action
                        fresh_status = checked_status
                        review_feedback = len(checked_status.unresolved_review_comments)
                        pending_review_feedback = _pending_review_feedback_count(
                            checked_status, state
                        )
                        unresolved_reviews = pending_review_feedback
                        _log.info(
                            "monitor.pre_merge_recheck_changed_action",
                            workspace_id=workspace_id,
                            pr_number=pr_number,
                            original_action="Merge",
                            fresh_action=type(checked_action).__name__,
                            head_sha=checked_status.head_sha[:10],
                            unresolved_threads=len(checked_status.unresolved_inline_threads),
                            # Historical field name, now state-filtered to avoid
                            # reporting retained handled feedback as unresolved.
                            unresolved_reviews=unresolved_reviews,
                            review_feedback=review_feedback,
                            pending_review_feedback=pending_review_feedback,
                            blocking_reviews=len(checked_status.blocking_reviews),
                            check_state=checked_status.check_state.value,
                            merge_state=(
                                checked_status.merge_state_status.value
                                if checked_status.merge_state_status
                                else None
                            ),
                        )
                    else:
                        merge_status = checked_status
                        pre_merge_status_refreshed = True

            await _refresh_operator_state_for_merge(
                event_name="monitor.merge_operator_hint_recheck_changed_action"
            )

            if (
                recheck_error is None
                and recheck_base_error is None
                and recheck_behind_error is None
                and fresh_action is None
            ):
                initial_grace_recheck_wait_seconds = _initial_review_grace_wait_seconds(
                    state,
                    pr_number=pr_number,
                    now=time.monotonic(),
                    grace_seconds=self._config.initial_review_grace_period_seconds,
                    poll_interval_seconds=self._config.poll_interval_seconds,
                )

            if (
                recheck_error is None
                and recheck_base_error is None
                and recheck_behind_error is None
                and fresh_action is None
                and initial_grace_recheck_wait_seconds <= 0
                and (pre_merge_status_refreshed or operator_state_refreshed)
            ):
                await _recheck_non_check_reviewer_settle()

            if (
                recheck_error is None
                and recheck_base_error is None
                and recheck_behind_error is None
                and fresh_action is None
                and settle_recheck_decision is None
                and initial_grace_recheck_wait_seconds <= 0
            ):
                queue_blockers_after_lock = await self._merge_queue_blockers_for_workspace(
                    workspace_id
                )
                if not queue_blockers_after_lock:
                    merge_gate_after_lock = await self._merge_gate_with_legacy_head_support(
                        workspace_id,
                        check_policy=True,
                        current_head_sha=merge_status.head_sha,
                    )
                if (
                    not queue_blockers_after_lock
                    and merge_gate_after_lock is not None
                    and not _merge_gate_blocks(merge_gate_after_lock)
                ):
                    final_operator_state_refreshed = await _refresh_operator_state_for_merge(
                        event_name="monitor.merge_operator_hint_final_recheck_changed_action"
                    )
                    needs_settle_recheck = final_operator_state_refreshed or (
                        pre_merge_status_refreshed and not settle_recheck_performed
                    )
                    if needs_settle_recheck and fresh_action is None:
                        initial_grace_recheck_wait_seconds = _initial_review_grace_wait_seconds(
                            state,
                            pr_number=pr_number,
                            now=time.monotonic(),
                            grace_seconds=self._config.initial_review_grace_period_seconds,
                            poll_interval_seconds=self._config.poll_interval_seconds,
                        )
                        if initial_grace_recheck_wait_seconds <= 0:
                            await _recheck_non_check_reviewer_settle()
                    if (
                        fresh_action is None
                        and settle_recheck_decision is None
                        and initial_grace_recheck_wait_seconds <= 0
                    ):
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
                            operation_id=(
                                merge_operation.operation_id
                                if merge_operation is not None
                                else None
                            ),
                            operation_type=OperationType.monitor_state.value,
                            monitor_log=monitor_log,
                        )
                        try:
                            merge_sha = await self._deps.gh.merge_pr(
                                repo=repo,
                                pr_number=pr_number,
                            )
                        except GitHubClientError as exc:
                            merge_blocker = exc
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
                                operation_id=(
                                    merge_operation.operation_id
                                    if merge_operation is not None
                                    else None
                                ),
                                operation_type=OperationType.monitor_state.value,
                                monitor_log=monitor_log,
                                evidence={
                                    "operation": "merge_pr",
                                    "error_message": str(exc),
                                },
                            )
                        else:
                            await self._finish_monitor_operation(
                                merge_operation,
                                status=OperationStatus.succeeded,
                                result={
                                    "status": "succeeded",
                                    "outcome": "merged",
                                    "merge_sha": merge_sha,
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
                                operation_id=(
                                    merge_operation.operation_id
                                    if merge_operation is not None
                                    else None
                                ),
                                operation_type=OperationType.monitor_state.value,
                                monitor_log=monitor_log,
                                evidence={"merge_sha": merge_sha},
                            )

        if initial_grace_recheck_wait_seconds > 0:
            _log.info(
                "monitor.initial_review_grace_waiting",
                workspace_id=workspace_id,
                pr_number=pr_number,
                wait_seconds=initial_grace_recheck_wait_seconds,
                grace_seconds=self._config.initial_review_grace_period_seconds,
                head_sha=merge_status.head_sha[:10],
            )
            await self._sleep_with_monitor_state_operation(
                workspace_id=workspace_id,
                action="grace_wait",
                requested_action="merge",
                reason="Initial review grace period is still active.",
                reason_code="INITIAL_REVIEW_GRACE",
                pr_number=pr_number,
                status=merge_status,
                base_branch=base_branch,
                remote_branch=remote_branch,
                wait_seconds=initial_grace_recheck_wait_seconds,
                monitor_log=monitor_log,
                extra_payload={
                    "grace_seconds": self._config.initial_review_grace_period_seconds,
                    "req_action": None,
                    "stale_reason": None,
                },
                extra_identity=(None, None),
            )
            return False

        if settle_recheck_decision is not None:
            settle_operation_context = _non_check_reviewer_settle_wait_operation_context(
                self._config,
                settle_recheck_decision,
            )
            await self._sleep_with_monitor_state_operation(
                workspace_id=workspace_id,
                action="reviewer_settle_wait",
                requested_action="merge",
                reason="Waiting for configured non-check reviewers to settle.",
                reason_code="NON_CHECK_REVIEWER_SETTLE",
                pr_number=pr_number,
                status=merge_status,
                base_branch=base_branch,
                remote_branch=remote_branch,
                wait_seconds=settle_recheck_decision.wait_seconds,
                monitor_log=monitor_log,
                extra_payload=settle_operation_context.extra_payload,
                extra_identity=settle_operation_context.extra_identity,
            )
            return False

        if queue_blockers_after_lock:
            await self._wait_for_merge_queue(
                blockers=queue_blockers_after_lock,
                workspace_id=workspace_id,
                repo_url=repo_url,
                base_branch=base_branch,
                pr_number=pr_number,
                status=merge_status,
                state=state,
                monitor_log=monitor_log,
            )
            return False

        if merge_gate_after_lock is not None and _merge_gate_blocks(merge_gate_after_lock):
            handled = await self._handle_merge_gate_blocker(
                gate=merge_gate_after_lock,
                workspace_id=workspace_id,
                repo_url=repo_url,
                repo=repo,
                pr_number=pr_number,
                status=merge_status,
                state=state,
                base_branch=base_branch,
                remote_branch=remote_branch,
                compose_project=compose_project,
                compose_file=compose_file,
                monitor_log=monitor_log,
            )
            if handled is None:  # pragma: no cover - defensive invariant
                raise RuntimeError("merge gate blocker was not handled")
            return cast(bool | None, handled)

        if fresh_action is not None:
            if fresh_status is None:  # pragma: no cover - defensive invariant
                raise RuntimeError("pre-merge recheck produced an action without status")
            # Re-enter the dispatcher for refreshed non-merge actions before
            # converting a simultaneous pre-merge recheck failure into retry or
            # terminal workspace state. Non-Merge actions do not perform this
            # pre-merge recheck, so decision oscillation remains bounded by the
            # outer monitor loop.
            return cast(
                bool | None,
                await self._execute(
                    action=fresh_action,
                    workspace_id=workspace_id,
                    repo_url=repo_url,
                    repo=repo,
                    pr_number=pr_number,
                    status=fresh_status,
                    state=state,
                    base_branch=base_branch,
                    remote_branch=remote_branch,
                    remote_push_url=remote_push_url,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    monitor_log=monitor_log,
                ),
            )

        if recheck_base_error is not None:
            base_fetch_result = await self._wait_after_transient_base_fetch_error(
                recheck_base_error,
                workspace_id=workspace_id,
                pr_number=pr_number,
                context="pre_merge_recheck",
                state=state,
                monitor_log=monitor_log,
            )
            if base_fetch_result.retry:
                return False
            await self._terminate_failed(
                workspace_id,
                message=(
                    f"monitor: could not refresh base branch during pre-merge recheck: "
                    f"{recheck_base_error}"
                )[:2000],
                reason_code=base_fetch_result.reason_code,
            )
            return True

        if recheck_behind_error is not None:
            await self._terminate_failed(
                workspace_id,
                message=(
                    "monitor: could not calculate base-behind count during "
                    f"pre-merge recheck: {recheck_behind_error}"
                )[:2000],
                reason_code=_GIT_BASE_BEHIND_FAILED_REASON,
            )
            return True

        if recheck_error is not None:
            if await self._wait_after_transient_github_error(
                recheck_error,
                workspace_id=workspace_id,
                pr_number=pr_number,
                context="pre_merge_recheck",
                monitor_log=monitor_log,
            ):
                return False
            await self._terminate_failed(
                workspace_id,
                message=(f"monitor: github error during pre-merge recheck: {recheck_error}")[:2000],
            )
            return True

        if merge_blocker is not None:
            if await self._wait_after_transient_github_error(
                merge_blocker,
                workspace_id=workspace_id,
                pr_number=pr_number,
                context="merge_pr",
                monitor_log=monitor_log,
            ):
                return False
            # Branch protection often blocks merges; fall back to the
            # release-PR flow rather than failing.
            _log.warning(
                "monitor.merge_blocked_falling_back_to_notify",
                workspace_id=workspace_id,
                stderr=_redact_and_truncate_github_error(merge_blocker.stderr),
            )
            await self._post_human_notification_once(
                repo=repo,
                pr_number=pr_number,
                status=merge_status,
                state=state,
                blocker_reason=_merge_rejection_reason(merge_blocker.stderr),
            )
            await self._deps.sleep(self._config.poll_interval_seconds)
            return False

        if merge_sha is None:  # pragma: no cover - defensive invariant
            raise RuntimeError("merge critical section exited without a merge result")
        self._write_defer_signal(
            workspace_id=workspace_id,
            pr_number=pr_number,
            terminal_action="Merge",
            merged=True,
            status=merge_status,
            state=state,
        )
        await self._record_monitor_state_operation(
            workspace_id=workspace_id,
            action="completed",
            requested_action="complete",
            reason="PR monitor completed after merging the PR.",
            reason_code="MERGE_COMPLETED",
            pr_number=pr_number,
            status=merge_status,
            base_branch=base_branch,
            remote_branch=remote_branch,
            result={
                "status": "succeeded",
                "outcome": "merged",
                "merge_sha": merge_sha,
            },
            monitor_log=monitor_log,
            extra_identity=("merge", merge_sha),
        )
        await self._terminate_completed(
            workspace_id,
            pr_merge_sha=merge_sha,
            repo_url=repo_url,
            base_branch=base_branch,
            compose_project=compose_project,
            compose_file=compose_file,
        )
        return True

    return None
