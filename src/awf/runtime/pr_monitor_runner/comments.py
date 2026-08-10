"""Submodule for handling review comments, thread addressing, and human notifications."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from awf.adapters.base import AgentRunError
from awf.common.audit import redact_audit_text
from awf.common.command_evidence import append_command_evidence
from awf.common.commands import CommandResult
from awf.common.compose_exec import ComposeExecCleanupError
from awf.common.logging import get_logger
from awf.db.repositories import WorkspaceRepository
from awf.node.git_manager import (
    GitOperationError,
    mirror_path_for_worktree,
    repair_mirror_hooks_path,
)
from awf.runtime.agent_scratch import apply_agent_scratch_excludes
from awf.runtime.monitor_prompts import (
    address_review_comment_prompt,
    address_thread_prompt,
    needs_human_reason_reask_prompt,
    ready_to_merge_comment,
)
from awf.runtime.ownership import (
    MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    repair_agent_runtime_ownership,
)
from awf.runtime.pr_monitor_runner.constants import (
    _AUDIT_COMMENT_RESOLUTION_EVENT,
    _MIRROR_HOOKS_PATH_POISONED_REASON,
    _NEEDS_HUMAN_REASON_CLARIFICATION_UNAVAILABLE,
    _NEEDS_HUMAN_REASON_MISSING,
    _TASK_TAG_UNSET,
    _TaskTagUnset,
)
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.mirror_hooks import mirror_hooks_repair_failure_details
from awf.runtime.pr_monitor_runner.types import (
    ProviderRecoveryAuthError,
    ProviderRecoveryFallbackError,
    ProviderRecoveryRetryError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorAgentServiceRecoverySupersededError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
)
from awf.runtime.validation_worktree_constants import VALIDATION_WORKTREE_CLEANUP_FAILED

# Verdicts the CLI reply parser can produce. Kept as a type alias so
# callers (and tests) can match against a closed set.
Verdict = Literal["fix_committed", "false_positive", "defer", "needs_human", "agent_failed"]


@dataclass(frozen=True)
class VerdictResult:
    verdict: Verdict
    reason: str | None = None


if TYPE_CHECKING:
    from awf.common.github_client import RepoRef
    from awf.runtime.logs import WorkspaceLogSink
    from awf.runtime.pr_monitor import MonitorState, PRStatus, ReviewComment, ReviewThread
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

_log = get_logger(__name__)

_GENERIC_HUMAN_BLOCKER_REASON = "human attention is required before AWF can continue"
_ISOLATED_REASK_WORKTREE_PREFIX = ".awf-needs-human-reask-"


@dataclass(frozen=True)
class _IsolatedReaskWorktree:
    """Tracked-only worktree used by one local NEEDS_HUMAN clarification re-ask."""

    source_worktree: Path
    path: Path


class _IsolatedReaskWorktreeCleanupFailedError(_MonitorPolicyBlockedError):
    """Raised when an unsuccessfully created re-ask checkout cannot be removed."""


async def _prepare_reask_primary_worktree(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
) -> None:
    """Preserve the primary-worktree cleanliness guard before a re-ask starts."""
    from awf.runtime.validation_worktree import check_validation_worktree_clean

    async def _run_git(args: list[str]) -> CommandResult:
        return await runner._deps.runner.run(git_worktree_command(worktree_path, *args))

    adapter = getattr(runner._deps, "adapter", None)
    await apply_agent_scratch_excludes(
        run_git=_run_git,
        worktree_path=worktree_path,
        scratch_paths=getattr(adapter, "runtime_scratch_paths", ()),
    )
    check = await check_validation_worktree_clean(
        run_git=_run_git,
        worktree_path=worktree_path,
        ignore_all_ignored=True,
    )
    if check.reason_code is not None:
        raise _MonitorPolicyBlockedError(
            "Could not prepare an isolated worktree before the NEEDS_HUMAN reason re-ask.",
            reason_code=check.reason_code,
        )


async def _check_reask_primary_worktree_clean(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
    restore_ref: str,
) -> str | None:
    """Report primary-worktree changes after a re-ask without modifying them."""
    from awf.runtime.validation_worktree import check_validation_worktree_clean

    async def _run_git(args: list[str]) -> CommandResult:
        return await runner._deps.runner.run(git_worktree_command(worktree_path, *args))

    check = await check_validation_worktree_clean(
        run_git=_run_git,
        worktree_path=worktree_path,
        ignore_all_ignored=True,
    )
    if not check.clean:
        return check.message or "Primary worktree changed during the NEEDS_HUMAN reason re-ask."

    current_head = await runner._rev_parse_head(worktree_path)
    if current_head != restore_ref:
        return "Primary worktree HEAD changed during the NEEDS_HUMAN reason re-ask."
    return None


async def _create_isolated_reask_worktree(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
    restore_ref: str,
) -> _IsolatedReaskWorktree | None:
    """Create a temporary tracked-only checkout for a local clarification re-ask."""
    if not (worktree_path / ".git").exists():
        # Lightweight test doubles do not have a worktree that can contain side
        # effects. Real AWF worktrees always contain a .git control file.
        return None

    await _prepare_reask_primary_worktree(runner, worktree_path=worktree_path)
    path = worktree_path / f"{_ISOLATED_REASK_WORKTREE_PREFIX}{uuid4().hex}"
    reask_worktree = _IsolatedReaskWorktree(
        source_worktree=worktree_path,
        path=path,
    )
    try:
        create = await runner._deps.runner.run(
            git_worktree_command(
                worktree_path,
                "worktree",
                "add",
                "--detach",
                str(path),
                restore_ref,
            )
        )
    except asyncio.CancelledError:
        # Git may have registered the worktree before cancellation reaches the
        # command runner. Remove that checkout before preserving cancellation.
        cleanup_task = asyncio.create_task(
            _cleanup_isolated_reask_worktree_after_creation_failure(
                runner,
                reask_worktree=reask_worktree,
                event_name=(
                    "monitor.needs_human_reason_reask_"
                    "isolated_cleanup_failed_after_creation_cancellation"
                ),
            )
        )
        while True:
            try:
                await asyncio.shield(cleanup_task)
                break
            except asyncio.CancelledError:
                if cleanup_task.done():
                    cleanup_task.result()
                    break
        raise
    if not create.ok:
        # Git can register and populate the checkout before reporting an error
        # (for example, when a post-checkout hook fails). Do not leave that
        # nested repository behind while treating clarification as unavailable.
        cleanup_error = await _cleanup_isolated_reask_worktree_after_creation_failure(
            runner,
            reask_worktree=reask_worktree,
            event_name=(
                "monitor.needs_human_reason_reask_isolated_cleanup_failed_after_creation_failure"
            ),
        )
        if cleanup_error is not None:
            raise _IsolatedReaskWorktreeCleanupFailedError(
                cleanup_error,
                reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
            )
        raise _MonitorPolicyBlockedError(
            "Could not create an isolated worktree before the NEEDS_HUMAN reason re-ask.",
            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
        )

    return reask_worktree


async def _cleanup_isolated_reask_worktree_after_creation_failure(
    runner: PullRequestMonitorRunner,
    *,
    reask_worktree: _IsolatedReaskWorktree,
    event_name: str,
) -> str | None:
    """Remove and report a checkout Git might create before it signals failure."""
    try:
        cleanup_error = await _remove_isolated_reask_worktree(runner, reask_worktree)
    except (GitOperationError, OSError, RuntimeError) as exc:
        # The failed creation may already have placed a nested repository in
        # the primary worktree. Treat an unconfirmed removal just like a
        # nonzero removal result so callers apply the terminal cleanup policy.
        cleanup_error = str(exc) or "`git worktree remove` failed during cleanup"
    if cleanup_error is not None:
        _log.warning(
            event_name,
            worktree_path=str(reask_worktree.source_worktree),
            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
            message=cleanup_error,
        )
    return cleanup_error


async def _remove_isolated_reask_worktree(
    runner: PullRequestMonitorRunner,
    reask_worktree: _IsolatedReaskWorktree | None,
) -> str | None:
    """Remove a clarification checkout before the primary-worktree cleanup runs."""
    if reask_worktree is None:
        return None

    remove = await runner._deps.runner.run(
        git_worktree_command(
            reask_worktree.source_worktree,
            "worktree",
            "remove",
            "--force",
            str(reask_worktree.path),
        )
    )
    if remove.ok:
        return None
    return "`git worktree remove` could not remove the NEEDS_HUMAN reason re-ask checkout"


async def _persist_reask_cleanup_failure_after_cancellation(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    item_id: str,
    needs_human_reason: str,
) -> None:
    """Durably block later monitor work while preserving the agent's reason."""
    from awf.runtime.pr_monitor_runner.notify_human_details import _needs_human_reason_state_key

    async with runner._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get_for_update(workspace_id)
        if workspace is None:
            return
        addressed = dict(workspace.monitor_threads_addressed or {})
        addressed[item_id] = "needs_human"
        addressed[_needs_human_reason_state_key(item_id)] = needs_human_reason
        workspace.monitor_threads_addressed = addressed
        await session.commit()


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
    base_branch: str = "",
    remote_branch: str | None = None,
    operation_id: str | None = None,
    operation_type: str | None = None,
    monitor_log: WorkspaceLogSink | None = None,
) -> Verdict:
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
    result = await runner._invoke_cli_for_verdict_result(
        workspace_id=workspace_id,
        prompt=prompt,
        commit_message=f"fix: address PR review thread {thread.thread_id}",
        compose_project=compose_project,
        compose_file=compose_file,
        state=state,
        task_tag=resolved_task_tag,
        operation_start_head=operation_start_head,
    )
    result = await _enforce_needs_human_reason(
        runner,
        result=result,
        original_prompt=prompt,
        workspace_id=workspace_id,
        pr_number=pr_number,
        item_id=thread.thread_id,
        item_kind="thread",
        item_author=getattr(thread, "author", None),
        item_path=getattr(thread, "path", None),
        item_line=getattr(thread, "line", None),
        commit_message=f"fix: address PR review thread {thread.thread_id}",
        compose_project=compose_project,
        compose_file=compose_file,
        state=state,
        task_tag=resolved_task_tag,
        operation_start_head=operation_start_head,
        base_branch=base_branch,
        remote_branch=remote_branch,
        operation_id=operation_id,
        operation_type=operation_type,
        monitor_log=monitor_log,
    )
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
) -> VerdictResult:
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
    result = await runner._invoke_cli_for_verdict_result(
        workspace_id=workspace_id,
        prompt=prompt,
        commit_message=f"fix: address PR review comment {comment.comment_id}",
        compose_project=compose_project,
        compose_file=compose_file,
        state=state,
        task_tag=resolved_task_tag,
        operation_start_head=operation_start_head,
    )
    return await _enforce_needs_human_reason(
        runner,
        result=result,
        original_prompt=prompt,
        workspace_id=workspace_id,
        pr_number=pr_number,
        item_id=comment.comment_id,
        item_kind="review",
        item_author=getattr(comment, "author", None),
        item_path=None,
        item_line=None,
        commit_message=f"fix: address PR review comment {comment.comment_id}",
        compose_project=compose_project,
        compose_file=compose_file,
        state=state,
        task_tag=resolved_task_tag,
        operation_start_head=operation_start_head,
        base_branch=base_branch,
        remote_branch=remote_branch,
        operation_id=operation_id,
        operation_type=operation_type,
        monitor_log=monitor_log,
    )


async def _enforce_needs_human_reason(
    runner: PullRequestMonitorRunner,
    *,
    result: VerdictResult,
    original_prompt: str,
    workspace_id: str,
    pr_number: int,
    item_id: str,
    item_kind: str,
    item_author: str | None,
    item_path: str | None,
    item_line: int | None,
    commit_message: str,
    compose_project: str,
    compose_file: Path,
    state: MonitorState | None,
    task_tag: str | None,
    operation_start_head: str | None,
    base_branch: str,
    remote_branch: str | None,
    operation_id: str | None,
    operation_type: str | None,
    monitor_log: WorkspaceLogSink | None,
) -> VerdictResult:
    """Bound one re-ask without changing the original blocking verdict."""
    from awf.runtime.pr_monitor_runner.helpers import (
        _needs_human_reason_missing,
        _sanitize_verdict_reason,
    )
    from awf.runtime.pr_monitor_runner.notify_human_details import _needs_human_reason_state_key

    result = replace(result, reason=_sanitize_verdict_reason(result.reason))
    if not _needs_human_reason_missing(result):
        return result

    adapter = getattr(getattr(runner, "_deps", None), "adapter", None)
    if bool(getattr(adapter, "is_hosted", False)):
        # Hosted execution has remote PR credentials and no read-only run
        # contract. It can advance the head before it reports a terminal SHA,
        # which local cleanup cannot reverse. Preserve the blocking verdict
        # and record the missing reason instead of issuing a reason-only re-ask.
        await _record_needs_human_reason_missing(
            runner,
            workspace_id=workspace_id,
            pr_number=pr_number,
            item_id=item_id,
            item_kind=item_kind,
            item_author=item_author,
            item_path=item_path,
            item_line=item_line,
            base_branch=base_branch,
            remote_branch=remote_branch,
            operation_id=operation_id,
            operation_type=operation_type,
            monitor_log=monitor_log,
            reason_code=_NEEDS_HUMAN_REASON_CLARIFICATION_UNAVAILABLE,
        )
        return result

    worktrees_root = getattr(runner, "_worktrees_root", None)
    worktree_path = worktrees_root / workspace_id if isinstance(worktrees_root, Path) else None
    reask_restore_ref: str | None = None
    reask_worktree: _IsolatedReaskWorktree | None = None
    if worktree_path is not None:
        # A real AWF worktree always has its Git control file. Unit-level
        # runners deliberately omit it, so retain their direct invocation seam
        # without treating it as an isolated-worktree setup failure.
        has_git_worktree = (worktree_path / ".git").exists()
        try:
            # The original repair invocation has already returned. It may have
            # created a clean repair commit, so snapshot its resulting HEAD
            # before the clarification-only invocation. Cleanup must preserve
            # that repair and discard only clarification side effects.
            if has_git_worktree:
                reask_restore_ref = await runner._rev_parse_head(worktree_path)
                if reask_restore_ref is None:
                    raise RuntimeError("could not capture the worktree restore ref")
                reask_worktree = await _create_isolated_reask_worktree(
                    runner,
                    worktree_path=worktree_path,
                    restore_ref=reask_restore_ref,
                )
            elif worktree_path.exists() and getattr(runner, "_deps", None) is not None:
                raise RuntimeError("worktree has no Git control file")
            else:
                reask_restore_ref = await runner._rev_parse_head(worktree_path)
        except _IsolatedReaskWorktreeCleanupFailedError:
            # A failed cleanup can leave a nested repository in the primary
            # checkout. It must stop the fix cycle rather than be downgraded to
            # an unavailable advisory clarification.
            raise
        except (GitOperationError, OSError, RuntimeError, _MonitorPolicyBlockedError) as exc:
            # Clarification is advisory and read-only. A worktree/setup failure
            # must preserve the original blocking verdict instead of blocking
            # the monitor or issuing an unisolated re-ask, and record why the
            # clarification follow-up was unavailable.
            _log.warning(
                "monitor.needs_human_reason_reask_setup_failed",
                workspace_id=workspace_id,
                pr_number=pr_number,
                item_id=redact_audit_text(item_id, limit=200),
                item_kind=item_kind,
                error=redact_audit_text(str(exc), limit=240),
            )
            await _record_needs_human_reason_missing(
                runner,
                workspace_id=workspace_id,
                pr_number=pr_number,
                item_id=item_id,
                item_kind=item_kind,
                item_author=item_author,
                item_path=item_path,
                item_line=item_line,
                base_branch=base_branch,
                remote_branch=remote_branch,
                operation_id=operation_id,
                operation_type=operation_type,
                monitor_log=monitor_log,
                reason_code=_NEEDS_HUMAN_REASON_CLARIFICATION_UNAVAILABLE,
            )
            return result

    async def _cleanup_reask_worktree() -> tuple[str | None, bool]:
        # The re-ask only collects a reason. Remove its tracked-only checkout
        # first, then only inspect the primary worktree. The primary checkout
        # is never mounted into the clarification container, so any changes
        # there are unrelated and must not be restored or deleted.
        if worktree_path is None or reask_restore_ref is None:
            return None, False

        try:
            isolated_cleanup_failure = await _remove_isolated_reask_worktree(runner, reask_worktree)
        except (GitOperationError, OSError, RuntimeError) as exc:
            # A command-runner exception leaves the isolated checkout's removal
            # unconfirmed, so preserve its policy-blocking cleanup phase.
            isolated_cleanup_failure = str(exc)
        if isolated_cleanup_failure is not None:
            _log.warning(
                "monitor.needs_human_reason_reask_isolated_cleanup_failed",
                workspace_id=workspace_id,
                restore_ref=reask_restore_ref,
                message=isolated_cleanup_failure,
            )
            return isolated_cleanup_failure, True
        primary_check_failure = await _check_reask_primary_worktree_clean(
            runner,
            worktree_path=worktree_path,
            restore_ref=reask_restore_ref,
        )
        if primary_check_failure is None:
            return None, False
        _log.warning(
            "monitor.needs_human_reason_reask_cleanup_failed",
            workspace_id=workspace_id,
            restore_ref=reask_restore_ref,
            message=primary_check_failure[:400],
        )
        return primary_check_failure, False

    async def _run_reask_cleanup(*, event_name: str) -> tuple[str | None, bool]:
        try:
            cleanup_error, isolated_cleanup_failed = await _cleanup_reask_worktree()
        except (GitOperationError, OSError, RuntimeError) as exc:
            cleanup_error = str(exc)
            isolated_cleanup_failed = False
        if cleanup_error is not None:
            _log.warning(
                event_name,
                workspace_id=workspace_id,
                error=redact_audit_text(cleanup_error, limit=240),
            )
        return cleanup_error, isolated_cleanup_failed

    async def _run_reask_cleanup_cancellation_safe(
        *,
        event_name: str,
        needs_human_reason: str | None = None,
    ) -> tuple[str | None, bool]:
        """Complete re-ask cleanup before propagating any worker cancellation."""
        cleanup_task = asyncio.create_task(_run_reask_cleanup(event_name=event_name))
        cancellation: asyncio.CancelledError | None = None
        while True:
            try:
                cleanup_result = await asyncio.shield(cleanup_task)
            except asyncio.CancelledError as exc:
                cancellation = exc
                if cleanup_task.done():
                    cleanup_task.result()
                    raise
            else:
                if cancellation is not None:
                    cleanup_error, _isolated_cleanup_failed = cleanup_result
                    if cleanup_error is not None:
                        human_reason = needs_human_reason or _GENERIC_HUMAN_BLOCKER_REASON
                        if state is not None:
                            state.mark_addressed(item_id, "needs_human")
                            state.mark_addressed(
                                _needs_human_reason_state_key(item_id), human_reason
                            )
                        persistence_task = asyncio.create_task(
                            _persist_reask_cleanup_failure_after_cancellation(
                                runner,
                                workspace_id=workspace_id,
                                item_id=item_id,
                                needs_human_reason=human_reason,
                            )
                        )
                        while True:
                            try:
                                await asyncio.shield(persistence_task)
                                break
                            except asyncio.CancelledError:
                                if persistence_task.done():
                                    persistence_task.result()
                                    break
                    raise cancellation
                return cleanup_result

    try:
        reask_result = await runner._invoke_cli_for_verdict_result(
            workspace_id=workspace_id,
            prompt=needs_human_reason_reask_prompt(original_prompt=original_prompt),
            commit_message=commit_message,
            compose_project=compose_project,
            compose_file=compose_file,
            state=state,
            task_tag=task_tag,
            operation_start_head=operation_start_head,
            commit_dirty_changes=False,
            isolated_worktree_host_path=(
                reask_worktree.path if reask_worktree is not None else None
            ),
        )
    except (
        ComposeExecCleanupError,
        ProviderRecoveryAuthError,
        ProviderRecoveryFallbackError,
        ProviderRecoveryRetryError,
        _MonitorAgentServiceRecoveryFailedError,
        _MonitorAgentServiceRecoverySupersededError,
        _MonitorAgentRuntimeOwnershipRepairFailedError,
        _MonitorHeadObjectMissingError,
        _MonitorMirrorHooksPathRepairFailedError,
        _MonitorPolicyBlockedError,
    ):
        # Preserve the terminal result that already stops the fix cycle;
        # cleanup failure is recorded but cannot permit another item to run.
        await _run_reask_cleanup_cancellation_safe(
            event_name="monitor.needs_human_reason_reask_cleanup_failed_after_terminal_error"
        )
        raise
    except asyncio.CancelledError:
        # Cancellation still owns control flow, but record a cleanup failure so
        # stranded clarification edits cannot be mistaken for intentional.
        await _run_reask_cleanup_cancellation_safe(
            event_name="monitor.needs_human_reason_reask_cleanup_failed_after_cancellation"
        )
        raise
    except Exception as exc:
        cleanup_error, _isolated_cleanup_failed = await _run_reask_cleanup_cancellation_safe(
            event_name="monitor.needs_human_reason_reask_cleanup_failed_after_error"
        )
        _log.warning(
            "monitor.needs_human_reason_reask_failed",
            workspace_id=workspace_id,
            pr_number=pr_number,
            item_id=redact_audit_text(item_id, limit=200),
            item_kind=item_kind,
            error=redact_audit_text(str(exc), limit=240),
        )
        if cleanup_error is not None:
            raise _MonitorPolicyBlockedError(
                cleanup_error or "Could not remove the NEEDS_HUMAN reason re-ask checkout.",
                reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
            ) from exc
    else:
        sanitized_reask_reason = _sanitize_verdict_reason(reask_result.reason)
        reask_needs_human_reason = (
            sanitized_reask_reason if reask_result.verdict == "needs_human" else None
        )
        cleanup_error, _isolated_cleanup_failed = await _run_reask_cleanup_cancellation_safe(
            event_name="monitor.needs_human_reason_reask_cleanup_failed_after_success",
            needs_human_reason=reask_needs_human_reason,
        )
        if cleanup_error is not None:
            raise _MonitorPolicyBlockedError(
                cleanup_error or "Could not remove the NEEDS_HUMAN reason re-ask checkout.",
                reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
            )
        if cleanup_error is None:
            reask_result = replace(
                reask_result,
                reason=sanitized_reask_reason,
            )
            if reask_result.verdict == "needs_human" and not _needs_human_reason_missing(
                reask_result
            ):
                return reask_result
    await _record_needs_human_reason_missing(
        runner,
        workspace_id=workspace_id,
        pr_number=pr_number,
        item_id=item_id,
        item_kind=item_kind,
        item_author=item_author,
        item_path=item_path,
        item_line=item_line,
        base_branch=base_branch,
        remote_branch=remote_branch,
        operation_id=operation_id,
        operation_type=operation_type,
        monitor_log=monitor_log,
    )
    return result


async def _record_needs_human_reason_missing(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    pr_number: int,
    item_id: str,
    item_kind: str,
    item_author: str | None,
    item_path: str | None,
    item_line: int | None,
    base_branch: str,
    remote_branch: str | None,
    operation_id: str | None,
    operation_type: str | None,
    monitor_log: WorkspaceLogSink | None,
    reason_code: str = _NEEDS_HUMAN_REASON_MISSING,
) -> None:
    """Warn and persist the reason-clarification diagnostic."""
    evidence = {
        "item_id": redact_audit_text(item_id, limit=200),
        "item_kind": item_kind,
        "item_author": redact_audit_text(item_author or "", limit=200),
        "item_path": redact_audit_text(item_path or "", limit=400),
        "item_line": item_line,
    }
    _log.warning(
        "monitor.needs_human_reason_missing",
        workspace_id=workspace_id,
        pr_number=pr_number,
        reason_code=reason_code,
        operation_id=operation_id,
        **evidence,
    )
    await runner._record_pr_monitor_audit_event(
        workspace_id=workspace_id,
        event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
        action=f"address_{item_kind}",
        outcome="needs_human",
        reason_code=reason_code,
        pr_number=pr_number,
        status=None,
        base_branch=base_branch,
        remote_branch=remote_branch,
        operation_id=operation_id,
        operation_type=operation_type,
        monitor_log=monitor_log,
        evidence=evidence,
    )


async def _owned_paths_for_prompt(
    runner: PullRequestMonitorRunner,
    workspace_id: str,
) -> list[str]:
    session_factory = runner._deps.session_factory
    session_context = session_factory()
    async with session_context as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        return list(workspace.owned_paths) if workspace is not None else []


async def _owned_paths_for_prompt_or_empty(
    runner: PullRequestMonitorRunner,
    workspace_id: str,
) -> list[str]:
    try:
        return await _owned_paths_for_prompt(runner, workspace_id)
    except Exception as exc:
        _log.warning(
            "monitor.owned_paths_prompt_unavailable",
            workspace_id=workspace_id,
            error_type=type(exc).__name__,
            error=redact_audit_text(str(exc), limit=240),
        )
        return []


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
    return (
        await runner._invoke_cli_for_verdict_result(
            workspace_id=workspace_id,
            prompt=prompt,
            commit_message=commit_message,
            compose_project=compose_project,
            compose_file=compose_file,
            state=state,
            task_tag=task_tag,
            operation_start_head=operation_start_head,
        )
    ).verdict


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
    isolated_worktree_host_path: Path | None = None,
) -> VerdictResult:
    from awf.runtime.pr_monitor_runner.helpers import _parse_verdict_result

    result_stdout = ""
    cli_failed = False
    command_evidence: list[str] = []
    if state is not None:
        state.hosted_terminal_head_advanced = False
    if await runner._provider_recovery_suppresses_cli(workspace_id):
        raise ProviderRecoveryRetryError()
    worktree_path = runner._worktrees_root / workspace_id
    if not await repair_agent_runtime_ownership(
        logger=_log,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="monitor_agent_pre_launch",
        event_name=MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    ):
        raise _MonitorAgentRuntimeOwnershipRepairFailedError(
            "AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED"
        )
    mirror_path = mirror_path_for_worktree(worktree_path)
    if mirror_path is not None:
        try:
            await repair_mirror_hooks_path(mirror_path)
        except (GitOperationError, OSError) as exc:
            repair_details = mirror_hooks_repair_failure_details(
                exc,
                repair_stage="before_comment_agent",
                mirror_path=mirror_path,
            )
            _log.warning(
                "monitor.mirror_hooks_path_repair_failed",
                workspace_id=workspace_id,
                reason_code=_MIRROR_HOOKS_PATH_POISONED_REASON,
                **repair_details,
            )
            raise _MonitorMirrorHooksPathRepairFailedError() from exc
    agent_run_err = None
    try:
        if isolated_worktree_host_path is not None:
            result = await runner._run_monitor_agent_with_service_recovery(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=prompt,
                log_source="recovery",
                command_evidence=command_evidence,
                operation_start_head=operation_start_head,
                state=state,
                isolated_worktree_host_path=isolated_worktree_host_path,
            )
        else:
            result = await runner._run_monitor_agent_with_service_recovery(
                workspace_id=workspace_id,
                compose_project=compose_project,
                compose_file=compose_file,
                prompt=prompt,
                log_source="recovery",
                command_evidence=command_evidence,
                operation_start_head=operation_start_head,
                state=state,
            )
        result_stdout = result.stdout
    except AgentRunError as exc:
        cli_failed = True
        result_stdout = exc.result.stdout
        agent_run_err = exc
        append_command_evidence(
            command_evidence,
            stdout=exc.result.stdout,
            stderr=exc.result.stderr,
        )
    except (ProviderRecoveryRetryError, _MonitorAgentServiceRecoverySupersededError):
        raise
    except _MonitorAgentServiceRecoveryFailedError:
        raise
    except (
        _MonitorAgentRuntimeOwnershipRepairFailedError,
        _MonitorHeadObjectMissingError,
        _MonitorMirrorHooksPathRepairFailedError,
    ):
        raise
    except Exception:
        if mirror_path is not None:
            try:
                await repair_mirror_hooks_path(mirror_path)
            except (GitOperationError, OSError) as exc:
                repair_details = mirror_hooks_repair_failure_details(
                    exc,
                    repair_stage="after_comment_agent_exception",
                    mirror_path=mirror_path,
                )
                _log.warning(
                    "monitor.mirror_hooks_path_repair_failed",
                    workspace_id=workspace_id,
                    reason_code=_MIRROR_HOOKS_PATH_POISONED_REASON,
                    **repair_details,
                )
                raise _MonitorMirrorHooksPathRepairFailedError() from exc
        if commit_dirty_changes:
            await runner._commit_dirty_worktree(
                workspace_id=workspace_id,
                message=commit_message,
                compose_project=compose_project,
                compose_file=compose_file,
                state=state,
                command_evidence=command_evidence,
                task_tag=task_tag,
                operation_start_head=operation_start_head,
            )
        raise

    committed_dirty_changes = (
        await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message=commit_message,
            compose_project=compose_project,
            compose_file=compose_file,
            state=state,
            command_evidence=command_evidence,
            task_tag=task_tag,
            operation_start_head=operation_start_head,
        )
        if commit_dirty_changes
        else False
    )

    if agent_run_err is not None:
        await runner._handle_provider_agent_run_error(workspace_id, agent_run_err, state=state)
        _log.warning(
            "monitor.cli_nonzero_exit",
            returncode=agent_run_err.result.returncode,
        )

    if committed_dirty_changes or (state is not None and state.hosted_terminal_head_advanced):
        parsed = _parse_verdict_result(result_stdout)
        return VerdictResult(verdict="fix_committed", reason=parsed.reason)
    if cli_failed:
        return VerdictResult(verdict="agent_failed")
    return _parse_verdict_result(result_stdout)


async def _post_human_notification_once(
    runner: PullRequestMonitorRunner,
    *,
    repo: RepoRef,
    pr_number: int,
    status: PRStatus,
    state: MonitorState,
    blocker_reason: str | None = None,
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
        ),
    )
    state.mark_addressed(key, "notified")
