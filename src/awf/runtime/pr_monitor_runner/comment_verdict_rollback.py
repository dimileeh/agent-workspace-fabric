"""Protocol-retry rollback and FIXED evidence helpers for comment verdicts.

Kept separate so ``comment_verdict`` stays under the first-party line budget.
Re-exported from ``comment_verdict`` for callers and tests.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from awf.common.commands import CommandResult
from awf.common.logging import get_logger
from awf.node.git_manager import (
    FORCE_FILE_MODE_TRACKING_GIT_CONFIG_ARGS,
    FORCE_FULL_STAT_CHECK_GIT_CONFIG_ARGS,
    FORCE_SYMLINK_TRACKING_GIT_CONFIG_ARGS,
    GitOperationError,
    repair_mirror_hooks_path,
)
from awf.runtime.pr_monitor_runner.constants import (
    _MIRROR_HOOKS_PATH_POISONED_REASON,
)
from awf.runtime.pr_monitor_runner.git_utils import (
    git_pinned_worktree_command,
    git_worktree_command,
)
from awf.runtime.pr_monitor_runner.mirror_hooks import mirror_hooks_repair_failure_details
from awf.runtime.pr_monitor_runner.types import (
    _MonitorMirrorHooksPathRepairFailedError,
)
from awf.runtime.worktree_writer_lock import hold_exclusive_worktree_writer_lock

if TYPE_CHECKING:
    from awf.runtime.pr_monitor import MonitorState
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

_log = get_logger(__name__)


async def _rollback_or_classify_failure(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
    item_start_head: str | None,
    item_start_last_push_sha: str | None = None,
    state: MonitorState | None,
) -> bool:
    """Roll back, classifying expected Git/HEAD I/O failures as failure.

    Reason-coded exceptions propagate unchanged so their codes reach the
    structured log, ``WorkspaceEvent``, ``FailureReason``, and policy paths.
    Untyped ``TimeoutError`` / ``OSError`` / ``RuntimeError`` from HEAD probes
    or Git spawn become ``False`` so callers can raise a typed protocol error.
    """
    try:
        return await _rollback_unaccepted_protocol_retry_changes(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            item_start_head=item_start_head,
            item_start_last_push_sha=item_start_last_push_sha,
            state=state,
        )
    except (TimeoutError, OSError, RuntimeError) as rollback_exc:
        if getattr(rollback_exc, "reason_code", None) is not None:
            raise
        return False


async def _rollback_unaccepted_protocol_retry_changes(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
    item_start_head: str | None,
    item_start_last_push_sha: str | None = None,
    state: MonitorState | None,
) -> bool:
    """Discard first-attempt edits when a corrected verdict is not FIXED.

    When HEAD still equals ``item_start_head``, uncommitted agent edits are
    cleaned via ``cleanup_validation_worktree_side_effects`` so they cannot
    contaminate the next review item in the same cycle.

    Returns ``True`` when rollback succeeded or was unnecessary, and ``False``
    when ``git reset --hard`` or cleanup/verification failed so the caller must
    not accept the verdict.
    """
    if item_start_head is None or not worktree_path.exists():
        return True

    from awf.runtime.pr_monitor_runner.comment_verdict_residue import (
        _RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS,
    )
    from awf.runtime.pr_monitor_runner.comment_verdict_residue_fingerprint import (
        item_start_pinned_git_dir,
        item_start_snapshot_covers_outer_git_dir,
        read_protocol_attempt_start_head,
        restore_item_start_local_git_configs,
    )

    # Prefer remembered item-start configs + timeout so a live include.path →
    # FIFO cannot hang the worker before Git configuration restore
    # (review 5101264783 / PRRT_kwDOSJAM6s6e30Rp family).
    rev_parse_head = getattr(runner, "_rev_parse_head", None)
    if not item_start_snapshot_covers_outer_git_dir(worktree_path) and not callable(rev_parse_head):
        return True

    current_head = await read_protocol_attempt_start_head(
        runner,
        worktree_path=worktree_path,
        rev_parse_head=rev_parse_head if callable(rev_parse_head) else None,
    )
    if not current_head:
        _log.warning(
            "monitor.agent_verdict_protocol_retry_rollback_head_unreadable",
            workspace_id=workspace_id,
            item_start_head=item_start_head,
        )
        return False

    needs_hosted_remote_rollback = False
    published_remote_head: str | None = None
    if state is not None and getattr(runner._deps.adapter, "is_hosted", False):
        saved_last_push_sha = (item_start_last_push_sha or "").strip()
        current_last_push_sha = (state.last_push_sha or "").strip()
        start_head_lower = item_start_head.lower()
        current_head_lower = current_head.lower()
        state_recorded_remote_advance = state.hosted_terminal_head_advanced or (
            current_last_push_sha.lower() != saved_last_push_sha.lower()
        )
        if state_recorded_remote_advance:
            needs_hosted_remote_rollback = True
        elif current_head_lower != start_head_lower:
            # Hosted agents publish terminal commits before AWF syncs and gates
            # them. When gating fails, ``_record_hosted_terminal_head_sync`` has not
            # run, so state still points at the pre-repair head even though the
            # local worktree and remote branch were advanced.
            needs_hosted_remote_rollback = True
        if needs_hosted_remote_rollback:
            if not state_recorded_remote_advance and current_head_lower != start_head_lower:
                candidate = current_head
            else:
                candidate = current_last_push_sha or current_head
            if candidate and candidate.lower() != start_head_lower:
                published_remote_head = candidate
            else:
                needs_hosted_remote_rollback = False

    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _git_env_for_merge_safety_object_lookup,
    )

    merge_safety_git_env = _git_env_for_merge_safety_object_lookup()
    rolled_back_from: str | None = None

    pinned_git_dir = item_start_pinned_git_dir(worktree_path)

    async def _run_git(args: list[str]) -> CommandResult:
        if pinned_git_dir is not None:
            command = git_pinned_worktree_command(pinned_git_dir, worktree_path, *args)
        else:
            command = git_worktree_command(worktree_path, *args)
        return await runner._deps.runner.run(
            command,
            env=merge_safety_git_env,
            timeout_seconds=_RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS,
        )

    from awf.runtime.pr_monitor_runner.remote_repair_unpublished import (
        _live_head_matches_pinned_recovery_head,
    )
    from awf.runtime.validation_worktree import cleanup_validation_worktree_side_effects

    # Keep the live HEAD recheck, destructive reset, and cleanup in one critical
    # section. `run_worktree_git` cannot be used inside this block because it
    # acquires a separate lock per mutating command.
    async with hold_exclusive_worktree_writer_lock(worktree_path):
        # Restore trusted `.git` linkage + local Git config *before* cleanup and
        # before other Git ops that would otherwise follow an agent-retargeted
        # gitfile (PRRT_kwDOSJAM6s6e1Vy1). Config restore also precedes cleanup
        # because cleanup omits ``git clean -x``, so an agent-set
        # ``core.excludesFile`` would hide matching untracked residue; restoring
        # afterward would re-expose those bytes with no further cleanliness
        # check (PRRT_kwDOSJAM6s6e0yQG).
        git_config_restore_ok = restore_item_start_local_git_configs(worktree_path)
        if not git_config_restore_ok:
            _log.warning(
                "monitor.agent_verdict_protocol_retry_rollback_git_config_restore_failed",
                workspace_id=workspace_id,
                item_start_head=item_start_head,
            )

        # Re-read HEAD through restored linkage / pinned git-dir so a swapped
        # gitfile cannot poison the reset decision.
        repinned_head_result = await _run_git(["rev-parse", "HEAD"])
        rollback_head = repinned_head_result.stdout.strip()
        if not repinned_head_result.ok or not rollback_head:
            _log.warning(
                "monitor.agent_verdict_protocol_retry_rollback_head_unreadable",
                workspace_id=workspace_id,
                item_start_head=item_start_head,
            )
            return False
        head_matches_start = rollback_head.lower() == item_start_head.lower()

        head_unchanged, live_head = await _live_head_matches_pinned_recovery_head(
            runner._deps.runner,
            worktree_path=worktree_path,
            pinned_head=rollback_head,
            git_env=merge_safety_git_env,
            git_dir=pinned_git_dir,
        )
        if not head_unchanged:
            _log.warning(
                "monitor.agent_verdict_protocol_retry_rollback_aborted_live_worktree_changed",
                workspace_id=workspace_id,
                item_start_head=item_start_head,
                current_head=rollback_head,
                live_head=live_head,
            )
            return False
        if not head_matches_start:
            reset = await _run_git(
                [
                    *FORCE_FILE_MODE_TRACKING_GIT_CONFIG_ARGS,
                    *FORCE_SYMLINK_TRACKING_GIT_CONFIG_ARGS,
                    *FORCE_FULL_STAT_CHECK_GIT_CONFIG_ARGS,
                    "reset",
                    "--hard",
                    item_start_head,
                ]
            )
            if not reset.ok:
                _log.warning(
                    "monitor.agent_verdict_protocol_retry_rollback_failed",
                    workspace_id=workspace_id,
                    item_start_head=item_start_head,
                    current_head=rollback_head,
                    reset_returncode=reset.returncode,
                    reset_stderr=(reset.stderr or "")[:400],
                )
                return False
            rolled_back_from = rollback_head

        cleanup = await cleanup_validation_worktree_side_effects(
            run_git=_run_git,
            worktree_path=worktree_path,
            restore_ref=item_start_head,
        )
    if not cleanup.ok:
        _log.warning(
            "monitor.agent_verdict_protocol_retry_rollback_cleanup_failed",
            workspace_id=workspace_id,
            item_start_head=item_start_head,
            reason_code=cleanup.reason_code,
            cleanup_stderr=(cleanup.cleanup_stderr or "")[:400],
        )
        return False

    hosted_remote_state_cleared = not needs_hosted_remote_rollback
    if needs_hosted_remote_rollback and published_remote_head is not None:
        hosted_identity_fn = getattr(runner, "_hosted_pr_identity_for_workspace", None)
        if not callable(hosted_identity_fn):
            _log.warning(
                "monitor.hosted_terminal_head_remote_rollback_unavailable",
                workspace_id=workspace_id,
                item_start_head=item_start_head,
                published_remote_head=published_remote_head,
            )
            return False
        hosted_pr_identity = await hosted_identity_fn(workspace_id, state=state)
        from awf.runtime.pr_monitor_runner.agent_service_recovery import (
            _rollback_hosted_terminal_head_on_remote,
        )

        remote_ok = await _rollback_hosted_terminal_head_on_remote(
            runner,
            workspace_id=workspace_id,
            hosted_pr_identity=hosted_pr_identity,
            rollback_target_sha=item_start_head,
            expected_remote_head_sha=published_remote_head,
        )
        if not remote_ok:
            return False
        hosted_remote_state_cleared = True

    restore_local_push_tracking = hosted_remote_state_cleared
    if state is not None and restore_local_push_tracking:
        state.hosted_terminal_head_advanced = False
        current_last_push_sha = (state.last_push_sha or "").strip()
        saved_last_push_sha = (item_start_last_push_sha or "").strip()
        if current_last_push_sha.lower() != saved_last_push_sha.lower():
            state.last_push_sha = item_start_last_push_sha

    if rolled_back_from is not None or not cleanup.check.clean:
        _log.info(
            "monitor.agent_verdict_protocol_retry_rollback",
            workspace_id=workspace_id,
            item_start_head=item_start_head,
            rolled_back_from=rolled_back_from,
            verdict_outcome="non_fix",
            cleaned_paths=list(cleanup.cleaned_paths),
        )
    return git_config_restore_ok


async def _repair_mirror_hooks_or_raise(
    *,
    workspace_id: str,
    mirror_path: Path,
    stage: str,
) -> None:
    try:
        await repair_mirror_hooks_path(mirror_path)
    except (GitOperationError, OSError) as exc:
        details = mirror_hooks_repair_failure_details(
            exc,
            repair_stage=stage,
            mirror_path=mirror_path,
        )
        _log.warning(
            "monitor.mirror_hooks_path_repair_failed",
            workspace_id=workspace_id,
            reason_code=_MIRROR_HOOKS_PATH_POISONED_REASON,
            **details,
        )
        raise _MonitorMirrorHooksPathRepairFailedError() from exc


async def _item_fix_evidence(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
    item_start_head: str | None,
    item_path: str | None,
    item_line: int | None,
    state: MonitorState | None,
    dirty_changes_committed: bool,
) -> bool:
    """Verify a contentful forward item-scoped change from the logical start."""
    if item_start_head is None:
        return dirty_changes_committed and not worktree_path.exists()

    candidate_heads: list[str] = []
    if worktree_path.exists():
        end_head = await runner._rev_parse_head(worktree_path)
        if end_head:
            candidate_heads.append(end_head)
    if state is not None and state.hosted_terminal_head_advanced:
        hosted_head = (state.last_push_sha or "").strip()
        if hosted_head and hosted_head not in candidate_heads:
            candidate_heads.append(hosted_head)

    descends = getattr(runner, "_head_descends_from", None)
    trees_differ = getattr(runner, "_commit_trees_differ", None)
    touches_path = getattr(runner, "_commit_range_touches_path", None)
    if not (callable(descends) and callable(trees_differ) and worktree_path.exists()):
        # Lightweight/mocked runners may not expose Git ancestry helpers. A
        # successful dirty-worktree sink is still scoped to this invocation;
        # the production runner always takes the stronger ancestry/scope branch.
        return dirty_changes_committed and not worktree_path.exists()

    for candidate in candidate_heads:
        if candidate.lower() == item_start_head.lower():
            continue
        if not await descends(
            worktree_path=worktree_path,
            ancestor=item_start_head,
            descendant=candidate,
        ):
            continue
        if not await trees_differ(
            worktree_path=worktree_path,
            left=item_start_head,
            right=candidate,
        ):
            continue
        if item_path is not None and (
            not callable(touches_path)
            or not await touches_path(
                worktree_path=worktree_path,
                left=item_start_head,
                right=candidate,
                path=item_path,
                line=item_line,
            )
        ):
            continue
        return True
    return False
