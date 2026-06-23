"""Merge action branch for the PR monitor decision loop."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from awf.common.bitbucket_client import BitbucketClientError
from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import GitHubClientError, RepoRef
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.monitor_state_keys import _merge_method_blocked_key
from awf.runtime.pr_monitor import (
    _MERGE_BLOCK_ATTENTION_STATE_KEY,
    Merge,
    MonitorAction,
    MonitorState,
    NotifyHuman,
    PRStatus,
    decide,
)
from awf.runtime.pr_monitor_runner.constants import (
    _GIT_BASE_BEHIND_FAILED_REASON,
)
from awf.runtime.pr_monitor_runner.gates import (
    _MergeGateResult,
    _NonCheckReviewerSettleDecision,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _awaiting_required_checks_grace,
    _bitbucket_merge_rejection_reason,
    _clear_transient_base_fetch_retry_state,
    _clear_transient_forge_retry_state,
    _gate_requires_validation_recovery,
    _initial_review_grace_wait_seconds,
    _is_transient_bitbucket_client_error,
    _is_transient_github_client_error,
    _merge_gate_blocks,
    _merge_rejection_reason,
    _non_check_reviewer_settle_decision,
    _non_check_reviewer_settle_wait_operation_context,
    _pending_review_feedback_count,
    _redact_and_truncate_forge_error,
)
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.merge_attempts import (
    _attempt_merge_method,
    _MergeAttemptOutcome,
    _record_empty_effective_merge_methods_blocker,
)
from awf.runtime.pr_monitor_runner.merge_attention import (
    _merge_block_attention_queue_verdict,
)
from awf.runtime.pr_monitor_runner.merge_methods import (
    _merge_method_mismatch_message,
    _merge_method_preflight_rejection_reason,
    _resolve_effective_merge_methods,
)
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
    """Execute a merge action and report whether monitor processing reached a terminal state."""
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

        async def _queue_wait_merge_attention_status(current_status: PRStatus) -> PRStatus | None:
            if not state.threads_addressed_ids.get(_MERGE_BLOCK_ATTENTION_STATE_KEY):
                return current_status
            if _merge_block_attention_queue_verdict(current_status) != "indeterminate":
                return current_status
            try:
                return cast(
                    PRStatus,
                    await self._fetch_status_for_decision(
                        repo=repo,
                        pr_number=pr_number,
                        workspace_id=workspace_id,
                        base_branch=base_branch,
                    ),
                )
            except (ForgeClientError, BaseFetchError, BaseBehindCountError):
                return None

        if queue_blockers:
            await self._clear_or_preserve_merge_attention_for_queue_wait(
                workspace_id,
                state,
                status=await _queue_wait_merge_attention_status(status),
            )
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
            await self._clear_or_preserve_merge_attention_for_queue_wait(
                workspace_id,
                state,
                status=await _queue_wait_merge_attention_status(status),
            )
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
        # Capture the wall-clock at coordinator ENTRY. The serialized merge
        # coordinator can block behind another merge longer than the merge-block TTL
        # with no fallback firing (no poll runs during the wait); judging the
        # marker's age against the post-wait clock would reclassify a FRESH-at-entry
        # marker as STALE and clear ``awaiting_human_since`` — flickering the
        # human-wait timer though the block never resolved
        # (PRRT_kwDOSJAM6s6La_SZ). Use the entry timestamp so a fresh-at-entry
        # marker is preserved; a stale-at-entry marker is still cleared.
        merge_critical_section_entered_at = datetime.now(UTC)
        # #661: clear a resolved ``NotifyHuman`` flag BEFORE the non-human merge-
        # lock wait so it does not stay "awaiting human" once ``decide()`` resumes
        # to ``Merge``. The entry timestamp (above) preserves a FRESH marker and
        # clears a STALE (resolved between polls) one (#663).
        await self._clear_stale_merge_attention(
            workspace_id,
            state,
            now=merge_critical_section_entered_at,
        )
        fresh_action: MonitorAction | None = None
        fresh_status: PRStatus | None = None
        merge_sha: str | None = None
        # Keep the explicit two-member union (not the ``ForgeClientError`` base): the
        # ``else`` arm below narrows ``not isinstance(..., BitbucketClientError)`` to
        # ``GitHubClientError`` to read ``.stderr``. Collapsing to the base drops that
        # narrowing and fails type-checking.
        merge_blocker: GitHubClientError | BitbucketClientError | None = None
        merge_method_preflight_error: GitHubClientError | None = None
        merge_method_notification_reason: str | None = None
        recheck_forge_error: ForgeClientError | None = None
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
                except ForgeClientError as exc:
                    # Both forges poll status through ``self._deps.gh``; either
                    # raises a ``ForgeClientError`` subclass on API/transport fault.
                    # Capture it here so the post-lock handling can retry transient
                    # blips instead of letting the error escape the merge critical
                    # section uncaught.
                    recheck_forge_error = exc
                except BaseFetchError as exc:
                    recheck_base_error = exc
                except BaseBehindCountError as exc:
                    recheck_behind_error = exc
                else:
                    pre_merge_state_changed = _clear_transient_base_fetch_retry_state(
                        state,
                        context="pre_merge_recheck",
                    )
                    if _clear_transient_forge_retry_state(
                        state,
                        context="pre_merge_recheck",
                    ):
                        pre_merge_state_changed = True
                    if await self._refresh_pr_feedback_resolution_state(
                        workspace_id=workspace_id,
                        repo=repo,
                        pr_number=pr_number,
                        status=checked_status,
                        state=state,
                    ):
                        pre_merge_state_changed = True
                    # #656: decorate the freshly fetched status with the same
                    # per-head required-CI-start grace the outer loop applies
                    # before ``decide`` (runner.py). This recheck fetches a NEW
                    # status that can show the transient empty-checks + BLOCKED
                    # CI-start gap (the head changed during the settle window, or
                    # GitHub recomputed mergeability); without the decoration
                    # ``decide`` sees the default
                    # ``awaiting_required_checks_grace_active=False``, gate 8b is
                    # skipped, and the monitor pages a human prematurely before
                    # the next outer poll can recover. Persisting the first-seen
                    # marker is mandatory for the same reason as in the outer
                    # loop: the runner reloads ``state`` from the DB every poll,
                    # so an unpersisted marker would re-read as absent forever and
                    # a genuine never-CI head would never escalate past the grace.
                    grace_active, grace_state_changed = _awaiting_required_checks_grace(
                        checked_status, state, self._config, now=datetime.now(UTC)
                    )
                    if grace_state_changed:
                        pre_merge_state_changed = True
                    checked_status = replace(
                        checked_status,
                        awaiting_required_checks_grace_active=grace_active,
                    )
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
                recheck_forge_error is None
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
                recheck_forge_error is None
                and recheck_base_error is None
                and recheck_behind_error is None
                and fresh_action is None
                and initial_grace_recheck_wait_seconds <= 0
                and (pre_merge_status_refreshed or operator_state_refreshed)
            ):
                await _recheck_non_check_reviewer_settle()

            if (
                recheck_forge_error is None
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
                        last_chance_operator_state_refreshed = (
                            await _refresh_operator_state_for_merge(
                                event_name=(
                                    "monitor.merge_operator_hint_last_chance_changed_action"
                                )
                            )
                        )
                        if last_chance_operator_state_refreshed and fresh_action is None:
                            initial_grace_recheck_wait_seconds = _initial_review_grace_wait_seconds(
                                state,
                                pr_number=pr_number,
                                now=time.monotonic(),
                                grace_seconds=(self._config.initial_review_grace_period_seconds),
                                poll_interval_seconds=(self._config.poll_interval_seconds),
                            )
                            if initial_grace_recheck_wait_seconds <= 0:
                                await _recheck_non_check_reviewer_settle()
                    if (
                        fresh_action is None
                        and settle_recheck_decision is None
                        and initial_grace_recheck_wait_seconds <= 0
                    ):
                        try:
                            effective_methods = await _resolve_effective_merge_methods(
                                self,
                                repo=repo,
                                base_branch=base_branch,
                            )
                        # Intentionally narrow to ``GitHubClientError`` (not the shared
                        # ``ForgeClientError`` base): ``_resolve_effective_merge_methods``
                        # only reaches the GitHub merge-method endpoints, while
                        # ``BitbucketClient.fetch_repo_merge_methods`` /
                        # ``fetch_branch_pull_request_allowed_merge_methods`` serve from
                        # cached ``_pr_context`` and never make HTTP calls, so no
                        # ``BitbucketClientError`` can surface here today. Load-bearing
                        # invariant: if a Bitbucket method is ever changed to make a live
                        # request, widen this to ``ForgeClientError`` so the fault still
                        # routes through ``merge_method_preflight_error`` instead of
                        # escaping ``handle_merge_action`` uncaught.
                        except GitHubClientError as exc:
                            merge_method_preflight_error = exc
                        else:
                            if not effective_methods:
                                merge_method_notification_reason = _merge_method_mismatch_message(
                                    base_branch=base_branch,
                                    attempted_method=None,
                                    effective_methods=effective_methods,
                                )
                                await _record_empty_effective_merge_methods_blocker(
                                    self,
                                    workspace_id=workspace_id,
                                    pr_number=pr_number,
                                    merge_status=merge_status,
                                    base_branch=base_branch,
                                    remote_branch=remote_branch,
                                    monitor_log=monitor_log,
                                    notification_reason=merge_method_notification_reason,
                                )
                                state.mark_addressed(
                                    _merge_method_blocked_key(
                                        pr_number=merge_status.number,
                                        head_sha=merge_status.head_sha,
                                    ),
                                    merge_method_notification_reason,
                                )
                                await self._persist_state(workspace_id, state)
                            else:
                                for attempt_index, merge_method in enumerate(effective_methods):
                                    merge_attempt = await _attempt_merge_method(
                                        self,
                                        workspace_id=workspace_id,
                                        repo=repo,
                                        pr_number=pr_number,
                                        merge_method=merge_method,
                                        merge_status=merge_status,
                                        base_branch=base_branch,
                                        remote_branch=remote_branch,
                                        monitor_log=monitor_log,
                                        state=state,
                                        effective_methods=effective_methods,
                                        attempt_index=attempt_index,
                                    )
                                    if merge_attempt.outcome is _MergeAttemptOutcome.SUCCESS:
                                        merge_sha = merge_attempt.merge_sha
                                        break
                                    if (
                                        merge_attempt.outcome
                                        is _MergeAttemptOutcome.RETRY_NEXT_METHOD
                                    ):
                                        continue
                                    if merge_attempt.outcome is _MergeAttemptOutcome.METHOD_BLOCKER:
                                        merge_method_notification_reason = (
                                            merge_attempt.method_blocker_notification_reason
                                        )
                                        break
                                    merge_blocker = merge_attempt.blocker
                                    if merge_blocker is None:  # pragma: no cover
                                        raise RuntimeError(
                                            "merge attempt blocker outcome without blocker"
                                        )
                                    break

        if initial_grace_recheck_wait_seconds > 0:
            await self._clear_or_preserve_merge_attention_for_queue_wait(
                workspace_id,
                state,
                status=await _queue_wait_merge_attention_status(merge_status),
            )
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
            await self._clear_or_preserve_merge_attention_for_queue_wait(
                workspace_id,
                state,
                status=await _queue_wait_merge_attention_status(merge_status),
            )
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
            await self._clear_or_preserve_merge_attention_for_queue_wait(
                workspace_id,
                state,
                status=await _queue_wait_merge_attention_status(merge_status),
            )
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

        if recheck_forge_error is not None:
            # A transient blip on either forge (rate-limit/transport/5xx) waits and
            # keeps polling; only a deterministic fault terminates the workspace.
            # Mirror the ``fetch_pr_status``/``_execute`` catches in ``runner.py``:
            # forward the forge's ``reason_code`` for both forges so the same GitHub
            # fault class persists ``GITHUB_API_ERROR`` here as it does there, rather
            # than this branch decaying GitHub to the ``MONITOR_ABORT`` default while
            # the other paths preserve it. Bitbucket carries its specific code.
            if await self._wait_after_transient_forge_error(
                recheck_forge_error,
                workspace_id=workspace_id,
                pr_number=pr_number,
                context="pre_merge_recheck",
                state=state,
                monitor_log=monitor_log,
            ):
                return False
            forge_label = (
                "bitbucket" if isinstance(recheck_forge_error, BitbucketClientError) else "github"
            )
            await self._terminate_failed(
                workspace_id,
                message=(
                    f"monitor: {forge_label} error during pre-merge recheck: {recheck_forge_error}"
                )[:2000],
                reason_code=recheck_forge_error.reason_code,
            )
            return True

        if merge_method_preflight_error is not None:
            if await self._wait_after_transient_github_error(
                merge_method_preflight_error,
                workspace_id=workspace_id,
                pr_number=pr_number,
                context="merge_method_preflight",
                state=state,
                monitor_log=monitor_log,
            ):
                return False
            # Reaching here, the wait helper returned False for one of two reasons:
            # a *deterministic* preflight fault (a definitive merge-method policy
            # rejection) or a still-*transient* blip (e.g. HTTP 502) whose bounded
            # retry budget was just exhausted (an under-budget transient would have
            # re-polled above). Only the deterministic case is a genuine policy
            # rejection. Recording the sticky ``_merge_method_blocked_key`` blocker
            # makes ``pr_monitor.decide`` return ``NotifyHuman`` for this head_sha on
            # every later poll, so labelling an exhausted-transient outage that way
            # would wedge the merge under a false "merge-method rejection" until the
            # commit changes. Instead, keep polling without a blocker so a recovered
            # forge re-runs the preflight next cycle — symmetric with the under-budget
            # retry path (#518 review). The exhausted counter stays persisted (kept by
            # the wait helper) so each later poll fails closed immediately.
            if _is_transient_github_client_error(merge_method_preflight_error):
                await self._deps.sleep(self._config.poll_interval_seconds)
                return False
            _log.warning(
                "monitor.merge_method_preflight_falling_back_to_notify",
                workspace_id=workspace_id,
                pr_number=pr_number,
                base_branch=base_branch,
                stderr=_redact_and_truncate_forge_error(merge_method_preflight_error.stderr),
            )
            notification_reason = _merge_method_preflight_rejection_reason(
                merge_method_preflight_error
            )
            state.mark_addressed(
                _merge_method_blocked_key(
                    pr_number=merge_status.number,
                    head_sha=merge_status.head_sha,
                ),
                notification_reason,
            )
            await self._persist_state(workspace_id, state)
            # Surface the escalation as a first-class attention signal now, in
            # parity with the ``NotifyHuman`` touch-point in ``loop._execute``.
            # This path notifies a human directly without re-entering
            # ``NotifyHuman``, so without this set ``awaiting_human_since`` stays
            # NULL until a later poll's ``decide()`` returns ``NotifyHuman`` off
            # the sticky merge-method blocker just marked above. No
            # ``mark_merge_block_attention`` here (unlike the branch-protection
            # fallback below): the sticky blocker flips ``decide()`` to
            # ``NotifyHuman`` next poll, so the ``Merge``-arm non-human gate
            # waits are never reached for this head and cannot clear it early.
            await self._set_workspace_attention(workspace_id, reason=notification_reason)
            try:
                await self._post_human_notification_once(
                    repo=repo,
                    pr_number=pr_number,
                    status=merge_status,
                    state=state,
                    blocker_reason=notification_reason,
                )
            except ForgeClientError as exc:
                # Both forges post the human notification through ``self._deps.gh``;
                # either raises a ``ForgeClientError`` subclass. A transient blip
                # waits then keeps polling; a permanent fault re-raises. Catching the
                # shared base keeps a Bitbucket comment failure during the
                # merge-method-preflight rejection notification from escaping
                # uncaught instead of being retried or surfaced.
                if await self._wait_after_transient_forge_error(
                    exc,
                    workspace_id=workspace_id,
                    pr_number=pr_number,
                    context="post_human_notification",
                    state=state,
                    monitor_log=monitor_log,
                ):
                    return False
                raise
            # The notification posted: clear any stale ``post_human_notification``
            # retry count so a recovered blip never accumulates toward the budget.
            await self._clear_forge_transient_retry_state_on_success(
                workspace_id=workspace_id,
                state=state,
                context="post_human_notification",
            )
            await self._deps.sleep(self._config.poll_interval_seconds)
            return False

        # Past the preflight guard with no captured error: the merge-method preflight
        # succeeded this attempt, so clear any stale ``merge_method_preflight`` retry
        # count to keep a recovered blip from accumulating across merge attempts.
        await self._clear_forge_transient_retry_state_on_success(
            workspace_id=workspace_id,
            state=state,
            context="merge_method_preflight",
        )

        if merge_method_notification_reason is not None:
            _log.warning(
                "monitor.merge_method_mismatch_notify_human",
                workspace_id=workspace_id,
                pr_number=pr_number,
                base_branch=base_branch,
                reason=merge_method_notification_reason,
            )
            return cast(
                bool | None,
                await self._execute(
                    action=NotifyHuman(message=merge_method_notification_reason),
                    workspace_id=workspace_id,
                    repo_url=repo_url,
                    repo=repo,
                    pr_number=pr_number,
                    status=merge_status,
                    state=state,
                    base_branch=base_branch,
                    remote_branch=remote_branch,
                    remote_push_url=remote_push_url,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    monitor_log=monitor_log,
                ),
            )

        if merge_blocker is not None:
            if isinstance(merge_blocker, BitbucketClientError):
                # Recoverable Bitbucket blips (rate limit/transport/5xx) wait and
                # re-poll; a deterministic merge fault falls through to the
                # notify-and-keep-polling path, symmetric to the GitHub arm.
                if await self._wait_after_transient_bitbucket_error(
                    merge_blocker,
                    workspace_id=workspace_id,
                    pr_number=pr_number,
                    context="merge_pr",
                    state=state,
                    monitor_log=monitor_log,
                ):
                    return False
                blocker_detail = _redact_and_truncate_forge_error(str(merge_blocker))
                blocker_reason = _bitbucket_merge_rejection_reason(merge_blocker)
                blocker_is_transient = _is_transient_bitbucket_client_error(merge_blocker)
            else:
                if await self._wait_after_transient_github_error(
                    merge_blocker,
                    workspace_id=workspace_id,
                    pr_number=pr_number,
                    context="merge_pr",
                    state=state,
                    monitor_log=monitor_log,
                ):
                    return False
                blocker_detail = _redact_and_truncate_forge_error(merge_blocker.stderr)
                blocker_reason = _merge_rejection_reason(merge_blocker.stderr)
                blocker_is_transient = _is_transient_github_client_error(merge_blocker)
            # Reaching here means the wait helper returned False for one of two
            # reasons: a *deterministic* blocker (the merge call got a definitive
            # rejection) or a still-*transient* blip whose bounded retry budget was
            # just exhausted (an under-budget transient would have returned True and
            # re-polled above). Only the deterministic case is "incident over", so
            # only then clear the stale ``merge_pr`` retry count to give the next
            # merge attempt a fresh bounded budget. On exhaustion the blocker is still
            # transient, so keep the persisted counter — otherwise each later poll
            # re-spends a full retry budget instead of failing closed via the
            # exhausted path, symmetric with fetch_pr_status / pre_merge_recheck.
            if not blocker_is_transient:
                await self._clear_forge_transient_retry_state_on_success(
                    workspace_id=workspace_id,
                    state=state,
                    context="merge_pr",
                )
            # Branch protection / restrictions often block merges; fall back to the release-PR notify flow rather than failing the workspace.
            _log.warning(
                "monitor.merge_blocked_falling_back_to_notify",
                workspace_id=workspace_id,
                stderr=blocker_detail,
            )
            # Surface the escalation as a first-class attention signal now, in
            # parity with ``loop._execute``'s ``NotifyHuman`` touch-point. This
            # fallback records no sticky blocker (unlike the merge-method preflight
            # arm), so ``decide()`` keeps returning ``Merge`` and never re-enters
            # ``NotifyHuman``; without this set ``awaiting_human_since`` would stay
            # NULL for the whole branch-protection wait.
            #
            # Mark this attention merge-loop-owned and active so later ``Merge``-poll
            # non-human gate waits do NOT clear it as a resolved ``NotifyHuman``
            # episode while the operator is still blocked
            # (PRRT_kwDOSJAM6s6LXscz); a non-``Merge`` ``decide()`` drops the marker.
            #
            # Persist the marker AND set ``awaiting_human_since`` atomically: the
            # prior two-commit ordering closed the marker-missing window
            # (PRRT_kwDOSJAM6s6LbY_X) but opened its reciprocal — a cancel/restart
            # between commits left a FRESH marker with NULL
            # ``awaiting_human_since``, so the next poll preserved the marker without
            # surfacing the escalation (PRRT_kwDOSJAM6s6Lcgk0). One row, one
            # transaction; the outer ``run()`` loop flushes the rest of ``state``
            # after ``_execute`` returns.
            state.mark_merge_block_attention()
            await self._set_workspace_attention_with_merge_block_marker(
                workspace_id,
                state,
                reason=blocker_reason,
            )
            try:
                await self._post_human_notification_once(
                    repo=repo,
                    pr_number=pr_number,
                    status=merge_status,
                    state=state,
                    blocker_reason=blocker_reason,
                )
            except ForgeClientError as exc:
                # Transient blip waits then keeps polling; permanent fault re-raises.
                # Mirrors the merge-method-preflight arm so this fallback shares the
                # same bounded ``post_human_notification`` backoff/counter instead of
                # letting a 502/429 escape uncaught and bypass the budget.
                if await self._wait_after_transient_forge_error(
                    exc,
                    workspace_id=workspace_id,
                    pr_number=pr_number,
                    context="post_human_notification",
                    state=state,
                    monitor_log=monitor_log,
                ):
                    return False
                raise
            # The notification posted: clear any stale ``post_human_notification``
            # retry count so a recovered blip never accumulates toward the budget.
            await self._clear_forge_transient_retry_state_on_success(
                workspace_id=workspace_id,
                state=state,
                context="post_human_notification",
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
