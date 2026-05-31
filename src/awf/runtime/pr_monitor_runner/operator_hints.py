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

    prompt = operator_hint_prompt(
        pr_number=pr_number,
        repo_slug=repo.slug(),
        reason=hint.reason,
        operation_id=hint.operation_id,
        workspace_runtime_context=self._workspace_runtime_context,
    )
    try:
        verdict = await self._invoke_cli_for_verdict_result(
            workspace_id=workspace_id,
            prompt=prompt,
            commit_message="fix: address operator remonitor hint",
            compose_project=compose_project,
            compose_file=compose_file,
            state=state,
        )
    except ProtectedScopeDiffError as exc:
        return cast(
            _GitPushResult,
            await self._protected_scope_diff_unavailable_push_result(
                workspace_id=workspace_id,
                remote_branch=remote_branch,
                exc=exc,
            ),
        )
    except _MonitorPolicyBlockedError as exc:
        reason = str(exc) or "monitor policy blocked the operator hint repair"
        mark_operator_hint_needs_human(state, reason)
        return _GitPushResult(pushed=False, failed=False, returncode=1, stderr=reason)
    except _MonitorAgentRuntimeOwnershipRepairFailedError as exc:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr=str(exc),
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
        if push_result.reason_code == _PROTECTED_SCOPE_PUSH_BLOCKED_REASON:
            reason = (
                push_result.stderr or ""
            ).strip() or "protected-scope policy blocked the operator hint repair push"
            mark_operator_hint_needs_human(state, reason)
        return cast(_GitPushResult, push_result)
    if not push_result.pushed:
        mark_operator_hint_needs_human(
            state,
            "operator hint repair did not produce a pushed fix commit",
        )
        return cast(_GitPushResult, push_result)

    pushed_head_sha = await self._rev_parse_head(worktree_path)
    if pushed_head_sha:
        state.last_push_sha = pushed_head_sha
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
