"""Operator remonitor hint repair handling."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

from awf.common.github_client import RepoRef
from awf.control.blocked_transition import (
    MONITOR_PROTECTED_SCOPE_SYNC_BASE_RESUME_PHASE,
    monitor_protected_scope_resume_origin,
)
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.monitor_prompts import operator_hint_prompt
from awf.runtime.operator_hints import (
    mark_operator_hint_agent_failed,
    mark_operator_hint_needs_human,
)
from awf.runtime.pr_monitor import (
    _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY,
    MonitorState,
    OperatorHint,
)
from awf.runtime.pr_monitor_runner.comment_repair_provenance_retirement import (
    retire_orphaned_item_commit_records,
)
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AgentVerdictExecutionError,
    AgentVerdictProtocolError,
)
from awf.runtime.pr_monitor_runner.comments import MonitorVerdictResult, VerdictResult
from awf.runtime.pr_monitor_runner.constants import (
    _GIT_PUSH_REJECTED_NON_FAST_FORWARD_REASON,
    _PARK_RESUME_UNEXPECTED_WORKTREE_CHANGES_REASON,
    _PROTECTED_SCOPE_PUSH_BLOCKED_REASON,
)
from awf.runtime.pr_monitor_runner.fix_cycle_terminal_provenance import (
    _agent_verdict_protocol_failure_result,
)

# The protected-block re-block helpers live in a sibling module (file-line
# guardrail); the resume cycle below and existing test imports resolve them here.
from awf.runtime.pr_monitor_runner.operator_hint_protected_reblock import (
    _clear_dropped_preserved_marker_after_terminal_directive,
    _reblock_preserved_protected_leak,
    _terminal_directive_grant_reblock,
)
from awf.runtime.pr_monitor_runner.operator_hint_retirement import (
    _finalize_processed_operator_hint as _finalize_processed_operator_hint,
)
from awf.runtime.pr_monitor_runner.operator_hint_retirement import (
    _mark_referenced_needs_human_feedback_answered as _mark_referenced_needs_human_feedback_answered,
)
from awf.runtime.pr_monitor_runner.operator_hint_timeout_retry import (
    clear_timeout_retry,
    mark_timeout_retry_used_durably,
    should_retry_timed_out_hint,
    timeout_retry_reason_code,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_constants import (
    _PRE_PUSH_VALIDATION_FAILED_REASON,
)
from awf.runtime.pr_monitor_runner.provider_park_stranded_edits import (
    commit_provider_park_stranded_edits,
    provider_park_resume_kept_head,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
)

_PROTECTED_HISTORY_DIRECTIVE_REBLOCK_PREFIX = "__awf_protected_history_directive_reblocked__:"

# Verdicts that END the resume without a push: each one parks the hint for a human
# instead of continuing toward merge, so each must clear the post-action terminal-PR
# guard first (#910).
_TERMINAL_HINT_VERDICTS = frozenset({"agent_failed", "needs_human", "defer", "false_positive"})

# CLI errors that END the resume without a push. Like the terminal verdicts above,
# each one either fails the workspace or parks a needs_human hint, so each must clear
# the post-action terminal-PR guard first (#910).
_OperatorHintAgentError = (
    AgentVerdictProtocolError
    | ProtectedScopeDiffError
    | _MonitorPolicyBlockedError
    | _MonitorAgentRuntimeOwnershipRepairFailedError
    | _MonitorHeadObjectMissingError
    | _MonitorMirrorHooksPathRepairFailedError
)


def _protected_history_directive_reblock_key(preserved_head_sha: str, directive: str) -> str:
    digest = hashlib.sha256(directive.strip().encode("utf-8")).hexdigest()[:16]
    return f"{_PROTECTED_HISTORY_DIRECTIVE_REBLOCK_PREFIX}{preserved_head_sha}:{digest}"


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
    # A managed-key park whose preserving sink failed left the repair's edits
    # uncommitted; finish that sink before the guard below rejects them as
    # pre-existing dirt, or the budget-raise resume could never continue the repair
    # (PRRT_kwDOSJAM6s618pY4). Its sink errors end the resume like the agent's own.
    try:
        stranded_result = await commit_provider_park_stranded_edits(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            state=state,
            compose_project=compose_project,
            compose_file=compose_file,
            remote_branch=remote_branch,
            remote_push_url=remote_push_url,
            operation_type="operator_hint_repair",
        )
    except (
        ProtectedScopeDiffError,
        _MonitorPolicyBlockedError,
        _MonitorAgentRuntimeOwnershipRepairFailedError,
        _MonitorHeadObjectMissingError,
        _MonitorMirrorHooksPathRepairFailedError,
    ) as exc:
        terminal_moot_result = await self._post_action_pr_terminal_push_result_if_moot(
            workspace_id=workspace_id,
            pr_number=pr_number,
            context="operator_hint_agent_error",
            operation_id=_operation_id,
            operation_type=_operation_type,
            repo=repo,
            worktree_path=worktree_path,
        )
        if terminal_moot_result is not None:
            return cast(_GitPushResult, terminal_moot_result)
        return await _operator_hint_agent_error_result(
            self,
            exc=exc,
            workspace_id=workspace_id,
            remote_branch=remote_branch,
            state=state,
            pr_number=pr_number,
            repo=repo,
        )
    if stranded_result is not None:
        if stranded_result.reason_code == _PARK_RESUME_UNEXPECTED_WORKTREE_CHANGES_REASON:
            # Changes the park did not leave stay put for a human (non-terminal);
            # like the sink errors above, a PR that ended meanwhile needs no one.
            terminal_moot_result = await self._post_action_pr_terminal_push_result_if_moot(
                workspace_id=workspace_id,
                pr_number=pr_number,
                context="operator_hint_provider_park_resume",
                operation_id=_operation_id,
                operation_type=_operation_type,
                repo=repo,
                worktree_path=worktree_path,
            )
            if terminal_moot_result is not None:
                return cast(_GitPushResult, terminal_moot_result)
            mark_operator_hint_needs_human(state, stranded_result.stderr)
        return stranded_result
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
    # A directive that dropped a rejected protected-scope commit from local history
    # orphans the item records citing it; retire them with an audit event so they
    # stop standing in as FIXED evidence for a commit the branch no longer has
    # (#1200).
    await retire_orphaned_item_commit_records(self, workspace_id=workspace_id, state=state)

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
            self,
            workspace_id=workspace_id,
            state=state,
            hint=hint,
            # This recovery shortcut skips the CLI, so a directiveless remonitor
            # reason is audit context, not acted-on feedback text.
            acted_feedback_text=hint.directive,
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
                self,
                workspace_id=workspace_id,
                state=state,
                hint=hint,
                acted_feedback_text=hint.directive,
            )
            return _GitPushResult(pushed=False, failed=False, returncode=0)
        # Repeat-directive short-circuit: a prior cycle re-blocked this exact
        # ``(preserved commit, directive)`` for leaving the ungranted protected commit
        # in local history (revert-on-top), so re-running the identical directive would
        # only repeat the leak — park needs_human without burning CLI tokens. But the
        # stored marker alone is NOT proof the leak still exists: a valid reset/rebase
        # (or an unfinalized drop whose marker clear was lost to a crash) can remove the
        # preserved commit from the unpushed range while the marker lingers. Re-confirm
        # against the same safety boundary the leak guard below uses —
        # ``_preserved_commit_in_unpushed_range`` returns False once the commit was
        # dropped or is already on the remote — so a now-resolved directive falls through
        # to normal processing instead of being wedged at needs_human
        # (PRRT_kwDOSJAM6s6Ms-zG).
        repeat_key = _protected_history_directive_reblock_key(preserved_head_sha, hint.directive)
        if state.threads_addressed_ids.get(
            repeat_key
        ) and await self._preserved_commit_in_unpushed_range(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            remote_branch=remote_branch,
            remote_push_url=remote_push_url,
            preserved_head_sha=preserved_head_sha,
        ):
            reason = (
                "The previous operator directive still left the ungranted protected "
                f"commit {preserved_head_sha} in local history. Provide a new directive "
                "that drops the commit with reset/rebase/cherry-pick, not a revert-on-top "
                "commit, or approve-and-keep the protected path."
            )
            mark_operator_hint_needs_human(state, reason)
            return _GitPushResult(pushed=False, failed=False, returncode=1, stderr=reason)
    acted_feedback_text = hint.directive
    # Set when a parked verdict kept a commit that must still be pushed (#1074).
    hint_parked = False
    if hint.directive or not active_grant_specs:
        if not hint.directive:
            acted_feedback_text = hint.reason
        # A directive against a preserved protected commit is told to reset to the
        # published PR head, not revert on top (#1200). Only a grant covering EVERY
        # blocked path keeps that commit; a partial grant still leaves an ungranted
        # change the push leak guard re-blocks, so it keeps the reset guidance
        # (PRRT_kwDOSJAM6s61o81S).
        reset_guidance_head_sha = (
            preserved_head_sha
            if hint.directive
            and (
                not active_grant_specs
                or not await self._preserved_protected_change_fully_granted(
                    workspace_id=workspace_id,
                    grant_specs=active_grant_specs,
                )
            )
            else None
        )
        prompt = operator_hint_prompt(
            pr_number=pr_number,
            repo_slug=repo.slug(),
            reason=hint.reason,
            directive=hint.directive,
            operation_id=hint.operation_id,
            workspace_runtime_context=self._workspace_runtime_context,
            task_tag=task_tag,
            preserved_head_sha=reset_guidance_head_sha,
            published_head_sha=pr_head_sha,
        )
        verdict: VerdictResult | MonitorVerdictResult
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
                # Prompt allows FIXED for GitHub-side / no-code directives.
                require_fix_evidence=False,
            )
        except AgentVerdictExecutionError as exc:
            verdict = MonitorVerdictResult(
                verdict="agent_failed",
                # Prefer the #932 preserved-work narrative when there is one;
                # the bare reason code remains the fallback and stays available
                # to the retry gate via ``reason_code``.
                reason=exc.reason or exc.reason_code,
                reason_code=exc.reason_code,
                preserved_head_sha=exc.preserved_head_sha,
            )
        except (
            AgentVerdictProtocolError,
            ProtectedScopeDiffError,
            _MonitorPolicyBlockedError,
            _MonitorAgentRuntimeOwnershipRepairFailedError,
            _MonitorHeadObjectMissingError,
            _MonitorMirrorHooksPathRepairFailedError,
        ) as exc:
            # A CLI error ENDS the resume just as terminally as a parked verdict:
            # it either fails the workspace (protocol violation, ownership/mirror
            # repair failure) or arms a needs_human notification. Re-read PR state
            # BEFORE building any of those results — a resume whose CLI outlived its
            # PR would otherwise terminally fail (or ping a human on) an already
            # merged/closed PR and record no ``workspace.monitor_action_moot`` event
            # (#910). The check runs ahead of the per-error handling so no side
            # effect — the protected-scope diff probe, the hint marking — fires
            # either.
            terminal_moot_result = await self._post_action_pr_terminal_push_result_if_moot(
                workspace_id=workspace_id,
                pr_number=pr_number,
                context="operator_hint_agent_error",
                operation_id=_operation_id,
                operation_type=_operation_type,
                repo=repo,
                worktree_path=worktree_path,
            )
            if terminal_moot_result is not None:
                return cast(_GitPushResult, terminal_moot_result)
            return await _operator_hint_agent_error_result(
                self,
                exc=exc,
                workspace_id=workspace_id,
                remote_branch=remote_branch,
                state=state,
                pr_number=pr_number,
                repo=repo,
            )
        # The directive agent is told to reset a preserved protected commit away,
        # so it can orphan item records the pre-agent pass above still saw as
        # ancestors of HEAD. Recheck now that history may be rewritten, before any
        # verdict path pushes or finishes the resume (PRRT_kwDOSJAM6s61n6zY).
        await retire_orphaned_item_commit_records(self, workspace_id=workspace_id, state=state)
        # A TERMINAL verdict parks the hint (``needs_human`` / ``agent_failed``) and
        # RETURNS from inside this block, so it never reaches the post-action guard
        # below. Re-read PR state here too: a resume whose CLI outlived its PR would
        # otherwise arm a stale human notification against an already merged/closed PR
        # and record no ``workspace.monitor_action_moot`` event (#910).
        # ``_terminal_directive_grant_reblock`` re-checks as well, but only on the
        # grant-bearing preserved-block path (it returns ``None`` before its own guard
        # when ``preserved_head_sha`` / ``active_grant_specs`` are absent), so the plain
        # resume — no marker, no grant — needs the check at this caller level.
        if verdict.verdict in _TERMINAL_HINT_VERDICTS:
            terminal_moot_result = await self._post_action_pr_terminal_push_result_if_moot(
                workspace_id=workspace_id,
                pr_number=pr_number,
                context="operator_hint_terminal_verdict",
                operation_id=_operation_id,
                operation_type=_operation_type,
                repo=repo,
                worktree_path=worktree_path,
            )
            if terminal_moot_result is not None:
                return cast(_GitPushResult, terminal_moot_result)
        if verdict.verdict == "agent_failed":
            if should_retry_timed_out_hint(state, hint, verdict):
                # The watchdog fired but the agent's work survived (#932). Spend
                # the single retry and leave the hint ``pending`` so ``decide()``
                # returns ``AddressOperatorHint`` again instead of parking the
                # monitor at ``NotifyHuman`` on the first timeout. Carry the
                # timeout reason code out on a flagged envelope: a bare no-op
                # result is indistinguishable from a processed hint, so the loop
                # would record this cycle as a succeeded ``needs_human`` and the
                # watchdog failure would vanish from operation history — exactly
                # the "retries must preserve reason codes" rule (AGENTS.md).
                #
                # The marker goes straight to the workspace row, ahead of the
                # return and every later await: the hint stays durably pending,
                # so a worker killed before the next ``_persist_state`` (or a
                # ``_finish_monitor_operation`` fault) would resume with a
                # pending hint and no budget and grant the "single" retry all
                # over again (PRRT_kwDOSJAM6s6fzBXq).
                await mark_timeout_retry_used_durably(
                    self,
                    workspace_id=workspace_id,
                    state=state,
                    hint=hint,
                )
                return _GitPushResult(
                    pushed=False,
                    failed=False,
                    returncode=0,
                    reason_code=timeout_retry_reason_code(verdict),
                    operator_hint_timeout_retry=True,
                )
            clear_timeout_retry(state, hint)
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
                repo=repo,
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
            clear_timeout_retry(state, hint)
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
                repo=repo,
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
            if not (
                isinstance(verdict, VerdictResult)
                and verdict.kept_commit
                and not active_grant_specs
            ):
                return _GitPushResult(pushed=False, failed=False, returncode=0)
            # The protocol parked this hint but kept its contentful commit (#1074).
            # Returning here would leave that commit local while ``decide()``
            # answers ``NotifyHuman``. Push it through the guarded push below, as a
            # comment batch would, and skip every finalize step so the hint stays
            # parked. An approve-and-keep grant is consumed only by a finalized
            # resume, so a grant-bearing hint keeps the plain park above.
            hint_parked = True

    # The operator-hint resume may have outlived its PR: ``decide()`` only
    # short-circuits merged/closed at the START of a poll cycle, so re-read PR
    # state before any push, re-block, or human notification (#910).
    moot_result = await self._post_action_pr_terminal_push_result_if_moot(
        workspace_id=workspace_id,
        pr_number=pr_number,
        context="operator_hint_repair",
        operation_id=_operation_id,
        operation_type=_operation_type,
        repo=repo,
        worktree_path=worktree_path,
    )
    if moot_result is not None:
        return cast(_GitPushResult, moot_result)

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
    baseline_park_pending = await self._protected_scope_baseline_park_resume_pending(workspace_id)
    # The commit a baseline-outage park kept for this resume to publish. A directive
    # may legitimately drop it, so only a directiveless resume must prove it landed
    # (PRRT_kwDOSJAM6s63M6sD). When the stranded-edit commit above kept the fix-pass
    # baseline by re-parenting the parked HEAD or resetting it to the baseline, the
    # parked SHA is gone by design, so require its replacement (PRRT_kwDOSJAM6s63OA3D).
    baseline_park_head_sha = (
        provider_park_resume_kept_head(
            state, await self._protected_scope_baseline_park_preserved_head(workspace_id)
        )
        if baseline_park_pending and not hint.directive
        else None
    )
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
        reblock_resume_phase = monitor_protected_scope_resume_origin(block_resume_phase)
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
                repo=repo,
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
    # protected change, so the guard keeps firing. A clean current net diff is NOT
    # enough to skip this check: a revert-on-top can remove the protected path from
    # the tree diff while the preserved protected commit remains an unpushed ancestor
    # of HEAD. ``_preserved_commit_in_unpushed_range`` is the safety boundary: it
    # returns false only when the preserved commit was dropped or is already remote.
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
            "would publish the ungranted protected change. Drop the preserved commit "
            "with reset/rebase/cherry-pick; do not add another revert-on-top commit. "
            "Alternatively, approve-and-keep the protected path, then resume."
        )
        # Key the repeat guard to the head the re-block will RECORD as the next
        # ``__awf_protected_block_preserved_head__`` marker, NOT the old preserved
        # SHA. ``_pause_monitor_for_protected_scope_block`` (reached via
        # ``_reblock_preserved_protected_leak`` below) re-reads the worktree HEAD —
        # here the revert-on-top commit — and overwrites the marker with it (falling
        # back to leaving the old marker when HEAD is unresolvable). The next resume
        # reads THAT rewritten marker to recompute its repeat key, so keying this mark
        # to the old preserved SHA always misses, re-running the identical directive
        # one more time instead of parking for human attention. Mirror the re-block's
        # own marker logic exactly (PRRT_kwDOSJAM6s6MtBzN).
        reblock_marker_sha = await self._rev_parse_head(worktree_path) or preserved_head_sha
        reblock_repeat_key = _protected_history_directive_reblock_key(
            reblock_marker_sha, hint.directive
        )
        # In-memory mark (flushed by the loop's later ``_persist_state``) AND durable
        # persistence in the re-block commit itself (via ``extra_state_markers`` below).
        # The re-block rewrites the preserved-head marker durably to this same HEAD, so
        # the repeat key must be just as durable; otherwise a crash before
        # ``_persist_state`` keeps the rewritten marker but loses the repeat key, and the
        # next identical directive recomputes the same key, misses, and re-runs the agent
        # instead of parking for human attention (PRRT_kwDOSJAM6s6MtULR).
        state.mark_addressed(reblock_repeat_key, "reblocked")
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
            extra_state_markers={reblock_repeat_key: "reblocked"},
            repo=repo,
        )
    # Idempotent push (divergence recovery, WS-2 §5): if the preserved commit is
    # already on the remote PR branch (a monitor/worker restart re-ran the resume
    # after the push landed), treat it as a no-op rather than re-pushing.
    if protected_scope_block is None and await self._preserved_commit_already_on_remote(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        remote_branch=remote_branch,
        remote_push_url=remote_push_url,
        preserved_head_sha=preserved_head_sha or baseline_park_head_sha,
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
        # A baseline-outage park whose HEAD was unresolvable recorded no kept SHA, so
        # the check above passed on the empty diff alone: refuse to finalize it as
        # published without proof (PRRT_kwDOSJAM6s63OKfO).
        if (
            baseline_park_pending
            and not hint.directive
            and not (preserved_head_sha or baseline_park_head_sha)
        ):
            reason = (
                "baseline-outage park recorded no preserved HEAD; refusing to finalize an "
                "empty-diff no-op without proof that the kept repair commit reached the "
                "remote PR branch. Resume with a directive or restore the worktree, then resume."
            )
            mark_operator_hint_needs_human(state, reason)
            return _GitPushResult(pushed=False, failed=False, returncode=1, stderr=reason)
        pushed_head_sha = await self._rev_parse_head(worktree_path)
        if pushed_head_sha:
            state.last_push_sha = pushed_head_sha
        if not hint_parked:
            await _finalize_operator_hint_resume(
                self,
                workspace_id=workspace_id,
                state=state,
                hint=hint,
                acted_feedback_text=acted_feedback_text,
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
            # A transient baseline outage parks under the resume's ORIGIN, like the
            # violation re-block above (#1221).
            resume_phase=monitor_protected_scope_resume_origin(block_resume_phase),
            repo=repo,
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
            # (PRRT_kwDOSJAM6s6KZK1v). A parked hint's push suppresses it too: the
            # reset would drop the kept commit its ``needs_human`` reason promises
            # to preserve, so the rejection surfaces with the commit intact
            # (PRRT_kwDOSJAM6s6uPSzO). A resume of a baseline-outage park suppresses
            # it as well: the park kept the unpushed repair commit, and the reset would
            # drop it if the PR branch advanced while blocked (PRRT_kwDOSJAM6s621cKV).
            allow_resync_on_rejection=(
                not active_grant_specs and not hint_parked and not baseline_park_pending
            ),
            # Re-arm the terminal guard AFTER pre-push validation: the check above
            # ran before a validation suite (plus its fix passes) that can take
            # minutes, so the PR can go terminal in between
            # (PRRT_kwDOSJAM6s6fjOze).
            pr_number=pr_number,
            pr_terminal_context="operator_hint_repair",
            repo=repo,
            operation_id=_operation_id,
            operation_type=_operation_type,
            # A fix-pass baseline outage parks under the resume's ORIGIN (#1221).
            protected_scope_resume_phase=monitor_protected_scope_resume_origin(block_resume_phase),
        )
    )
    if push_result.pr_terminal is not None:
        # The post-validation recheck observed the PR as merged/closed, so the
        # repair was deliberately NOT pushed. Return the moot envelope before the
        # branches below consume the single-use operator grant, finalize the hint,
        # or park needs_human against a PR that no longer exists; the loop's shared
        # terminal finisher runs the handling ``decide()`` would have chosen (#910).
        return cast(_GitPushResult, push_result)
    if push_result.paused_into_blocked:
        # A transient protected-scope baseline outage parked the workspace in
        # ``blocked`` (#1221). Its bumped block epoch supersedes this hint, so
        # clear it like the re-block branches instead of parking it needs_human.
        state.pending_operator_hint = None
        return cast(_GitPushResult, push_result)
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
                    repo=repo,
                )
            # No preserved-head marker to anchor the re-block (effectively unreachable
            # for a grant-active resume, which always followed a genuine block). Park
            # needs_human with a NON-failed result so the recoverable workspace stays
            # at ``monitoring_pr`` rather than being terminally failed.
            mark_operator_hint_needs_human(state, reason)
            return _GitPushResult(pushed=False, failed=False, returncode=1, stderr=reason)
        if (
            push_result.reason_code == _GIT_PUSH_REJECTED_NON_FAST_FORWARD_REASON
            and baseline_park_pending
            and not hint_parked
        ):
            # The baseline-outage park's kept repair commit diverged from a PR branch
            # advanced while blocked; the resync was suppressed so the commit is still
            # HEAD. Park needs_human (non-failed, so the workspace is not terminally
            # failed and cleaned up) until an operator directive rebases or drops it
            # (PRRT_kwDOSJAM6s621cKV).
            reason = (
                "the PR branch advanced while the workspace was parked for a protected-"
                "scope baseline outage, so the kept repair commit was rejected "
                "non-fast-forward; it was kept (not reset to the remote head). Resume "
                "with a directive that rebases it onto the PR branch or drops it."
            )
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
        #
        # A baseline-outage park's kept repair commit gets the same guard: a no-op push
        # from a worktree recreated at the remote head must not finalize the park as
        # published while the kept commit never landed (PRRT_kwDOSJAM6s63M6sD).
        kept_head_sha = preserved_head_sha or baseline_park_head_sha
        if (
            not hint.directive
            and kept_head_sha
            and not await self._preserved_commit_already_on_remote(
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                remote_branch=remote_branch,
                remote_push_url=remote_push_url,
                preserved_head_sha=kept_head_sha,
            )
        ):
            reason = (
                (
                    "operator-approved preserved commit "
                    f"{preserved_head_sha} is not on the remote PR branch after a no-op "
                    "push; the worktree was reset and the approved protected change was "
                    "dropped"
                )
                if preserved_head_sha
                else (
                    f"repair commit {kept_head_sha} kept by the protected-scope baseline "
                    "park is not on the remote PR branch after a no-op push; the "
                    "worktree was reset and the kept repair was dropped"
                )
            )
            mark_operator_hint_needs_human(state, reason)
            return cast(_GitPushResult, push_result)
        # A baseline-outage park with no recorded kept SHA has nothing to confirm on
        # the remote, so a no-op push is no proof the repair landed either; mirror the
        # idempotent no-op guard above (PRRT_kwDOSJAM6s63O2cy).
        if baseline_park_pending and not hint.directive and not kept_head_sha:
            reason = (
                "baseline-outage park recorded no preserved HEAD; refusing to finalize a "
                "no-op push without proof that the kept repair commit reached the remote "
                "PR branch. Resume with a directive or restore the worktree, then resume."
            )
            mark_operator_hint_needs_human(state, reason)
            return cast(_GitPushResult, push_result)
        if not hint_parked:
            await _finalize_operator_hint_resume(
                self,
                workspace_id=workspace_id,
                state=state,
                hint=hint,
                acted_feedback_text=acted_feedback_text,
            )
        return cast(_GitPushResult, push_result)

    pushed_head_sha = await self._rev_parse_head(worktree_path)
    if pushed_head_sha:
        state.last_push_sha = pushed_head_sha
    # A pushed NEW commit is no proof the baseline-outage park's kept repair landed:
    # from a worktree recreated at the remote head, the directiveless repair agent's
    # commit pushes cleanly without it. Require the kept SHA on the remote too
    # (PRRT_kwDOSJAM6s63PY73). A park with no recorded kept SHA has nothing to
    # confirm, so the push is no proof either (PRRT_kwDOSJAM6s63QAPz).
    if (
        baseline_park_pending
        and not hint.directive
        and not hint_parked
        and not baseline_park_head_sha
    ):
        reason = (
            "baseline-outage park recorded no preserved HEAD; refusing to finalize the "
            "resume push without proof that the kept repair commit reached the remote "
            "PR branch. Resume with a directive or restore the worktree, then resume."
        )
        mark_operator_hint_needs_human(state, reason)
        return cast(_GitPushResult, push_result)
    if (
        baseline_park_head_sha
        and not hint_parked
        and not await self._preserved_commit_already_on_remote(
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            remote_branch=remote_branch,
            remote_push_url=remote_push_url,
            preserved_head_sha=baseline_park_head_sha,
        )
    ):
        reason = (
            f"repair commit {baseline_park_head_sha} kept by the protected-scope baseline "
            "park is not on the remote PR branch after the resume push; the worktree was "
            "reset and the kept repair was dropped"
        )
        mark_operator_hint_needs_human(state, reason)
        return cast(_GitPushResult, push_result)
    # Single-use: consume the operator grants now that the resumed change pushed,
    # so a later DIFFERENT protected change re-blocks and must be granted again.
    # A parked hint pushed only its kept commit and stays ``needs_human``.
    if not hint_parked:
        await _finalize_operator_hint_resume(
            self,
            workspace_id=workspace_id,
            state=state,
            hint=hint,
            acted_feedback_text=acted_feedback_text,
        )
    return cast(_GitPushResult, push_result)


async def _operator_hint_agent_error_result(
    self: Any,
    *,
    exc: _OperatorHintAgentError,
    workspace_id: str,
    remote_branch: str,
    state: MonitorState,
    pr_number: int,
    repo: RepoRef,
) -> _GitPushResult:
    """Build the terminal push result for a CLI error raised by an operator-hint resume.

    Lifted verbatim out of the ``except`` arms of ``_run_operator_hint_cycle`` so the
    post-action terminal-PR guard can run ONCE, before any of these results is built
    and before any needs_human hint is armed (#910). Per-error behavior is unchanged:
    a protocol violation is a terminal agent failure with no hint marking, and every
    other error parks the hint for a human.
    """
    if isinstance(exc, AgentVerdictProtocolError):
        # Nothing was pushed, so the envelope must not say ``git push`` (#1074).
        return _agent_verdict_protocol_failure_result(exc)
    if isinstance(exc, ProtectedScopeDiffError):
        # Keep a sync-base origin on a re-park so the next resume still selects the
        # sync-base-aware validator (PRRT_kwDOSJAM6s622NyQ).
        block_resume_phase = await self._resolve_block_resume_phase(workspace_id)
        push_result = cast(
            _GitPushResult,
            await self._protected_scope_diff_unavailable_push_result(
                workspace_id=workspace_id,
                remote_branch=remote_branch,
                exc=exc,
                # Re-arm the #910 recheck: the resume-phase read and diff probe
                # above run after the caller's terminal-PR guard
                # (PRRT_kwDOSJAM6s63MpIE).
                pr_number=pr_number,
                repo=repo,
                resume_phase=monitor_protected_scope_resume_origin(block_resume_phase),
            ),
        )
        if push_result.paused_into_blocked:
            # Transient baseline outage parked the workspace (#1221); see above.
            state.pending_operator_hint = None
            return push_result
        reason = (
            push_result.stderr or str(exc)
        ).strip() or "protected-scope policy could not verify the operator hint repair push"
        mark_operator_hint_needs_human(state, reason)
        return push_result
    if isinstance(exc, _MonitorPolicyBlockedError):
        reason = str(exc) or "monitor policy blocked the operator hint repair"
        mark_operator_hint_needs_human(state, reason)
        return _GitPushResult(pushed=False, failed=False, returncode=1, stderr=reason)
    if isinstance(exc, _MonitorAgentRuntimeOwnershipRepairFailedError):
        default_reason = "agent runtime ownership repair failed"
    elif isinstance(exc, _MonitorHeadObjectMissingError):
        default_reason = "HEAD commit object is missing from the canonical mirror"
    else:
        default_reason = "mirror hooks path repair failed"
    reason = str(exc) or default_reason
    mark_operator_hint_needs_human(state, reason)
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr=reason,
        reason_code=exc.reason_code,
    )


async def _finalize_operator_hint_resume(
    self: Any,
    *,
    workspace_id: str,
    state: MonitorState,
    hint: OperatorHint | None = None,
    acted_feedback_text: str | None = None,
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
    _finalize_processed_operator_hint(state, hint=hint, acted_feedback_text=acted_feedback_text)


def _operator_hint_block_reason(
    verdict: VerdictResult | MonitorVerdictResult,
) -> str:
    if verdict.verdict == "false_positive":
        return verdict.reason or "agent reported the operator hint was not actionable"
    if verdict.verdict == "defer":
        return verdict.reason or "agent deferred the operator hint"
    if verdict.verdict == "needs_human":
        return verdict.reason or "agent requested human input for the operator hint"
    return verdict.reason or "agent failed while processing the operator hint"
