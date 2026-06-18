"""Pre-push validation dirty-finalize helpers split from ``pre_push_validation``.

Holds the dirty-finalize implementation (operation-owned delta resolution, the
finalize commit sink, and the provider-recovery residue rollback) so the parent
module stays under the first-party line limit. The parent module re-exports
these symbols to preserve its existing public surface and the test monkeypatch
semantics (the same convention used for ``pre_push_validation_fix_pass.py``).

``_pre_push_validation_cleanup`` and ``_pre_push_validation_worktree_check``
remain in the parent module because they are monkeypatched via the parent
namespace and shared with ``_run_pre_push_validation``; this sub-module resolves
them through the parent namespace at call time (late import) so test
monkeypatches still intercept them, mirroring ``pre_push_validation_fix_pass.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from awf.common.logging import get_logger
from awf.runtime.pr_monitor_runner.constants import (
    _MONITOR_POLICY_BLOCKED_REASON,
    _PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
)
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
from awf.runtime.pr_monitor_runner.path_parsing import (
    _changed_paths_from_name_status_z,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_constants import (
    _PRE_PUSH_DIRTY_FINALIZE_DELTA_UNAVAILABLE_REASON,
    _PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON,
)
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    ProviderRecoveryAuthError,
    ProviderRecoveryFallbackError,
    ProviderRecoveryRetryError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorPolicyBlockedError,
)
from awf.runtime.validation_worktree_constants import (
    VALIDATION_WORKTREE_STATUS_FAILED,
)

if TYPE_CHECKING:
    from awf.runtime.validation_worktree import (
        ValidationWorktreeCheck,
    )

_log = get_logger(__name__)


async def _operation_owned_delta_paths(
    self: Any,
    *,
    worktree_path: Path,
    operation_start_head: str,
) -> set[str] | None:
    """Return paths changed by the current operation's committed delta.

    The repair-start dirty guard proves the worktree was clean at
    ``operation_start_head``, so the current monitor operation owns its
    committed delta:

    - its committed delta: ``git diff --name-status -z operation_start_head..HEAD``

    The delta is parsed from ``--name-status -z`` (via
    ``_changed_paths_from_name_status_z``) rather than raw ``--name-only``
    lines so the owned set uses the same path representation as the dirty
    check (``check_validation_worktree_clean`` ->
    ``changed_paths_from_porcelain``). ``--name-status -z`` emits NUL-delimited
    records with both the source and destination for ``R``/``C`` records, and
    never C-quotes paths (the ``-z`` form always emits raw bytes), so a
    committed rename's source path and a non-ASCII path are not mistaken for
    unrelated dirt and stranded as ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY``
    (review thread ``PRRT_kwDOSJAM6s6KaAWk``).

    The gate deliberately does NOT union the staged delta
    (``git diff --name-status -z --cached operation_start_head``). Per
    ``git diff -h``, the ``--cached [<commit>]`` form compares the *current
    index* against the commit, so a tracked file staged after the repair-start
    dirty guard by a failed cleanup or another local process is treated as
    operation-owned merely because it is staged. ``_commit_dirty_worktree``
    then runs a fresh ``git status`` and ``git add -A --`` on every
    non-ignored dirty path, staging and committing that unrelated edit, and the
    post-commit re-validation does not catch it because the pre-commit owned
    set already held the path. The committed delta is the only set the
    operation actually captured, which is what the gate must use rather than
    whatever is in the live index at finalization time (review thread
    ``PRRT_kwDOSJAM6s6KdVXx``, mirroring the ``PRRT_kwDOSJAM6s6KbbE6``
    working-tree-delta removal and the ``PRRT_kwDOSJAM6s6KcSj`` untracked
    fold-in removal).

    The staged delta was previously unioned for the case where the operation's
    ``_commit_dirty_worktree`` returned False *before* creating a commit
    (e.g. ``git commit`` failed after the agent already staged its edits via
    ``git add -A``): ``operation_start_head..HEAD`` was then empty, but the
    operation still owned the staged paths it attempted to commit. That
    recovery (``PRRT_kwDOSJAM6s6KYd-r``) regresses to fail-closed
    ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` — visible and human-recoverable,
    not a silent sweep. Restoring it without the over-broadening requires
    capturing the operation's attempted paths (the ``stage_paths`` the commit
    sink computes) and threading them to the gate; tracked as a deferred
    follow-up (see ``plans/PRRT_kwDOSJAM6s6KdVXx_PLAN.md``).

    Returns ``None`` when the committed delta cannot be resolved (e.g. the
    start ref is unknown, git failed, or the parsed ``--name-status -z``
    output was malformed) so the caller can keep the fail-closed dirty path
    instead of committing unowned dirt.
    """
    committed_result = await self._deps.runner.run(
        git_worktree_command(
            worktree_path, "diff", "--name-status", "-z", f"{operation_start_head}..HEAD"
        )
    )
    if not committed_result.ok:
        _log.warning(
            "monitor.pre_push_dirty_finalize_delta_unavailable",
            operation_start_head=operation_start_head,
            returncode=committed_result.returncode,
            stderr=(committed_result.stderr or "")[:400],
        )
        return None
    try:
        return set(_changed_paths_from_name_status_z(committed_result.stdout or ""))
    except ProtectedScopeDiffError:
        _log.warning(
            "monitor.pre_push_dirty_finalize_delta_malformed",
            operation_start_head=operation_start_head,
            source=(committed_result.stdout or "")[:400],
        )
        return None


async def _committed_delta_paths(
    self: Any,
    *,
    worktree_path: Path,
    operation_start_head: str,
) -> set[str] | None:
    """Return only the paths committed since ``operation_start_head``.

    Unlike ``_operation_owned_delta_paths``, this excludes the staged and
    unstaged working-tree deltas. A successful finalize commit moves the
    operation's owned residue into the committed delta, so the post-commit
    unowned-delta re-validation only needs to inspect what was actually
    committed — paths that remain only in the working tree were not added by
    the finalize commit and must not be flagged as
    ``PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA`` (review thread
    ``PRRT_kwDOSJAM6s6Ka0aO``).

    Returns ``None`` when the committed delta cannot be resolved so the caller
    can keep the fail-closed dirty path instead of trusting the commit.
    """
    committed_result = await self._deps.runner.run(
        git_worktree_command(
            worktree_path, "diff", "--name-status", "-z", f"{operation_start_head}..HEAD"
        )
    )
    if not committed_result.ok:
        _log.warning(
            "monitor.pre_push_dirty_finalize_committed_delta_unavailable",
            operation_start_head=operation_start_head,
            returncode=committed_result.returncode,
            stderr=(committed_result.stderr or "")[:400],
        )
        return None
    try:
        return set(_changed_paths_from_name_status_z(committed_result.stdout or ""))
    except ProtectedScopeDiffError:
        _log.warning(
            "monitor.pre_push_dirty_finalize_committed_delta_malformed",
            operation_start_head=operation_start_head,
            source=(committed_result.stdout or "")[:400],
        )
        return None


async def _try_finalize_pre_push_dirty_repair_state(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    compose_project: str,
    compose_file: Path,
    state: object | None,
    check: ValidationWorktreeCheck,
    operation_start_head: str | None = None,
    remote_branch: str | None = None,
    remote_url: str | None = None,
    finalize_start_head: str | None = None,
) -> ValidationWorktreeCheck | None:
    """Commit monitor-owned residual repair dirt before pre-push validation.

    The finalize is gated on the dirty paths being operation-owned: the
    repair-start dirty guard (``_pre_existing_dirty_repair_worktree_result``)
    proves the worktree was clean at ``operation_start_head``, so the current
    monitor operation's own delta is its committed delta
    (``git diff --name-status -z operation_start_head..HEAD``). The delta is
    parsed from ``--name-status -z`` so the owned set uses the same path
    representation as the dirty check (``changed_paths_from_porcelain``):
    ``--name-status -z`` emits both the source and destination for ``R``/``C``
    records and never C-quotes paths, so a committed rename's source and a
    non-ASCII path are not mistaken for unrelated dirt (review thread
    ``PRRT_kwDOSJAM6s6KaAWk``). Only dirt confined to those paths is safe to
    finalize — dirt on any other path was introduced after the repair-start
    guard (e.g. by a failed cleanup or another local process) and must stay
    fail-closed as ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` so it is never
    silently swept into the PR (review thread ``PRRT_kwDOSJAM6s6KXLaI``).
    When ``operation_start_head`` is unavailable (no operation-owned anchor),
    the finalize is skipped entirely.

    The gate deliberately does NOT consult the live working-tree delta
    (``git diff --name-status -z operation_start_head``, commit vs working
    tree): that diff reports every tracked file differing from the anchor, so
    an unrelated tracked modification introduced after the repair-start guard
    would be treated as operation-owned merely because it differs from
    ``operation_start_head``, swept into the PR via ``_commit_dirty_worktree``'s
    ``git add -A``, and missed by the post-commit re-validation. The committed
    delta is the only set the operation actually captured, so it is the
    correct ownership proxy (review thread ``PRRT_kwDOSJAM6s6KbbE6``). The
    previous working-tree-delta union (added for ``PRRT_kwDOSJAM6s6KaUHP`` to
    recover operation-owned unstaged tracked edits left by a failed
    ``git add -A``) is removed here as over-broadening; that recovery now fails
    closed as ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` and is tracked as a
    deferred follow-up (capturing the operation's attempted paths would restore
    it without the over-broadening).

    The gate deliberately does NOT consult the staged delta
    (``git diff --name-status -z --cached operation_start_head``): per
    ``git diff -h`` the ``--cached [<commit>]`` form compares the *current
    index* against the commit, so a tracked file staged after the repair-start
    guard by a failed cleanup or another local process would be treated as
    operation-owned merely because it is staged, swept into the PR via
    ``_commit_dirty_worktree``'s ``git add -A``, and missed by the post-commit
    re-validation because the pre-commit owned set already held the path. The
    committed delta is the correct ownership proxy (review thread
    ``PRRT_kwDOSJAM6s6KdVXx``, mirroring the ``PRRT_kwDOSJAM6s6KbbE6``
    working-tree delta removal). The previous staged-delta union (added for
    ``PRRT_kwDOSJAM6s6KYd-r`` to recover operation-owned staged edits left by
    a failed ``git commit`` after a successful ``git add -A``) is removed here
    as over-broadening; that recovery now fails closed as
    ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` and is tracked as a deferred
    follow-up (capturing the operation's attempted paths would restore it
    without the over-broadening).

    The gate deliberately does NOT fold ``check.untracked_paths`` into the
    owned set. The repair-start dirty guard only proves the worktree was clean
    at ``operation_start_head`` at repair *start*; ``check.untracked_paths`` is
    computed at pre-push validation time, which is later. A failed cleanup or
    another local process can create an untracked file in that window, and
    folding it in would treat that unrelated file as operation-owned solely
    because it is untracked — ``_commit_dirty_worktree`` would then stage it via
    ``git add -A`` and the post-commit re-validation would see it as committed
    and confined to the owned set, silently sweeping the unrelated untracked
    file into the PR instead of failing closed (review thread
    ``PRRT_kwDOSJAM6s6KcSj``, mirroring the ``PRRT_kwDOSJAM6s6KbbE6`` working-tree
    delta removal). The previous untracked fold-in (added by
    ``PRRT_kwDOSJAM6s6Ka0aK`` to recover operation-owned purely-untracked repair
    output left by a never-run ``git add -A``) is removed here as over-broadening
    for the same safety reason: that recovery now fails closed as
    ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` and is tracked as a deferred
    follow-up (capturing only the operation's attempted untracked paths would
    restore it without the over-broadening).

    The pre-commit gate is necessary but not sufficient: ``_commit_dirty_worktree``
    runs a fresh ``git status``, may invoke protected-scope repair (which runs
    the agent CLI), and then stages all non-ignored dirty paths. A side effect
    between the gate check and the fresh staging scan can introduce an extra
    path outside ``owned_delta_paths``, bypassing the stale gate. After a
    successful commit the finalize therefore re-validates the operation's
    committed delta and fails closed with
    ``PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA`` if any unowned path appears, so
    the unowned commit is never silently pushed (review thread
    ``PRRT_kwDOSJAM6s6KZP8f``).

    ``remote_branch``/``remote_url`` are forwarded to
    ``_commit_dirty_worktree`` as ``protected_scope_revert_remote_branch`` /
    ``remote_push_url`` so ``_repair_protected_scope_changes_before_commit``
    can filter out protected files already restored to the remote PR branch.
    Omitting them (as this call previously did) leaves a restored protected
    file counted as a violation, so the monitor launches another provider
    repair or falls back to a no-commit dirty failure instead of committing
    the safe rollback and proceeding to validation (review thread
    ``PRRT_kwDOSJAM6s6KZjtR``).

    ``finalize_start_head`` is the HEAD captured by the caller immediately
    before the finalize (the operation's committed work). It is the rollback
    anchor FALLBACK for provider-recovery control-flow exceptions raised by
    ``_commit_dirty_worktree`` -> ``_repair_protected_scope_changes_before_commit``:
    the protected-scope repair agent can leave the dirty residue the finalize
    was trying to commit still in the worktree, and re-raising without rolling
    back strands it for the next repair cycle's repair-start guard
    (``_pre_existing_dirty_repair_worktree_result``) as
    ``PRE_EXISTING_DIRTY_WORKTREE``, wedging the workspace on a transient
    outage (review thread ``PRRT_kwDOSJAM6s6KewGH``, mirroring the fix-pass
    residue rollback ``PRRT_kwDOSJAM6s6Kc_Ak``). The preferred anchor is the
    POST-AGENT/PRE-SINK HEAD captured in each provider-recovery handler after
    ``_commit_dirty_worktree`` raised: the protected-scope repair agent runs
    INSIDE ``_commit_dirty_worktree`` and may self-commit, advancing HEAD
    past ``finalize_start_head`` before the exception is raised.
    ``_pre_push_validation_cleanup`` ->
    ``cleanup_validation_worktree_side_effects`` resets HEAD back to the
    restore_ref when it sees HEAD advanced, so anchoring to
    ``finalize_start_head`` would discard the valid protected-scope repair
    self-commit and the provider retry would start from the old tree and
    lose or repeatedly redo the repair work (review thread
    ``PRRT_kwDOSJAM6s6KnWkn``, mirroring the fix-pass rollback
    ``PRRT_kwDOSJAM6s6Klf78`` and the CI-repair rollback
    ``PRRT_kwDOSJAM6s6Klf74``). It is the post-agent HEAD, not
    ``operation_start_head``: the operation may have committed repair work
    since ``operation_start_head``, and resetting to the older anchor would
    discard that committed work. ``None`` means the caller could not capture
    HEAD; the rollback is skipped (the residue strands, but a missing anchor
    makes a safe ``git restore`` impossible).
    """
    # Resolve sibling helpers through the parent module namespace so test
    # monkeypatches on ``pre_push_validation._<helper>`` intercept these calls
    # (mirrors the late-import convention in ``pre_push_validation_fix_pass.py``).
    from awf.runtime.pr_monitor_runner import pre_push_validation as _ppv

    _pre_push_validation_worktree_check = _ppv._pre_push_validation_worktree_check

    # Skip finalization if: no state provided, the tree is already clean, or
    # the worktree status check itself failed. When the status check failed,
    # the working tree state cannot be reliably determined, so committing
    # would be unsafe; skip and let the caller surface the failed status.
    if state is None or check.clean or check.reason_code == VALIDATION_WORKTREE_STATUS_FAILED:
        return None
    # Without an operation-owned anchor the finalize cannot prove the dirt is
    # operation-owned, so it must not commit. Keep the fail-closed dirty path.
    if operation_start_head is None:
        return None
    owned_delta_paths = await _operation_owned_delta_paths(
        self,
        worktree_path=worktree_path,
        operation_start_head=operation_start_head,
    )
    if owned_delta_paths is None:
        # The delta could not be resolved; do not commit unowned dirt.
        return None
    # The ownership gate is intentionally limited to the committed delta —
    # paths the operation actually captured. It deliberately does NOT fold in
    # ``check.untracked_paths``. The repair-start dirty guard
    # (``_pre_existing_dirty_repair_worktree_result``) only proves the worktree
    # was clean at ``operation_start_head`` at repair *start*;
    # ``check.untracked_paths`` is computed at pre-push validation time, which
    # is later. A failed cleanup or another local process can create an
    # untracked file in that window, and folding it in would treat that
    # unrelated file as operation-owned solely because it is untracked;
    # ``_commit_dirty_worktree`` would then stage it via ``git add -A`` and the
    # post-commit re-validation would see it as committed and confined to the
    # owned set, silently sweeping the unrelated untracked file into the PR
    # instead of failing closed. The committed delta is the correct ownership
    # proxy (committed = successfully committed); the untracked fold-in is
    # removed for the same reason the live working-tree delta and the live
    # staged delta were removed (review threads ``PRRT_kwDOSJAM6s6KbbE6``,
    # ``PRRT_kwDOSJAM6s6KdVXx``, ``PRRT_kwDOSJAM6s6KcSj``). The recovery for
    # operation-owned purely-untracked repair output left by a never-run
    # ``git add -A`` (added by ``PRRT_kwDOSJAM6s6Ka0aK``) regresses to
    # fail-closed ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` — visible and
    # human-recoverable, not a silent sweep; restoring it without the
    # over-broadening requires capturing only the operation's attempted
    # untracked paths and is tracked as a deferred follow-up.
    dirty_paths = set(check.paths)
    if not dirty_paths:
        return None
    unrelated_dirty = dirty_paths - owned_delta_paths
    if unrelated_dirty:
        _log.warning(
            "monitor.pre_push_dirty_finalize_skipped_unrelated_dirt",
            workspace_id=workspace_id,
            operation_start_head=operation_start_head,
            dirty_paths=sorted(dirty_paths),
            unrelated_dirty=sorted(unrelated_dirty),
        )
        return None
    from awf.runtime.validation_worktree import ValidationWorktreeCheck

    try:
        committed = bool(
            await self._commit_dirty_worktree(
                workspace_id=workspace_id,
                message=f"awf: finalize PR monitor repair for {workspace_id}",
                compose_project=compose_project,
                compose_file=compose_file,
                state=state,
                protected_scope_revert_remote_branch=remote_branch,
                remote_push_url=remote_url,
            )
        )
    except _MonitorPolicyBlockedError as exc:
        # ``_commit_dirty_worktree`` raises this when monitor-authored changes
        # violate blocking workspace policy. The policy check runs BEFORE the
        # actual ``git commit`` (``_refresh_supply_chain_policy_before_push``),
        # so the dirty paths being finalized are still in the worktree. Like the
        # other commit callers (``remote_ops.py``, ``ci_ops.py``,
        # ``fix_cycle.py``, ``operator_hints.py``), preserve the policy reason
        # code end-to-end instead of letting it collapse into the generic
        # pre-existing-dirty failure. Returning a non-clean check carrying the
        # reason code flows through ``_pre_push_dirty_result`` into
        # ``_GitPushResult.reason_code``. That reason is intentionally
        # NON-terminal (``_GitPushResult.terminal_monitor_failure`` does not
        # include ``MONITOR_POLICY_BLOCKED``), so the monitor loop increments
        # and retries. Returning without rolling back strands the residue, and
        # the next repair cycle's repair-start guard
        # (``_pre_existing_dirty_repair_worktree_result``) trips as
        # ``PRE_EXISTING_DIRTY_WORKTREE``, losing the policy reason and wedging
        # recovery instead of re-polling cleanly. Roll back to
        # ``finalize_start_head`` before returning this reason, mirroring the
        # fix-pass policy-blocked rollback (review thread
        # ``PRRT_kwDOSJAM6s6KjRRL``, mirroring ``PRRT_kwDOSJAM6s6Kg7Dm``). A
        # rollback failure is logged but never clobbers the policy reason: the
        # loop still sees ``MONITOR_POLICY_BLOCKED`` and a stranded residue
        # surfaces as the next attempt's pre-existing-dirty guard rather than
        # being silently swallowed here.
        await _rollback_finalize_dirty_residue_before_provider_recovery(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            restore_ref=finalize_start_head,
            reason="policy_blocked_dirty_residue",
        )
        _log.warning(
            "monitor.pre_push_dirty_finalize_policy_blocked",
            workspace_id=workspace_id,
            error=repr(exc),
            paths=list(check.paths),
        )
        return ValidationWorktreeCheck(
            clean=False,
            paths=check.paths,
            reason_code=_MONITOR_POLICY_BLOCKED_REASON,
            message=str(exc) or "monitor policy blocked the pre-push dirty finalize",
        )
    except _MonitorAgentRuntimeOwnershipRepairFailedError as exc:
        # ``_commit_dirty_worktree`` raises this (carrying a ``reason_code``
        # property) when monitor cannot repair agent worktree ownership.
        # Preserve that reason code end-to-end like the other commit callers
        # instead of collapsing it into the generic pre-existing-dirty failure.
        _log.warning(
            "monitor.pre_push_dirty_finalize_ownership_repair_failed",
            workspace_id=workspace_id,
            error=repr(exc),
            paths=list(check.paths),
            reason_code=exc.reason_code,
        )
        return ValidationWorktreeCheck(
            clean=False,
            paths=check.paths,
            reason_code=exc.reason_code,
            message=str(exc) or "agent runtime ownership repair failed",
        )
    except ProtectedScopeDiffError as exc:
        # ``_commit_dirty_worktree`` -> ``_repair_protected_scope_changes_before_commit``
        # raises this when the committed diff against the remote PR branch cannot be
        # verified (e.g. the protected-scope remote-diff baseline is unavailable). Like
        # the other commit callers (``ci_ops.py``, ``operator_hints.py``,
        # ``fix_cycle.py``, ``remote_ops.py``), preserve the
        # ``PROTECTED_SCOPE_DIFF_UNAVAILABLE`` reason code end-to-end instead of
        # letting it collapse into the generic pre-existing-dirty failure.
        # Returning a non-clean check carrying the reason code flows through
        # ``_pre_push_dirty_result`` into ``_GitPushResult.reason_code``.
        _log.warning(
            "monitor.pre_push_dirty_finalize_protected_scope_diff_unavailable",
            workspace_id=workspace_id,
            error=repr(exc),
            paths=list(check.paths),
        )
        return ValidationWorktreeCheck(
            clean=False,
            paths=check.paths,
            reason_code=_PROTECTED_SCOPE_DIFF_UNAVAILABLE_REASON,
            message=str(exc) or "protected-scope diff unavailable before pre-push dirty finalize",
        )
    except ProviderRecoveryRetryError:
        # ``_commit_dirty_worktree`` -> ``_repair_protected_scope_changes_before_commit``
        # raises this when provider recovery suppresses the CLI and the operation must
        # back off and retry later. Every other commit caller lets it propagate so the
        # loop's ``except ProviderRecoveryRetryError`` handler surfaces ``PROVIDER_OUTAGE``
        # retry semantics; re-raise here instead of swallowing it into the generic
        # pre-existing-dirty failure. BUT first roll back the dirty-finalize residue
        # the protected-scope repair agent left behind, otherwise the next repair
        # cycle's repair-start guard trips ``PRE_EXISTING_DIRTY_WORKTREE`` and the
        # provider retry never actually runs — a transient outage wedges the
        # workspace (review thread ``PRRT_kwDOSJAM6s6KewGH``, mirroring the fix-pass
        # rollback ``PRRT_kwDOSJAM6s6Kc_Ak``). The rollback anchor is the
        # post-agent/pre-sink HEAD captured HERE (after ``_commit_dirty_worktree``
        # raised), NOT ``finalize_start_head``: the protected-scope repair agent
        # runs INSIDE ``_commit_dirty_worktree`` and may self-commit, advancing
        # HEAD past ``finalize_start_head`` before the provider-recovery exception
        # is raised. ``_pre_push_validation_cleanup`` ->
        # ``cleanup_validation_worktree_side_effects`` resets HEAD back to the
        # restore_ref when it sees HEAD advanced, so anchoring to
        # ``finalize_start_head`` would discard the valid protected-scope repair
        # self-commit and the provider retry would start from the old tree and
        # lose or repeatedly redo the repair work (review thread
        # ``PRRT_kwDOSJAM6s6KnWkn``, mirroring the fix-pass rollback
        # ``PRRT_kwDOSJAM6s6Klf78`` and the CI-repair rollback
        # ``PRRT_kwDOSJAM6s6Klf74``, which both anchor against the
        # post-agent/pre-sink HEAD for the same reason). A missing anchor falls
        # back to ``finalize_start_head`` so the residue is still rolled back
        # (the self-commit cannot be preserved without a resolved HEAD, but
        # stranding the residue would wedge the next cycle). A rollback failure
        # is logged but never clobbers the recovery exception: the loop's
        # recovery handlers still run, and a stranded residue surfaces as the
        # next attempt's pre-existing-dirty guard rather than being silently
        # swallowed here.
        post_agent_head = await self._rev_parse_head(worktree_path)
        await _rollback_finalize_dirty_residue_before_provider_recovery(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            restore_ref=post_agent_head or finalize_start_head,
            reason="provider_recovery_retry",
        )
        _log.warning(
            "monitor.pre_push_dirty_finalize_provider_recovery_retry",
            workspace_id=workspace_id,
            paths=list(check.paths),
        )
        raise
    except ProviderRecoveryFallbackError:
        # ``_commit_dirty_worktree`` -> ``_repair_protected_scope_changes_before_commit`` ->
        # ``_handle_provider_agent_run_error`` raises this when a provider failure
        # triggers a fallback workspace. The loop's
        # ``except ProviderRecoveryFallbackError`` handler surfaces ``PROVIDER_FALLBACK``
        # semantics, so the finalize must re-raise it instead of letting the broad
        # ``except Exception`` below swallow it into the generic pre-existing-dirty
        # failure (review thread ``PRRT_kwDOSJAM6s6KYd-t``). Roll back the dirty-finalize
        # residue first (see the ``ProviderRecoveryRetryError`` handler for the
        # post-agent/pre-sink HEAD anchoring — review thread
        # ``PRRT_kwDOSJAM6s6KnWkn``, mirroring ``PRRT_kwDOSJAM6s6Klf78`` /
        # ``PRRT_kwDOSJAM6s6Klf74``) so the next repair cycle is not wedged as
        # ``PRE_EXISTING_DIRTY_WORKTREE`` (review thread
        # ``PRRT_kwDOSJAM6s6KewGH``, mirroring ``PRRT_kwDOSJAM6s6Kc_Ak``).
        post_agent_head = await self._rev_parse_head(worktree_path)
        await _rollback_finalize_dirty_residue_before_provider_recovery(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            restore_ref=post_agent_head or finalize_start_head,
            reason="provider_recovery_fallback",
        )
        _log.warning(
            "monitor.pre_push_dirty_finalize_provider_recovery_fallback",
            workspace_id=workspace_id,
            paths=list(check.paths),
        )
        raise
    except ProviderRecoveryAuthError:
        # ``_commit_dirty_worktree`` -> ``_repair_protected_scope_changes_before_commit`` ->
        # ``_handle_provider_agent_run_error`` raises this when provider auth is broken
        # and the operation cannot continue. The loop's
        # ``except ProviderRecoveryAuthError`` handler surfaces the auth-failed operation
        # outcome, so the finalize must re-raise it instead of letting the broad
        # ``except Exception`` below swallow it into the generic pre-existing-dirty
        # failure (review thread ``PRRT_kwDOSJAM6s6KYd-t``). Roll back the dirty-finalize
        # residue first (see the ``ProviderRecoveryRetryError`` handler for the
        # post-agent/pre-sink HEAD anchoring — review thread
        # ``PRRT_kwDOSJAM6s6KnWkn``, mirroring ``PRRT_kwDOSJAM6s6Klf78`` /
        # ``PRRT_kwDOSJAM6s6Klf74``) so the next repair cycle is not wedged as
        # ``PRE_EXISTING_DIRTY_WORKTREE`` (review thread
        # ``PRRT_kwDOSJAM6s6KewGH``, mirroring ``PRRT_kwDOSJAM6s6Kc_Ak``).
        post_agent_head = await self._rev_parse_head(worktree_path)
        await _rollback_finalize_dirty_residue_before_provider_recovery(
            self,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            restore_ref=post_agent_head or finalize_start_head,
            reason="provider_recovery_auth",
        )
        _log.warning(
            "monitor.pre_push_dirty_finalize_provider_recovery_auth",
            workspace_id=workspace_id,
            paths=list(check.paths),
        )
        raise
    except Exception as exc:
        _log.warning(
            "monitor.pre_push_dirty_finalize_failed",
            workspace_id=workspace_id,
            error=repr(exc),
            paths=list(check.paths),
        )
        return None
    if not committed:
        # ``_commit_dirty_worktree`` can have side effects (e.g. protected-scope
        # repair restores the only dirty files) yet return False because there
        # was nothing left to commit. Re-check the tree before giving up so a
        # cleanup-only repair can proceed to validation instead of being
        # stranded as ``VALIDATION_WORKTREE_PRE_EXISTING_DIRTY`` on the stale
        # dirty check captured before the finalize attempt.
        recheck = await _pre_push_validation_worktree_check(
            self,
            worktree_path=worktree_path,
        )
        if recheck.clean:
            # The protected-scope repair agent invoked inside
            # ``_commit_dirty_worktree`` (via
            # ``_repair_protected_scope_changes_before_commit``) can self-commit,
            # advancing HEAD past ``finalize_start_head`` while leaving the
            # worktree clean, so the sink finds no remaining ``stage_paths`` and
            # returns False. A clean tree therefore does NOT prove the absence
            # of an agent-created commit: the self-commit may contain paths
            # outside the operation-owned delta, which would bypass the
            # post-commit ``PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA`` gate below
            # (that gate only runs when ``committed=True``) and proceed to
            # validation/push. Before accepting the clean recheck, detect HEAD
            # movement and run the same committed-delta ownership check so an
            # agent-created commit containing paths outside the operation-owned
            # delta fails closed here (review thread
            # ``PRRT_kwDOSJAM6s6KpCpP``, mirroring the ``PRRT_kwDOSJAM6s6KZP8f``
            # post-commit re-validation).
            post_agent_head = await self._rev_parse_head(worktree_path)
            if (
                finalize_start_head is not None
                and post_agent_head is not None
                and post_agent_head != finalize_start_head
            ):
                post_no_commit_delta = await _committed_delta_paths(
                    self,
                    worktree_path=worktree_path,
                    operation_start_head=operation_start_head,
                )
                if post_no_commit_delta is None:
                    # The post-no-commit-clean committed delta could not be
                    # inspected; do not trust the agent's self-commit. Fail
                    # closed with the dedicated delta-unavailable reason, as
                    # the committed-delta re-validation does for the
                    # ``committed=True`` path (review thread
                    # ``PRRT_kwDOSJAM6s6KhtZJ``).
                    _log.warning(
                        "monitor.pre_push_dirty_finalize_no_commit_clean_delta_unavailable",
                        workspace_id=workspace_id,
                        operation_start_head=operation_start_head,
                        post_agent_head=post_agent_head,
                        finalize_start_head=finalize_start_head,
                        paths=list(check.paths),
                    )
                    return ValidationWorktreeCheck(
                        clean=False,
                        paths=check.paths,
                        reason_code=_PRE_PUSH_DIRTY_FINALIZE_DELTA_UNAVAILABLE_REASON,
                        message=(
                            "pre-push dirty finalize could not re-validate the "
                            "committed operation delta after a no-commit-clean "
                            "agent self-commit"
                        ),
                    )
                unowned_no_commit = post_no_commit_delta - owned_delta_paths
                if unowned_no_commit:
                    _log.warning(
                        "monitor.pre_push_dirty_finalize_no_commit_clean_unowned_delta",
                        workspace_id=workspace_id,
                        operation_start_head=operation_start_head,
                        post_agent_head=post_agent_head,
                        finalize_start_head=finalize_start_head,
                        owned_delta_paths=sorted(owned_delta_paths),
                        unowned_no_commit=sorted(unowned_no_commit),
                    )
                    return ValidationWorktreeCheck(
                        clean=False,
                        paths=tuple(sorted(unowned_no_commit)),
                        reason_code=_PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON,
                        message=(
                            "pre-push dirty finalize accepted a clean "
                            "no-commit worktree whose agent self-commit "
                            "contained paths outside the operation-owned delta"
                        ),
                    )
            _log.info(
                "monitor.pre_push_dirty_finalize_no_commit_clean",
                workspace_id=workspace_id,
                paths=list(check.paths),
            )
            return recheck
        _log.warning(
            "monitor.pre_push_dirty_finalize_no_commit",
            workspace_id=workspace_id,
            paths=list(check.paths),
        )
        return recheck

    # Re-validate the operation's committed delta AFTER the commit sink's side
    # effects. The ownership gate above was computed before calling
    # ``_commit_dirty_worktree``, but that sink runs a fresh ``git status``, may
    # invoke protected-scope repair (which runs the agent CLI), and then stages
    # all non-ignored dirty paths. A side effect between the gate check and the
    # fresh staging scan can introduce an extra path outside
    # ``owned_delta_paths``; without this post-commit re-validation the stale
    # gate would let the unowned commit through and the verify recheck below
    # would observe a clean tree (the unowned path was just committed), silently
    # sweeping unowned dirt into the PR. Fail closed with a dedicated reason
    # code so the unowned commit is never pushed (review thread
    # ``PRRT_kwDOSJAM6s6KZP8f``).
    #
    # Only the *committed* delta is re-validated here. ``owned_delta_paths``
    # (the pre-commit committed delta) is the baseline the finalize was allowed
    # to commit; after a successful finalize commit the operation's owned
    # residue has moved into the committed delta. Re-using the full
    # ``_operation_owned_delta_paths`` union would include the
    # commit-vs-working-tree diff, so an unrelated tracked edit that remains
    # only in the working tree after a valid finalize would be flagged as
    # ``PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA`` even though the finalize commit
    # did not add it. Restricting the re-validation to the committed delta
    # confines the fail-closed check to paths the finalize commit actually
    # introduced (review thread ``PRRT_kwDOSJAM6s6Ka0aO``).
    post_commit_delta = await _committed_delta_paths(
        self,
        worktree_path=worktree_path,
        operation_start_head=operation_start_head,
    )
    if post_commit_delta is None:
        # The post-commit committed delta could not be resolved; do not trust
        # the commit. This is distinct from an *unowned* delta: no path was
        # proven to be unowned, the committed delta simply could not be
        # inspected, so a dedicated reason code is used instead of
        # ``PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA`` (review thread
        # ``PRRT_kwDOSJAM6s6KhtZJ``).
        _log.warning(
            "monitor.pre_push_dirty_finalize_post_commit_delta_unavailable",
            workspace_id=workspace_id,
            operation_start_head=operation_start_head,
            paths=list(check.paths),
        )
        return ValidationWorktreeCheck(
            clean=False,
            paths=check.paths,
            reason_code=_PRE_PUSH_DIRTY_FINALIZE_DELTA_UNAVAILABLE_REASON,
            message=(
                "pre-push dirty finalize could not re-validate the committed "
                "operation delta after the commit sink side effects"
            ),
        )
    unowned_committed = post_commit_delta - owned_delta_paths
    if unowned_committed:
        _log.warning(
            "monitor.pre_push_dirty_finalize_unowned_delta",
            workspace_id=workspace_id,
            operation_start_head=operation_start_head,
            owned_delta_paths=sorted(owned_delta_paths),
            unowned_committed=sorted(unowned_committed),
        )
        return ValidationWorktreeCheck(
            clean=False,
            paths=tuple(sorted(unowned_committed)),
            reason_code=_PRE_PUSH_DIRTY_FINALIZE_UNOWNED_DELTA_REASON,
            message=(
                "pre-push dirty finalize committed paths outside the "
                "operation-owned delta after the commit sink side effects"
            ),
        )

    verify = await _pre_push_validation_worktree_check(
        self,
        worktree_path=worktree_path,
    )
    if verify.clean:
        _log.info(
            "monitor.pre_push_dirty_finalized",
            workspace_id=workspace_id,
            paths=list(check.paths),
        )
    else:
        _log.warning(
            "monitor.pre_push_dirty_finalize_still_dirty",
            workspace_id=workspace_id,
            paths=list(verify.paths),
        )
    return verify


async def _rollback_finalize_dirty_residue_before_provider_recovery(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    restore_ref: str | None,
    reason: str,
) -> None:
    """Roll back dirty-finalize residue before re-raising provider recovery.

    ``_commit_dirty_worktree`` -> ``_repair_protected_scope_changes_before_commit``
    can run the agent CLI to remove protected-scope edits; if that raises a
    provider-recovery control-flow exception (retry/fallback/auth) the agent
    may leave the dirty residue the finalize was trying to commit still in the
    worktree. Re-raising without rolling back strands that residue: the outer
    monitor records a provider outcome and exits the operation, then the next
    repair cycle's repair-start guard
    (``_pre_existing_dirty_repair_worktree_result``) sees the still-dirty
    worktree and fails as ``PRE_EXISTING_DIRTY_WORKTREE`` before the provider
    retry can actually run, wedging the workspace on a transient outage
    (review thread ``PRRT_kwDOSJAM6s6KewGH``, mirroring the fix-pass residue
    rollback ``PRRT_kwDOSJAM6s6Kc_Ak``).

    ``restore_ref`` is the HEAD captured AFTER the protected-scope repair
    agent ran but BEFORE the residue cleanup (the post-agent/pre-sink HEAD),
    NOT ``finalize_start_head``. The protected-scope repair agent runs INSIDE
    ``_commit_dirty_worktree`` (via
    ``_repair_protected_scope_changes_before_commit``) and may self-commit,
    advancing HEAD past ``finalize_start_head`` before the provider-recovery
    exception is raised. ``_commit_dirty_worktree`` raises before its own
    ``git commit``, so HEAD has not moved since the agent's self-commit;
    restoring tracked paths to this ref preserves the committed repair while
    discarding only the uncommitted residue. Anchoring to
    ``finalize_start_head`` instead would reset HEAD back to the pre-agent
    ref via ``cleanup_validation_worktree_side_effects``' HEAD-verification
    ``git reset --hard``, discarding the valid protected-scope repair
    self-commit and forcing the provider retry to start from the old tree
    and lose or repeatedly redo the repair work (review thread
    ``PRRT_kwDOSJAM6s6KnWkn``, mirroring the fix-pass rollback
    ``PRRT_kwDOSJAM6s6Klf78`` and the CI-repair rollback
    ``PRRT_kwDOSJAM6s6Klf74``, which both anchor against the
    post-agent/pre-sink HEAD for the same reason). ``None`` means the
    finalize-start HEAD could not be captured; the rollback is skipped (the
    residue strands, but a missing anchor makes a safe ``git restore``
    impossible — better to strand visibly than restore against the wrong ref).

    A cleanup failure is logged but never clobbers the pending
    provider-recovery exception: the loop's recovery handlers still run, and
    a stranded residue surfaces as the next attempt's pre-existing-dirty
    guard rather than being silently swallowed here.
    """
    if restore_ref is None:
        _log.warning(
            "monitor.pre_push_dirty_finalize_provider_recovery_rollback_skipped_no_anchor",
            workspace_id=workspace_id,
            reason=reason,
        )
        return
    # Resolve through the parent namespace so test monkeypatches on
    # ``pre_push_validation._pre_push_validation_cleanup`` intercept this call
    # (mirrors ``pre_push_validation_fix_pass.py``).
    from awf.runtime.pr_monitor_runner import pre_push_validation as _ppv

    cleanup = await _ppv._pre_push_validation_cleanup(
        self,
        worktree_path=worktree_path,
        restore_ref=restore_ref,
    )
    if not cleanup.ok:
        _log.warning(
            "monitor.pre_push_dirty_finalize_provider_recovery_rollback_failed",
            workspace_id=workspace_id,
            reason=reason,
            restore_ref=restore_ref,
            cleanup_reason_code=cleanup.reason_code,
            cleanup_message=cleanup.message[:400],
        )
