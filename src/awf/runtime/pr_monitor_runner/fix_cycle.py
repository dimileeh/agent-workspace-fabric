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

from awf.common.github_client import (
    GitHubClientError,
)
from awf.runtime.pr_monitor import (
    _mark_review_thread_addressed,
    _review_thread_needs_attention,
)
from awf.runtime.pr_monitor_runner.comments import (
    VerdictResult,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _clear_addressed_state_by_id,
    _mark_review_comment_addressed,
    _review_comment_needs_attention,
)
from awf.runtime.pr_monitor_runner.remote_ops import (
    _GitPushResult,
)
from awf.runtime.pr_monitor_runner.shared import (
    _AUDIT_COMMENT_RESOLUTION_EVENT,
    _AUDIT_GIT_PUSH_EVENT,
    _GITHUB_TRANSIENT_RETRY_REASON,
    MonitorState,
    ProtectedScopeDiffError,
    RepoRef,
    ReviewComment,
    ReviewThread,
    WorkspaceLogSink,
    _agent_can_triage_review_comment,
    _log,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorPolicyBlockedError,
)


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
            _mark_review_thread_addressed(state, t, verdict)
            if verdict not in {"defer", "agent_failed"}:
                threads_to_resolve.append(t.thread_id)
                publish_dependent_ids.append(t.thread_id)
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
            if verdict not in {"defer", "agent_failed"}:
                publish_dependent_ids.append(c.comment_id)

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
        else await self._git_push_result(
            worktree_path=worktree_path,
            remote_branch=remote_branch,
            remote_url=remote_push_url,
        )
    )
    pushed_head_sha: str | None = None
    if push_result.failed:
        reason_code = push_result.reason_code
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
    for tid in threads_to_resolve:
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
