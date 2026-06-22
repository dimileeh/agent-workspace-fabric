"""NotifyHuman action branch for the PR monitor decision loop.

Mechanically extracted from :mod:`awf.runtime.pr_monitor_runner.loop`; behavior
is unchanged. Mirrors the ``merge_loop.handle_merge_action`` delegation pattern:
returns ``None`` when the action is not :class:`NotifyHuman` so the caller can
fall through to its unhandled-action guard.
"""

from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from awf.common.bitbucket_client import BitbucketClientError
from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import RepoRef
from awf.db.enums import (
    OperationStatus,
    OperationType,
)
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.pr_monitor import (
    MonitorState,
    NotifyHuman,
    PRStatus,
)
from awf.runtime.pr_monitor_runner.gates import (
    _NonCheckReviewerSettleDecision,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _gate_requires_validation_recovery,
    _is_manual_ready_handoff,
    _non_check_reviewer_settle_decision,
    _non_check_reviewer_settle_wait_operation_context,
    _notify_human_reason,
)


async def handle_notify_human_action(
    self: Any,
    *,
    action: NotifyHuman,
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
) -> bool:
    """Execute a NotifyHuman action and report whether processing reached a terminal state.

    The caller dispatches on ``isinstance(action, NotifyHuman)`` before invoking
    this helper, so ``action`` is always a :class:`NotifyHuman` here.
    """
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
                # Resolve any stale awaiting-human flag before this non-human gate
                # wait: this resolved manual-ready handoff (auto_merge=false) is
                # only queued behind another candidate, so the operator signal must
                # not stay "awaiting human" while the monitor waits on the merge
                # queue. ``loop._execute`` excludes ``NotifyHuman`` from its general
                # clear, so — like ``handle_merge_action``'s ``Merge`` arm — clear
                # it here before parking (#659).
                await self._clear_workspace_attention(workspace_id)
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
                    # Resolve any stale awaiting-human flag before this non-human
                    # settle wait so a resolved ``NotifyHuman`` episode does not
                    # keep surfacing "awaiting human" while this manual-ready
                    # handoff merely waits on reviewer settle before validation
                    # (#659).
                    await self._clear_workspace_attention(workspace_id)
                    settle_operation_context = _non_check_reviewer_settle_wait_operation_context(
                        settle_config,
                        notify_settle_decision,
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
                    skip_initial_review_grace=(self._config.non_check_reviewer_settle_seconds > 0),
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
                # Resolve any stale awaiting-human flag before this non-human
                # settle wait so a resolved ``NotifyHuman`` episode does not keep
                # surfacing "awaiting human" while this manual-ready handoff merely
                # waits on reviewer settle (#659).
                await self._clear_workspace_attention(workspace_id)
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

    # ``NotifyHuman.message`` is optional and ``_notify_human_reason`` returns
    # ``None`` for a clean manual-ready handoff (green PR, ``auto_merge`` off),
    # so fall back to a sensible default. A ``None`` reason would otherwise be
    # subscripted by ``set_workspace_attention``'s length clamp and raise
    # ``TypeError`` mid-poll, and persist a null ``awaiting_human_reason`` (#659).
    human_wait_reason = (
        action.message
        or _notify_human_reason(status, state)
        or "PR monitor is waiting for human attention."
    )
    operation = await self._begin_monitor_operation(
        workspace_id=workspace_id,
        operation_type=OperationType.human_wait,
        action="human_wait",
        requested_action="notify_human",
        reason=human_wait_reason,
        reason_code="HUMAN_WAIT",
        pr_number=pr_number,
        status=status,
        base_branch=base_branch,
        remote_branch=remote_branch,
        monitor_log=monitor_log,
        extra_identity=(action.message or "", state.iter_count),
    )
    # Surface the escalation as a first-class, operator-visible attention
    # signal on the still-``monitoring_pr`` row. ``since`` stays stable across
    # repeated NotifyHuman for the same block; the reason tracks the latest.
    await self._set_workspace_attention(workspace_id, reason=human_wait_reason)
    try:
        await self._post_human_notification_once(
            repo=repo,
            pr_number=pr_number,
            status=status,
            state=state,
            blocker_reason=action.message,
        )
    except ForgeClientError as exc:
        # Both forges post the human notification through ``self._deps.gh``;
        # either raises a ``ForgeClientError`` subclass. Catching the shared base
        # keeps a Bitbucket fault from escaping ``_execute`` uncaught. A transient
        # blip waits then keeps polling; a permanent fault finishes the operation
        # as failed and re-raises. The operation outcome/error-code strings stay
        # forge-specific (catalogued reason codes) via the selection below.
        is_bitbucket = isinstance(exc, BitbucketClientError)
        transient_outcome = (
            "transient_bitbucket_error" if is_bitbucket else "transient_github_error"
        )
        transient_code = "BITBUCKET_TRANSIENT_ERROR" if is_bitbucket else "GITHUB_TRANSIENT_ERROR"
        failed_outcome = "bitbucket_error" if is_bitbucket else "github_error"
        failed_code = "BITBUCKET_ERROR" if is_bitbucket else "GITHUB_ERROR"
        if await self._wait_after_transient_forge_error(
            exc,
            workspace_id=workspace_id,
            pr_number=pr_number,
            context="post_human_notification",
            state=state,
            monitor_log=monitor_log,
        ):
            await self._finish_monitor_operation(
                operation,
                status=OperationStatus.failed,
                result={
                    "status": "failed",
                    "outcome": transient_outcome,
                    "reason_code": transient_code,
                },
                error_code=transient_code,
                error_message=str(exc),
            )
            return False
        await self._finish_monitor_operation(
            operation,
            status=OperationStatus.failed,
            result={
                "status": "failed",
                "outcome": failed_outcome,
                "reason_code": failed_code,
            },
            error_code=failed_code,
            error_message=str(exc),
        )
        raise
    # The notification posted: clear any stale ``post_human_notification`` retry
    # count so a recovered blip never accumulates toward the bounded budget.
    await self._clear_forge_transient_retry_state_on_success(
        workspace_id=workspace_id,
        state=state,
        context="post_human_notification",
    )
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
