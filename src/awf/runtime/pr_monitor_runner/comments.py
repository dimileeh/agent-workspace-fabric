"""PR review comment handling and human notifications."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from awf.common.logging import get_logger
from awf.node.git_manager import (
    mirror_path_for_worktree,
    repair_mirror_hooks_path,
)
from awf.runtime.monitor_prompts import (
    address_review_comment_prompt,
    address_thread_prompt,
    ready_to_merge_comment,
)
from awf.runtime.ownership import (
    repair_agent_runtime_ownership,
)
from awf.runtime.pr_monitor_runner import comment_verdict as _comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AgentVerdict,
    AgentVerdictExecutionError,
    MonitorVerdictResult,
    Verdict,
    VerdictResult,
    _owned_paths_for_prompt,
    _owned_paths_for_prompt_or_empty,
)
from awf.runtime.pr_monitor_runner.constants import (
    _TASK_TAG_UNSET,
    _TaskTagUnset,
)
from awf.runtime.pr_monitor_runner.mirror_hooks import mirror_hooks_repair_failure_details

__all__ = (
    "AgentVerdict",
    "MonitorVerdictResult",
    "Verdict",
    "VerdictResult",
    "_owned_paths_for_prompt",
    "_owned_paths_for_prompt_or_empty",
)

if TYPE_CHECKING:
    from awf.common.github_client import RepoRef
    from awf.runtime.logs import WorkspaceLogSink
    from awf.runtime.pr_monitor import MonitorState, PRStatus, ReviewComment, ReviewThread
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

_log = get_logger(__name__)
_GENERIC_HUMAN_BLOCKER_REASON = "human attention is required before AWF can continue"


async def _address_thread(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    thread: ReviewThread,
    compose_project: str,
    compose_file: Path,
    state: MonitorState | None = None,
    owned_paths: Sequence[str] | None = None,
    task_tag: str | None | _TaskTagUnset = _TASK_TAG_UNSET,
    operation_start_head: str | None = None,
    cycle_start_head: str | None = None,
    base_branch: str = "",
    remote_branch: str | None = None,
    operation_id: str | None = None,
    operation_type: str | None = None,
    monitor_log: WorkspaceLogSink | None = None,
) -> Verdict:
    """Ask the monitor agent to resolve a review thread and return its verdict."""
    del base_branch, remote_branch, operation_id, operation_type, monitor_log
    from awf.runtime.pr_monitor import _review_thread_body_hash
    from awf.runtime.pr_monitor_runner.helpers import (
        _defer_reason_state_key,
        _sync_needs_human_reason,
    )

    prompt_owned_paths = (
        owned_paths
        if owned_paths is not None
        else await _owned_paths_for_prompt(runner, workspace_id)
    )
    # The workspace's optional Jira issue key is immutable, so resolve it once per
    # repair cycle and thread it (alongside ``owned_paths``) into every item in the
    # fix-cycle loops. Self-resolve only as a fallback for callers that pass nothing
    # (the sentinel default), so a single comment-repair cycle with many threads
    # opens one workspace lookup instead of one per item (#537).
    resolved_task_tag = (
        await runner._resolve_task_tag(workspace_id)
        if isinstance(task_tag, _TaskTagUnset)
        else task_tag
    )
    prompt = address_thread_prompt(
        pr_number=pr_number,
        repo_slug=repo.slug(),
        thread=thread,
        workspace_runtime_context=runner._workspace_runtime_context,
        owned_paths=prompt_owned_paths,
        task_tag=resolved_task_tag,
    )
    try:
        result = await runner._invoke_cli_for_verdict_result(
            workspace_id=workspace_id,
            prompt=prompt,
            commit_message=f"fix: address PR review thread {thread.thread_id}",
            compose_project=compose_project,
            compose_file=compose_file,
            state=state,
            task_tag=resolved_task_tag,
            operation_start_head=operation_start_head,
            evidence_item_id=thread.thread_id,
            evidence_body_hash=_review_thread_body_hash(thread),
            evidence_item_path=thread.path,
            evidence_item_line=getattr(thread, "line", None),
            evidence_anchor_head=cycle_start_head,
        )
    except AgentVerdictExecutionError:
        return "agent_failed"
    if isinstance(result, MonitorVerdictResult):
        return result.verdict
    # Stash the agent's defer reason so the deferred-capture path can preserve it
    # in the filed tracking issue (the verdict alone loses that follow-up detail).
    # On any defer, overwrite/clear the stored reason so a re-triage with a bare
    # DEFER (no reason) can't leave a stale reason from a prior pass.
    if state is not None:
        _sync_needs_human_reason(state, thread.thread_id, result)
        if result.verdict == "defer":
            reason_key = _defer_reason_state_key(thread.thread_id)
            if result.reason:
                state.mark_addressed(reason_key, result.reason)
            else:
                state.threads_addressed_ids.pop(reason_key, None)
    return result.verdict


async def _address_review_comment(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    comment: ReviewComment,
    compose_project: str,
    compose_file: Path,
    state: MonitorState | None = None,
    owned_paths: Sequence[str] | None = None,
    task_tag: str | None | _TaskTagUnset = _TASK_TAG_UNSET,
    operation_start_head: str | None = None,
    base_branch: str = "",
    remote_branch: str | None = None,
    operation_id: str | None = None,
    operation_type: str | None = None,
    monitor_log: WorkspaceLogSink | None = None,
) -> Verdict:
    """Resolve a review comment and return only its verdict."""
    result = await runner._address_review_comment_result(
        workspace_id=workspace_id,
        repo=repo,
        pr_number=pr_number,
        comment=comment,
        compose_project=compose_project,
        compose_file=compose_file,
        state=state,
        owned_paths=owned_paths,
        task_tag=task_tag,
        operation_start_head=operation_start_head,
        base_branch=base_branch,
        remote_branch=remote_branch,
        operation_id=operation_id,
        operation_type=operation_type,
        monitor_log=monitor_log,
    )
    return result.verdict


async def _address_review_comment_result(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    comment: ReviewComment,
    compose_project: str,
    compose_file: Path,
    state: MonitorState | None = None,
    owned_paths: Sequence[str] | None = None,
    task_tag: str | None | _TaskTagUnset = _TASK_TAG_UNSET,
    operation_start_head: str | None = None,
    base_branch: str = "",
    remote_branch: str | None = None,
    operation_id: str | None = None,
    operation_type: str | None = None,
    monitor_log: WorkspaceLogSink | None = None,
) -> VerdictResult | MonitorVerdictResult:
    """Resolve a review comment while retaining its full monitor result."""
    del base_branch, remote_branch, operation_id, operation_type, monitor_log
    from awf.runtime.pr_monitor_runner.helpers import _review_comment_body_hash

    prompt_owned_paths = (
        owned_paths
        if owned_paths is not None
        else await _owned_paths_for_prompt(runner, workspace_id)
    )
    # The workspace's optional Jira issue key is immutable, so resolve it once per
    # repair cycle and thread it (alongside ``owned_paths``) into every item in the
    # fix-cycle loops. Self-resolve only as a fallback for callers that pass nothing
    # (the sentinel default), so a single comment-repair cycle with many comments
    # opens one workspace lookup instead of one per item (#537).
    resolved_task_tag = (
        await runner._resolve_task_tag(workspace_id)
        if isinstance(task_tag, _TaskTagUnset)
        else task_tag
    )
    prompt = address_review_comment_prompt(
        pr_number=pr_number,
        repo_slug=repo.slug(),
        comment=comment,
        workspace_runtime_context=runner._workspace_runtime_context,
        owned_paths=prompt_owned_paths,
        task_tag=resolved_task_tag,
    )
    try:
        return await runner._invoke_cli_for_verdict_result(
            workspace_id=workspace_id,
            prompt=prompt,
            commit_message=f"fix: address PR review comment {comment.comment_id}",
            compose_project=compose_project,
            compose_file=compose_file,
            state=state,
            task_tag=resolved_task_tag,
            operation_start_head=operation_start_head,
            evidence_item_id=comment.comment_id,
            evidence_body_hash=_review_comment_body_hash(comment),
        )
    except AgentVerdictExecutionError:
        return MonitorVerdictResult(verdict="agent_failed")


def _sync_comment_verdict_dependencies() -> None:
    """Keep legacy comment-module monkeypatch seams for verdict invocation tests."""
    _comment_verdict.mirror_path_for_worktree = mirror_path_for_worktree  # type: ignore[attr-defined]
    _comment_verdict.repair_agent_runtime_ownership = repair_agent_runtime_ownership  # type: ignore[attr-defined]
    _comment_verdict.repair_mirror_hooks_path = repair_mirror_hooks_path
    _comment_verdict.mirror_hooks_repair_failure_details = mirror_hooks_repair_failure_details  # type: ignore[attr-defined]


async def _invoke_cli_for_verdict(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    prompt: str,
    commit_message: str,
    compose_project: str,
    compose_file: Path,
    state: MonitorState | None = None,
    task_tag: str | None | _TaskTagUnset = _TASK_TAG_UNSET,
    operation_start_head: str | None = None,
) -> Verdict:
    """Invoke the extracted verdict operation through the legacy module seam."""
    _sync_comment_verdict_dependencies()
    try:
        return await _comment_verdict._invoke_cli_for_verdict(
            runner,
            workspace_id=workspace_id,
            prompt=prompt,
            commit_message=commit_message,
            compose_project=compose_project,
            compose_file=compose_file,
            state=state,
            task_tag=task_tag,
            operation_start_head=operation_start_head,
        )
    except AgentVerdictExecutionError:
        return "agent_failed"


async def _invoke_cli_for_verdict_result(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    prompt: str,
    commit_message: str,
    compose_project: str,
    compose_file: Path,
    state: MonitorState | None = None,
    task_tag: str | None | _TaskTagUnset = _TASK_TAG_UNSET,
    operation_start_head: str | None = None,
    commit_dirty_changes: bool = True,
    require_fix_evidence: bool = True,
    evidence_item_id: str | None = None,
    evidence_body_hash: str | None = None,
    evidence_item_path: str | None = None,
    evidence_item_line: int | None = None,
    evidence_anchor_head: str | None = None,
) -> VerdictResult | MonitorVerdictResult:
    """Invoke the extracted verdict operation through the legacy module seam."""
    _sync_comment_verdict_dependencies()
    try:
        return await _comment_verdict._invoke_cli_for_verdict_result(
            runner,
            workspace_id=workspace_id,
            prompt=prompt,
            commit_message=commit_message,
            compose_project=compose_project,
            compose_file=compose_file,
            state=state,
            task_tag=task_tag,
            operation_start_head=operation_start_head,
            commit_dirty_changes=commit_dirty_changes,
            require_fix_evidence=require_fix_evidence,
            evidence_item_id=evidence_item_id,
            evidence_body_hash=evidence_body_hash,
            evidence_item_path=evidence_item_path,
            evidence_item_line=evidence_item_line,
            evidence_anchor_head=evidence_anchor_head,
        )
    except AgentVerdictExecutionError:
        return MonitorVerdictResult(verdict="agent_failed")


async def _post_human_notification_once(
    runner: PullRequestMonitorRunner,
    *,
    repo: RepoRef,
    pr_number: int,
    status: PRStatus,
    state: MonitorState,
    blocker_reason: str | None = None,
    preserve_full_blocker_reason: bool = False,
) -> None:
    """Post a single human-attention PR comment, deduped once per (head, reason).

    The dedupe key is head/reason scoped (``_notification_key``), matching the
    once-per-(head, reason) behavior every caller relies on. The protected-block
    pause needs different semantics (epoch-keyed dedupe, ``ForgeClientError``
    swallowing, best-effort skip on missing monitor context) and so posts via its
    own ``_post_protected_block_notification`` rather than through this helper.
    """
    from awf.runtime.pr_monitor_runner.helpers import (
        _notification_key,
        _notify_human_blocker_items,
        _notify_human_reason,
        _sanitize_verdict_reason,
    )
    from awf.runtime.pr_monitor_runner.notify_human_details import _notification_items_digest

    bot_items, human_items = _notify_human_blocker_items(status, state)
    items = bot_items + human_items
    items_digest = _notification_items_digest(items) if items else None
    raw_reason = (
        blocker_reason
        if blocker_reason is not None
        else _notify_human_reason(status, state, blocker_items=(bot_items, human_items))
    )
    reason = _sanitize_verdict_reason(raw_reason)
    if reason is None and blocker_reason is not None:
        reason = _sanitize_verdict_reason(
            _notify_human_reason(status, state, blocker_items=(bot_items, human_items))
        )
    if reason is None and blocker_reason is not None:
        reason = _GENERIC_HUMAN_BLOCKER_REASON
    key = _notification_key(
        head_sha=status.head_sha,
        blocker_reason=reason,
        items_digest=items_digest,
    )
    if state.threads_addressed_ids.get(key) == "notified":
        _log.info(
            "monitor.notify_human_already_posted",
            pr_number=pr_number,
            head_sha=status.head_sha[:10],
            reason=reason,
        )
        return
    await runner._deps.gh.post_comment(
        repo=repo,
        pr_number=pr_number,
        body=ready_to_merge_comment(
            pr_number=pr_number,
            head_sha=status.head_sha,
            blocker_reason=reason,
            blocker_items=items,
            preserve_full_blocker_reason=preserve_full_blocker_reason,
        ),
    )
    state.mark_addressed(key, "notified")
