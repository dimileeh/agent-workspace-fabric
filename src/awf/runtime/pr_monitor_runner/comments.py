"""Submodule for handling review comments, thread addressing, and human notifications."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import re
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from awf.common.audit import redact_audit_text
from awf.common.commands import CommandResult
from awf.common.companions import (
    ISOLATED_REASK_WORKTREE_SUFFIX,
    isolated_reask_worktree_liveness_lock_path,
)
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
    AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
    MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    repair_agent_runtime_ownership,
)
from awf.runtime.pr_monitor_runner import comment_verdict as _comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict import (
    Verdict as Verdict,
)
from awf.runtime.pr_monitor_runner.comment_verdict import (
    VerdictResult as VerdictResult,
)
from awf.runtime.pr_monitor_runner.comment_verdict import (
    _owned_paths_for_prompt as _owned_paths_for_prompt,
)
from awf.runtime.pr_monitor_runner.comment_verdict import (
    _owned_paths_for_prompt_or_empty as _owned_paths_for_prompt_or_empty,
)
from awf.runtime.pr_monitor_runner.constants import (
    _AUDIT_COMMENT_RESOLUTION_EVENT,
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

if TYPE_CHECKING:
    from awf.common.github_client import RepoRef
    from awf.runtime.logs import WorkspaceLogSink
    from awf.runtime.pr_monitor import MonitorState, PRStatus, ReviewComment, ReviewThread
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

_log = get_logger(__name__)

_GENERIC_HUMAN_BLOCKER_REASON = "human attention is required before AWF can continue"
# Name interrupted re-ask checkouts as managed siblings of their source. Their
# UUID-qualified suffix lets the orphan reconciler distinguish them from
# policy-declared companions if creation is interrupted before cleanup.
_CLARIFICATION_MODEL_SERVICE_RECOVERY_FAILED = "CLARIFICATION_MODEL_SERVICE_RECOVERY_FAILED"
_CLARIFICATION_MODEL_NETWORK_CLEANUP_FAILED = "CLARIFICATION_MODEL_NETWORK_CLEANUP_FAILED"
# Git expands include.path before creating the linked worktree. The writable
# mirror can therefore point an include at a special file that never responds.
_ISOLATED_REASK_WORKTREE_CREATION_TIMEOUT_SECONDS = 30.0
_FILTER_DRIVER_CONFIG_KEY_RE = re.compile(
    r"^(filter\.[A-Za-z0-9][A-Za-z0-9._-]*)\.(?:smudge|process)$"
)


@dataclass(frozen=True)
class _IsolatedReaskWorktree:
    """Tracked-only worktree used by one local NEEDS_HUMAN clarification re-ask."""

    source_worktree: Path
    path: Path
    liveness_lock_fd: int | None = None
    liveness_lock_path: Path | None = None


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
        """Run a Git command against the primary worktree."""
        return await runner._deps.runner.run(
            git_worktree_command(worktree_path, "-c", "core.fsmonitor=false", *args)
        )

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
        """Run a Git command against the primary worktree."""
        return await runner._deps.runner.run(
            git_worktree_command(worktree_path, "-c", "core.fsmonitor=false", *args)
        )

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


async def _checkout_filter_overrides(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
) -> tuple[str, ...]:
    """Return Git options that prevent configured checkout filters from running."""
    configured_filters = await runner._deps.runner.run(
        git_worktree_command(
            worktree_path,
            "config",
            "--includes",
            "--name-only",
            "--get-regexp",
            r"^filter\..*\.(smudge|process)$",
        )
    )
    if configured_filters.returncode == 1:
        return ()
    if not configured_filters.ok:
        raise _MonitorPolicyBlockedError(
            "Could not determine checkout filters before the NEEDS_HUMAN reason re-ask.",
            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
        )

    driver_prefixes: set[str] = set()
    for config_key in configured_filters.stdout.splitlines():
        match = _FILTER_DRIVER_CONFIG_KEY_RE.fullmatch(config_key)
        if match is None:
            raise _MonitorPolicyBlockedError(
                "Could not safely disable checkout filters before the NEEDS_HUMAN reason re-ask.",
                reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
            )
        driver_prefixes.add(match.group(1))

    return tuple(
        option
        for driver_prefix in sorted(driver_prefixes)
        for option in (
            "-c",
            f"{driver_prefix}.smudge=",
            "-c",
            f"{driver_prefix}.process=",
            "-c",
            f"{driver_prefix}.required=false",
        )
    )


async def _create_isolated_reask_worktree(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
    restore_ref: str,
    on_cleanup_failure_after_cancellation: Callable[[str], Coroutine[Any, Any, None]] | None = None,
) -> _IsolatedReaskWorktree | None:
    """Create a temporary tracked-only checkout for a local clarification re-ask."""
    if not (worktree_path / ".git").exists():
        # Lightweight test doubles do not have a worktree that can contain side
        # effects. Real AWF worktrees always contain a .git control file.
        return None

    await _prepare_reask_primary_worktree(runner, worktree_path=worktree_path)
    # Keep an interrupted checkout outside the primary worktree: otherwise a
    # later repair could stage the nested repository as a gitlink.
    path = worktree_path.parent / (
        f"{worktree_path.name}{ISOLATED_REASK_WORKTREE_SUFFIX}{uuid4().hex}"
    )
    try:
        liveness_lock_fd, liveness_lock_path = _acquire_isolated_reask_liveness_lock(path)
    except OSError as exc:
        raise _MonitorPolicyBlockedError(
            "Could not protect the isolated worktree before the NEEDS_HUMAN reason re-ask.",
            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
        ) from exc
    reask_worktree = _IsolatedReaskWorktree(
        source_worktree=worktree_path,
        path=path,
        liveness_lock_fd=liveness_lock_fd,
        liveness_lock_path=liveness_lock_path,
    )

    async def _cleanup_after_cancellation(*, event_name: str) -> None:
        """Remove a possibly-created checkout before preserving cancellation."""
        cleanup_task = asyncio.create_task(
            _cleanup_isolated_reask_worktree_after_creation_failure(
                runner,
                reask_worktree=reask_worktree,
                event_name=event_name,
            )
        )
        while True:
            try:
                cleanup_error = await asyncio.shield(cleanup_task)
                break
            except asyncio.CancelledError:
                if cleanup_task.done():
                    cleanup_error = cleanup_task.result()
                    break
        if cleanup_error is not None and on_cleanup_failure_after_cancellation is not None:
            persistence_task = asyncio.create_task(
                on_cleanup_failure_after_cancellation(cleanup_error)
            )
            while True:
                try:
                    await asyncio.shield(persistence_task)
                    break
                except asyncio.CancelledError:
                    if persistence_task.done():
                        persistence_task.result()
                        break

    try:
        create = await runner._deps.runner.run(
            git_worktree_command(
                worktree_path,
                # Register the linked worktree without populating it. Its
                # effective configuration can differ from the primary
                # worktree through includeIf.gitdir rules, so filters must be
                # discovered after the linked gitdir exists.
                "worktree",
                "add",
                "--detach",
                "--no-checkout",
                str(path),
                restore_ref,
            ),
            timeout_seconds=_ISOLATED_REASK_WORKTREE_CREATION_TIMEOUT_SECONDS,
        )
    except (GitOperationError, OSError, RuntimeError):
        _release_isolated_reask_liveness_lock(reask_worktree)
        raise
    except asyncio.CancelledError:
        # Git may have registered the worktree before cancellation reaches the
        # command runner. Remove that checkout before preserving cancellation.
        await _cleanup_after_cancellation(
            event_name=(
                "monitor.needs_human_reason_reask_"
                "isolated_cleanup_failed_after_creation_cancellation"
            )
        )
        raise
    if not create.ok:
        # Git can register and populate the checkout before reporting an error
        # (for example, when a post-checkout hook fails). Do not leave that
        # sibling repository behind while treating clarification as unavailable.
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

    try:
        checkout_filter_overrides = await _checkout_filter_overrides(
            runner,
            worktree_path=path,
        )
        checkout = await runner._deps.runner.run(
            git_worktree_command(
                path,
                # The primary mirror is writable by the prior agent. Disable
                # its default hooks directory and every filter effective for
                # this linked worktree while Git populates the clarification
                # checkout. Disable the configured filesystem monitor too:
                # a string-valued core.fsmonitor is an executable hook path.
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                *checkout_filter_overrides,
                "checkout",
                # A re-ask only needs the superproject checkout. Do not
                # inherit submodule.recurse from the source worktree.
                "--no-recurse-submodules",
                "--detach",
                restore_ref,
            )
        )
    except asyncio.CancelledError:
        # The linked worktree is registered before its checkout, so cleanup
        # must cover cancellation during the filter probe or population too.
        await _cleanup_after_cancellation(
            event_name=(
                "monitor.needs_human_reason_reask_"
                "isolated_cleanup_failed_after_checkout_cancellation"
            )
        )
        raise
    except Exception as exc:
        cleanup_error = await _cleanup_isolated_reask_worktree_after_creation_failure(
            runner,
            reask_worktree=reask_worktree,
            event_name=(
                "monitor.needs_human_reason_reask_"
                "isolated_cleanup_failed_after_checkout_setup_failure"
            ),
        )
        if cleanup_error is not None:
            raise _IsolatedReaskWorktreeCleanupFailedError(
                cleanup_error,
                reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
            ) from exc
        raise
    if not checkout.ok:
        cleanup_error = await _cleanup_isolated_reask_worktree_after_creation_failure(
            runner,
            reask_worktree=reask_worktree,
            event_name=(
                "monitor.needs_human_reason_reask_isolated_cleanup_failed_after_checkout_failure"
            ),
        )
        if cleanup_error is not None:
            raise _IsolatedReaskWorktreeCleanupFailedError(
                cleanup_error,
                reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
            )
        raise _MonitorPolicyBlockedError(
            "Could not populate an isolated worktree before the NEEDS_HUMAN reason re-ask.",
            reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
        )

    try:
        ownership_repaired = await repair_agent_runtime_ownership(
            logger=_log,
            workspace_id=worktree_path.name,
            worktree_path=path,
            reason="needs_human_reason_reask_pre_launch",
            event_name=MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
            linked_worktree_id=path.name,
            repair_shared_git_metadata=False,
        )
    except asyncio.CancelledError:
        # Ownership repair runs asynchronously after Git creates the checkout,
        # so it needs the same cancellation-safe cleanup as `git worktree add`.
        await _cleanup_after_cancellation(
            event_name=(
                "monitor.needs_human_reason_reask_"
                "isolated_cleanup_failed_after_ownership_repair_cancellation"
            )
        )
        raise
    if not ownership_repaired:
        cleanup_error = await _cleanup_isolated_reask_worktree_after_creation_failure(
            runner,
            reask_worktree=reask_worktree,
            event_name=(
                "monitor.needs_human_reason_reask_"
                "isolated_cleanup_failed_after_ownership_repair_failure"
            ),
        )
        if cleanup_error is not None:
            raise _IsolatedReaskWorktreeCleanupFailedError(
                cleanup_error,
                reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
            )
        raise _MonitorPolicyBlockedError(
            "Could not repair isolated worktree ownership before the NEEDS_HUMAN reason re-ask.",
            reason_code=AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE,
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
        # The failed creation may already have placed a sibling repository.
        # Treat an unconfirmed removal just like a
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

    try:
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
    finally:
        _release_isolated_reask_liveness_lock(reask_worktree)


def _acquire_isolated_reask_liveness_lock(path: Path) -> tuple[int, Path]:
    """Lock a re-ask before Git creates its checkout so GC never sees it bare."""
    lock_path = isolated_reask_worktree_liveness_lock_path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        with contextlib.suppress(OSError):
            lock_path.unlink()
        raise

    try:
        # GC holds the same advisory lock through stale-marker removal.  Confirm
        # the pathname still identifies this descriptor after locking: if GC
        # reaped the just-created, not-yet-locked marker, this monitor must not
        # continue with an unprotected checkout.
        marker_stat = lock_path.stat()
        lock_stat = os.fstat(lock_fd)
        if (marker_stat.st_dev, marker_stat.st_ino) != (lock_stat.st_dev, lock_stat.st_ino):
            raise OSError("isolated re-ask liveness marker was replaced")
    except OSError:
        os.close(lock_fd)
        raise
    return lock_fd, lock_path


def _release_isolated_reask_liveness_lock(reask_worktree: _IsolatedReaskWorktree) -> None:
    """Release and remove the re-ask liveness marker after monitor use ends."""
    if reask_worktree.liveness_lock_fd is not None:
        with contextlib.suppress(OSError):
            os.close(reask_worktree.liveness_lock_fd)
    if reask_worktree.liveness_lock_path is not None:
        with contextlib.suppress(FileNotFoundError):
            reask_worktree.liveness_lock_path.unlink()


def _review_item_body_state_key(item_id: str, item_kind: str) -> str | None:
    """Return the addressed-state key for a supported review feedback item."""
    if item_kind == "thread":
        from awf.runtime.pr_monitor import _review_thread_body_state_key

        return _review_thread_body_state_key(item_id)
    if item_kind == "review":
        from awf.runtime.pr_monitor_runner.helpers import _review_comment_body_state_key

        return _review_comment_body_state_key(item_id)
    return None


async def _persist_reask_cleanup_failure_after_cancellation(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    pr_number: int,
    item_id: str,
    item_kind: str,
    item_author: str | None,
    item_path: str | None,
    item_line: int | None,
    needs_human_reason: str | None,
    item_body_hash: str | None,
    cleanup_error: str,
    base_branch: str,
    remote_branch: str | None,
    operation_id: str | None,
    operation_type: str | None,
    monitor_log: WorkspaceLogSink | None,
) -> None:
    """Persist a cleanup blocker without attributing it to the agent."""
    from awf.runtime.pr_monitor_runner.notify_human_details import _needs_human_reason_state_key

    async with runner._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get_for_update(workspace_id)
        if workspace is None:
            return
        addressed = dict(workspace.monitor_threads_addressed or {})
        addressed[item_id] = "needs_human"
        if (
            item_body_hash is not None
            and (body_state_key := _review_item_body_state_key(item_id, item_kind)) is not None
        ):
            addressed[body_state_key] = item_body_hash
        reason_key = _needs_human_reason_state_key(item_id)
        if needs_human_reason is not None:
            addressed[reason_key] = needs_human_reason
        else:
            addressed.pop(reason_key, None)
        workspace.monitor_threads_addressed = addressed
        await session.commit()

    await runner._record_pr_monitor_audit_event(
        workspace_id=workspace_id,
        event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
        action=f"address_{item_kind}",
        outcome="failed",
        reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
        pr_number=pr_number,
        status=None,
        base_branch=base_branch,
        remote_branch=remote_branch,
        operation_id=operation_id,
        operation_type=operation_type,
        monitor_log=monitor_log,
        evidence={
            "item_id": redact_audit_text(item_id, limit=200),
            "item_kind": item_kind,
            "item_author": redact_audit_text(item_author or "", limit=200),
            "item_path": redact_audit_text(item_path or "", limit=400),
            "item_line": item_line,
            "reask_cleanup_error": redact_audit_text(cleanup_error, limit=240),
        },
    )


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
    """Ask the monitor agent to resolve a review thread and return its verdict."""
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
        item_body_hash=_review_thread_body_hash(thread),
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
) -> VerdictResult:
    """Resolve a review comment while retaining its full verdict result."""
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
        item_body_hash=_review_comment_body_hash(comment),
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
    item_body_hash: str | None = None,
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

    async def _persist_cleanup_failure_after_cancellation(
        cleanup_error: str,
        *,
        needs_human_reason: str | None = None,
    ) -> None:
        """Persist cancellation cleanup failure and retain needs-human state."""
        if state is not None:
            state.mark_addressed(item_id, "needs_human")
            if (
                item_body_hash is not None
                and (body_state_key := _review_item_body_state_key(item_id, item_kind)) is not None
            ):
                state.mark_addressed(body_state_key, item_body_hash)
            reason_key = _needs_human_reason_state_key(item_id)
            if needs_human_reason is not None:
                state.mark_addressed(reason_key, needs_human_reason)
            else:
                state.threads_addressed_ids.pop(reason_key, None)
        await _persist_reask_cleanup_failure_after_cancellation(
            runner,
            workspace_id=workspace_id,
            pr_number=pr_number,
            item_id=item_id,
            item_kind=item_kind,
            item_author=item_author,
            item_path=item_path,
            item_line=item_line,
            needs_human_reason=needs_human_reason,
            item_body_hash=item_body_hash,
            cleanup_error=cleanup_error,
            base_branch=base_branch,
            remote_branch=remote_branch,
            operation_id=operation_id,
            operation_type=operation_type,
            monitor_log=monitor_log,
        )

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
                    on_cleanup_failure_after_cancellation=_persist_cleanup_failure_after_cancellation,
                )
            elif getattr(runner, "_deps", None) is not None:
                if worktree_path.exists():
                    raise RuntimeError("worktree has no Git control file")
                raise RuntimeError("worktree is missing")
            else:
                reask_restore_ref = await runner._rev_parse_head(worktree_path)
        except _IsolatedReaskWorktreeCleanupFailedError:
            # A failed cleanup must stop the fix cycle rather than be
            # downgraded to an unavailable advisory clarification.
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
        """Remove the isolated checkout, then verify the primary worktree."""
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
        """Run re-ask cleanup and log any failure under the supplied event."""
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
        cleanup_result: tuple[str | None, bool] | None = None
        while True:
            try:
                cleanup_result = await asyncio.shield(cleanup_task)
            except asyncio.CancelledError as exc:
                cancellation = exc
                if cleanup_task.done():
                    cleanup_result = cleanup_task.result()
                    break
            else:
                break

        assert cleanup_result is not None
        if cancellation is None:
            return cleanup_result

        cleanup_error, _isolated_cleanup_failed = cleanup_result
        if cleanup_error is not None:
            persistence_task = asyncio.create_task(
                _persist_cleanup_failure_after_cancellation(
                    cleanup_error,
                    needs_human_reason=needs_human_reason,
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

    needs_human_reason_code = _NEEDS_HUMAN_REASON_MISSING
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
            isolated_worktree_ref=(reask_restore_ref if reask_worktree is not None else None),
        )
    except (
        ProviderRecoveryAuthError,
        ProviderRecoveryFallbackError,
        ProviderRecoveryRetryError,
    ) as exc:
        # The clarification is advisory. Preserve its original blocking verdict
        # after cleanup, even when provider recovery cannot service the re-ask.
        cleanup_error, _isolated_cleanup_failed = await _run_reask_cleanup_cancellation_safe(
            event_name="monitor.needs_human_reason_reask_cleanup_failed_after_provider_recovery"
        )
        if cleanup_error is not None:
            raise _MonitorPolicyBlockedError(
                cleanup_error or "Could not remove the NEEDS_HUMAN reason re-ask checkout.",
                reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
            ) from exc
        needs_human_reason_code = _NEEDS_HUMAN_REASON_CLARIFICATION_UNAVAILABLE
    except (
        ComposeExecCleanupError,
        _MonitorAgentServiceRecoveryFailedError,
        _MonitorAgentServiceRecoverySupersededError,
        _MonitorAgentRuntimeOwnershipRepairFailedError,
        _MonitorHeadObjectMissingError,
        _MonitorMirrorHooksPathRepairFailedError,
        _MonitorPolicyBlockedError,
    ) as exc:
        # A terminal result stops the fix cycle. Only a stranded isolated
        # checkout takes precedence: recovery must not run another item against
        # that unknown state. A failed primary-worktree inspection remains
        # advisory to the terminal error's reason-code handler.
        cleanup_error, isolated_cleanup_failed = await _run_reask_cleanup_cancellation_safe(
            event_name="monitor.needs_human_reason_reask_cleanup_failed_after_terminal_error"
        )
        if isolated_cleanup_failed and cleanup_error is not None:
            raise _MonitorPolicyBlockedError(
                cleanup_error or "Could not remove the NEEDS_HUMAN reason re-ask checkout.",
                reason_code=VALIDATION_WORKTREE_CLEANUP_FAILED,
            ) from exc
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
        reason_code=needs_human_reason_code,
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


def _sync_comment_verdict_dependencies() -> None:
    """Keep legacy comment-module monkeypatch seams for verdict invocation tests."""
    _comment_verdict.mirror_path_for_worktree = mirror_path_for_worktree  # type: ignore[attr-defined]
    _comment_verdict.repair_agent_runtime_ownership = repair_agent_runtime_ownership  # type: ignore[attr-defined]
    _comment_verdict.repair_mirror_hooks_path = repair_mirror_hooks_path  # type: ignore[attr-defined]
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
    isolated_worktree_ref: str | None = None,
) -> VerdictResult:
    """Invoke the extracted verdict operation through the legacy module seam."""
    _sync_comment_verdict_dependencies()
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
        isolated_worktree_host_path=isolated_worktree_host_path,
        isolated_worktree_ref=isolated_worktree_ref,
    )


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
