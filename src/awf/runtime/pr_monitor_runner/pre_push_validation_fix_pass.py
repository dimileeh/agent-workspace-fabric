"""Pre-push validation fix-pass helpers split from ``pre_push_validation``.

Holds the fix-pass orchestration (agent run, commit, reparent, rollback and
cleanup) so the parent module stays under the first-party line limit. The
parent module re-exports these symbols to preserve its existing public surface.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from awf.adapters.base import AgentRunError
from awf.common.command_evidence import append_command_evidence
from awf.common.compose_exec import ComposeExecCleanupError
from awf.common.git_identity import git_identity_config_args
from awf.common.logging import get_logger
from awf.common.task_tag import commit_message_with_task_tag
from awf.control.protected_file_diffs import protected_file_diffs_for_committed_paths
from awf.control.quality_gates import (
    QualityGateViolation,
    find_protected_quality_gate_changes,
)
from awf.control.validation_fix_cycle import (
    ValidationFixContext,
    build_fix_prompt,
    read_output_tail,
)
from awf.db.repositories import WorkspaceRepository
from awf.node.git_manager import (
    GitOperationError,
    git_env_without_object_lookup_overrides,
    mirror_path_for_worktree,
    repair_mirror_hooks_path,
    verify_head_object_exists,
)
from awf.runtime.ownership import (
    MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    repair_agent_runtime_ownership,
)
from awf.runtime.pr_monitor_runner.constants import (
    _HEAD_OBJECT_MISSING_RECOVERED_REASON,
    _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
    _MIRROR_HOOKS_PATH_POISONED_REASON,
    _PROTECTED_SCOPE_REPAIR_FAILED_REASON,
)
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.mirror_hooks import mirror_hooks_repair_failure_details
from awf.runtime.pr_monitor_runner.path_parsing import _changed_paths_from_name_status_z
from awf.runtime.pr_monitor_runner.pre_push_validation_constants import (
    _PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON,
    _PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON,
    _PRE_PUSH_VALIDATION_ROLLBACK_FAILED_REASON,
)
from awf.runtime.pr_monitor_runner.remote_repair import (
    _mirror_commit_object_exists,
    _open_merge_candidate_head_sha,
    _recover_missing_head_object_from_filesystem,
)
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    ProviderRecoveryAuthError,
    ProviderRecoveryFallbackError,
    ProviderRecoveryRetryError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorHeadObjectMissingError,
    _MonitorMirrorHooksPathRepairFailedError,
    _MonitorPolicyBlockedError,
)
from awf.runtime.validation_worktree_constants import VALIDATION_WORKTREE_CLEANUP_FAILED

if TYPE_CHECKING:
    from awf.runtime.pr_monitor_runner.pre_push_validation import _PrePushValidationResult

_log = get_logger(__name__)

PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON = _PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON
PRE_PUSH_VALIDATION_ROLLBACK_FAILED_REASON = _PRE_PUSH_VALIDATION_ROLLBACK_FAILED_REASON


async def _protected_scope_violations_for_recovered_commit(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    base_ref: str,
    changed_paths: Sequence[str],
) -> list[QualityGateViolation]:
    async with self._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        if workspace is None:
            raise ProtectedScopeDiffError(
                f"Workspace row {workspace_id} disappeared; cannot load owned_paths "
                "for recovered protected-scope validation."
            )
        owned_paths = list(workspace.owned_paths)
    try:
        protected_file_diffs = await protected_file_diffs_for_committed_paths(
            self._deps.runner,
            worktree_path=worktree_path,
            base_ref=base_ref,
            changed_paths=changed_paths,
            owned_paths=owned_paths,
        )
    except RuntimeError as exc:
        raise ProtectedScopeDiffError(
            "Could not read recovered committed protected-scope file contents "
            "for validation before push: "
            f"{exc}"
        ) from exc
    return find_protected_quality_gate_changes(
        changed_paths=tuple(changed_paths),
        owned_paths=owned_paths,
        protected_file_diffs=protected_file_diffs,
    )


async def _head_descends_from(
    self: Any,
    *,
    worktree_path: Path,
    ancestor: str,
    descendant: str,
) -> bool:
    """Return True when ``descendant`` is a descendant of ``ancestor``.

    Uses ``git merge-base --is-ancestor`` which exits 0 when the first ref is an
    ancestor of the second and non-zero otherwise. Callers only invoke this with
    distinct SHAs, so a 0 exit means the fix-pass agent advanced HEAD on top of
    the pre-fix commit rather than moving it sideways or backward.
    """
    result = await self._deps.runner.run(
        git_worktree_command(
            worktree_path,
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
        )
    )
    return bool(result.ok)


async def _reparent_fix_pass_commit(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    fix_start_head: str,
    current_head: str,
    pass_number: int,
    task_tag: str | None,
) -> tuple[str | None, bool, str | None]:
    """Re-parent the fix agent's resulting tree onto ``fix_start_head`` (issue #411).

    A fix-pass agent that rewrites the tip (e.g. ``git commit --amend`` or
    ``git reset --hard HEAD~1`` + recommit) leaves HEAD as a non-descendant of
    ``fix_start_head``. A legitimate content-modifying amend and a work-dropping
    reset+recommit can produce byte-identical trees, so they are topologically and
    tree-content indistinguishable. Rather than discriminate (and risk orphaning a
    valid fix, the #406-class over-rollback), we ALWAYS reconstruct whatever the
    agent produced as a single commit whose parent is ``fix_start_head``:

    - ``fix_start_head`` is the new commit's parent, so it can never be orphaned and
      is rescued from danglinghood after an amend.
    - The agent's resulting tree is always kept, so the fix pass converges.
    - Any dropped work is visible as ``git diff fix_start_head..HEAD`` and is
      re-validated before push by the surrounding fix-pass loop.

    Returns ``(new_head, no_net_change, failure_reason)``:
      - ``(new_sha, False, None)``      -> reconstructed commit; accept against new_sha.
      - ``(None, True, None)``          -> agent produced no net change; signal no-commit.
      - ``(None, False, REPARENT)``     -> infra failure (rev-parse/commit-tree/reset).
    """

    def _warn(git_step: str, result: Any) -> None:
        """Warn on a re-parent git step failure without logging secrets.

        Logs both ``ok`` and a truncated ``stdout`` alongside ``stderr`` so the
        "exit 0 but blank/whitespace-only output" path (where ``stderr`` is
        typically empty) is distinguishable from a genuine non-zero exit.
        """
        _log.warning(
            "monitor.pre_push_validation_fix_reparent_failed",
            workspace_id=workspace_id,
            pass_number=pass_number,
            git_step=git_step,
            ok=result.ok,
            stdout=(result.stdout or "")[:400],
            stderr=(result.stderr or "")[:400],
        )

    git_env = git_env_without_object_lookup_overrides()
    current_tree_result = await self._deps.runner.run(
        git_worktree_command(worktree_path, "rev-parse", f"{current_head}^{{tree}}"),
        env=git_env,
    )
    current_tree = current_tree_result.stdout.strip() if current_tree_result.ok else ""
    if not current_tree:
        _warn("rev-parse current tree", current_tree_result)
        return None, False, _PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON

    start_tree_result = await self._deps.runner.run(
        git_worktree_command(worktree_path, "rev-parse", f"{fix_start_head}^{{tree}}"),
        env=git_env,
    )
    start_tree = start_tree_result.stdout.strip() if start_tree_result.ok else ""
    if not start_tree:
        _warn("rev-parse fix_start_head tree", start_tree_result)
        return None, False, _PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON

    if current_tree == start_tree:
        # The agent produced no net change relative to the pre-fix-pass commit. Emit a
        # dedicated audit event so this case is distinguishable in logs from a genuine
        # no-commit rollback, then signal no-commit so the caller falls through to the
        # existing rollback.
        _log.info(
            "monitor.pre_push_validation_fix_reparent_no_net_change",
            workspace_id=workspace_id,
            pass_number=pass_number,
            current_head=current_head,
            fix_start_head=fix_start_head,
        )
        return None, True, None

    message_result = await self._deps.runner.run(
        git_worktree_command(worktree_path, "log", "-1", "--format=%B", current_head),
        env=git_env,
    )
    message = message_result.stdout.strip() if message_result.ok else ""
    if not message:
        message = f"awf: pre-push validation fix for {workspace_id}"

    # Prepend the workspace's Jira issue key (if any) so the reparented fix commit
    # links to the issue, matching ``_commit_dirty_worktree`` in this same flow.
    # ``task_tag`` is resolved once by the caller (``_run_pre_push_validation_fix_pass``)
    # for the fix prompt and threaded in here, avoiding a second DB round-trip.
    # Idempotent: a ``%B`` body that already carries the tag is left unchanged.
    # Unlike the single-line dirty-worktree subject, the reparented message reuses
    # the agent's full ``%B`` body, so it is NOT truncated to [:72] (that would drop
    # the commit body); the tag only prefixes the subject line.
    message = commit_message_with_task_tag(message, task_tag)

    commit_result = await self._deps.runner.run(
        git_worktree_command(
            worktree_path,
            *git_identity_config_args(),
            "commit-tree",
            current_tree,
            "-p",
            fix_start_head,
            "-m",
            message,
        ),
        env=git_env,
    )
    new_sha = commit_result.stdout.strip() if commit_result.ok else ""
    if not new_sha:
        _warn("commit-tree", commit_result)
        return None, False, _PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON

    reset_result = await self._deps.runner.run(
        git_worktree_command(worktree_path, "reset", "--hard", new_sha),
        env=git_env,
    )
    if not reset_result.ok:
        _warn("reset --hard reparented commit", reset_result)
        return None, False, _PRE_PUSH_VALIDATION_REPARENT_FAILED_REASON

    return new_sha, False, None


async def _cleanup_committed_pre_push_validation_fix_pass(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    committed_head: str,
    pass_number: int,
) -> str | None:
    """Clean validation side effects against a committed fix head.

    Used for both the dirty-worktree commit produced by ``_commit_dirty_worktree``
    and the agent self-commit detected when HEAD advanced but the worktree is
    clean. Returns a failure reason code when cleanup fails, otherwise ``None``.
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation import (
        _pre_push_validation_cleanup,
    )

    cleanup = await _pre_push_validation_cleanup(
        self,
        worktree_path=worktree_path,
        restore_ref=committed_head,
    )
    ok = bool(cleanup.ok)
    log = _log.info if ok else _log.warning
    log(
        "monitor.pre_push_validation_fix_commit_cleanup",
        workspace_id=workspace_id,
        pass_number=pass_number,
        restore_ref=committed_head,
        reason_code=None if ok else cleanup.reason_code,
        cleanup_stderr=(cleanup.cleanup_stderr or "")[:400],
    )
    if not ok:
        return cleanup.reason_code or VALIDATION_WORKTREE_CLEANUP_FAILED
    return None


async def _rollback_failed_pre_push_validation_fix_pass(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    restore_ref: str | None,
    pass_number: int,
    reason: str,
) -> str | None:
    """Roll back fix-pass edits and return a failure reason when recovery fails.

    ``restore_ref`` is the HEAD captured AFTER ``_commit_dirty_worktree``
    raised (the post-raise HEAD, captured INSIDE each commit-sink ``except``
    clause in ``_run_pre_push_validation_fix_pass``), NOT a pre-sink
    ``post_agent_head`` snapshot and NOT ``fix_start_head``. The protected-scope
    repair agent runs INSIDE ``_commit_dirty_worktree`` (via
    ``_repair_protected_scope_changes_before_commit``) and may self-commit,
    advancing HEAD past ``fix_start_head`` and past any pre-sink snapshot
    BEFORE the provider-recovery / policy-blocked / generic exception is
    raised; ``_commit_dirty_worktree`` raises before its own ``git commit``, so
    HEAD has not moved since the in-sink self-commit. A pre-sink snapshot would
    be stale against that in-sink self-commit: resetting to it would discard
    the valid protected-scope repair self-commit (and the agent's own
    validation fix) along with the stranded residue, so the retry would start
    from the old tree and lose or repeatedly redo valid repair work (review
    threads ``PRRT_kwDOSJAM6s6Klf78`` and ``PRRT_kwDOSJAM6s6KpAD6``). Capturing
    the anchor AFTER the sink raised preserves both the agent's
    already-committed validation fix and any in-sink protected-scope repair
    self-commit while discarding only the stranded residue. This mirrors the
    CI-repair rollback (``_rollback_ci_fix_residue_before_provider_recovery``,
    thread ``PRRT_kwDOSJAM6s6Klf74`` / ``PRRT_kwDOSJAM6s6KpAD6``) and the
    finalize rollback (``_rollback_finalize_dirty_residue_before_provider_recovery``,
    thread ``PRRT_kwDOSJAM6s6KnWkn``), which both anchor against the
    post-raise HEAD for the same reason. ``None`` means the post-raise HEAD
    could not be resolved; the rollback is skipped (the residue strands, but a
    missing anchor makes a safe ``git reset --hard`` impossible — better to
    strand visibly than restore against the wrong ref, mirroring the CI-repair
    and finalize rollbacks' ``restore_ref is None`` guards).
    """
    if restore_ref is None:
        _log.warning(
            "monitor.pre_push_validation_fix_rollback_skipped_no_anchor",
            workspace_id=workspace_id,
            pass_number=pass_number,
            reason=reason,
        )
        return None
    from awf.runtime.pr_monitor_runner.pre_push_validation import (
        _pre_push_validation_cleanup,
    )

    reset = await self._deps.runner.run(
        git_worktree_command(worktree_path, "reset", "--hard", restore_ref)
    )
    if not reset.ok:
        log = _log.warning
        log(
            "monitor.pre_push_validation_fix_rollback",
            workspace_id=workspace_id,
            pass_number=pass_number,
            reason=reason,
            restore_ref=restore_ref,
            reset_returncode=reset.returncode,
            clean_returncode=None,
            reset_stderr=(reset.stderr or "")[:400],
            clean_stderr=None,
        )
        return PRE_PUSH_VALIDATION_ROLLBACK_FAILED_REASON

    cleanup = await _pre_push_validation_cleanup(
        self,
        worktree_path=worktree_path,
        restore_ref=restore_ref,
    )
    ok = bool(cleanup.ok)
    log = _log.info if ok else _log.warning
    log(
        "monitor.pre_push_validation_fix_rollback",
        workspace_id=workspace_id,
        pass_number=pass_number,
        reason=reason,
        restore_ref=restore_ref,
        reset_returncode=reset.returncode,
        clean_returncode=0 if ok else None,
        clean_reason_code=None if ok else cleanup.reason_code,
        reset_stderr=(reset.stderr or "")[:400],
        clean_stderr=(cleanup.cleanup_stderr or "")[:400],
    )
    if ok:
        return None
    return cleanup.reason_code or VALIDATION_WORKTREE_CLEANUP_FAILED


async def _repair_pre_push_validation_fix_mirror_hooks(
    *,
    workspace_id: str,
    pass_number: int,
    mirror_path: Path | None,
) -> str | None:
    if mirror_path is None:
        return None
    try:
        await repair_mirror_hooks_path(mirror_path)
    except (GitOperationError, OSError) as exc:
        repair_details = mirror_hooks_repair_failure_details(
            exc,
            repair_stage="pre_push_validation_fix_pass",
            mirror_path=mirror_path,
            extra={"pass_number": pass_number},
        )
        _log.warning(
            "monitor.mirror_hooks_path_repair_failed",
            workspace_id=workspace_id,
            reason_code=_MIRROR_HOOKS_PATH_POISONED_REASON,
            **repair_details,
        )
        return _MIRROR_HOOKS_PATH_POISONED_REASON
    return None


async def _run_pre_push_validation_fix_pass(
    self: Any,
    *,
    workspace_id: str,
    compose_project: str,
    compose_file: Path,
    remote_branch: str,
    remote_url: str | None,
    state: object | None,
    validation_result: _PrePushValidationResult,
    pass_number: int,
    total_passes: int,
    validation_commands: tuple[str, ...],
) -> tuple[bool, str | None]:
    """Attempt a validation fix pass and return commit status plus terminal failure reason."""
    # Resolve sibling helpers through the parent module namespace so test
    # monkeypatches on ``pre_push_validation._<helper>`` intercept these calls.
    # The parent module re-exports these names from this sub-module; the late
    # import reads the (possibly patched) attribute at call time.
    from awf.runtime.pr_monitor_runner import pre_push_validation as _ppv

    _cleanup_committed_fix_pass = _ppv._cleanup_committed_pre_push_validation_fix_pass  # type: ignore[attr-defined]
    _head_descends_from_impl = _ppv._head_descends_from  # type: ignore[attr-defined]
    _reparent_fix_pass_commit_impl = _ppv._reparent_fix_pass_commit  # type: ignore[attr-defined]
    _rollback_failed_fix_pass = _ppv._rollback_failed_pre_push_validation_fix_pass  # type: ignore[attr-defined]

    first_fail = validation_result.first_failure
    if first_fail is None:
        return False, None
    worktree_path = self._worktrees_root / workspace_id
    fix_start_head = await self._rev_parse_head(worktree_path)
    if fix_start_head is None:
        _log.warning(
            "monitor.pre_push_validation_fix_start_head_unavailable",
            workspace_id=workspace_id,
            pass_number=pass_number,
        )
        return False, None
    # Missing-HEAD recovery may discover that the original start head is unusable.
    # Post-recovery checks must anchor to the commit used to build the recovered head.
    fix_pass_baseline_head = fix_start_head
    # Resolve the workspace's optional Jira issue key once; reused for the fix prompt
    # and threaded into any ``_reparent_fix_pass_commit`` call below so the reparent
    # path does not re-query the DB for the same value.
    task_tag = await self._resolve_task_tag(workspace_id)
    context = ValidationFixContext(
        failed_command=first_fail.command,
        returncode=first_fail.returncode,
        stdout_tail=read_output_tail(first_fail.stdout_path),
        stderr_tail=read_output_tail(first_fail.stderr_path),
        pass_number=pass_number,
        total_passes=total_passes,
        test_commands=validation_commands,
        reason_code=(
            validation_result.validation_reason_code
            if validation_result.validation_reason_code is not None
            else validation_result.reason_code
        ),
        coverage_percent=(
            validation_result.coverage.percent if validation_result.coverage is not None else None
        ),
        coverage_minimum_percent=(
            validation_result.coverage.minimum_percent
            if validation_result.coverage is not None
            else None
        ),
        failing_test_node_ids=(
            tuple(validation_result.coverage.failing_test_node_ids)
            if validation_result.coverage is not None
            else ()
        ),
        failing_test_evidence=(
            tuple(validation_result.coverage.failing_test_evidence)
            if validation_result.coverage is not None
            else ()
        ),
        task_tag=task_tag,
    )
    command_evidence: list[str] = []
    if not await repair_agent_runtime_ownership(
        logger=_log,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        reason="monitor_agent_pre_launch",
        event_name=MONITOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME,
    ):
        return False, "AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED"
    mirror_path = mirror_path_for_worktree(worktree_path)
    mirror_repair_failure_reason = await _repair_pre_push_validation_fix_mirror_hooks(
        workspace_id=workspace_id,
        pass_number=pass_number,
        mirror_path=mirror_path,
    )
    if mirror_repair_failure_reason is not None:
        return False, mirror_repair_failure_reason
    try:
        await self._run_monitor_agent_with_service_recovery(
            workspace_id=workspace_id,
            compose_project=compose_project,
            compose_file=compose_file,
            prompt=build_fix_prompt(context),
            log_source="monitor-pre-push-validation-fix",
            command_evidence=command_evidence,
            operation_start_head=fix_start_head,
        )
    except AgentRunError as exc:
        append_command_evidence(
            command_evidence,
            stdout=exc.result.stdout,
            stderr=exc.result.stderr,
        )
        _log.warning(
            "monitor.pre_push_validation_fix_agent_nonzero",
            workspace_id=workspace_id,
            pass_number=pass_number,
            reason_code=exc.reason_code,
        )
    except _MonitorAgentServiceRecoveryFailedError:
        raise
    except ComposeExecCleanupError as exc:
        _log.warning(
            "monitor.pre_push_validation_fix_cleanup_failed",
            workspace_id=workspace_id,
            pass_number=pass_number,
            reason_code=exc.reason_code,
        )
        rollback_failure_reason = await _rollback_failed_fix_pass(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            restore_ref=fix_start_head,
            pass_number=pass_number,
            reason="compose_cleanup_failed",
        )
        mirror_repair_failure_reason = await _repair_pre_push_validation_fix_mirror_hooks(
            workspace_id=workspace_id,
            pass_number=pass_number,
            mirror_path=mirror_path,
        )
        if mirror_repair_failure_reason is not None:
            return False, mirror_repair_failure_reason
        if rollback_failure_reason is not None:
            return False, rollback_failure_reason
        return False, None
    except Exception as exc:
        _log.warning(
            "monitor.pre_push_validation_fix_failed",
            workspace_id=workspace_id,
            pass_number=pass_number,
            error=repr(exc),
        )
        rollback_failure_reason = await _rollback_failed_fix_pass(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            restore_ref=fix_start_head,
            pass_number=pass_number,
            reason="agent_exception",
        )
        mirror_repair_failure_reason = await _repair_pre_push_validation_fix_mirror_hooks(
            workspace_id=workspace_id,
            pass_number=pass_number,
            mirror_path=mirror_path,
        )
        if mirror_repair_failure_reason is not None:
            return False, mirror_repair_failure_reason
        if rollback_failure_reason is not None:
            return False, rollback_failure_reason
        return False, None

    mirror_repair_failure_reason = await _repair_pre_push_validation_fix_mirror_hooks(
        workspace_id=workspace_id,
        pass_number=pass_number,
        mirror_path=mirror_path,
    )
    if mirror_repair_failure_reason is not None:
        return False, mirror_repair_failure_reason

    head_object_exists = await verify_head_object_exists(worktree_path)
    recovered_head_for_protected_scope: str | None = None
    recovered_base_for_protected_scope: str | None = None
    if not head_object_exists:
        _log.warning(
            "monitor.pre_push_validation_fix_head_object_missing",
            workspace_id=workspace_id,
            pass_number=pass_number,
            reason_code=_HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
        )
        recovery_head = fix_start_head
        if mirror_path is not None:
            recovery_head_exists = await _mirror_commit_object_exists(
                self, mirror_path, recovery_head
            )
            if not recovery_head_exists:
                _log.warning(
                    "monitor.pre_push_validation_fix_head_object_missing_recovery_anchor_missing",
                    workspace_id=workspace_id,
                    pass_number=pass_number,
                    operation_start_head=recovery_head[:10],
                )
                recovery_head = await _open_merge_candidate_head_sha(self, workspace_id)
        if recovery_head is None:
            return False, _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON
        recovered = await _recover_missing_head_object_from_filesystem(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            operation_start_head=recovery_head,
            task_tag=task_tag,
            command_evidence=command_evidence,
        )
        if recovered is None:
            return False, _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON
        fix_pass_baseline_head = recovery_head
        _log.info(
            "monitor.pre_push_validation_fix_head_object_missing_recovered",
            workspace_id=workspace_id,
            pass_number=pass_number,
            recovered_head=recovered[:10],
            reason_code=_HEAD_OBJECT_MISSING_RECOVERED_REASON,
        )
        if recovered != recovery_head:
            recovered_base_for_protected_scope = recovery_head
            recovered_head_for_protected_scope = recovered

    # The rollback anchor for the commit-sink ``except`` clauses below is
    # captured INSIDE each clause (after ``_commit_dirty_worktree`` raised),
    # NOT here before the sink. The protected-scope repair agent runs INSIDE
    # ``_commit_dirty_worktree`` (via
    # ``_repair_protected_scope_changes_before_commit``) and may self-commit,
    # advancing HEAD past any pre-sink snapshot BEFORE the provider-recovery
    # / policy-blocked / generic exception is raised; a pre-sink anchor would
    # be stale against that in-sink self-commit and ``git reset --hard`` would
    # discard it, forcing the retry to start from the old tree and lose or
    # repeatedly redo the repair work. Mirrors the CI-repair rollback
    # (``_rollback_ci_fix_residue_before_provider_recovery``, thread
    # ``PRRT_kwDOSJAM6s6Klf74`` / ``PRRT_kwDOSJAM6s6KpAD6``) and the
    # dirty-finalize rollback
    # (``_rollback_finalize_dirty_residue_before_provider_recovery``, thread
    # ``PRRT_kwDOSJAM6s6KnWkn``), which both capture the post-raise HEAD
    # inside each provider-recovery ``except`` clause for the same reason.

    try:
        if recovered_head_for_protected_scope is not None:
            assert recovered_base_for_protected_scope is not None
            recovered_delta = await self._deps.runner.run(
                git_worktree_command(
                    worktree_path,
                    "diff",
                    "--name-status",
                    "-z",
                    (f"{recovered_base_for_protected_scope}..{recovered_head_for_protected_scope}"),
                ),
                env=git_env_without_object_lookup_overrides(),
            )
            if not recovered_delta.ok:
                _log.warning(
                    "monitor.pre_push_validation_fix_recovered_delta_failed",
                    workspace_id=workspace_id,
                    pass_number=pass_number,
                    returncode=recovered_delta.returncode,
                    stderr=recovered_delta.stderr[:400],
                    reason_code=_HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
                )
                rollback_failure_reason = await _rollback_failed_fix_pass(
                    self,
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    restore_ref=fix_pass_baseline_head,
                    pass_number=pass_number,
                    reason="recovered_delta_failed",
                )
                if rollback_failure_reason is not None:
                    return False, rollback_failure_reason
                return False, _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON
            recovered_paths = _changed_paths_from_name_status_z(recovered_delta.stdout or "")
            if recovered_paths:
                try:
                    violations = await _protected_scope_violations_for_recovered_commit(
                        self,
                        workspace_id=workspace_id,
                        worktree_path=worktree_path,
                        base_ref=recovered_base_for_protected_scope,
                        changed_paths=recovered_paths,
                    )
                except ProtectedScopeDiffError:
                    rollback_failure_reason = await _rollback_failed_fix_pass(
                        self,
                        workspace_id=workspace_id,
                        worktree_path=worktree_path,
                        restore_ref=fix_pass_baseline_head,
                        pass_number=pass_number,
                        reason="recovered_protected_scope_diff_unavailable",
                    )
                    if rollback_failure_reason is not None:
                        return False, rollback_failure_reason
                    raise
                if violations:
                    _log.warning(
                        "monitor.pre_push_validation_fix_recovered_protected_scope_blocked",
                        workspace_id=workspace_id,
                        pass_number=pass_number,
                        paths=[violation.path for violation in violations],
                    )
                    rollback_failure_reason = await _rollback_failed_fix_pass(
                        self,
                        workspace_id=workspace_id,
                        worktree_path=worktree_path,
                        restore_ref=fix_pass_baseline_head,
                        pass_number=pass_number,
                        reason="recovered_protected_scope_repair_failed",
                    )
                    if rollback_failure_reason is not None:
                        return False, rollback_failure_reason
                    return False, _PROTECTED_SCOPE_REPAIR_FAILED_REASON
        committed = bool(
            await self._commit_dirty_worktree(
                workspace_id=workspace_id,
                message=f"awf: pre-push validation fix for {workspace_id}",
                compose_project=compose_project,
                compose_file=compose_file,
                state=state,
                command_evidence=command_evidence,
                protected_scope_revert_remote_branch=remote_branch,
                remote_push_url=remote_url,
                task_tag=task_tag,
                operation_start_head=fix_pass_baseline_head,
            )
        )
    except (
        ProviderRecoveryAuthError,
        ProviderRecoveryFallbackError,
        ProviderRecoveryRetryError,
    ):
        # ``_commit_dirty_worktree`` -> ``_repair_protected_scope_changes_before_commit``
        # raises these provider-recovery control-flow exceptions when a provider
        # outage suppresses the CLI or a recoverable agent-run error triggers
        # retry/fallback/auth. They must propagate so the monitor loop's dedicated
        # handlers surface ``PROVIDER_OUTAGE`` / ``PROVIDER_FALLBACK`` / auth-failed
        # semantics — BUT only AFTER rolling back the fix-pass residue the agent
        # left behind. Without this rollback the protected-scope edits remain
        # dirty and the next monitor attempt trips ``_pre_existing_dirty_repair_worktree_result``
        # as ``PRE_EXISTING_DIRTY_WORKTREE``, masking the provider outage and
        # wedging the PR (review thread ``PRRT_kwDOSJAM6s6Kc_Ak``). The rollback
        # anchors against the HEAD captured HERE (after ``_commit_dirty_worktree``
        # raised), NOT ``fix_start_head`` and NOT a stale pre-sink snapshot:
        # the protected-scope repair agent runs INSIDE the commit sink (via
        # ``_repair_protected_scope_changes_before_commit``) and may self-commit,
        # advancing HEAD past ``fix_start_head`` and past any pre-sink HEAD
        # BEFORE the provider-recovery exception is raised; ``_commit_dirty_worktree``
        # raises before its own ``git commit``, so HEAD has not moved since the
        # in-sink self-commit. Anchoring against ``fix_start_head`` would discard
        # the agent's already-committed validation fix, and anchoring against a
        # pre-sink ``post_agent_head`` would discard the in-sink protected-scope
        # repair self-commit (review threads ``PRRT_kwDOSJAM6s6Klf78`` and
        # ``PRRT_kwDOSJAM6s6KpAD6``). Capturing the anchor AFTER the sink raised
        # preserves both while discarding only the stranded residue, mirroring
        # the CI-repair rollback (``_rollback_ci_fix_residue_before_provider_recovery``,
        # thread ``PRRT_kwDOSJAM6s6Klf74`` / ``PRRT_kwDOSJAM6s6KpAD6``) and the
        # dirty-finalize rollback
        # (``_rollback_finalize_dirty_residue_before_provider_recovery``, thread
        # ``PRRT_kwDOSJAM6s6KnWkn``). ``None`` means HEAD could not be resolved;
        # the rollback helper then skips the reset (a missing anchor makes a
        # safe ``git reset --hard`` impossible — better to strand visibly than
        # restore against the wrong ref, mirroring the CI-repair and finalize
        # rollbacks' ``restore_ref is None`` guards). A rollback failure is
        # logged but never clobbers the recovery exception: the loop's recovery
        # handlers still run, and a stranded residue surfaces as the next
        # attempt's pre-existing-dirty guard rather than being silently
        # swallowed here.
        post_agent_head = await self._rev_parse_head(worktree_path)
        rollback_failure_reason = await _rollback_failed_fix_pass(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            restore_ref=post_agent_head,
            pass_number=pass_number,
            reason="provider_recovery_dirty_residue",
        )
        if rollback_failure_reason is not None:
            _log.warning(
                "monitor.pre_push_validation_fix_pass_provider_recovery_rollback_failed",
                workspace_id=workspace_id,
                pass_number=pass_number,
                rollback_failure_reason=rollback_failure_reason,
            )
        raise
    except _MonitorPolicyBlockedError:
        # ``_commit_dirty_worktree`` raises this when monitor-authored changes
        # violate blocking supply-chain policy. The policy check runs BEFORE the
        # actual ``git commit`` (``_refresh_supply_chain_policy_before_push``),
        # so the agent's fix-pass residue is still dirty in the worktree. The
        # parent converts this to a ``MONITOR_POLICY_BLOCKED`` push failure,
        # which is intentionally NON-terminal: the monitor loop increments and
        # retries (matching the other commit-sink callers in ``fix_cycle.py`` /
        # ``ci_ops.py``). Re-raising without rolling back strands the residue,
        # and the next cycle's repair-start dirty guard
        # (``_pre_existing_dirty_repair_worktree_result``) trips as
        # ``PRE_EXISTING_DIRTY_WORKTREE``, losing the policy reason and wedging
        # recovery instead of re-polling cleanly. Roll back to the HEAD
        # captured HERE (after ``_commit_dirty_worktree`` raised) before
        # re-raising so the next attempt starts clean, mirroring the
        # provider-recovery residue rollback above (review thread
        # ``PRRT_kwDOSJAM6s6Kg7Dm``). The anchor is the post-raise HEAD, NOT
        # ``fix_start_head`` and NOT a stale pre-sink ``post_agent_head``, for
        # the same reason as the provider-recovery handler: the protected-scope
        # repair agent runs INSIDE the commit sink and may self-commit,
        # advancing HEAD past ``fix_start_head`` and past any pre-sink snapshot
        # BEFORE the policy exception is raised; anchoring against either stale
        # ref would discard valid repair progress via ``git reset --hard``
        # (review threads ``PRRT_kwDOSJAM6s6Klf78`` and ``PRRT_kwDOSJAM6s6KpAD6``).
        # Mirrors the CI-repair and dirty-finalize rollbacks, which capture the
        # post-raise HEAD inside each commit-sink ``except`` clause for the same
        # reason (threads ``PRRT_kwDOSJAM6s6Klf74`` / ``PRRT_kwDOSJAM6s6KpAD6``
        # / ``PRRT_kwDOSJAM6s6KnWkn``). ``None`` means HEAD could not be
        # resolved; the rollback helper skips the reset. A rollback failure is
        # logged but never clobbers the policy exception.
        post_agent_head = await self._rev_parse_head(worktree_path)
        rollback_failure_reason = await _rollback_failed_fix_pass(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            restore_ref=post_agent_head,
            pass_number=pass_number,
            reason="policy_blocked_dirty_residue",
        )
        if rollback_failure_reason is not None:
            _log.warning(
                "monitor.pre_push_validation_fix_pass_policy_blocked_rollback_failed",
                workspace_id=workspace_id,
                pass_number=pass_number,
                rollback_failure_reason=rollback_failure_reason,
            )
        raise
    except (
        ProtectedScopeDiffError,
        _MonitorAgentRuntimeOwnershipRepairFailedError,
        _MonitorHeadObjectMissingError,
        _MonitorMirrorHooksPathRepairFailedError,
    ):
        # ``_commit_dirty_worktree`` (and the protected-scope paths it invokes)
        # raises these deterministic reason-coded exceptions so the monitor loop's
        # dedicated handlers surface the right reason code. The broad
        # ``except Exception`` below would collapse them into a generic
        # ``commit_exception`` rollback reason, hiding the structured failure
        # semantics — re-raise before that handler. Mirrors the explicit-clause
        # pattern every other ``_commit_dirty_worktree`` caller already uses
        # (``fix_cycle.py``, ``remote_ops.py``, ``ci_ops.py``,
        # ``operator_hints.py``, ``_try_finalize_pre_push_dirty_repair_state``).
        # These two map to TERMINAL reason codes (``PROTECTED_SCOPE_DIFF_UNAVAILABLE``
        # / ``AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED``), so the monitor loop
        # terminates instead of retrying — no residue rollback is needed (the
        # stranded dirt surfaces only on a later operator resume, matching every
        # other commit-sink caller's treatment of these two).
        raise
    except Exception as exc:
        _log.warning(
            "monitor.pre_push_validation_fix_commit_failed",
            workspace_id=workspace_id,
            pass_number=pass_number,
            error=repr(exc),
        )
        # Anchor against the HEAD captured HERE (after ``_commit_dirty_worktree``
        # raised), NOT ``fix_start_head`` and NOT a stale pre-sink
        # ``post_agent_head``: the protected-scope repair agent runs INSIDE the
        # commit sink (via ``_repair_protected_scope_changes_before_commit``)
        # and may self-commit, advancing HEAD past ``fix_start_head`` and past
        # any pre-sink snapshot BEFORE this generic exception is raised, so
        # resetting to either stale ref would discard the already-committed
        # validation fix (or the in-sink protected-scope repair self-commit)
        # along with the stranded residue (review threads
        # ``PRRT_kwDOSJAM6s6Klf78`` and ``PRRT_kwDOSJAM6s6KpAD6``, mirroring the
        # provider-recovery and policy-blocked handlers above and the
        # CI-repair / dirty-finalize rollbacks). ``None`` means HEAD could not
        # be resolved; the rollback helper skips the reset.
        post_agent_head = await self._rev_parse_head(worktree_path)
        rollback_failure_reason = await _rollback_failed_fix_pass(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            restore_ref=post_agent_head,
            pass_number=pass_number,
            reason="commit_exception",
        )
        if rollback_failure_reason is not None:
            return False, rollback_failure_reason
        return False, None
    if not committed:
        current_head = await self._rev_parse_head(worktree_path)
        if current_head is not None and current_head != fix_pass_baseline_head:
            # The fix-pass agent self-committed a valid repair (the worktree is clean,
            # so ``_commit_dirty_worktree`` had nothing to commit and returned False).
            advanced = await _head_descends_from_impl(
                self,
                worktree_path=worktree_path,
                ancestor=fix_pass_baseline_head,
                descendant=current_head,
            )
            if advanced:
                # HEAD strictly advanced on top of the pre-fix commit (issue #406):
                # accept as-is.
                _log.info(
                    "monitor.pre_push_validation_fix_self_commit_detected",
                    workspace_id=workspace_id,
                    pass_number=pass_number,
                    fix_start_head=fix_pass_baseline_head,
                    committed_head=current_head,
                    self_commit_kind="advance",
                )
                cleanup_failure_reason = await _cleanup_committed_fix_pass(
                    self,
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    committed_head=current_head,
                    pass_number=pass_number,
                )
                return True, cleanup_failure_reason
            # Non-descendant rewrite (plain amend / content-modifying amend /
            # reset+recommit). These are topologically and tree-content
            # indistinguishable, so we STOP DISCRIMINATING and ALWAYS re-parent the
            # agent's resulting tree onto ``fix_start_head`` (issue #411). This keeps
            # the agent's fix (the pass converges), can never orphan ``fix_start_head``
            # (it is the new commit's parent), and leaves any dropped work auditable as
            # ``git diff fix_start_head..HEAD`` to be re-validated before push.
            new_head, no_net_change, reparent_failure_reason = await _reparent_fix_pass_commit_impl(
                self,
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                fix_start_head=fix_pass_baseline_head,
                current_head=current_head,
                pass_number=pass_number,
                task_tag=task_tag,
            )
            if reparent_failure_reason is not None:
                return True, reparent_failure_reason
            if not no_net_change and new_head is not None:
                _log.info(
                    "monitor.pre_push_validation_fix_reparented",
                    workspace_id=workspace_id,
                    pass_number=pass_number,
                    from_sha=current_head,
                    to_sha=new_head,
                    fix_start_head=fix_pass_baseline_head,
                )
                cleanup_failure_reason = await _cleanup_committed_fix_pass(
                    self,
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    committed_head=new_head,
                    pass_number=pass_number,
                )
                return True, cleanup_failure_reason
            # no_net_change: the agent's tree equals ``fix_start_head``'s tree, so there
            # is nothing to preserve. Fall through to the existing rollback below.
        rollback_failure_reason = await _rollback_failed_fix_pass(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            restore_ref=fix_pass_baseline_head,
            pass_number=pass_number,
            reason="commit_failed",
        )
        if rollback_failure_reason is not None:
            return False, rollback_failure_reason
        return False, None

    committed_head = await self._rev_parse_head(worktree_path)
    if committed_head is None:
        _log.warning(
            "monitor.pre_push_validation_fix_commit_head_unavailable",
            workspace_id=workspace_id,
            pass_number=pass_number,
        )
        return True, PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILED_REASON
    # ``_commit_dirty_worktree`` commits onto whatever the agent left as HEAD. If the
    # fix-pass agent rewrote the tip (``commit --amend`` / ``reset`` to a non-descendant
    # of ``fix_start_head``) AND also left dirty or untracked work, that commit lands as a
    # child of the rewritten tip — so ``committed_head`` no longer descends from
    # ``fix_start_head``, which is orphaned, and the later non-force push (remote_ops.py)
    # would be rejected as a non-fast-forward (issue #411 gap). Re-parent the committed
    # tree onto ``fix_start_head`` so the original tip is preserved as the new commit's
    # parent and the push stays fast-forward, mirroring the self-commit reparent above.
    if not await _head_descends_from_impl(
        self,
        worktree_path=worktree_path,
        ancestor=fix_pass_baseline_head,
        descendant=committed_head,
    ):
        new_head, no_net_change, reparent_failure_reason = await _reparent_fix_pass_commit_impl(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            fix_start_head=fix_pass_baseline_head,
            current_head=committed_head,
            pass_number=pass_number,
            task_tag=task_tag,
        )
        if reparent_failure_reason is not None:
            return True, reparent_failure_reason
        if not no_net_change and new_head is not None:
            _log.info(
                "monitor.pre_push_validation_fix_reparented",
                workspace_id=workspace_id,
                pass_number=pass_number,
                from_sha=committed_head,
                to_sha=new_head,
                fix_start_head=fix_pass_baseline_head,
            )
            committed_head = new_head
        else:
            # The committed tree equals ``fix_start_head``'s tree: the rewrite carried no
            # net change worth preserving. Roll back to the pre-fix tip rather than accept
            # an orphaning rewrite that the push would reject.
            rollback_failure_reason = await _rollback_failed_fix_pass(
                self,
                workspace_id=workspace_id,
                worktree_path=worktree_path,
                restore_ref=fix_pass_baseline_head,
                pass_number=pass_number,
                reason="commit_reparent_no_net_change",
            )
            if rollback_failure_reason is not None:
                return False, rollback_failure_reason
            return False, None
    cleanup_failure_reason = await _cleanup_committed_fix_pass(
        self,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        committed_head=committed_head,
        pass_number=pass_number,
    )
    return True, cleanup_failure_reason
