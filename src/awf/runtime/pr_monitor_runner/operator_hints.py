"""Operator remonitor hint repair handling."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

from awf.common.github_client import RepoRef
from awf.control.blocked_transition import (
    MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE,
    MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE,
)
from awf.control.quality_gates import QualityGateViolation
from awf.db.repositories import WorkspaceRepository
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.monitor_prompts import operator_hint_prompt
from awf.runtime.operator_hints import (
    mark_operator_hint_agent_failed,
    mark_operator_hint_needs_human,
    mark_operator_hint_processed,
)
from awf.runtime.pr_monitor import (
    _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY,
    MonitorState,
    OperatorHint,
)
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.constants import (
    _GIT_PUSH_REJECTED_NON_FAST_FORWARD_REASON,
    _PROTECTED_SCOPE_PUSH_BLOCKED_REASON,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_constants import (
    _PRE_PUSH_VALIDATION_FAILED_REASON,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult, _ProtectedScopePushBlock
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
)

# Recognize the persisted review-comment key forms surfaced back to operators:
# GitHub review bodies may be bare databaseIds, while issue/review comments use
# ``issue:<databaseId>``. Retirement still requires a stale ``needs_human``
# storage-key match before clearing anything, which preserves the over-clear
# guard for unrelated numbers in guide text.
_OPERATOR_HINT_FEEDBACK_ID_RE = re.compile(r"\bissue:\d{6,}\b|\b\d{6,}\b")


async def _run_operator_hint_cycle(
    self: Any,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    pr_head_sha: str,
    hint: OperatorHint,
    state: MonitorState,
    remote_branch: str,
    remote_push_url: str | None = None,
    compose_project: str,
    compose_file: Path,
    _monitor_log: WorkspaceLogSink | None = None,
    base_branch: str | None = None,
    _operation_id: str | None = None,
    _operation_type: str | None = None,
) -> _GitPushResult:
    """Run one repair pass for an operator hint and push committed work."""
    worktree_path = self._worktrees_root / workspace_id
    dirty_result = await self._pre_existing_dirty_repair_worktree_result(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        operation_type="operator_hint_repair",
    )
    if dirty_result is not None:
        return cast(_GitPushResult, dirty_result)
    operation_start_head, head_result = await self._repair_operation_start_head_result(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        operation_type="operator_hint_repair",
        fallback_head_sha=pr_head_sha,
    )
    if head_result is not None:
        return cast(_GitPushResult, head_result)

    # Resolve the workspace's optional Jira issue key once for this repair path
    # and thread it into the commit sink so the cycle does not re-query the DB.
    task_tag = await self._resolve_task_tag(workspace_id)
    # A DIRECTIVE resume (revert/redo) runs the CLI with the operator directive.
    # A remonitor hint has no directive but still runs the CLI (prompt falls back
    # to the reason). A GRANT-ONLY (approve-and-keep) protected-scope resume — no
    # directive AND active operator grants — skips the CLI (no tokens) and pushes
    # the PRESERVED commit straight through the now grant-aware gate.
    active_grant_specs = await self._active_operator_grant_specs(workspace_id)
    preserved_head_sha = state.threads_addressed_ids.get(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY)
    # Restart-after-consume recovery: a prior approve-and-keep resume — grant-only
    # OR combined ``--directive ... --grant ...`` — can push the preserved commit
    # and consume its single-use grant (committed immediately by
    # ``_finalize_operator_hint_resume``) and then crash BEFORE the processed marker
    # is persisted. On restart the grant is gone, so ``not active_grant_specs`` is
    # True. A grant-only hint would re-run the CLI on just the reason string; a
    # directive hint would re-run the directive CLI WITHOUT the consumed grant —
    # either way the agent can make extra unapproved commits, and a directive re-run
    # can re-block an already-resolved pause (PRRT_kwDOSJAM6s6KUNoL). The
    # preserved-head marker (recorded at block time, durable across the restart)
    # plus the approved commit already being on the remote prove the protected work
    # is finished, so finalize the bookkeeping instead of re-invoking the agent —
    # regardless of whether the consumed resume carried a directive. The conditions
    # are tight enough to exclude a genuine first run: a plain remonitor has no
    # marker; a first-run grant (or directive+grant) resume still has an active
    # grant; and a first-run directive-only revert has not yet pushed its preserved
    # commit, so it is not ``already_on_remote``. This shortcut keys on the preserved
    # commit being ON the remote, which covers a grant resume or a revert-on-top
    # directive (both publish the preserved commit). A directive that DROPS the
    # preserved commit publishes a corrected head WITHOUT the preserved SHA, so the
    # adjacent branch below handles it (PRRT_kwDOSJAM6s6KUf46).
    if (
        not active_grant_specs
        and preserved_head_sha
        and await self._preserved_commit_already_on_remote(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            remote_branch=remote_branch,
            remote_push_url=remote_push_url,
            preserved_head_sha=preserved_head_sha,
        )
    ):
        pushed_head_sha = await self._rev_parse_head(worktree_path)
        if pushed_head_sha:
            state.last_push_sha = pushed_head_sha
        await _finalize_operator_hint_resume(
            self, workspace_id=workspace_id, state=state, hint=hint
        )
        return _GitPushResult(pushed=False, failed=False, returncode=0)
    # Restart-after-consume recovery for a DIRECTIVE that DROPPED the preserved
    # commit. A directive-only resume can resolve the block by dropping the preserved
    # offending commit and pushing a corrected head, then crash BEFORE the processed
    # marker persists. On restart the persisted state still carries the pending
    # directive and the preserved marker but no grant (a directive-only resume never
    # had one), so ``not active_grant_specs`` is True; the shortcut above never fires
    # because the dropped preserved SHA is intentionally NOT on the remote. Without
    # this branch the fall-through would re-invoke the directive CLI against the
    # already-resolved branch, creating extra unreviewed commits or re-blocking the
    # workspace instead of just finalizing the hint (PRRT_kwDOSJAM6s6KUf46). Detect the
    # finished drop instead: confirm the LOCAL HEAD itself is contained in the freshly
    # fetched remote (an ancestor of — or equal to — the remote PR head), proving the
    # corrected push landed and NO unpushed commit remains in local history. Pass the
    # local HEAD as ``preserved_head_sha`` so the helper runs the ancestry check, NOT
    # ``None`` (which checks only that the worktree TREE matches the remote). A
    # tree-only check is unsafe: a prior directive that reverted the preserved edit ON
    # TOP and re-blocked resets the preserved marker to that revert-on-top commit,
    # whose tree matches the remote PR branch even though the commit was never pushed
    # and the ungranted protected commit still sits below it in local history; a
    # tree-only match would finalize, clear the leak marker, and let a later repair
    # push publish that ungranted history (PRRT_kwDOSJAM6s6KUx2T). Requiring HEAD
    # containment proves the entire local history is already published, so finalizing
    # is safe. Finalize the bookkeeping rather than re-running the agent; this mirrors
    # the no-op-push directive branch further below, just early enough to skip the
    # wasteful CLI re-invocation. A genuine first run is excluded: its preserved
    # offending commit is still the unpushed HEAD, which is not contained in the
    # remote. When HEAD cannot be resolved we cannot prove containment, so the shortcut
    # is skipped and the fall-through runs the directive CLI behind the leak guard.
    if hint.directive and not active_grant_specs and preserved_head_sha:
        local_head_sha = await self._rev_parse_head(worktree_path)
        if local_head_sha and await self._preserved_commit_already_on_remote(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            remote_branch=remote_branch,
            remote_push_url=remote_push_url,
            preserved_head_sha=local_head_sha,
        ):
            state.last_push_sha = local_head_sha
            await _finalize_operator_hint_resume(
                self, workspace_id=workspace_id, state=state, hint=hint
            )
            return _GitPushResult(pushed=False, failed=False, returncode=0)
    if hint.directive or not active_grant_specs:
        prompt = operator_hint_prompt(
            pr_number=pr_number,
            repo_slug=repo.slug(),
            reason=hint.reason,
            directive=hint.directive,
            operation_id=hint.operation_id,
            workspace_runtime_context=self._workspace_runtime_context,
            task_tag=task_tag,
        )
        try:
            verdict = await self._invoke_cli_for_verdict_result(
                workspace_id=workspace_id,
                prompt=prompt,
                commit_message="fix: address operator hint",
                compose_project=compose_project,
                compose_file=compose_file,
                state=state,
                task_tag=task_tag,
                operation_start_head=operation_start_head,
            )
        except ProtectedScopeDiffError as exc:
            push_result = cast(
                _GitPushResult,
                await self._protected_scope_diff_unavailable_push_result(
                    workspace_id=workspace_id,
                    remote_branch=remote_branch,
                    exc=exc,
                ),
            )
            reason = (
                push_result.stderr or str(exc)
            ).strip() or "protected-scope policy could not verify the operator hint repair push"
            mark_operator_hint_needs_human(state, reason)
            return push_result
        except _MonitorPolicyBlockedError as exc:
            reason = str(exc) or "monitor policy blocked the operator hint repair"
            mark_operator_hint_needs_human(state, reason)
            return _GitPushResult(pushed=False, failed=False, returncode=1, stderr=reason)
        except _MonitorAgentRuntimeOwnershipRepairFailedError as exc:
            reason = str(exc) or "agent runtime ownership repair failed"
            mark_operator_hint_needs_human(state, reason)
            return _GitPushResult(
                pushed=False,
                failed=True,
                returncode=1,
                stderr=reason,
                reason_code=exc.reason_code,
            )
        except _MonitorHeadObjectMissingError as exc:
            reason = str(exc) or "HEAD commit object is missing from the canonical mirror"
            mark_operator_hint_needs_human(state, reason)
            return _GitPushResult(
                pushed=False,
                failed=True,
                returncode=1,
                stderr=reason,
                reason_code=exc.reason_code,
            )
        except _MonitorMirrorHooksPathRepairFailedError as exc:
            reason = str(exc) or "mirror hooks path repair failed"
            mark_operator_hint_needs_human(state, reason)
            return _GitPushResult(
                pushed=False,
                failed=True,
                returncode=1,
                stderr=reason,
                reason_code=exc.reason_code,
            )
        if verdict.verdict == "agent_failed":
            reason = _operator_hint_block_reason(verdict)
            reblock_result = await _terminal_directive_grant_reblock(
                self,
                workspace_id=workspace_id,
                pr_number=pr_number,
                pr_head_sha=pr_head_sha,
                worktree_path=worktree_path,
                state=state,
                remote_branch=remote_branch,
                base_branch=base_branch,
                operation_id=_operation_id,
                operation_type=_operation_type,
                operation_start_head=operation_start_head,
                preserved_head_sha=preserved_head_sha,
                active_grant_specs=active_grant_specs,
                verdict=verdict,
            )
            if reblock_result is not None:
                return reblock_result
            await _clear_dropped_preserved_marker_after_terminal_directive(
                self,
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                state=state,
                hint=hint,
                active_grant_specs=active_grant_specs,
                preserved_head_sha=preserved_head_sha,
            )
            mark_operator_hint_agent_failed(state, reason)
            return _GitPushResult(pushed=False, failed=False, returncode=0)
        if verdict.verdict in {"needs_human", "defer", "false_positive"}:
            reason = _operator_hint_block_reason(verdict)
            reblock_result = await _terminal_directive_grant_reblock(
                self,
                workspace_id=workspace_id,
                pr_number=pr_number,
                pr_head_sha=pr_head_sha,
                worktree_path=worktree_path,
                state=state,
                remote_branch=remote_branch,
                base_branch=base_branch,
                operation_id=_operation_id,
                operation_type=_operation_type,
                operation_start_head=operation_start_head,
                preserved_head_sha=preserved_head_sha,
                active_grant_specs=active_grant_specs,
                verdict=verdict,
            )
            if reblock_result is not None:
                return reblock_result
            await _clear_dropped_preserved_marker_after_terminal_directive(
                self,
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                state=state,
                hint=hint,
                active_grant_specs=active_grant_specs,
                preserved_head_sha=preserved_head_sha,
            )
            mark_operator_hint_needs_human(state, reason)
            return _GitPushResult(pushed=False, failed=False, returncode=0)

    # Select the protected-scope validator by the resume's ORIGIN. Only a
    # sync-base-originated block (``monitor_protected_scope_sync_base``) may use
    # the sync-base-aware validator: it intentionally drops paths whose final
    # tree matches the merged base so a directive that reverts the agent-authored
    # protected edit is not re-blocked by a target-branch-owned change the merge
    # pulled in — matching how ``_run_sync_base`` first raised the block
    # (PRRT_kwDOSJAM6s6KFDHO). For an ordinary remonitor or a non-sync-base
    # protected-block resume the monitor loop still supplies ``base_branch``, so
    # passing it unconditionally would wrongly select that validator: a repair
    # that reverts an UNOWNED protected file back to the base contents (while the
    # PR branch held a different protected version) drops out of
    # ``changed_from_base`` and would push without re-blocking or a grant. Gate on
    # the recorded resume phase and fall back to the generic unpushed-commit
    # validator otherwise (PRRT_kwDOSJAM6s6KFZN_).
    block_resume_phase = await self._resolve_block_resume_phase(workspace_id)
    protected_scope_base_branch = (
        base_branch
        if block_resume_phase == MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE
        else None
    )
    protected_scope_block = await self._protected_scope_push_block(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        remote_branch=remote_branch,
        remote_push_url=remote_push_url,
        base_branch=protected_scope_base_branch,
    )
    if protected_scope_block is not None and protected_scope_block.violations:
        # The resume did NOT clear the protected violation (a "revert" directive
        # that didn't actually revert, or a grant that doesn't cover the path).
        # RE-BLOCK with a bumped epoch — invalidating the just-applied grants and
        # re-arming a fresh notification — instead of proceeding toward merge.
        # Preserve the resume ORIGIN when re-blocking. A sync-base-originated block
        # whose directive resume still leaves a violation must re-record the
        # ``monitor_protected_scope_sync_base`` phase, NOT fall back to the default
        # generic phase: otherwise the next resume would select the generic
        # unpushed-commit validator and re-block on a target-branch-owned protected
        # change the base merge pulled in, even after the agent-authored protected
        # edit was correctly reverted (PRRT_kwDOSJAM6s6KGCXl).
        reblock_resume_phase = (
            MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE
            if block_resume_phase == MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE
            else MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE
        )
        reblock_result = cast(
            _GitPushResult,
            await self._pause_monitor_for_protected_scope_block(
                workspace_id=workspace_id,
                pr_number=pr_number,
                pr_head_sha=pr_head_sha,
                protected_scope_block=protected_scope_block,
                worktree_path=worktree_path,
                resume_phase=reblock_resume_phase,
                state=state,
                remote_branch=remote_branch,
                base_branch=base_branch or "",
                operation_id=_operation_id,
                operation_type=_operation_type,
                source_head_sha=operation_start_head,
            ),
        )
        if reblock_result.paused_into_blocked:
            # The shared block transition clears the pre-PR ``pending_operator_hint``
            # column but cannot reach the monitor hint map. Clear the in-memory
            # monitor hint too so the state the loop persists after this re-block
            # does not show a pending resume while status is already ``blocked``;
            # the bumped block epoch supersedes this hint and a later resume re-arms
            # a fresh one (mirrors ``blocked_transition`` clearing the pre-PR column).
            state.pending_operator_hint = None
        return reblock_result
    # Directive resumes must not LEAK the ungranted protected commit. A DIRECTIVE
    # (revert/redo) that resolves the block by adding a revert commit ON TOP of the
    # preserved offending commit — rather than resetting it away — leaves the net
    # tree matching the remote PR branch, so ``protected_scope_block`` is None, yet
    # pushing HEAD would still fast-forward the remote branch over the original
    # ungranted protected-file commit plus its revert. Only an approve-and-keep
    # grant authorizes publishing the preserved commit, so this guard is scoped to
    # directive resumes. When the preserved commit is still inside the range this
    # push would publish, surface needs_human instead of leaking it; the operator can
    # re-issue a directive that resets the worktree (or grant the path)
    # (PRRT_kwDOSJAM6s6KFytV).
    #
    # EXCEPTION: a combined ``--directive ... --grant ...`` resume can intentionally
    # KEEP the preserved protected edit while the directive fixes other files. The
    # same-request grant (preserved across the combined guide) covers the protected
    # paths, so ``protected_scope_block`` is None for an authorized keep — NOT a leak.
    # Honor that operator decision: skip the guard when active grants cover every
    # protected path of the current block, otherwise this wedges a valid push at
    # needs_human where no further grant can be added (PRRT_kwDOSJAM6s6KG1hs). A
    # grant that covers only some of those paths (or none) still leaves an ungranted
    # protected change, so the guard keeps firing.
    if (
        protected_scope_block is None
        and hint.directive
        and preserved_head_sha
        and not await self._preserved_protected_change_fully_granted(
            workspace_id=workspace_id,
            grant_specs=active_grant_specs,
        )
        and await self._preserved_commit_in_unpushed_range(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            remote_branch=remote_branch,
            remote_push_url=remote_push_url,
            preserved_head_sha=preserved_head_sha,
        )
    ):
        reason = (
            "operator directive resolved the protected block by reverting on top of "
            f"the preserved commit {preserved_head_sha} instead of dropping it; pushing "
            "would publish the ungranted protected change. Reset the worktree to remove "
            "the preserved commit, or approve-and-keep the protected path, then resume."
        )
        # RE-BLOCK into ``blocked`` so the approve-and-keep grant this message
        # advertises is actually reachable. ``guide_workspace`` only accepts
        # ``--grant`` inputs for a ``blocked`` workspace (non-blocked + grants ->
        # ``WorkspaceGuideGrantNotAllowedError``), so merely parking a needs_human
        # hint at ``monitoring_pr`` strands the operator with NO way to perform the
        # approve-and-keep this very message recommends. Rebuild the protected block
        # from the violations recorded at the original block — exactly the protected
        # paths the preserved commit changed — and reuse the shared WS-1
        # ``enter_blocked`` pause path so the existing blocked-status + operator-grant
        # machinery resolves it (``guide --grant`` -> ``blocked -> monitoring_pr``
        # grant-only resume -> grant-aware push). Preserve the resume ORIGIN exactly
        # as the net-diff re-block branch above (PR #609 codex P2 thread).
        return await _reblock_preserved_protected_leak(
            self,
            workspace_id=workspace_id,
            pr_number=pr_number,
            pr_head_sha=pr_head_sha,
            worktree_path=worktree_path,
            state=state,
            remote_branch=remote_branch,
            base_branch=base_branch or "",
            operation_id=_operation_id,
            operation_type=_operation_type,
            operation_start_head=operation_start_head,
            block_resume_phase=block_resume_phase,
            reason=reason,
        )
    # Idempotent push (divergence recovery, WS-2 §5): if the preserved commit is
    # already on the remote PR branch (a monitor/worker restart re-ran the resume
    # after the push landed), treat it as a no-op rather than re-pushing.
    if protected_scope_block is None and await self._preserved_commit_already_on_remote(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        remote_branch=remote_branch,
        remote_push_url=remote_push_url,
        preserved_head_sha=preserved_head_sha,
    ):
        # An approve-and-keep resume (active grant) must NOT finalize this no-op
        # without PROOF the approved commit landed. ``_preserved_commit_already_on_remote``
        # only runs its positive SHA-containment check when ``preserved_head_sha`` is
        # set; with the marker missing it returns True on an empty worktree↔remote
        # diff ALONE, so a worktree reset to the remote head would consume the
        # single-use grant while the approved protected commit never landed. The
        # marker is recorded durably at block time and cleared only by finalize (which
        # also consumes the grant), so a still-active grant without a marker is an
        # ambiguous/anomalous state: keep the grant active and surface needs_human
        # instead of silently dropping the approved change, mirroring the post-push
        # EtU2 fall-through guard below (PR #609 comment 4521107313).
        if active_grant_specs and not preserved_head_sha:
            reason = (
                "approve-and-keep resume cannot confirm the approved protected commit "
                "landed on the remote PR branch (no preserved-head marker recorded); "
                "refusing to consume the grant on an empty-diff no-op. Re-issue the "
                "approve-and-keep grant or a directive, then resume."
            )
            mark_operator_hint_needs_human(state, reason)
            return _GitPushResult(pushed=False, failed=False, returncode=1, stderr=reason)
        pushed_head_sha = await self._rev_parse_head(worktree_path)
        if pushed_head_sha:
            state.last_push_sha = pushed_head_sha
        await _finalize_operator_hint_resume(
            self, workspace_id=workspace_id, state=state, hint=hint
        )
        return _GitPushResult(pushed=False, failed=False, returncode=0)
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
            # Disable the pre-push validation fix passes while an approve-and-keep
            # grant is still active (grant-only or directive+grant resume). The grant
            # is consumed only by ``_finalize_operator_hint_resume`` AFTER this push,
            # so a fix pass committing through ``_commit_dirty_worktree`` — which
            # consults the still-active grant — could publish NEW edits to the granted
            # protected path under an approval meant only for the preserved commit
            # (PR #609 comment 4512881681). Validation still runs; a real failure
            # surfaces and the grant survives for a re-resume.
            allow_validation_fix_passes=not active_grant_specs,
            # Suppress the non-fast-forward resync (fetch + reset --hard to the
            # remote head) while an approve-and-keep grant is active. If the PR
            # branch was advanced externally during the blocked interval, that
            # recovery would DROP the preserved protected commit the grant is meant
            # to land before the grant is consumed, then no-op the next cycle and
            # wedge the hint at ``monitoring_pr`` where ``guide --grant`` is rejected.
            # Keep the commit and re-block instead (handled below)
            # (PRRT_kwDOSJAM6s6KZK1v).
            allow_resync_on_rejection=not active_grant_specs,
        )
    )
    if push_result.failed:
        if (
            push_result.reason_code == _GIT_PUSH_REJECTED_NON_FAST_FORWARD_REASON
            and active_grant_specs
        ):
            # Approve-and-keep resume whose push was rejected non-fast-forward: the
            # PR branch was advanced externally while the workspace was blocked. The
            # push helper was told NOT to resync (``allow_resync_on_rejection`` above),
            # so the preserved protected commit is STILL the worktree HEAD — not reset
            # to the remote head. Re-block (keeping the commit) so ``guide --grant``
            # becomes reachable and the operator can resolve the divergence, instead of
            # dropping the approved commit and wedging the hint at ``monitoring_pr``
            # (PRRT_kwDOSJAM6s6KZK1v).
            reason = (push_result.stderr or "").strip() or (
                "approve-and-keep resume could not fast-forward the PR branch (it was "
                "advanced externally while the workspace was blocked); the preserved "
                "commit was kept (not reset to the remote head)"
            )
            if preserved_head_sha:
                return await _reblock_preserved_protected_leak(
                    self,
                    workspace_id=workspace_id,
                    pr_number=pr_number,
                    pr_head_sha=pr_head_sha,
                    worktree_path=worktree_path,
                    state=state,
                    remote_branch=remote_branch,
                    base_branch=base_branch or "",
                    operation_id=_operation_id,
                    operation_type=_operation_type,
                    operation_start_head=operation_start_head,
                    block_resume_phase=block_resume_phase,
                    reason=reason,
                )
            # No preserved-head marker to anchor the re-block (effectively unreachable
            # for a grant-active resume, which always followed a genuine block). Park
            # needs_human with a NON-failed result so the recoverable workspace stays
            # at ``monitoring_pr`` rather than being terminally failed.
            mark_operator_hint_needs_human(state, reason)
            return _GitPushResult(pushed=False, failed=False, returncode=1, stderr=reason)
        if (
            push_result.reason_code == _PROTECTED_SCOPE_PUSH_BLOCKED_REASON
            or push_result.protected_scope_diff_unavailable
        ):
            default_reason = (
                "protected-scope diff unavailable blocked the operator hint repair push"
                if push_result.protected_scope_diff_unavailable
                else "protected-scope policy blocked the operator hint repair push"
            )
            reason = (push_result.stderr or "").strip() or default_reason
            mark_operator_hint_needs_human(state, reason)
            return cast(_GitPushResult, push_result)
        if (
            push_result.reason_code == _PRE_PUSH_VALIDATION_FAILED_REASON
            and active_grant_specs
            and not hint.directive
        ):
            # Grant-only (approve-and-keep) resume: the CLI is SKIPPED (no directive)
            # and the pre-push validation fix passes are DISABLED while the grant is
            # active (see ``allow_validation_fix_passes`` above), so NOTHING in the
            # worktree can change between iterations. ``PRE_PUSH_VALIDATION_FAILED`` is
            # non-terminal, so the ``AddressOperatorHint`` failure branch would leave
            # the hint pending and the next monitor cycle would re-run the identical
            # grant-only resume — re-failing the same validation unchanged until the
            # outer loop eventually ``_terminate_failed``s the workspace (which also
            # rejects a later approve-and-keep grant because the row is no longer
            # recoverable), never surfacing an operator-actionable pause. Mark the hint
            # ``needs_human`` and return a NON-failed result so the loop parks the
            # workspace at ``monitoring_pr`` awaiting the operator (who must fix the
            # preserved commit or withdraw the grant, then resume), mirroring the other
            # needs-human branches (PRRT_kwDOSJAM6s6KI221).
            reason = (push_result.stderr or "").strip() or (
                "grant-only operator resume could not pass pre-push validation and has "
                "no CLI or fix-pass step to change the worktree; fix the preserved "
                "commit or withdraw the approve-and-keep grant, then resume"
            )
            mark_operator_hint_needs_human(state, reason)
            return _GitPushResult(pushed=False, failed=False, returncode=1, stderr=reason)
        return cast(_GitPushResult, push_result)
    if not push_result.pushed:
        # A fixed verdict can reflect non-code PR work (for example posting an
        # allowed reply) where there is no commit to publish. A successful no-op
        # push still means the operator hint was handled.
        #
        # BUT a protected-block grant resume MUST land its approved preserved
        # commit. If the worktree was reset/recreated at the remote head during
        # the blocked interval, that commit is gone and the push no-ops as
        # "everything up-to-date". The SHA-containment check guards the early skip
        # and the idempotent no-op return above, but NOT this fall-through; without
        # this guard the no-op would consume the grant and mark the hint processed
        # even though the approved protected change never landed — silently dropping
        # it. When a preserved HEAD SHA was recorded and is NOT on the remote head,
        # keep the grant active and surface needs_human instead of finishing the
        # bookkeeping (PRRT_kwDOSJAM6s6KEtU2).
        #
        # Scope this to grant-only (approve-and-keep) resumes: a DIRECTIVE revert
        # can legitimately remove the preserved commit by resetting/cleaning the
        # worktree back to the remote PR head, so an up-to-date no-op push is a valid
        # successful revert — requiring the old preserved SHA on the remote would
        # wedge an applied directive at needs_human (PRRT_kwDOSJAM6s6KFytV).
        if (
            not hint.directive
            and preserved_head_sha
            and not await self._preserved_commit_already_on_remote(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                remote_branch=remote_branch,
                remote_push_url=remote_push_url,
                preserved_head_sha=preserved_head_sha,
            )
        ):
            reason = (
                "operator-approved preserved commit "
                f"{preserved_head_sha} is not on the remote PR branch after a no-op "
                "push; the worktree was reset and the approved protected change was "
                "dropped"
            )
            mark_operator_hint_needs_human(state, reason)
            return cast(_GitPushResult, push_result)
        await _finalize_operator_hint_resume(
            self, workspace_id=workspace_id, state=state, hint=hint
        )
        return cast(_GitPushResult, push_result)

    pushed_head_sha = await self._rev_parse_head(worktree_path)
    if pushed_head_sha:
        state.last_push_sha = pushed_head_sha
    # Single-use: consume the operator grants now that the resumed change pushed,
    # so a later DIFFERENT protected change re-blocks and must be granted again.
    await _finalize_operator_hint_resume(self, workspace_id=workspace_id, state=state, hint=hint)
    return cast(_GitPushResult, push_result)


async def _reblock_preserved_protected_leak(
    self: Any,
    *,
    workspace_id: str,
    pr_number: int,
    pr_head_sha: str,
    worktree_path: Path,
    state: MonitorState,
    remote_branch: str,
    base_branch: str,
    operation_id: str | None,
    operation_type: str | None,
    operation_start_head: str | None,
    block_resume_phase: str,
    reason: str,
) -> _GitPushResult:
    """Re-block a still-undeliverable preserved protected commit into ``blocked``.

    Shared by the directive-leak guard and the grant-active non-fast-forward
    rejection branch. Both reach a state where the preserved ungranted/undelivered
    protected commit is still in local history but the workspace is at
    ``monitoring_pr`` — where ``guide_workspace`` rejects ``--grant`` inputs
    (``WorkspaceGuideGrantNotAllowedError``). Rebuild the protected block from the
    violations recorded at the original block (exactly the protected paths the
    preserved commit changed) and reuse the shared WS-1 ``enter_blocked`` pause path
    so the existing blocked-status + operator-grant machinery resolves it
    (``guide --grant`` -> ``blocked -> monitoring_pr`` grant resume -> grant-aware
    push). The resume ORIGIN is preserved exactly as the net-diff re-block branch.

    Defensive fallback: a real protected block always records >=1 violation
    (``_protected_scope_push_block`` only returns a block for non-empty violations),
    so a ``None`` reconstruction is effectively unreachable once the preserved-head
    marker was set by a genuine block. When nothing can be reconstructed, mark the
    hint needs_human and return a NON-failed result so the loop parks the recoverable
    workspace at ``monitoring_pr`` rather than ``_terminate_failed``ing it — a
    failed/terminal row would also reject a later approve-and-keep grant
    (PRRT_kwDOSJAM6s6KHEEU)."""
    leak_block = await _directive_preserved_leak_protected_block(
        self, workspace_id=workspace_id, message=reason
    )
    if leak_block is not None:
        reblock_resume_phase = (
            MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE
            if block_resume_phase == MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE
            else MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE
        )
        reblock_result = cast(
            _GitPushResult,
            await self._pause_monitor_for_protected_scope_block(
                workspace_id=workspace_id,
                pr_number=pr_number,
                pr_head_sha=pr_head_sha,
                protected_scope_block=leak_block,
                worktree_path=worktree_path,
                resume_phase=reblock_resume_phase,
                state=state,
                remote_branch=remote_branch,
                base_branch=base_branch,
                operation_id=operation_id,
                operation_type=operation_type,
                source_head_sha=operation_start_head,
            ),
        )
        if reblock_result.paused_into_blocked:
            # Mirror the net-diff re-block branch: the bumped block epoch supersedes
            # this hint, so clear the in-memory monitor hint and let the resume
            # re-arm a fresh one.
            state.pending_operator_hint = None
        return reblock_result
    mark_operator_hint_needs_human(state, reason)
    return _GitPushResult(pushed=False, failed=False, returncode=1, stderr=reason)


async def _directive_preserved_leak_protected_block(
    self: Any, *, workspace_id: str, message: str
) -> _ProtectedScopePushBlock | None:
    """Rebuild the protected block from the recorded violations for a leak re-block.

    The directive-leak guard fires when the net diff is clean
    (``_protected_scope_push_block`` returned ``None``) yet the preserved ungranted
    protected commit is still in the unpushed range. To re-block (so ``guide
    --grant`` becomes reachable) the shared ``enter_blocked`` pause path needs the
    protected violations, but the net-diff block carrying them is ``None`` here.
    Reconstruct them from ``workspace.block_violations`` — recorded by the original
    block, i.e. exactly the protected paths the preserved commit changed — using
    ``message`` (the leak reason) as the block message so the pause event and
    operator notification stay informative. Returns ``None`` when no violation can
    be rebuilt so the caller falls back to parking a needs_human hint."""
    async with self._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        if workspace is None:  # pragma: no cover - the active monitor cycle holds the row
            return None
        recorded = list(workspace.block_violations or [])
    violations: list[QualityGateViolation] = []
    for entry in recorded:
        path = entry.get("path")
        if not path:
            continue
        line = entry.get("line")
        violations.append(
            QualityGateViolation(
                path=str(path),
                protected_pattern=str(entry.get("protected_pattern") or path),
                section=cast("str | None", entry.get("section")),
                line=line if isinstance(line, int) and not isinstance(line, bool) else None,
                reason=str(
                    entry.get("reason")
                    or "protected quality-gate file changed outside declared owned_paths"
                ),
            )
        )
    if not violations:
        return None
    return _ProtectedScopePushBlock(
        message=message,
        reason_code=_PROTECTED_SCOPE_PUSH_BLOCKED_REASON,
        violations=tuple(violations),
    )


async def _terminal_directive_grant_reblock(
    self: Any,
    *,
    workspace_id: str,
    pr_number: int,
    pr_head_sha: str,
    worktree_path: Path,
    state: MonitorState,
    remote_branch: str,
    base_branch: str | None,
    operation_id: str | None,
    operation_type: str | None,
    operation_start_head: str | None,
    preserved_head_sha: str | None,
    active_grant_specs: Any,
    verdict: VerdictResult,
) -> _GitPushResult | None:
    """Re-block a TERMINAL combined directive+grant protected-block resume.

    A monitor-origin protected block resumed with a combined ``--directive ...
    --grant ...`` runs the directive CLI; when that pass returns a TERMINAL verdict
    (``agent_failed`` / ``needs_human`` / ``defer`` / ``false_positive``) BEFORE any
    push, the preserved protected commit and the single-use approve-and-keep grant
    are BOTH still unfinalized (the grant is consumed and the marker dropped only on
    a successful resume). Merely parking the hint at ``monitoring_pr`` strands the
    operator: ``decide()`` only surfaces ``NotifyHuman`` while ``guide_workspace``
    rejects new ``--grant`` inputs unless the row is ``blocked`` and a fresh
    ``--directive`` revokes the existing grant — so neither approve-and-keep nor
    consuming the grant to land the preserved commit is reachable. Re-block via the
    shared WS-1 pause path (mirroring the directive-leak guard above) so
    ``guide --grant`` / a new directive can resolve it; the preserved commit is kept
    and the bumped block epoch supersedes the stale grant (PRRT_kwDOSJAM6s6KT0rd).

    Scoped to a grant-bearing preserved-block resume (``preserved_head_sha`` AND
    ``active_grant_specs`` present). Returns ``None`` otherwise — a plain remonitor,
    a directive-only resume, or a grant-only resume (which never runs the CLI) keep
    the existing park-the-hint behavior — and also when no violation can be
    reconstructed, so the caller falls back to parking the terminal hint. Returns
    ``None`` too when the directive CLI moved HEAD away from the preserved commit so
    it is no longer reachable: re-blocking would overwrite the preserved marker with
    the moved HEAD and let a later grant-only resume no-op while consuming the grant
    (PRRT_kwDOSJAM6s6KVCYD). In that dropped-commit case it ALSO clears the stale
    preserved-head marker AND consumes the single-use grant before parking. Clearing
    the marker is required because with a grant still active
    ``_clear_dropped_preserved_marker_after_terminal_directive`` deliberately skips
    clearing, so otherwise a later FRESH directive (which revokes the grant in
    ``guide_workspace``) would leave no active grants plus a stale marker and a local
    HEAD already on the remote — satisfying the directive-drop restart shortcut at the
    top of ``_run_operator_hint_cycle``, which would finalize the new hint as a no-op
    and SKIP the CLI, silently ignoring the operator's follow-up (PRRT_kwDOSJAM6s6KVgwV).
    Consuming the grant is required because clearing the marker drops
    ``has_preserved_protected_block`` to False, so ``decide()`` stops running this
    resume ahead of ``SyncBase``; a surviving grant for the dropped commit would then
    let a base-sync conflict edit push to the granted protected path under a stale
    approval — a grant leak (PRRT_kwDOSJAM6s6KVt_Q)."""
    if not (preserved_head_sha and active_grant_specs):
        return None
    # The combined directive CLI may have MOVED HEAD before returning the terminal
    # verdict — e.g. it reset the worktree back to the remote PR head (dropping the
    # preserved protected commit) and then reported needs_human. Re-blocking here
    # routes through ``_pause_monitor_for_protected_scope_block``, which re-records
    # the CURRENT rev-parse HEAD as the preserved marker, OVERWRITING the original
    # ``preserved_head_sha``. With the marker now pointing at the reset HEAD (already
    # on the remote), a later grant-only resume would see it already pushed, finalize
    # as a no-op, and CONSUME the single-use grant while the approved protected commit
    # never lands. Only re-block while the ORIGINAL preserved commit is still
    # reachable from HEAD (the operator can still land it via approve-and-keep);
    # otherwise fall back to parking the terminal hint so the operator is surfaced
    # without silently burning the grant (PRRT_kwDOSJAM6s6KVCYD).
    if not await self._preserved_commit_reachable_from_head(
        worktree_path=worktree_path,
        preserved_head_sha=preserved_head_sha,
    ):
        # The preserved commit was DROPPED by the combined directive CLI. There is
        # nothing left to protect, so clear the stale marker before parking — the
        # grant-active early return in
        # ``_clear_dropped_preserved_marker_after_terminal_directive`` would otherwise
        # leave it set, and a later grant-revoking directive's drop-restart shortcut
        # would silently no-op the operator's follow-up (PRRT_kwDOSJAM6s6KVgwV).
        # Clearing the marker drops ``state.has_preserved_protected_block`` to False, so
        # ``decide()`` no longer runs this protected resume ahead of ``SyncBase``. The
        # single-use grant approved ONLY the now-dropped preserved commit, yet it stays
        # active until consumed; if the base advanced, the next poll runs ``SyncBase``
        # first and ``_protected_scope_violations_for_sync_base_push`` loads that
        # still-active grant, letting a conflict-resolution edit on the granted
        # protected path push under an approval meant for the dropped commit — a grant
        # leak (PRRT_kwDOSJAM6s6KVt_Q, the KGX2A class). Consume the grant together with
        # the marker so nothing carries the stale approval forward; a re-introduced
        # protected change re-blocks and must be granted again.
        state.threads_addressed_ids.pop(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, None)
        # Make the marker clear DURABLE before parking, mirroring the directive-only
        # drop branch (PRRT_kwDOSJAM6s6KWAIx). Popping only the in-memory copy here is
        # not durable until the loop's later _persist_state, and the follow-up
        # _clear_dropped_preserved_marker_after_terminal_directive early-returns once a
        # grant is present, so nothing else persists the removal. The marker clear and
        # the grant consume must be ATOMIC: clearing the marker drops
        # ``has_preserved_protected_block`` to False so ``decide()`` stops ranking this
        # resume ahead of ``SyncBase``, so a crash BETWEEN two separate commits (marker
        # cleared, grant not yet consumed) would durably leave marker-gone + grant-active
        # — the very KVt_Q grant leak the consume closes. Do both in one transaction so a
        # crash before the commit leaves marker-present + grant-active (re-blocks /
        # re-runs safely) and a crash after leaves marker-gone + grant-consumed.
        await self._clear_preserved_marker_and_consume_grants_durably(workspace_id)
        return None
    message = (
        f"operator directive resume returned '{verdict.verdict}' before landing the "
        f"approved protected change for preserved commit {preserved_head_sha}; "
        "re-blocking so an approve-and-keep grant or a new directive can resolve it"
    )
    leak_block = await _directive_preserved_leak_protected_block(
        self, workspace_id=workspace_id, message=message
    )
    if leak_block is None:
        return None
    # Preserve the resume ORIGIN exactly as the net-diff / leak re-block branches.
    block_resume_phase = await self._resolve_block_resume_phase(workspace_id)
    reblock_resume_phase = (
        MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE
        if block_resume_phase == MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE
        else MONITOR_PROTECTED_SCOPE_PUSH_RESUME_PHASE
    )
    reblock_result = cast(
        _GitPushResult,
        await self._pause_monitor_for_protected_scope_block(
            workspace_id=workspace_id,
            pr_number=pr_number,
            pr_head_sha=pr_head_sha,
            protected_scope_block=leak_block,
            worktree_path=worktree_path,
            resume_phase=reblock_resume_phase,
            state=state,
            remote_branch=remote_branch,
            base_branch=base_branch or "",
            operation_id=operation_id,
            operation_type=operation_type,
            source_head_sha=operation_start_head,
        ),
    )
    if reblock_result.paused_into_blocked:
        # Mirror the leak / net-diff re-block branches: the bumped block epoch
        # supersedes this hint, so clear the in-memory monitor hint and let the
        # resume re-arm a fresh one.
        state.pending_operator_hint = None
    return reblock_result


async def _clear_dropped_preserved_marker_after_terminal_directive(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    state: MonitorState,
    hint: OperatorHint,
    active_grant_specs: Any,
    preserved_head_sha: str | None,
) -> None:
    """Drop the preserved-head marker when a directive-only resume RESET the preserved
    commit away before returning a TERMINAL verdict.

    A directive-only protected-block resume can resolve the block by dropping the
    preserved offending commit (resetting the worktree back to the remote PR head) and
    then return a TERMINAL verdict (``agent_failed`` / ``needs_human`` / ``defer`` /
    ``false_positive``) BEFORE any push. A terminal verdict never finalizes, so the
    preserved-head marker — recorded at block time — survives in monitor state even
    though the commit it pointed at is gone. Because the drop leaves the local HEAD
    already contained in the remote, a LATER operator guide carrying a FRESH directive
    and no grants would satisfy the directive-drop restart shortcut at the top of
    ``_run_operator_hint_cycle``: that shortcut only checks the local HEAD is contained
    in the remote, NOT that the pending operation is the one that ran the drop, so it
    would finalize the new guide as a no-op and SKIP the CLI — silently ignoring the
    follow-up instruction (PRRT_kwDOSJAM6s6KVCYE). Once the preserved commit is dropped
    there is nothing left to protect, so clear the marker; a genuinely new protected
    change re-records a fresh marker via ``_protected_scope_push_block``.

    Scoped to directive-only resumes (``hint.directive`` AND no active grants): a
    grant-bearing terminal resume is handled by ``_terminal_directive_grant_reblock``
    (it re-blocks while the preserved commit is reachable, and clears the marker before
    parking when the commit was dropped — PRRT_kwDOSJAM6s6KVgwV), and the
    two restart shortcuts both require ``not active_grant_specs`` so a surviving grant
    already excludes them. Clears ONLY when a resolvable local HEAD proves the preserved
    commit is gone from local history: an unresolvable HEAD is ambiguous (the drop may
    not have landed), so the marker is RETAINED to keep the leak guard armed for the
    corrected resume — mirroring the shortcut's own HEAD-missing skip
    (PRRT_kwDOSJAM6s6KUx2T).

    The clear is made DURABLE before returning, not left to the outer loop's later
    ``_persist_state``. The block-time RECORDING of the marker is durable as soon as
    the block transition commits (PRRT_kwDOSJAM6s6KEtU6); the clear must be symmetric.
    The terminal verdict that drove this drop is marked in memory by the caller AFTER
    this returns and persisted only at the end of the loop cycle, so a crash in that
    window would otherwise reload the pending directive with the STALE marker — and the
    directive-drop restart shortcut at the top of ``_run_operator_hint_cycle`` (which
    only checks the local HEAD is on the remote) would finalize the new hint as a no-op,
    SKIP the CLI, and swallow the terminal verdict (PRRT_kwDOSJAM6s6KWAIx). Removing the
    key durably leaves NO marker after a crash, so the shortcut is skipped and the
    directive CLI re-runs behind the normal guards and re-derives the verdict."""
    if not (hint.directive and preserved_head_sha) or active_grant_specs:
        return
    local_head_sha = await self._rev_parse_head(worktree_path)
    if not local_head_sha:
        return
    if await self._preserved_commit_reachable_from_head(
        worktree_path=worktree_path,
        preserved_head_sha=preserved_head_sha,
    ):
        return
    state.threads_addressed_ids.pop(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, None)
    await self._clear_preserved_head_marker_durably(workspace_id)


async def _finalize_operator_hint_resume(
    self: Any, *, workspace_id: str, state: MonitorState, hint: OperatorHint | None = None
) -> None:
    """Close out a settled protected-block resume so the next cycle starts clean.

    Bundles the teardown steps a finalized resume must perform: consume the
    single-use operator grants AND durably drop the preserved-head marker in ONE
    transaction, reset the ``block_resume_phase`` discriminator (set at block time
    and never overwritten except by a fresh block) so a later operator-hint /
    remonitor cycle — which arms a hint WITHOUT re-blocking — cannot reuse a stale
    sync-base phase to skew the protected-scope validator selection
    (PRRT_kwDOSJAM6s6KFqEg), and mark the operator hint processed in memory.

    The marker drop MUST be durable, not left to the outer loop's later
    ``_persist_state``: the grant consume commits in its own helper transaction, so a
    crash AFTER it but BEFORE ``_persist_state`` would durably reload the row with the
    grant GONE but the preserved-head marker STILL PRESENT (recorded durably at block
    time, PRRT_kwDOSJAM6s6KEtU6). A fresh operator guide arriving before recovery would
    then satisfy the no-op restart shortcut at the top of ``_run_operator_hint_cycle``
    (``not active_grant_specs`` + the old preserved SHA already on the remote) and be
    finalized WITHOUT running its directive — silently swallowing the follow-up
    (PRRT_kwDOSJAM6s6KaBND, the KE2BX/KWAIx durability class). Clearing the marker and
    consuming the grant atomically keeps marker-gone tied to grant-consumed across a
    crash, so the shortcut is skipped (no marker) until a fresh block re-records one.
    ``_finalize_processed_operator_hint`` still pops the in-memory copy and marks the
    hint processed for the remainder of this cycle."""
    await self._clear_preserved_marker_and_consume_grants_durably(workspace_id)
    await self._clear_block_resume_phase(workspace_id)
    _finalize_processed_operator_hint(state, hint=hint)


def _finalize_processed_operator_hint(
    state: MonitorState, *, hint: OperatorHint | None = None
) -> None:
    """Mark the operator hint processed and drop the protected-block preserved-head
    marker.

    The marker (``_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY``) is recorded at block
    time and powers the divergence-recovery / restart-after-consume short-circuits
    for THIS resume only. Once the resume is finalized it has served its purpose;
    leaving it in persisted monitor state would let a later plain remonitor (no
    directive, no grant) whose old preserved commit is still on the remote take the
    restart-recovery shortcut and skip the CLI — silently ignoring the operator's
    new repair request (PRRT_kwDOSJAM6s6KE2BX). A fresh block re-records the marker.
    """
    pending_hint = getattr(state, "pending_operator_hint", None)
    active_hint = pending_hint or hint
    _mark_referenced_needs_human_feedback_answered(state, hint=active_hint)
    state.threads_addressed_ids.pop(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, None)
    if hasattr(state, "pending_operator_hint") and pending_hint is None and active_hint is not None:
        state.pending_operator_hint = active_hint
    mark_operator_hint_processed(state)


def _mark_referenced_needs_human_feedback_answered(
    state: MonitorState, *, hint: OperatorHint | None = None
) -> None:
    """Retire review-level ``needs_human`` verdicts a guide explicitly answered.

    Operator guides are the sanctioned path for resolving a monitor HUMAN_WAIT.
    For review-level comments there is no GitHub thread to resolve, so a consumed
    guide that names the original issue/review feedback id must also update the
    persisted verdict. Otherwise the hint is marked processed and the next
    ``decide()`` poll immediately re-enters the same stale HUMAN_WAIT.

    This helper intentionally leaves any stored ``__review_comment_body_hash__``
    marker unchanged because it does not receive the live ``ReviewComment`` needed
    to recompute the hash. A caller that retires the verdict while holding that
    object should refresh the marker at the same time; otherwise a concurrent
    reviewer edit can make the next stale-state sweep clear this false-positive
    retirement and requeue the comment for triage.
    """
    if hint is None:
        return
    text = "\n".join(part for part in (hint.directive, hint.reason) if part)
    if not text:
        return
    for referenced_id in _operator_hint_feedback_id_candidates(text):
        for item_id in _operator_hint_feedback_storage_key_candidates(referenced_id):
            if state.threads_addressed_ids.get(item_id) != "needs_human":
                continue
            state.mark_addressed(item_id, "false_positive")
            state.threads_addressed_ids.pop(f"__needs_human_reason__:{item_id}", None)
            break


def _operator_hint_feedback_id_candidates(text: str) -> tuple[str, ...]:
    candidates: list[str] = []
    seen: set[str] = set()
    for match in _OPERATOR_HINT_FEEDBACK_ID_RE.finditer(text):
        item_id = match.group(0)
        if item_id in seen:
            continue
        seen.add(item_id)
        candidates.append(item_id)
    return tuple(candidates)


def _operator_hint_feedback_storage_key_candidates(referenced_id: str) -> tuple[str, ...]:
    if referenced_id.startswith("issue:"):
        return (referenced_id, referenced_id.removeprefix("issue:"))
    return (referenced_id,)


def _operator_hint_block_reason(verdict: VerdictResult) -> str:
    if verdict.verdict == "false_positive":
        return verdict.reason or "agent reported the operator hint was not actionable"
    if verdict.verdict == "defer":
        return verdict.reason or "agent deferred the operator hint"
    if verdict.verdict == "needs_human":
        return verdict.reason or "agent requested human input for the operator hint"
    return verdict.reason or "agent failed while processing the operator hint"
