"""Submodule for handling review comments, thread addressing, and human notifications."""

from __future__ import annotations

import shutil
import stat
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal

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


@dataclass(frozen=True)
class _IgnoredWorktreeSnapshot:
    """Temporary backup of ignored paths present before a clarification re-ask."""

    root: Path
    paths: tuple[str, ...]


def _snapshot_relative_path(path: str) -> PurePosixPath:
    """Return a safe worktree-relative path reported by git status."""
    relative = PurePosixPath(path.rstrip("/"))
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe ignored worktree path: {path!r}")
    return relative


def _collapsed_snapshot_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    """Keep only top-level ignored paths so directory snapshots do not overlap."""
    selected: list[str] = []
    selected_normalized: list[str] = []
    for path in sorted(paths, key=lambda value: (value.rstrip("/").count("/"), value)):
        normalized = path.rstrip("/")
        if not normalized or any(
            normalized == parent or normalized.startswith(f"{parent}/")
            for parent in selected_normalized
        ):
            continue
        selected.append(path)
        selected_normalized.append(normalized)
    return tuple(selected)


def _copy_ignored_snapshot_entry(source: Path, destination: Path) -> None:
    """Copy one ignored path without dereferencing an agent-controlled symlink."""
    source_mode = source.lstat().st_mode
    if stat.S_ISLNK(source_mode):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(source.readlink())
        shutil.copystat(source, destination, follow_symlinks=False)
    elif stat.S_ISDIR(source_mode):
        shutil.copytree(source, destination, symlinks=True)
    elif stat.S_ISREG(source_mode):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=False)
    else:
        raise OSError(f"unsupported ignored path type: {source}")


def _remove_ignored_snapshot_entry(path: Path) -> None:
    """Remove one path without following a symlink that a re-ask may have planted."""
    try:
        path_mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISDIR(path_mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def _safe_restore_destination(worktree_path: Path, relative: PurePosixPath) -> Path:
    """Refuse to write a backup through a symlinked worktree parent."""
    destination = worktree_path.joinpath(*relative.parts)
    parent = worktree_path
    for part in relative.parts[:-1]:
        parent /= part
        if parent.is_symlink():
            raise OSError(f"ignored snapshot restore parent is a symlink: {parent}")
    return destination


async def _snapshot_reask_ignored_paths(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
) -> _IgnoredWorktreeSnapshot | None:
    """Snapshot ignored paths so a reason-only re-ask cannot persist edits to them."""
    if not (worktree_path / ".git").exists():
        # Lightweight test doubles do not have a worktree that can contain
        # side effects. Real AWF worktrees always contain a .git control file.
        return None

    from awf.runtime.validation_worktree import check_validation_worktree_clean

    async def _run_git(args: list[str]) -> CommandResult:
        return await runner._deps.runner.run(git_worktree_command(worktree_path, *args))

    check = await check_validation_worktree_clean(
        run_git=_run_git,
        worktree_path=worktree_path,
        ignore_all_ignored=True,
    )
    if check.reason_code is not None:
        raise _MonitorPolicyBlockedError(
            "Could not snapshot ignored worktree paths before the NEEDS_HUMAN reason re-ask.",
            reason_code=check.reason_code,
        )

    paths = _collapsed_snapshot_paths(check.ignored_paths)
    if not paths:
        return None

    snapshot_root = Path(tempfile.mkdtemp(prefix="awf-needs-human-reask-ignored-"))
    try:
        for path in paths:
            relative = _snapshot_relative_path(path)
            source = worktree_path.joinpath(*relative.parts)
            if not source.exists() and not source.is_symlink():
                raise FileNotFoundError(source)
            _copy_ignored_snapshot_entry(source, snapshot_root.joinpath(*relative.parts))
    except (OSError, ValueError) as exc:
        shutil.rmtree(snapshot_root, ignore_errors=True)
        raise _MonitorPolicyBlockedError(
            "Could not snapshot ignored worktree paths before the NEEDS_HUMAN reason re-ask.",
            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
        ) from exc
    return _IgnoredWorktreeSnapshot(root=snapshot_root, paths=paths)


async def _restore_reask_ignored_paths(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
    snapshot: _IgnoredWorktreeSnapshot | None,
) -> str | None:
    """Delete re-ask ignored output and restore the ignored state captured before it."""
    if not (worktree_path / ".git").exists():
        return None

    clean = await runner._deps.runner.run(git_worktree_command(worktree_path, "clean", "-ffdX"))
    failure: str | None = None
    if not clean.ok:
        failure = "`git clean -ffdX` could not remove ignored re-ask side effects"
    try:
        if snapshot is not None:
            for path in snapshot.paths:
                relative = _snapshot_relative_path(path)
                destination = _safe_restore_destination(worktree_path, relative)
                _remove_ignored_snapshot_entry(destination)
                _copy_ignored_snapshot_entry(snapshot.root.joinpath(*relative.parts), destination)
    except (OSError, ValueError) as exc:
        failure = "Could not restore ignored worktree paths after the NEEDS_HUMAN reason re-ask"
        _log.warning(
            "monitor.needs_human_reason_reask_ignored_restore_failed",
            worktree_path=str(worktree_path),
            error=redact_audit_text(str(exc), limit=240),
        )
    finally:
        if snapshot is not None:
            shutil.rmtree(snapshot.root, ignore_errors=True)
    return failure


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

    worktree_path = runner._worktrees_root / workspace_id
    # The original repair invocation has already returned. It may have created
    # a clean repair commit, so snapshot its resulting HEAD before the
    # clarification-only invocation. Cleanup must preserve that repair and
    # discard only clarification side effects.
    reask_restore_ref = await runner._rev_parse_head(worktree_path)
    if reask_restore_ref is None:
        raise _MonitorPolicyBlockedError(
            "Could not capture a worktree restore ref before the NEEDS_HUMAN reason re-ask.",
            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
        )
    reask_ignored_snapshot = await _snapshot_reask_ignored_paths(
        runner,
        worktree_path=worktree_path,
    )

    async def _cleanup_reask_worktree() -> None:
        # The re-ask only collects a reason and must not leave edits for the
        # next item in the fix cycle to commit under a different message.
        from awf.runtime.pr_monitor_runner import pre_push_validation as _ppv

        cleanup = await _ppv._pre_push_validation_cleanup(
            runner,
            worktree_path=worktree_path,
            restore_ref=reask_restore_ref,
        )
        ignored_cleanup_failure = await _restore_reask_ignored_paths(
            runner,
            worktree_path=worktree_path,
            snapshot=reask_ignored_snapshot,
        )
        if ignored_cleanup_failure is not None:
            _log.warning(
                "monitor.needs_human_reason_reask_ignored_cleanup_failed",
                workspace_id=workspace_id,
                restore_ref=reask_restore_ref,
                message=ignored_cleanup_failure,
            )
            raise _MonitorPolicyBlockedError(
                f"Could not clean NEEDS_HUMAN reason re-ask worktree: {ignored_cleanup_failure}",
                reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
            )
        if cleanup.ok:
            return
        _log.warning(
            "monitor.needs_human_reason_reask_cleanup_failed",
            workspace_id=workspace_id,
            restore_ref=reask_restore_ref,
            reason_code=cleanup.reason_code,
            message=cleanup.message[:400],
        )
        raise _MonitorPolicyBlockedError(
            f"Could not clean NEEDS_HUMAN reason re-ask worktree: {cleanup.message}",
            reason_code=cleanup.reason_code or VALIDATION_WORKTREE_CLEANUP_FAILED,
        )

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
        try:
            await _cleanup_reask_worktree()
        except Exception as cleanup_exc:
            # Preserve the terminal result that already stops the fix cycle;
            # cleanup failure is recorded but cannot permit another item to run.
            _log.warning(
                "monitor.needs_human_reason_reask_cleanup_failed_after_terminal_error",
                workspace_id=workspace_id,
                error=redact_audit_text(str(cleanup_exc), limit=240),
            )
        raise
    except Exception as exc:
        try:
            await _cleanup_reask_worktree()
        except Exception as cleanup_exc:
            _log.warning(
                "monitor.needs_human_reason_reask_cleanup_failed_after_error",
                workspace_id=workspace_id,
                error=redact_audit_text(str(cleanup_exc), limit=240),
            )
            raise cleanup_exc from exc
        _log.warning(
            "monitor.needs_human_reason_reask_failed",
            workspace_id=workspace_id,
            pr_number=pr_number,
            item_id=redact_audit_text(item_id, limit=200),
            item_kind=item_kind,
            error=redact_audit_text(str(exc), limit=240),
        )
    else:
        await _cleanup_reask_worktree()
        reask_result = replace(
            reask_result,
            reason=_sanitize_verdict_reason(reask_result.reason),
        )
        if reask_result.verdict == "needs_human" and not _needs_human_reason_missing(reask_result):
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
