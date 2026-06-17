"""Operator remonitor hint repair handling."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from awf.common.github_client import RepoRef
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.monitor_prompts import operator_hint_prompt
from awf.runtime.operator_hints import (
    mark_operator_hint_agent_failed,
    mark_operator_hint_needs_human,
    mark_operator_hint_processed,
)
from awf.runtime.pr_monitor import MonitorState, OperatorHint
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.constants import _PROTECTED_SCOPE_PUSH_BLOCKED_REASON
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorPolicyBlockedError,
)


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
        if verdict.verdict == "agent_failed":
            reason = _operator_hint_block_reason(verdict)
            mark_operator_hint_agent_failed(state, reason)
            return _GitPushResult(pushed=False, failed=False, returncode=0)
        if verdict.verdict in {"needs_human", "defer", "false_positive"}:
            reason = _operator_hint_block_reason(verdict)
            mark_operator_hint_needs_human(state, reason)
            return _GitPushResult(pushed=False, failed=False, returncode=0)

    protected_scope_block = await self._protected_scope_push_block(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        remote_branch=remote_branch,
        remote_push_url=remote_push_url,
    )
    if protected_scope_block is not None and protected_scope_block.violations:
        # The resume did NOT clear the protected violation (a "revert" directive
        # that didn't actually revert, or a grant that doesn't cover the path).
        # RE-BLOCK with a bumped epoch — invalidating the just-applied grants and
        # re-arming a fresh notification — instead of proceeding toward merge.
        reblock_result = cast(
            _GitPushResult,
            await self._pause_monitor_for_protected_scope_block(
                workspace_id=workspace_id,
                pr_number=pr_number,
                pr_head_sha=pr_head_sha,
                protected_scope_block=protected_scope_block,
                worktree_path=worktree_path,
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
    # Idempotent push (divergence recovery, WS-2 §5): if the preserved commit is
    # already on the remote PR branch (a monitor/worker restart re-ran the resume
    # after the push landed), treat it as a no-op rather than re-pushing.
    if protected_scope_block is None and await self._preserved_commit_already_on_remote(
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        remote_branch=remote_branch,
        remote_push_url=remote_push_url,
    ):
        pushed_head_sha = await self._rev_parse_head(worktree_path)
        if pushed_head_sha:
            state.last_push_sha = pushed_head_sha
        await self._consume_active_operator_grants(workspace_id)
        mark_operator_hint_processed(state)
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
        )
    )
    if push_result.failed:
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
    if not push_result.pushed:
        # A fixed verdict can reflect non-code PR work (for example posting an
        # allowed reply) where there is no commit to publish. A successful no-op
        # push still means the operator hint was handled.
        await self._consume_active_operator_grants(workspace_id)
        mark_operator_hint_processed(state)
        return cast(_GitPushResult, push_result)

    pushed_head_sha = await self._rev_parse_head(worktree_path)
    if pushed_head_sha:
        state.last_push_sha = pushed_head_sha
    # Single-use: consume the operator grants now that the resumed change pushed,
    # so a later DIFFERENT protected change re-blocks and must be granted again.
    await self._consume_active_operator_grants(workspace_id)
    mark_operator_hint_processed(state)
    return cast(_GitPushResult, push_result)


def _operator_hint_block_reason(verdict: VerdictResult) -> str:
    if verdict.verdict == "false_positive":
        return verdict.reason or "agent reported the operator hint was not actionable"
    if verdict.verdict == "defer":
        return verdict.reason or "agent deferred the operator hint"
    if verdict.verdict == "needs_human":
        return verdict.reason or "agent requested human input for the operator hint"
    return verdict.reason or "agent failed while processing the operator hint"
