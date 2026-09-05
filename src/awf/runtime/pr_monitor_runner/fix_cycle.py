"""Extracted PullRequestMonitorRunner domain operations.

This module contains mechanically moved methods from ``awf.runtime.pr_monitor_runner.runner`` and keeps behavior unchanged.
"""

from __future__ import annotations

import asyncio as asyncio
import hashlib as hashlib
import json as json
import os as os
import re as re
import subprocess as subprocess
import time as time
from pathlib import Path
from typing import Any, cast

from awf.common.bitbucket_client import BitbucketClientError
from awf.common.bitbucket_client_parsing import is_task_thread_id
from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import (
    GitHubClientError,
    RepoRef,
)
from awf.db.enums import FailureReason
from awf.node.git_manager import git_env_without_object_lookup_overrides
from awf.runtime.feedback_policy import (
    RESOLVABLE_THREAD_VERDICTS,
    canonical_unresolved_inline_threads,
    review_thread_body_hashes,
)
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.pr_monitor import (
    MonitorState,
    PRStatus,
    ReviewComment,
    ReviewThread,
    _agent_can_triage_review_comment,
    _mark_review_thread_addressed,
    _needs_comment_attention,
    _review_thread_body_hash,
    _review_thread_needs_attention,
)
from awf.runtime.pr_monitor_runner.comment_verdict import AgentVerdictProtocolError
from awf.runtime.pr_monitor_runner.comments import (
    VerdictResult,
    _owned_paths_for_prompt_or_empty,
)
from awf.runtime.pr_monitor_runner.constants import (
    _AUDIT_COMMENT_RESOLUTION_EVENT,
    _AUDIT_GIT_PUSH_EVENT,
    _BITBUCKET_TRANSIENT_RETRY_EXHAUSTED_REASON,
    _BITBUCKET_TRANSIENT_RETRY_REASON,
    _GITHUB_TRANSIENT_RETRY_EXHAUSTED_REASON,
    _GITHUB_TRANSIENT_RETRY_REASON,
    _GITHUB_WORKFLOW_SCOPE_REQUIRED_REASON,
    _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
)
from awf.runtime.pr_monitor_runner.fix_cycle_resolution_invariant import (
    escalate_owner_missing_threads,
    stranded_resolvable_thread_ids,
)
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.helpers import (
    _clear_addressed_state_by_id,
    _defer_reason_state_key,
    _is_transient_bitbucket_client_error,
    _is_transient_github_client_error,
    _mark_review_comment_addressed,
    _redact_and_truncate_forge_error,
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
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
)

# Verdicts whose threads may be resolved on GitHub. ``needs_human`` and
# ``agent_failed`` must keep the thread open, so a thread re-addressed to one of
# them in a later fix-cycle pass is never resolved even if an earlier pass
# queued it for resolution. Aliases the shared taxonomy so the in-cycle resolve
# loop and the stranded-thread invariant (#925) cannot drift apart.
_RESOLVABLE_THREAD_VERDICTS = RESOLVABLE_THREAD_VERDICTS


def _agent_verdict_protocol_failure_result(
    exc: AgentVerdictProtocolError,
) -> _GitPushResult:
    """Return a terminal agent failure without creating human-attention state."""
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr=str(exc),
        reason_code=exc.reason_code,
        failure_reason=FailureReason.agent_failure,
    )


def _git_push_result_with_terminal_head_provenance_unavailable(
    push_result: _GitPushResult,
) -> _GitPushResult:
    """Mark terminal failures whose unpushed HEAD could not be fingerprinted."""
    if not push_result.failed:
        return push_result
    details = dict(push_result.details or {})
    if details.get("local_terminal_head_provenance_unavailable"):
        return push_result
    if details.get("local_terminal_head_sha"):
        return push_result
    details["local_terminal_head_provenance_unavailable"] = True
    return _GitPushResult(
        pushed=push_result.pushed,
        failed=push_result.failed,
        returncode=push_result.returncode,
        stdout=push_result.stdout,
        stderr=push_result.stderr,
        recovered_by_resync=push_result.recovered_by_resync,
        reason_code=push_result.reason_code,
        failure_reason=push_result.failure_reason,
        details=details,
        paused_into_blocked=push_result.paused_into_blocked,
    )


def _git_push_result_with_local_terminal_head(
    push_result: _GitPushResult,
    *,
    operation_start_head: str,
    local_head: str | None,
) -> _GitPushResult:
    """Attach unpushed local HEAD provenance to a failed fix-cycle result."""
    if not push_result.failed:
        return push_result
    if not local_head or local_head.lower() == operation_start_head.lower():
        return push_result
    details = dict(push_result.details or {})
    if details.get("local_terminal_head_sha"):
        return push_result
    details["local_terminal_head_sha"] = local_head
    return _GitPushResult(
        pushed=push_result.pushed,
        failed=push_result.failed,
        returncode=push_result.returncode,
        stdout=push_result.stdout,
        stderr=push_result.stderr,
        recovered_by_resync=push_result.recovered_by_resync,
        reason_code=push_result.reason_code,
        failure_reason=push_result.failure_reason,
        details=details,
        paused_into_blocked=push_result.paused_into_blocked,
    )


async def _enrich_failed_fix_cycle_result(
    self: Any,
    push_result: _GitPushResult,
    *,
    worktree_path: Path,
    operation_start_head: str,
) -> _GitPushResult:
    """Record unpushed local HEAD on terminal failed fix-cycle exits for provenance."""
    if not push_result.failed or not push_result.terminal_monitor_failure:
        return push_result
    if push_result.reason_code == _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON:
        return _git_push_result_with_terminal_head_provenance_unavailable(push_result)
    try:
        local_head = await self._rev_parse_head(worktree_path)
    except (TimeoutError, OSError, subprocess.SubprocessError):
        _log.warning(
            "monitor.fix_cycle_terminal_head_provenance_unavailable",
            reason_code=push_result.reason_code,
        )
        return _git_push_result_with_terminal_head_provenance_unavailable(push_result)
    if not local_head:
        _log.warning(
            "monitor.fix_cycle_terminal_head_provenance_unavailable",
            reason_code=push_result.reason_code,
        )
        return _git_push_result_with_terminal_head_provenance_unavailable(push_result)
    return _git_push_result_with_local_terminal_head(
        push_result,
        operation_start_head=operation_start_head,
        local_head=local_head,
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
    # Threads whose in-cycle resolution was deferred to outdated hygiene because
    # they were already outdated at batch entry. Re-checked against the final
    # settle feed so a thread hygiene cannot own is not stranded (#925).
    deferred_resolution_ids: list[str] = []
    publish_dependent_ids: list[str] = []
    workflow_scope_publish_dependent_ids: list[str] = []
    workflow_scope_resolution_dependent_ids: list[str] = []
    # Last settle-poll status; used after the loop to skip resolving threads that
    # gained fresh feedback we couldn't re-address (e.g. at the pass limit).
    status: PRStatus | None = None
    # Whether the *most recent* settle poll succeeded. A later poll can fail
    # transiently after an earlier one passed, leaving ``status`` holding the
    # earlier feed. That feed is still valid *positive* evidence (a thread that
    # needed attention then still does), but it cannot prove the absence of a
    # reviewer reply that landed during the failed poll window — so the stranded
    # sweep must treat it as "no settle evidence" and escalate rather than
    # resolve (PRRT_kwDOSJAM6s6fm4VR).
    settle_status_is_fresh = False
    fixed_review_comments: list[tuple[ReviewComment, VerdictResult]] = []
    fixed_review_contexts: dict[str, tuple[ReviewComment, VerdictResult]] = {}
    threads = list(initial_threads)
    reviews = list(initial_reviews)
    # Threads already outdated when this AddressComments batch began. The fix
    # cycle may triage / commit / push their repair, but must NOT resolve them
    # in-cycle — the next outer poll's outdated-hygiene path owns resolution
    # (and will re-route a reviewer reply that landed during settle).
    already_outdated_at_batch_entry = {t.thread_id for t in initial_threads if t.is_outdated}
    independently_addressed_review_ids = {comment.comment_id for comment in reviews}
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
        allow_candidate_fallback=False,
    )
    if head_result is not None:
        return cast(_GitPushResult, head_result)
    operation_start_head, abandoned_result = await self._abandon_unpublished_comment_repairs(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        remote_branch=remote_branch,
        remote_push_url=remote_push_url,
        expected_remote_head=pr_head_sha,
        local_head=operation_start_head,
        state=state,
        current_operation_id=operation_id,
    )
    if abandoned_result is not None:
        return await _enrich_failed_fix_cycle_result(
            self,
            abandoned_result,
            worktree_path=worktree_path,
            operation_start_head=operation_start_head,
        )

    async def _return_failed_fix_cycle_result(result: _GitPushResult) -> _GitPushResult:
        return await _enrich_failed_fix_cycle_result(
            self,
            result,
            worktree_path=worktree_path,
            operation_start_head=operation_start_head,
        )

    owned_paths = await _owned_paths_for_prompt_or_empty(self, workspace_id)
    # The workspace's Jira issue key is immutable, so resolve it once for the whole
    # repair cycle (alongside ``owned_paths``) and thread it into every per-item
    # ``_address_thread`` / ``_address_review_comment_result`` call below. Without
    # this each thread/comment — and each settle pass — would re-open a workspace
    # lookup for the same value, the #537 regression in the busiest repair path.
    task_tag = await self._resolve_task_tag(workspace_id)

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
        deferred_resolution_ids[:] = [
            queued_id for queued_id in deferred_resolution_ids if queued_id != item_id
        ]

    async def _current_item_operation_start_head() -> str:
        # Fail closed when the live per-item HEAD cannot be verified. Reusing the
        # cycle-start SHA after a prior item advanced HEAD would let a later
        # no-change FIXED inherit that earlier commit as false fix evidence
        # (PRRT_kwDOSJAM6s6ZoHvG). ``git cat-file -e`` does not distinguish
        # absence from command failure, so both count as unverifiable. A missing
        # worktree still falls back to the cycle-start SHA (already established
        # by ``_repair_operation_start_head_result``) because there is no live
        # HEAD to probe.
        if not worktree_path.exists():
            return cast(str, operation_start_head)
        current_head = cast(str | None, await self._rev_parse_head(worktree_path))
        if not current_head:
            raise _MonitorHeadObjectMissingError(
                _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
                "per-item HEAD unavailable: rev-parse failed",
            )
        current_head_ok = await self._deps.runner.run(
            git_worktree_command(worktree_path, "cat-file", "-e", f"{current_head}^{{commit}}"),
            env=git_env_without_object_lookup_overrides(),
        )
        if current_head_ok.ok:
            return current_head
        raise _MonitorHeadObjectMissingError(
            _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
            "per-item HEAD unavailable: commit object probe failed",
        )

    # Inline thread path/line coords are relative to the remote PR head from the
    # status that supplied the batch — not local worktree HEAD. Non-hosted agents
    # commit locally before push, so local HEAD can advance while settle re-polls
    # still return coordinates for the unpublished remote tip. Using local HEAD as
    # ``cycle_start_head`` skips the required remote→local mapping and can
    # misclassify a real FIXED as AGENT_FIXED_WITHOUT_EVIDENCE
    # (PRRT_kwDOSJAM6s6dFLGV). Keep one stable remote anchor per batch; do not
    # replace it with an advanced settle tip until that tip is reconciled
    # (PRRT_kwDOSJAM6s6dIQm6).
    batch_anchor_head = pr_head_sha

    for _pass_num in range(self._runner_config.max_fix_cycle_passes):
        pass_anchor_head = batch_anchor_head
        # 1) Address each item in the current batch.
        for t in threads:
            try:
                item_operation_start_head = await _current_item_operation_start_head()
                verdict = await self._address_thread(
                    workspace_id=workspace_id,
                    repo=repo,
                    pr_number=pr_number,
                    thread=t,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    state=state,
                    owned_paths=owned_paths,
                    task_tag=task_tag,
                    operation_start_head=item_operation_start_head,
                    cycle_start_head=pass_anchor_head,
                    base_branch=base_branch or "",
                    remote_branch=remote_branch,
                    operation_id=operation_id,
                    operation_type=operation_type,
                    monitor_log=monitor_log,
                )
            except AgentVerdictProtocolError as exc:
                for item_id in publish_dependent_ids:
                    _clear_addressed_state_by_id(state, item_id)
                return await _return_failed_fix_cycle_result(
                    _agent_verdict_protocol_failure_result(exc)
                )
            except ProtectedScopeDiffError as exc:
                for item_id in publish_dependent_ids:
                    _clear_addressed_state_by_id(state, item_id)
                return await _return_failed_fix_cycle_result(
                    cast(
                        _GitPushResult,
                        await self._protected_scope_diff_unavailable_push_result(
                            workspace_id=workspace_id,
                            remote_branch=remote_branch,
                            exc=exc,
                        ),
                    )
                )
            except _MonitorPolicyBlockedError as exc:
                # Roll back like the other early-exit paths: a captured defer in
                # this cycle is in publish_dependent_ids, and leaving it marked
                # addressed-but-unresolved would wedge the merge gate (the next
                # poll skips re-addressing it). The filed-issue marker survives.
                for item_id in publish_dependent_ids:
                    _clear_addressed_state_by_id(state, item_id)
                return await _return_failed_fix_cycle_result(
                    _GitPushResult(
                        pushed=False,
                        failed=True,
                        returncode=1,
                        stderr=str(exc),
                        reason_code=exc.reason_code,
                    )
                )
            except _MonitorAgentRuntimeOwnershipRepairFailedError as exc:
                for item_id in publish_dependent_ids:
                    _clear_addressed_state_by_id(state, item_id)
                return await _return_failed_fix_cycle_result(
                    _GitPushResult(
                        pushed=False,
                        failed=True,
                        returncode=1,
                        stderr=str(exc),
                        reason_code=exc.reason_code,
                    )
                )
            except _MonitorHeadObjectMissingError as exc:
                for item_id in publish_dependent_ids:
                    _clear_addressed_state_by_id(state, item_id)
                return await _return_failed_fix_cycle_result(
                    _GitPushResult(
                        pushed=False,
                        failed=True,
                        returncode=1,
                        stderr=str(exc),
                        reason_code=exc.reason_code,
                    )
                )
            except _MonitorMirrorHooksPathRepairFailedError as exc:
                for item_id in publish_dependent_ids:
                    _clear_addressed_state_by_id(state, item_id)
                return await _return_failed_fix_cycle_result(
                    _GitPushResult(
                        pushed=False,
                        failed=True,
                        returncode=1,
                        stderr=str(exc),
                        reason_code=exc.reason_code,
                    )
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
                    if t.thread_id in already_outdated_at_batch_entry:
                        deferred_resolution_ids.append(t.thread_id)
                    else:
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
                    # The prior ``defer`` reason describes work that was meant
                    # for a tracking issue, not why this thread now needs human
                    # attention. Clear it so the notification does not conceal
                    # the permanent deferred-capture failure.
                    _sync_needs_human_reason(
                        state, t.thread_id, VerdictResult(verdict="needs_human")
                    )
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
                if t.thread_id in already_outdated_at_batch_entry:
                    deferred_resolution_ids.append(t.thread_id)
                else:
                    threads_to_resolve.append(t.thread_id)
                publish_dependent_ids.append(t.thread_id)
                if verdict == "fix_committed":
                    workflow_scope_publish_dependent_ids.append(t.thread_id)
                elif verdict == "false_positive":
                    workflow_scope_resolution_dependent_ids.append(t.thread_id)
            if (
                t.review_context is not None
                and t.review_context.comment_id not in independently_addressed_review_ids
            ):
                context = t.review_context
                existing_review_verdict = state.threads_addressed_ids.get(context.comment_id)
                if existing_review_verdict is not None and not _needs_comment_attention(
                    existing_review_verdict
                ):
                    # The bundled review body was already triaged in an earlier
                    # cycle. Re-addressing its inline thread must not copy the
                    # new thread verdict onto the body when it is absent from
                    # this cycle's independent inbox.
                    pass
                else:
                    _drop_pending_publish_state(context.comment_id)
                    fixed_review_contexts.pop(context.comment_id, None)
                    effective_verdict = state.threads_addressed_ids.get(t.thread_id)
                    if effective_verdict is None:
                        _clear_addressed_state_by_id(state, context.comment_id)
                    else:
                        _mark_review_comment_addressed(state, context, effective_verdict)
                    if effective_verdict == "false_positive":
                        await self._record_pr_feedback_resolution(
                            workspace_id=workspace_id,
                            repo=repo,
                            pr_number=pr_number,
                            pr_head_sha=pr_head_sha,
                            comment=context,
                            verdict_result=VerdictResult(verdict="false_positive"),
                            operation_id=operation_id,
                        )
                    elif effective_verdict == "defer":
                        await self._record_pr_feedback_resolution(
                            workspace_id=workspace_id,
                            repo=repo,
                            pr_number=pr_number,
                            pr_head_sha=pr_head_sha,
                            comment=context,
                            verdict_result=VerdictResult(verdict="defer"),
                            operation_id=operation_id,
                        )
                    elif effective_verdict == "fix_committed":
                        fixed_review_contexts[context.comment_id] = (
                            context,
                            VerdictResult(verdict="fix_committed"),
                        )
                        publish_dependent_ids.append(context.comment_id)
                        workflow_scope_publish_dependent_ids.append(context.comment_id)
        for c in reviews:
            try:
                item_operation_start_head = await _current_item_operation_start_head()
                verdict_result = await self._address_review_comment_result(
                    workspace_id=workspace_id,
                    repo=repo,
                    pr_number=pr_number,
                    comment=c,
                    compose_project=compose_project,
                    compose_file=compose_file,
                    state=state,
                    owned_paths=owned_paths,
                    task_tag=task_tag,
                    operation_start_head=item_operation_start_head,
                    base_branch=base_branch or "",
                    remote_branch=remote_branch,
                    operation_id=operation_id,
                    operation_type=operation_type,
                    monitor_log=monitor_log,
                )
            except AgentVerdictProtocolError as exc:
                for item_id in publish_dependent_ids:
                    _clear_addressed_state_by_id(state, item_id)
                return await _return_failed_fix_cycle_result(
                    _agent_verdict_protocol_failure_result(exc)
                )
            except ProtectedScopeDiffError as exc:
                for item_id in publish_dependent_ids:
                    _clear_addressed_state_by_id(state, item_id)
                return await _return_failed_fix_cycle_result(
                    cast(
                        _GitPushResult,
                        await self._protected_scope_diff_unavailable_push_result(
                            workspace_id=workspace_id,
                            remote_branch=remote_branch,
                            exc=exc,
                        ),
                    )
                )
            except _MonitorPolicyBlockedError as exc:
                # Roll back like the other early-exit paths: a captured defer in
                # this cycle is in publish_dependent_ids, and leaving it marked
                # addressed-but-unresolved would wedge the merge gate (the next
                # poll skips re-addressing it). The filed-issue marker survives.
                for item_id in publish_dependent_ids:
                    _clear_addressed_state_by_id(state, item_id)
                return await _return_failed_fix_cycle_result(
                    _GitPushResult(
                        pushed=False,
                        failed=True,
                        returncode=1,
                        stderr=str(exc),
                        reason_code=exc.reason_code,
                    )
                )
            except _MonitorAgentRuntimeOwnershipRepairFailedError as exc:
                for item_id in publish_dependent_ids:
                    _clear_addressed_state_by_id(state, item_id)
                return await _return_failed_fix_cycle_result(
                    _GitPushResult(
                        pushed=False,
                        failed=True,
                        returncode=1,
                        stderr=str(exc),
                        reason_code=exc.reason_code,
                    )
                )
            except _MonitorHeadObjectMissingError as exc:
                for item_id in publish_dependent_ids:
                    _clear_addressed_state_by_id(state, item_id)
                return await _return_failed_fix_cycle_result(
                    _GitPushResult(
                        pushed=False,
                        failed=True,
                        returncode=1,
                        stderr=str(exc),
                        reason_code=exc.reason_code,
                    )
                )
            except _MonitorMirrorHooksPathRepairFailedError as exc:
                for item_id in publish_dependent_ids:
                    _clear_addressed_state_by_id(state, item_id)
                return await _return_failed_fix_cycle_result(
                    _GitPushResult(
                        pushed=False,
                        failed=True,
                        returncode=1,
                        stderr=str(exc),
                        reason_code=exc.reason_code,
                    )
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
            # retry=False: the settle re-poll classifies a single failure itself
            # (transient -> wait + break settle to push; permanent -> re-raise), so an
            # in-cycle transport retry here would consume the next queued step.
            status = await self._deps.gh.fetch_pr_status(
                repo=repo, pr_number=pr_number, base_behind_count=0, retry=False
            )
        except ForgeClientError as exc:
            # Both forges re-poll the PR through ``self._deps.gh`` (a GitHub or
            # Bitbucket client); either raises a ``ForgeClientError`` subclass. A
            # transient blip during the settle re-poll must wait then break settle
            # (proceed to push the fixes already committed); a permanent fault
            # re-raises. Catching the shared base means a Bitbucket fault can no
            # longer escape to the runner's outer handler and silently skip the push.
            if await self._wait_after_transient_forge_error(
                exc,
                workspace_id=workspace_id,
                pr_number=pr_number,
                context="fix_cycle_settle_fetch_pr_status",
                state=state,
                monitor_log=monitor_log,
            ):
                # ``status`` still holds the previous pass's feed; mark it stale so
                # the stranded sweep below does not mistake it for the final one.
                settle_status_is_fresh = False
                break
            raise
        settle_status_is_fresh = True
        # The settle re-poll succeeded: clear any stale retry count for this context
        # so a recovered blip never accumulates toward the budget across fix cycles.
        await self._clear_forge_transient_retry_state_on_success(
            workspace_id=workspace_id,
            state=state,
            context="fix_cycle_settle_fetch_pr_status",
        )
        # Pass addressed-state into settle dedupe (same as decide()): equal-rank
        # same-ID transport copies must prefer the body matching the recorded
        # hash, not the later feed occurrence — otherwise a stale ghost can be
        # re-addressed mid-cycle and overwrite the handled hash
        # (PRRT_kwDOSJAM6s6dfSrA).
        new_threads = [
            t
            for t in canonical_unresolved_inline_threads(
                status.unresolved_inline_threads,
                status.outdated_unresolved_inline_threads,
                state.threads_addressed_ids,
            )
            if _review_thread_needs_attention(state, t)
        ]
        new_reviews = [
            c
            for c in status.unresolved_review_comments
            if _agent_can_triage_review_comment(c) and _review_comment_needs_attention(state, c)
        ]
        if not new_threads and not new_reviews:
            break  # burst settled
        # Settle thread/review path/line coords are relative to this status's
        # remote head. Only continue when that head still matches the batch
        # anchor we already hold: adopting an advanced tip without fetch /
        # reconcile maps coords from an unavailable or divergent SHA onto
        # unpublished local history (AGENT_FIXED_WITHOUT_EVIDENCE /
        # PRRT_kwDOSJAM6s6dIQm6). Blank head is equally unverifiable — break
        # and push; the outer loop re-enters with abandon/reconcile.
        next_anchor = (status.head_sha or "").strip()
        if not next_anchor or next_anchor.lower() != batch_anchor_head.lower():
            break
        threads = new_threads
        reviews = new_reviews
        independently_addressed_review_ids.update(comment.comment_id for comment in reviews)
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
        # A real protected-scope violation in an unpushed commit PAUSES the
        # workspace into ``blocked`` for an operator decision (WS-2), preserving
        # the offending commit, instead of silently rolling it back. A
        # diff-unavailable block (no violations) keeps the terminal handling.
        await self._pause_monitor_for_protected_scope_block(
            workspace_id=workspace_id,
            pr_number=pr_number,
            pr_head_sha=pr_head_sha,
            protected_scope_block=protected_scope_block,
            worktree_path=worktree_path,
            state=state,
            remote_branch=remote_branch,
            base_branch=base_branch or "",
            operation_id=operation_id,
            operation_type=operation_type,
            monitor_log=monitor_log,
            source_head_sha=operation_start_head,
        )
        if protected_scope_block is not None and protected_scope_block.violations
        else await self._repair_protected_scope_commits_before_push(
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
            operation_start_head=operation_start_head,
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
        return await _return_failed_fix_cycle_result(push_result)
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
    for comment, verdict_result in fixed_review_contexts.values():
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
    # Use the canonical active+outdated view: an initially active thread can
    # flip to outdated with a changed body during settle and would otherwise
    # stay invisible to the active-only feed (PRRT_kwDOSJAM6s6dcFNb).
    stale_thread_ids = (
        {
            t.thread_id
            for t in canonical_unresolved_inline_threads(
                status.unresolved_inline_threads,
                status.outdated_unresolved_inline_threads,
                state.threads_addressed_ids,
            )
            if _review_thread_needs_attention(state, t)
        }
        if status is not None
        else set()
    )
    # Threads the settle re-poll reports as already OUTDATED (#484). An in-place
    # fix can flip a thread to ``isOutdated`` on the forge; a resolve fault on such
    # a thread must NOT clear its fix verdict. Clearing strands it: outdated threads
    # are dropped from the actionable feed, so the fix cycle never re-addresses them,
    # and the next poll's outdated-resolution step skips a thread with no recorded
    # verdict — leaving the conversation permanently unresolved and the PR BLOCKED.
    # Preserving ``fix_committed`` lets that step retry the resolve (or escalate to
    # ``needs_human`` via its own permanent path) instead.
    outdated_thread_ids = (
        {t.thread_id for t in status.outdated_unresolved_inline_threads}
        if status is not None
        else set()
    )
    # Active-wins: when the same ID is still in the active feed, outdated hygiene
    # skips it. Preserve (#484) only for *outdated-only* IDs — dual-feed IDs must
    # clear/escalate like active so AddressComments owns the retry
    # (PRRT_kwDOSJAM6s6dcgS0).
    active_thread_ids = (
        {t.thread_id for t in status.unresolved_inline_threads} if status is not None else set()
    )
    outdated_only_thread_ids = outdated_thread_ids - active_thread_ids
    # ``already_outdated_at_batch_entry`` threads are excluded when enqueueing
    # ``threads_to_resolve`` (single resolution owner: outdated hygiene on the
    # next outer poll). That hand-off only holds while the thread is still
    # outdated at settle: an in-place fix that is later rolled back re-activates
    # it, hygiene never walks it, and its verdict + matching hash suppress
    # AddressComments forever (#925). Adopt those orphans here, and escalate the
    # ones no owner can be demonstrated for. The sweep covers the whole settle
    # feed, not just this cycle's deferred ids: a thread stranded that way by an
    # *earlier* cycle keeps its resolvable verdict and matching hash, so it never
    # re-enters AddressComments and never becomes a candidate again — only the
    # feed still shows it (PRRT_kwDOSJAM6s6fmmKc). Threads already on
    # ``threads_to_resolve`` are excluded: the loop below owns those.
    settle_feed = (
        canonical_unresolved_inline_threads(
            status.unresolved_inline_threads,
            status.outdated_unresolved_inline_threads,
            state.threads_addressed_ids,
        )
        if status is not None
        else None
    )
    swept_thread_ids, owner_missing_thread_ids = stranded_resolvable_thread_ids(
        candidate_ids=deferred_resolution_ids,
        queued_resolution_ids=set(threads_to_resolve),
        state_map=state.threads_addressed_ids,
        # Only a *fresh* settle feed can prove a thread has no pending reply. A
        # feed left over from an earlier pass whose successor poll failed is
        # passed as ``None`` so unownable threads escalate to ``needs_human``
        # instead of being resolved past feedback we never saw
        # (PRRT_kwDOSJAM6s6fm4VR).
        settle_threads=settle_feed if settle_status_is_fresh else None,
        # ...but that superseded feed still proves those conversations were open,
        # so hand over its ids. Dropping them entirely would let an orphan
        # stranded by an earlier cycle — absent from ``candidate_ids`` because its
        # matching hash keeps it out of AddressComments — slip through a transient
        # poll blip unresolved AND unescalated (PRRT_kwDOSJAM6s6fm7wj).
        prior_feed_thread_ids=(
            frozenset()
            if settle_status_is_fresh or settle_feed is None
            else frozenset(thread.thread_id for thread in settle_feed)
        ),
        stale_thread_ids=stale_thread_ids,
        outdated_only_thread_ids=outdated_only_thread_ids,
    )
    if swept_thread_ids:
        _log.info(
            "monitor.thread_resolution_adopted_in_cycle",
            workspace_id=workspace_id,
            pr_number=pr_number,
            thread_ids=list(swept_thread_ids),
        )
        threads_to_resolve.extend(swept_thread_ids)
    escalate_owner_missing_threads(
        state,
        owner_missing_thread_ids,
        workspace_id=workspace_id,
        pr_number=pr_number,
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
        except ForgeClientError as exc:
            # Both forges resolve threads through ``self._deps.gh`` (GitHub or
            # Bitbucket), each raising a ``ForgeClientError`` subclass on API/
            # transport faults. Catching the shared base keeps a Bitbucket fault
            # from escaping ``_execute`` to the runner's generic handler, which
            # would terminate the workspace on a permanent fault instead of keeping
            # the poll loop alive — and would skip the addressed-state rollback, so
            # ``decide()`` would treat the still-open thread as handled forever and
            # let auto-merge bypass live feedback (the #305 mode). Transient blips
            # wait and requeue (``continue``); permanent faults clear the addressed
            # marker and record the forge-native ``exc.reason_code`` without dropping
            # out of the monitor. Both the transient-retry and permanent-failure audit
            # reason codes stay forge-specific so a Bitbucket fault keeps its actionable
            # code (e.g. ``BITBUCKET_AUTH_FAILED``) instead of a generic placeholder.
            transient_retry_reason = (
                _BITBUCKET_TRANSIENT_RETRY_REASON
                if isinstance(exc, BitbucketClientError)
                else _GITHUB_TRANSIENT_RETRY_REASON
            )
            if await self._wait_after_transient_forge_error(
                exc,
                workspace_id=workspace_id,
                pr_number=pr_number,
                context="resolve_thread",
                state=state,
                monitor_log=monitor_log,
            ):
                # Transient fault: clear the addressed marker so the next poll
                # re-attempts the resolve. ``bbtask:`` reviewer tasks clear the same
                # way as comment threads — the task is still UNRESOLVED on Bitbucket
                # and re-surfaces, so it re-routes through AddressComments and the
                # agent re-addresses already-handled content (redundant but harmless;
                # the permanent path below special-cases tasks to needs_human).
                # #484: an already-OUTDATED-only thread is the exception — preserve
                # its verdict so the next poll's outdated-resolution step retries it,
                # since it can never re-route through AddressComments.
                if tid not in outdated_only_thread_ids:
                    _clear_addressed_state_by_id(state, tid)
                await self._record_pr_monitor_audit_event(
                    workspace_id=workspace_id,
                    event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
                    action="resolve_thread",
                    outcome="requeued",
                    reason_code=transient_retry_reason,
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
            # ``redacted_detail()`` normalizes the human detail across forges (gh
            # stderr / Bitbucket body, both already redacted).
            _log.warning(
                "monitor.resolve_thread_failed",
                thread_id=tid,
                stderr=exc.redacted_detail(),
            )
            if is_task_thread_id(tid):
                # A Bitbucket reviewer task whose resolution PUT failed permanently
                # (e.g. 403 ``BITBUCKET_TASK_RESOLVE_FORBIDDEN`` — the token lacks
                # task-resolution scope). Clearing the addressed marker like a comment
                # thread would re-route the task to AddressComments next poll and re-run
                # the agent forever (a retry storm) against a fault the agent cannot
                # fix. Instead downgrade the verdict to ``needs_human``: the task stays
                # UNRESOLVED on Bitbucket so the merge gate keeps blocking, decide()
                # routes it to NotifyHuman (not AddressComments), and an operator grants
                # the scope or resolves the task. The reason code flows into the audit
                # event so the escalation is diagnosable.
                state.mark_addressed(tid, "needs_human")
                await self._record_pr_monitor_audit_event(
                    workspace_id=workspace_id,
                    event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
                    action="resolve_thread",
                    outcome="needs_human",
                    reason_code=exc.reason_code,
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
                        "needs_human_thread_count": 1,
                        "error_message": str(exc),
                    },
                )
                continue
            # ``_wait_after_transient_forge_error`` returns False for BOTH a
            # deterministic resolve fault and a still-*transient* blip whose bounded
            # retry budget was just exhausted. These must NOT share the clear-marker
            # path for a still-open (non-outdated) comment thread: a deterministic
            # fault clears the marker so the next poll re-routes the open thread
            # through AddressComments (the #305-safe default). On exhaustion the
            # underlying fault is still transient (auth/transport/5xx) — something the
            # agent cannot fix — and the per-context counter is deliberately kept at
            # its ceiling, so clearing the marker would re-address the thread, hit the
            # budget again on the very next resolve, re-clear, and re-run the agent
            # every poll: the exact fix-cycle storm the bounded budget exists to stop.
            # Escalate to ``needs_human`` instead (mirroring the task path above): the
            # thread stays UNRESOLVED so the merge gate keeps blocking, decide() routes
            # it to NotifyHuman (not AddressComments), and an operator repairs the
            # forge fault. Outdated-only threads are excluded — they can never re-route
            # through AddressComments, so the storm does not apply and their verdict
            # is preserved for the outdated-resolution step below.
            if isinstance(exc, BitbucketClientError):
                forge_fault_is_transient = _is_transient_bitbucket_client_error(exc)
                exhausted_reason = _BITBUCKET_TRANSIENT_RETRY_EXHAUSTED_REASON
            else:
                forge_fault_is_transient = _is_transient_github_client_error(
                    cast(GitHubClientError, exc)
                )
                exhausted_reason = _GITHUB_TRANSIENT_RETRY_EXHAUSTED_REASON
            if forge_fault_is_transient and tid not in outdated_only_thread_ids:
                state.mark_addressed(tid, "needs_human")
                await self._record_pr_monitor_audit_event(
                    workspace_id=workspace_id,
                    event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
                    action="resolve_thread",
                    outcome="needs_human",
                    reason_code=exhausted_reason,
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
                        "needs_human_thread_count": 1,
                        "error_message": str(exc),
                    },
                )
                continue
            # Do NOT drop out of the monitor. Also do not keep the thread in
            # addressed-state: decide() filters addressed IDs before it returns
            # AddressComments, so retaining a failed resolve would make the next poll
            # treat an open thread as handled forever.
            # #484: an already-OUTDATED-only thread is the exception — clearing
            # strands it (it can never re-route through AddressComments), so preserve
            # its verdict and let the next poll's outdated-resolution step retry the
            # resolve or escalate it to ``needs_human`` via that path's own permanent
            # arm — never silently merging over it.
            if tid not in outdated_only_thread_ids:
                _clear_addressed_state_by_id(state, tid)
            await self._record_pr_monitor_audit_event(
                workspace_id=workspace_id,
                event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
                action="resolve_thread",
                outcome="failed",
                # Forward the forge-native reason code (GitHub: GITHUB_API_ERROR;
                # Bitbucket: e.g. BITBUCKET_API_ERROR / BITBUCKET_AUTH_FAILED) so a
                # permanent comment-resolve fault stays diagnosable — matching the
                # task path above rather than collapsing to a generic placeholder.
                reason_code=exc.reason_code,
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
            # The resolve landed: clear any stale ``resolve_thread`` retry count so a
            # recovered transient blip never accumulates toward the bounded budget.
            await self._clear_forge_transient_retry_state_on_success(
                workspace_id=workspace_id,
                state=state,
                context="resolve_thread",
            )
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
    return await _return_failed_fix_cycle_result(push_result)


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


def _deferred_issue_already_filed(state: MonitorState, thread: ReviewThread) -> bool:
    """True when a tracking issue was filed for this conversation (any hash era).

    Accepts markers keyed by the current content-only hash or either
    pre-normalize legacy form (ID-bearing or fallback null-id) so an in-flight
    resume does not file a duplicate — PRRT_kwDOSJAM6s6dfH8h /
    PRRT_kwDOSJAM6s6dfSq-.
    """
    return any(
        state.threads_addressed_ids.get(_deferred_issue_filed_marker(thread.thread_id, body_hash))
        for body_hash in review_thread_body_hashes(thread)
    )


def _deferred_thread_conversation(thread: ReviewThread) -> str:
    """Render the full review-bundle history for the tracking-issue body.

    A body-aware recapture (see ``_deferred_issue_filed_marker``) fires precisely
    because review evidence changed, so the filed issue must carry the associated
    review body and whole inline conversation — not just the truncated
    first-comment excerpt — or resolving the thread would lose deferred work.
    """
    blocks: list[str] = []
    if thread.review_context is not None:
        context = thread.review_context
        quoted = "\n".join(
            f"> {line}" for line in (context.body or context.body_excerpt).splitlines() or [""]
        )
        blocks.append(f"**Associated review body ({context.author or 'reviewer'})**:\n\n{quoted}")
    for comment in thread.comments:
        quoted = "\n".join(f"> {line}" for line in (comment.body or "").splitlines() or [""])
        blocks.append(f"**{comment.author or 'reviewer'}**:\n\n{quoted}")
    if not thread.comments:
        blocks.append(f"> {thread.body_excerpt}")
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
    if _deferred_issue_already_filed(state, thread):
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
    except ForgeClientError as exc:
        # Both forges file the tracking issue through ``self._deps.gh``; either
        # raises a ``ForgeClientError`` subclass (e.g. a 403 when the token lacks
        # the issues-create scope, which Bitbucket cannot fall back to a comment).
        # Catching the shared base keeps a Bitbucket fault from escaping to the
        # runner's generic handler and terminating the monitor instead of
        # downgrading to ``needs_human``. Transient blips clear the verdict to
        # re-attempt next poll (``None``); permanent faults downgrade to
        # ``needs_human`` (``False``) so the merge gate keeps blocking and the
        # operator is notified. The transient-retry audit reason stays forge-specific.
        transient_retry_reason = (
            _BITBUCKET_TRANSIENT_RETRY_REASON
            if isinstance(exc, BitbucketClientError)
            else _GITHUB_TRANSIENT_RETRY_REASON
        )
        if await self._wait_after_transient_forge_error(
            exc,
            workspace_id=workspace_id,
            pr_number=pr_number,
            context="capture_deferred_thread",
            state=state,
            monitor_log=monitor_log,
        ):
            # Transient (502 / rate-limit / reset): a temporary issue-API outage
            # must not permanently downgrade a valid defer to needs_human. Clear
            # the verdict so the next poll re-addresses and re-attempts capture
            # once the forge recovers. The thread stays unresolved meanwhile.
            _clear_addressed_state_by_id(state, thread.thread_id)
            await self._record_pr_monitor_audit_event(
                workspace_id=workspace_id,
                event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
                action="capture_deferred_thread",
                outcome="requeued",
                reason_code=transient_retry_reason,
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
        # already redacts; redact again defensively before logging/persisting.
        # ``redacted_detail()`` normalizes the human detail (gh stderr / Bitbucket
        # body) across forges.
        redacted_error = _redact_and_truncate_forge_error(str(exc))
        _log.warning(
            "monitor.deferred_capture_failed",
            thread_id=thread.thread_id,
            stderr=_redact_and_truncate_forge_error(exc.redacted_detail()),
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
    # The tracking issue was filed: clear any stale ``capture_deferred_thread``
    # retry count so a recovered blip never accumulates toward the bounded budget.
    await self._clear_forge_transient_retry_state_on_success(
        workspace_id=workspace_id,
        state=state,
        context="capture_deferred_thread",
    )
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
    except ForgeClientError as exc:
        # The tracking issue is already filed and recorded; this explanatory
        # comment is best-effort courtesy, so a failure on either forge is
        # swallowed. Catching the shared base keeps a Bitbucket fault from escaping
        # and terminating the monitor after the durable capture is already done.
        # ``redacted_detail()`` normalizes the human detail across forges.
        _log.warning(
            "monitor.deferred_capture_comment_failed",
            thread_id=thread.thread_id,
            issue_url=issue_url,
            stderr=_redact_and_truncate_forge_error(exc.redacted_detail()),
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
