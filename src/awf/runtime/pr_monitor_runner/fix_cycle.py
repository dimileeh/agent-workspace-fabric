"""Extracted PullRequestMonitorRunner domain operations.

This module contains mechanically moved methods from ``awf.runtime.pr_monitor_runner.runner`` and keeps behavior unchanged.
"""

from __future__ import annotations

import asyncio as asyncio
import hashlib as hashlib
import json as json
import os as os
import re as re
import time as time
from pathlib import Path
from typing import Any, cast

from awf.common.bitbucket_client import BitBucketClientError
from awf.common.github_client import (
    GitHubClientError,
    RepoRef,
)
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.pr_monitor import (
    MonitorState,
    PRStatus,
    ReviewComment,
    ReviewThread,
    _agent_can_triage_review_comment,
    _mark_review_thread_addressed,
    _review_thread_body_hash,
    _review_thread_needs_attention,
)
from awf.runtime.pr_monitor_runner.comments import (
    VerdictResult,
    _owned_paths_for_prompt_or_empty,
)
from awf.runtime.pr_monitor_runner.constants import (
    _AUDIT_COMMENT_RESOLUTION_EVENT,
    _AUDIT_GIT_PUSH_EVENT,
    _BITBUCKET_TRANSIENT_RETRY_REASON,
    _GITHUB_TRANSIENT_RETRY_REASON,
    _GITHUB_WORKFLOW_SCOPE_REQUIRED_REASON,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _clear_addressed_state_by_id,
    _defer_reason_state_key,
    _mark_review_comment_addressed,
    _redact_and_truncate_github_error,
    _review_comment_needs_attention,
    _sync_needs_human_reason,
)
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.remote_ops import (
    _GitPushResult,
)
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorPolicyBlockedError,
)

# Verdicts whose threads may be resolved on GitHub. ``needs_human`` and
# ``agent_failed`` must keep the thread open, so a thread re-addressed to one of
# them in a later fix-cycle pass is never resolved even if an earlier pass
# queued it for resolution.
_RESOLVABLE_THREAD_VERDICTS = frozenset({"defer", "false_positive", "fix_committed"})


async def _run_fix_cycle(
    self: Any,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    pr_head_sha: str,
    initial_threads: tuple[ReviewThread, ...],
    initial_reviews: tuple[ReviewComment, ...],
    state: MonitorState,
    remote_branch: str,
    remote_push_url: str | None = None,
    compose_project: str,
    compose_file: Path,
    monitor_log: WorkspaceLogSink | None = None,
    base_branch: str | None = None,
    operation_id: str | None = None,
    operation_type: str | None = None,
) -> _GitPushResult:
    """Implements the commit-then-push-on-settle behaviour from the plan.

    Invokes the coding CLI once per thread/review comment (locally
    committing fixes), then polls for new comments arriving during
    the fix pass. If any new ones arrive within ``settle_interval``,
    they're addressed in the next pass. When the comment burst is
    quiet, push everything and resolve the threads we addressed.
    """
    threads_to_resolve: list[str] = []
    publish_dependent_ids: list[str] = []
    workflow_scope_publish_dependent_ids: list[str] = []
    workflow_scope_resolution_dependent_ids: list[str] = []
    # Last settle-poll status; used after the loop to skip resolving threads that
    # gained fresh feedback we couldn't re-address (e.g. at the pass limit).
    status: PRStatus | None = None
    fixed_review_comments: list[tuple[ReviewComment, VerdictResult]] = []
    threads = list(initial_threads)
    reviews = list(initial_reviews)
    worktree_path = self._worktrees_root / workspace_id
    dirty_result = await self._pre_existing_dirty_repair_worktree_result(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        operation_type="comment_repair",
    )
    if dirty_result is not None:
        return cast(_GitPushResult, dirty_result)
    operation_start_head, head_result = await self._repair_operation_start_head_result(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        operation_type="comment_repair",
        fallback_head_sha=pr_head_sha,
    )
    if head_result is not None:
        return cast(_GitPushResult, head_result)
    owned_paths = await _owned_paths_for_prompt_or_empty(self, workspace_id)

    def _drop_pending_publish_state(item_id: str) -> None:
        publish_dependent_ids[:] = [
            queued_id for queued_id in publish_dependent_ids if queued_id != item_id
        ]
        workflow_scope_publish_dependent_ids[:] = [
            queued_id for queued_id in workflow_scope_publish_dependent_ids if queued_id != item_id
        ]
        workflow_scope_resolution_dependent_ids[:] = [
            queued_id
            for queued_id in workflow_scope_resolution_dependent_ids
            if queued_id != item_id
        ]
        threads_to_resolve[:] = [
            queued_id for queued_id in threads_to_resolve if queued_id != item_id
        ]

    for _pass_num in range(self._runner_config.max_fix_cycle_passes):
        # 1) Address each item in the current batch.
        for t in threads:
            try:
                verdict = await self._address_thread(
                    workspace_id=workspace_id,
                    repo=repo,
                    pr_number=pr_number,
                    thread=t,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    state=state,
                    owned_paths=owned_paths,
                )
            except ProtectedScopeDiffError as exc:
                for item_id in publish_dependent_ids:
                    _clear_addressed_state_by_id(state, item_id)
                return cast(
                    _GitPushResult,
                    await self._protected_scope_diff_unavailable_push_result(
                        workspace_id=workspace_id,
                        remote_branch=remote_branch,
                        exc=exc,
                    ),
                )
            except _MonitorPolicyBlockedError as exc:
                # Roll back like the other early-exit paths: a captured defer in
                # this cycle is in publish_dependent_ids, and leaving it marked
                # addressed-but-unresolved would wedge the merge gate (the next
                # poll skips re-addressing it). The filed-issue marker survives.
                for item_id in publish_dependent_ids:
                    _clear_addressed_state_by_id(state, item_id)
                return _GitPushResult(
                    pushed=False,
                    failed=True,
                    returncode=1,
                    stderr=str(exc),
                )
            except _MonitorAgentRuntimeOwnershipRepairFailedError as exc:
                for item_id in publish_dependent_ids:
                    _clear_addressed_state_by_id(state, item_id)
                return _GitPushResult(
                    pushed=False,
                    failed=True,
                    returncode=1,
                    stderr=str(exc),
                    reason_code=exc.reason_code,
                )
            # The same thread can be re-addressed in a later settle pass after
            # new reviewer feedback changes its verdict. Remove stale
            # publish/resolve queues before recording the latest outcome so
            # workflow-scope push rollback follows the newest verdict only.
            _drop_pending_publish_state(t.thread_id)
            _mark_review_thread_addressed(state, t, verdict)
            if verdict == "defer":
                # Follow-up defer (#305): durably capture the deferred work
                # (explanatory comment + tracking issue) before the thread is
                # resolved. On capture failure, downgrade to needs_human so the
                # merge gate keeps blocking instead of silently resolving.
                captured = await _capture_deferred_review_thread(
                    self,
                    workspace_id=workspace_id,
                    repo=repo,
                    pr_number=pr_number,
                    thread=t,
                    state=state,
                    base_branch=base_branch,
                    remote_branch=remote_branch,
                    operation_id=operation_id,
                    operation_type=operation_type,
                    monitor_log=monitor_log,
                )
                if captured:
                    threads_to_resolve.append(t.thread_id)
                    # Roll back with the generic publish-dependent set: if a
                    # non-workflow push later fails, the "defer" addressed
                    # marker is cleared so the thread is re-addressed (and
                    # re-resolved) next cycle instead of staying
                    # marked-addressed-but-unresolved and wedging the merge
                    # gate. The filed-issue marker survives the clear, so the
                    # idempotent capture never re-files.
                    publish_dependent_ids.append(t.thread_id)
                    workflow_scope_resolution_dependent_ids.append(t.thread_id)
                elif captured is False:
                    _mark_review_thread_addressed(state, t, "needs_human")
                # captured is None: a transient capture failure already cleared
                # the verdict so the next poll re-attempts capture — don't
                # permanently downgrade a valid defer to needs_human.
            elif verdict in {"needs_human", "agent_failed"}:
                # A thread re-addressed to needs_human/agent_failed in a later
                # pass must drop out of the rollback/resolve sets an earlier
                # capture added it to. Otherwise a push failure would clear the
                # verdict (forcing a pointless re-address of feedback already
                # judged to need a human), and the stale queued id could be
                # resolved on the now-superseded defer.
                _drop_pending_publish_state(t.thread_id)
            else:
                threads_to_resolve.append(t.thread_id)
                publish_dependent_ids.append(t.thread_id)
                if verdict == "fix_committed":
                    workflow_scope_publish_dependent_ids.append(t.thread_id)
                elif verdict == "false_positive":
                    workflow_scope_resolution_dependent_ids.append(t.thread_id)
        for c in reviews:
            try:
                verdict_result = await self._address_review_comment_result(
                    workspace_id=workspace_id,
                    repo=repo,
                    pr_number=pr_number,
                    comment=c,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    state=state,
                    owned_paths=owned_paths,
                )
            except ProtectedScopeDiffError as exc:
                for item_id in publish_dependent_ids:
                    _clear_addressed_state_by_id(state, item_id)
                return cast(
                    _GitPushResult,
                    await self._protected_scope_diff_unavailable_push_result(
                        workspace_id=workspace_id,
                        remote_branch=remote_branch,
                        exc=exc,
                    ),
                )
            except _MonitorPolicyBlockedError as exc:
                # Roll back like the other early-exit paths: a captured defer in
                # this cycle is in publish_dependent_ids, and leaving it marked
                # addressed-but-unresolved would wedge the merge gate (the next
                # poll skips re-addressing it). The filed-issue marker survives.
                for item_id in publish_dependent_ids:
                    _clear_addressed_state_by_id(state, item_id)
                return _GitPushResult(
                    pushed=False,
                    failed=True,
                    returncode=1,
                    stderr=str(exc),
                )
            except _MonitorAgentRuntimeOwnershipRepairFailedError as exc:
                for item_id in publish_dependent_ids:
                    _clear_addressed_state_by_id(state, item_id)
                return _GitPushResult(
                    pushed=False,
                    failed=True,
                    returncode=1,
                    stderr=str(exc),
                    reason_code=exc.reason_code,
                )
            verdict = verdict_result.verdict
            # Review-level comments can also be re-addressed across settle
            # passes. Drop stale publish dependencies before storing the latest
            # verdict so a prior fix_committed pass cannot override it.
            _drop_pending_publish_state(c.comment_id)
            _sync_needs_human_reason(state, c.comment_id, verdict_result)
            _mark_review_comment_addressed(state, c, verdict)
            if verdict in {"false_positive", "defer"}:
                await self._record_pr_feedback_resolution(
                    workspace_id=workspace_id,
                    repo=repo,
                    pr_number=pr_number,
                    pr_head_sha=pr_head_sha,
                    comment=c,
                    verdict_result=verdict_result,
                    operation_id=operation_id,
                )
            elif verdict == "fix_committed":
                fixed_review_comments.append((c, verdict_result))
            # Exclude ``needs_human`` from the rollback set, mirroring the inline
            # thread path: the agent already judged this comment needs a human,
            # so a push failure must not clear that verdict and force a pointless
            # re-address next cycle.
            if verdict in {"needs_human", "agent_failed"}:
                # A review comment re-addressed in a later pass may have been
                # queued for rollback by an earlier fix_committed pass. Drop it
                # from that stale queue so the latest non-publish-dependent
                # verdict survives push-failure cleanup.
                _drop_pending_publish_state(c.comment_id)
            elif verdict != "defer":
                publish_dependent_ids.append(c.comment_id)
                if verdict == "fix_committed":
                    workflow_scope_publish_dependent_ids.append(c.comment_id)

        # 2) Settle window — small sleep, then re-poll for new activity.
        await self._deps.sleep(self._config.settle_interval_seconds)
        try:
            status = await self._deps.gh.fetch_pr_status(
                repo=repo, pr_number=pr_number, base_behind_count=0
            )
        except GitHubClientError as exc:
            if await self._wait_after_transient_github_error(
                exc,
                workspace_id=workspace_id,
                pr_number=pr_number,
                context="fix_cycle_settle_fetch_pr_status",
                monitor_log=monitor_log,
            ):
                break
            raise
        new_threads = [
            t for t in status.unresolved_inline_threads if _review_thread_needs_attention(state, t)
        ]
        new_reviews = [
            c
            for c in status.unresolved_review_comments
            if _agent_can_triage_review_comment(c) and _review_comment_needs_attention(state, c)
        ]
        if not new_threads and not new_reviews:
            break  # burst settled
        threads = new_threads
        reviews = new_reviews
    # (If we hit max_fix_cycle_passes we still fall through to push —
    # whatever we did commit is worth shipping; next outer loop
    # iteration will re-poll and see what's left.)

    # 3) Push everything we committed.
    protected_scope_block = await self._protected_scope_push_block(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        remote_branch=remote_branch,
        remote_push_url=remote_push_url,
    )
    push_result = (
        await self._repair_protected_scope_commits_before_push(
            workspace_id=workspace_id,
            pr_number=pr_number,
            protected_scope_block=protected_scope_block,
            compose_project=compose_project,
            compose_file=compose_file,
            remote_branch=remote_branch,
            remote_push_url=remote_push_url,
            base_branch=base_branch or "",
            operation_id=operation_id,
            operation_type=operation_type,
            monitor_log=monitor_log,
            operation_start_head=operation_start_head,
            source_head_sha=operation_start_head,
        )
        if protected_scope_block is not None
        else await self._validated_git_push_result(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            remote_branch=remote_branch,
            compose_project=compose_project,
            compose_file=compose_file,
            remote_url=remote_push_url,
            state=state,
        )
    )
    pushed_head_sha: str | None = None
    if push_result.failed:
        reason_code = push_result.reason_code
        if reason_code == _GITHUB_WORKFLOW_SCOPE_REQUIRED_REASON:
            _requeue_workflow_scope_publish_dependent_items(
                state,
                workflow_scope_publish_dependent_ids,
                resolution_dependent_ids=workflow_scope_resolution_dependent_ids,
                reason=push_result.error_message or _GITHUB_WORKFLOW_SCOPE_REQUIRED_REASON,
            )
        else:
            for item_id in publish_dependent_ids:
                _clear_addressed_state_by_id(state, item_id)
        await self._record_pr_monitor_audit_event(
            workspace_id=workspace_id,
            event_type=_AUDIT_GIT_PUSH_EVENT,
            action="comment_repair_push",
            outcome="failed",
            reason_code=reason_code,
            pr_number=pr_number,
            status=None,
            base_branch=base_branch or "",
            remote_branch=remote_branch,
            operation_id=operation_id,
            operation_type=operation_type,
            monitor_log=monitor_log,
            evidence=push_result.failure_evidence(),
        )
        return cast(_GitPushResult, push_result)
    # Record the pushed HEAD before resolving review threads. The
    # pushed commit is local git state; a transient GraphQL resolve
    # failure should not affect the monitor's push bookkeeping.
    if push_result.pushed:
        pushed_head_sha = await self._rev_parse_head(worktree_path)
        state.last_push_sha = pushed_head_sha
        await self._record_pr_monitor_audit_event(
            workspace_id=workspace_id,
            event_type=_AUDIT_GIT_PUSH_EVENT,
            action="comment_repair_push",
            outcome="succeeded",
            reason_code="COMMENT_REPAIR",
            pr_number=pr_number,
            status=None,
            base_branch=base_branch or "",
            remote_branch=remote_branch,
            operation_id=operation_id,
            operation_type=operation_type,
            monitor_log=monitor_log,
            source_head_sha=pushed_head_sha,
        )

    resolution_head_sha = pushed_head_sha or pr_head_sha
    for comment, verdict_result in fixed_review_comments:
        await self._record_pr_feedback_resolution(
            workspace_id=workspace_id,
            repo=repo,
            pr_number=pr_number,
            pr_head_sha=resolution_head_sha,
            comment=comment,
            verdict_result=verdict_result,
            operation_id=operation_id,
        )

    # 4) Resolve threads on GitHub. Only inline threads have IDs we can
    # resolve via the GraphQL mutation; review-level comments are
    # marked addressed in state and the reviewer's re-read usually
    # clears them.
    # Threads that the latest settle poll flagged as still needing attention —
    # e.g. a reviewer reply landed during the final settle poll after we hit
    # max_fix_cycle_passes, so its body changed but we never re-addressed it.
    # Resolving such a thread would let auto-merge proceed past fresh unhandled
    # feedback and leave the filed issue missing that reply (the #305 mode).
    stale_thread_ids = (
        {
            t.thread_id
            for t in status.unresolved_inline_threads
            if _review_thread_needs_attention(state, t)
        }
        if status is not None
        else set()
    )
    for tid in threads_to_resolve:
        if tid in stale_thread_ids:
            continue
        # A later pass in this fix cycle may have re-addressed the thread (a new
        # reviewer reply landed during the settle window) and downgraded its
        # verdict to one that must keep the thread open. Resolve only when the
        # *latest* verdict is still resolvable — never resolve a thread the
        # current evidence says needs human/actionable follow-up, or auto-merge
        # could proceed past unaddressed feedback (the #305 failure mode).
        if state.threads_addressed_ids.get(tid) not in _RESOLVABLE_THREAD_VERDICTS:
            continue
        try:
            await self._deps.gh.resolve_thread(thread_id=tid)
        except GitHubClientError as exc:
            if await self._wait_after_transient_github_error(
                exc,
                workspace_id=workspace_id,
                pr_number=pr_number,
                context="resolve_thread",
                monitor_log=monitor_log,
            ):
                _clear_addressed_state_by_id(state, tid)
                await self._record_pr_monitor_audit_event(
                    workspace_id=workspace_id,
                    event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
                    action="resolve_thread",
                    outcome="requeued",
                    reason_code=_GITHUB_TRANSIENT_RETRY_REASON,
                    pr_number=pr_number,
                    status=None,
                    base_branch=base_branch or "",
                    remote_branch=remote_branch,
                    operation_id=operation_id,
                    operation_type=operation_type,
                    monitor_log=monitor_log,
                    source_head_sha=pushed_head_sha,
                    evidence={
                        "thread_ids": [tid],
                        "resolved_thread_count": 0,
                        "requeued_thread_count": 1,
                        "error_message": str(exc),
                    },
                )
                continue
            _log.warning(
                "monitor.resolve_thread_failed",
                thread_id=tid,
                stderr=exc.stderr,
            )
            # Do NOT drop out of the monitor. Also do not keep the
            # thread in addressed-state: decide() filters addressed
            # IDs before it returns AddressComments, so retaining a
            # failed resolve would make the next poll treat an open
            # GitHub thread as handled forever.
            _clear_addressed_state_by_id(state, tid)
            await self._record_pr_monitor_audit_event(
                workspace_id=workspace_id,
                event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
                action="resolve_thread",
                outcome="failed",
                reason_code="COMMENT_RESOLUTION_FAILED",
                pr_number=pr_number,
                status=None,
                base_branch=base_branch or "",
                remote_branch=remote_branch,
                operation_id=operation_id,
                operation_type=operation_type,
                monitor_log=monitor_log,
                source_head_sha=pushed_head_sha,
                evidence={
                    "thread_ids": [tid],
                    "resolved_thread_count": 0,
                    "failed_thread_count": 1,
                    "error_message": str(exc),
                },
            )
        except BitBucketClientError as exc:
            # BitBucket workspaces resolve threads through ``BitBucketClient``,
            # whose ``resolve_thread`` raises ``BitBucketClientError`` (not
            # ``GitHubClientError``) on API/transport faults. Without this arm the
            # error escapes ``_execute`` and the runner's generic BitBucket handler
            # *terminates the workspace* on a permanent fault instead of keeping the
            # poll loop alive — and the addressed-state rollback below never runs,
            # so ``decide()`` would treat the still-open thread as handled forever
            # and let auto-merge bypass live feedback (the #305 mode). Mirror the
            # GitHub arm so the forge-neutral resolve behaviour holds: transient
            # blips wait and requeue (``continue``); permanent faults clear the
            # addressed marker and record ``COMMENT_RESOLUTION_FAILED`` without
            # dropping out of the monitor.
            if await self._wait_after_transient_bitbucket_error(
                exc,
                workspace_id=workspace_id,
                pr_number=pr_number,
                context="resolve_thread",
                monitor_log=monitor_log,
            ):
                _clear_addressed_state_by_id(state, tid)
                await self._record_pr_monitor_audit_event(
                    workspace_id=workspace_id,
                    event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
                    action="resolve_thread",
                    outcome="requeued",
                    reason_code=_BITBUCKET_TRANSIENT_RETRY_REASON,
                    pr_number=pr_number,
                    status=None,
                    base_branch=base_branch or "",
                    remote_branch=remote_branch,
                    operation_id=operation_id,
                    operation_type=operation_type,
                    monitor_log=monitor_log,
                    source_head_sha=pushed_head_sha,
                    evidence={
                        "thread_ids": [tid],
                        "resolved_thread_count": 0,
                        "requeued_thread_count": 1,
                        "error_message": str(exc),
                    },
                )
                continue
            # ``BitBucketClientError`` has no ``stderr`` field and its ``str()``
            # already carries a redacted body; log that instead of ``exc.stderr``.
            _log.warning(
                "monitor.resolve_thread_failed",
                thread_id=tid,
                stderr=str(exc),
            )
            # Same rationale as the GitHub arm: do NOT drop out of the monitor, and
            # clear the addressed marker so the next poll re-addresses the still-open
            # thread rather than treating an open BitBucket thread as handled forever.
            _clear_addressed_state_by_id(state, tid)
            await self._record_pr_monitor_audit_event(
                workspace_id=workspace_id,
                event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
                action="resolve_thread",
                outcome="failed",
                reason_code="COMMENT_RESOLUTION_FAILED",
                pr_number=pr_number,
                status=None,
                base_branch=base_branch or "",
                remote_branch=remote_branch,
                operation_id=operation_id,
                operation_type=operation_type,
                monitor_log=monitor_log,
                source_head_sha=pushed_head_sha,
                evidence={
                    "thread_ids": [tid],
                    "resolved_thread_count": 0,
                    "failed_thread_count": 1,
                    "error_message": str(exc),
                },
            )
        else:
            await self._record_pr_monitor_audit_event(
                workspace_id=workspace_id,
                event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
                action="resolve_thread",
                outcome="succeeded",
                reason_code="COMMENT_REPAIR",
                pr_number=pr_number,
                status=None,
                base_branch=base_branch or "",
                remote_branch=remote_branch,
                operation_id=operation_id,
                operation_type=operation_type,
                monitor_log=monitor_log,
                source_head_sha=pushed_head_sha,
                evidence={
                    "thread_ids": [tid],
                    "resolved_thread_count": 1,
                },
            )
    return cast(_GitPushResult, push_result)


def _requeue_workflow_scope_publish_dependent_items(
    state: MonitorState,
    item_ids: list[str],
    *,
    resolution_dependent_ids: list[str],
    reason: str,
) -> None:
    """Requeue blocked fixes and inline states needing GitHub resolution.

    GitHub rejects workflow-file pushes before the local commits reach the PR,
    and retrying the same repair cannot succeed until an operator provides a token
    with ``workflow`` scope. The failure path already records the permission
    reason and posts the human notification, so clear state for committed fixes
    whose publication was blocked. That lets the next monitor pass retry pushing
    the existing local fix once credentials are repaired. Also clear inline
    false-positive state that still depends on a later GraphQL ``resolve_thread``
    call, including captured defers whose durable issue marker survives state
    cleanup. Preserve durable review-level false-positive resolutions.
    """
    del reason
    for item_id in dict.fromkeys([*resolution_dependent_ids, *item_ids]):
        _clear_addressed_state_by_id(state, item_id)


def _deferred_issue_filed_marker(thread_id: str, body_hash: str) -> str:
    """State key recording that a tracking issue was filed for a deferred thread.

    Distinct from the verdict/body-hash keys that ``_clear_addressed_state_by_id``
    pops, so the marker survives a resolve-retry's state clear and keeps the
    capture idempotent across outer monitor iterations (no duplicate issues).

    Keyed by the thread body hash as well as the id: a same-body resolve-retry
    stays idempotent, but if the thread later gains new reviewer replies the
    hash changes and the new feedback is captured into a fresh issue rather than
    silently resolved under the stale one.
    """
    return f"__deferred_issue_filed__:{thread_id}:{body_hash}"


def _deferred_thread_conversation(thread: ReviewThread) -> str:
    """Render the full review-thread history for the tracking-issue body.

    A body-aware recapture (see ``_deferred_issue_filed_marker``) fires precisely
    because new reviewer replies changed the thread, so the filed issue must
    carry the whole conversation — not just the truncated first-comment excerpt —
    or the very feedback that triggered the recapture would be lost on resolve.
    """
    if not thread.comments:
        return f"> {thread.body_excerpt}"
    blocks: list[str] = []
    for comment in thread.comments:
        quoted = "\n".join(f"> {line}" for line in (comment.body or "").splitlines() or [""])
        blocks.append(f"**{comment.author or 'reviewer'}**:\n\n{quoted}")
    return "\n\n".join(blocks)


async def _capture_deferred_review_thread(
    self: Any,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    thread: ReviewThread,
    state: MonitorState,
    base_branch: str | None,
    remote_branch: str,
    operation_id: str | None,
    operation_type: str | None,
    monitor_log: WorkspaceLogSink | None,
) -> bool | None:
    """Durably capture a follow-up ``defer`` before its thread is resolved (#305).

    Posts an explanatory PR comment and files a tracking issue. Idempotent per
    thread *and body*: a marker records that the issue was already filed so a
    later same-body resolve-retry (which clears the verdict and re-addresses the
    thread) does not file a duplicate, while new reviewer replies (a changed
    body) are captured into a fresh issue. Returns ``True`` when the deferred
    work is durably captured (caller may resolve the thread); ``False`` on a
    *permanent* capture failure (caller downgrades to ``needs_human`` so the
    merge stays blocked and the operator is notified); or ``None`` on a
    *transient* failure — the thread verdict is cleared so the next poll
    re-addresses and re-attempts capture once GitHub recovers, instead of
    permanently downgrading a valid defer.
    """
    marker = _deferred_issue_filed_marker(thread.thread_id, _review_thread_body_hash(thread))
    if state.threads_addressed_ids.get(marker):
        return True
    location = thread.path or "the PR diff"
    thread_ref = thread.url or f"PR #{pr_number}"
    issue_title = f"Deferred from PR #{pr_number}: {location}"
    agent_reason = state.threads_addressed_ids.get(_defer_reason_state_key(thread.thread_id))
    agent_reason_section = (
        f"Agent's deferral reason:\n\n> {agent_reason}\n\n" if agent_reason else ""
    )
    issue_body = (
        f"AWF deferred a review thread while monitoring PR #{pr_number}.\n\n"
        f"- Path: {location}\n"
        f"- Thread: {thread_ref}\n\n"
        f"{agent_reason_section}"
        f"Review thread (full history):\n\n{_deferred_thread_conversation(thread)}\n\n"
        "This issue tracks the deferred follow-up so the PR thread could be "
        "resolved without losing the work."
    )
    try:
        issue_url = await self._deps.gh.create_issue(
            repo=repo,
            title=issue_title,
            body=issue_body,
        )
    except GitHubClientError as exc:
        if await self._wait_after_transient_github_error(
            exc,
            workspace_id=workspace_id,
            pr_number=pr_number,
            context="capture_deferred_thread",
            monitor_log=monitor_log,
        ):
            # Transient (502 / rate-limit / reset): a temporary issue-API outage
            # must not permanently downgrade a valid defer to needs_human. Clear
            # the verdict so the next poll re-addresses and re-attempts capture
            # once GitHub recovers. The thread stays unresolved meanwhile.
            _clear_addressed_state_by_id(state, thread.thread_id)
            await self._record_pr_monitor_audit_event(
                workspace_id=workspace_id,
                event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
                action="capture_deferred_thread",
                outcome="requeued",
                reason_code=_GITHUB_TRANSIENT_RETRY_REASON,
                pr_number=pr_number,
                status=None,
                base_branch=base_branch or "",
                remote_branch=remote_branch,
                operation_id=operation_id,
                operation_type=operation_type,
                monitor_log=monitor_log,
                evidence={"thread_ids": [thread.thread_id]},
            )
            return None
        # Permanent failure (e.g. token missing the issues scope). Redact before
        # logging/persisting: gh CLI errors can echo tokens or credentialed URLs.
        redacted_error = _redact_and_truncate_github_error(str(exc))
        _log.warning(
            "monitor.deferred_capture_failed",
            thread_id=thread.thread_id,
            stderr=_redact_and_truncate_github_error(exc.stderr),
        )
        await self._record_pr_monitor_audit_event(
            workspace_id=workspace_id,
            event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
            action="capture_deferred_thread",
            outcome="failed",
            reason_code="DEFERRED_CAPTURE_FAILED",
            pr_number=pr_number,
            status=None,
            base_branch=base_branch or "",
            remote_branch=remote_branch,
            operation_id=operation_id,
            operation_type=operation_type,
            monitor_log=monitor_log,
            evidence={"thread_ids": [thread.thread_id], "error_message": redacted_error},
        )
        return False
    except BitBucketClientError as exc:
        # BitBucket workspaces reach this same capture path, but ``create_issue``
        # raises ``BitBucketClientError`` (e.g. a 403 when the token lacks the
        # issues-create scope — a non-404 the client cannot fall back to a
        # comment). Without this arm the error would escape to the runner's
        # generic BitBucket handler and *terminate the monitor* rather than
        # downgrading to ``needs_human`` the way the GitHub path does. Mirror the
        # GitHub handling so the forge-neutral downgrade behaviour holds:
        # transient blips clear the verdict to re-attempt next poll (``None``),
        # permanent faults downgrade to ``needs_human`` (``False``) so the merge
        # gate keeps blocking and the operator is notified.
        if await self._wait_after_transient_bitbucket_error(
            exc,
            workspace_id=workspace_id,
            pr_number=pr_number,
            context="capture_deferred_thread",
            monitor_log=monitor_log,
        ):
            _clear_addressed_state_by_id(state, thread.thread_id)
            await self._record_pr_monitor_audit_event(
                workspace_id=workspace_id,
                event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
                action="capture_deferred_thread",
                outcome="requeued",
                reason_code=_BITBUCKET_TRANSIENT_RETRY_REASON,
                pr_number=pr_number,
                status=None,
                base_branch=base_branch or "",
                remote_branch=remote_branch,
                operation_id=operation_id,
                operation_type=operation_type,
                monitor_log=monitor_log,
                evidence={"thread_ids": [thread.thread_id]},
            )
            return None
        # Permanent failure (e.g. token missing the issues scope). ``str(exc)``
        # carries an already-redacted body; redact again defensively before
        # logging/persisting. ``BitBucketClientError`` has no ``stderr`` field, so
        # log the redacted message instead.
        redacted_error = _redact_and_truncate_github_error(str(exc))
        _log.warning(
            "monitor.deferred_capture_failed",
            thread_id=thread.thread_id,
            stderr=redacted_error,
        )
        await self._record_pr_monitor_audit_event(
            workspace_id=workspace_id,
            event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
            action="capture_deferred_thread",
            outcome="failed",
            reason_code="DEFERRED_CAPTURE_FAILED",
            pr_number=pr_number,
            status=None,
            base_branch=base_branch or "",
            remote_branch=remote_branch,
            operation_id=operation_id,
            operation_type=operation_type,
            monitor_log=monitor_log,
            evidence={"thread_ids": [thread.thread_id], "error_message": redacted_error},
        )
        return False
    # Filing the tracking issue is the durable capture. Record it immediately so
    # a later retry (e.g. after a failed push) never files a duplicate, even if
    # the explanatory comment below fails. The comment is best-effort courtesy.
    state.mark_addressed(marker, issue_url)
    try:
        await self._deps.gh.post_comment(
            repo=repo,
            pr_number=pr_number,
            body=(
                f"AWF deferred the review thread on `{location}` and filed "
                f"{issue_url} to track the follow-up. Resolving this thread; the "
                "deferred work lives in that issue."
            ),
        )
    except GitHubClientError as exc:
        _log.warning(
            "monitor.deferred_capture_comment_failed",
            thread_id=thread.thread_id,
            issue_url=issue_url,
            stderr=_redact_and_truncate_github_error(exc.stderr),
        )
    await self._record_pr_monitor_audit_event(
        workspace_id=workspace_id,
        event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
        action="capture_deferred_thread",
        outcome="succeeded",
        reason_code="DEFERRED_CAPTURE",
        pr_number=pr_number,
        status=None,
        base_branch=base_branch or "",
        remote_branch=remote_branch,
        operation_id=operation_id,
        operation_type=operation_type,
        monitor_log=monitor_log,
        evidence={"thread_ids": [thread.thread_id], "issue_url": issue_url},
    )
    return True
