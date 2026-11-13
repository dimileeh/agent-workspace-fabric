"""Preserved protected-commit re-block handling for operator-hint resumes.

Extracted from :mod:`awf.runtime.pr_monitor_runner.operator_hints` so that module
stays within the first-party file-line guardrail
(``tests/unit/test_core_decomposition_maintainability.py``). Behavior is
unchanged; the symbols are re-exported from ``operator_hints``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from awf.common.github_client import RepoRef
from awf.control.blocked_transition import (
    monitor_protected_scope_resume_origin,
)
from awf.control.quality_gates import QualityGateViolation
from awf.db.repositories import WorkspaceRepository
from awf.runtime.operator_hints import (
    mark_operator_hint_needs_human,
)
from awf.runtime.pr_monitor import (
    _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY,
    MonitorState,
    OperatorHint,
)
from awf.runtime.pr_monitor_runner.comments import MonitorVerdictResult, VerdictResult
from awf.runtime.pr_monitor_runner.constants import (
    _PROTECTED_SCOPE_PUSH_BLOCKED_REASON,
)
from awf.runtime.pr_monitor_runner.operator_hint_retirement import (
    _finalize_processed_operator_hint as _finalize_processed_operator_hint,
)
from awf.runtime.pr_monitor_runner.operator_hint_retirement import (
    _mark_referenced_needs_human_feedback_answered as _mark_referenced_needs_human_feedback_answered,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult, _ProtectedScopePushBlock


async def _reblock_preserved_protected_leak(
    self: Any,
    *,
    workspace_id: str,
    pr_number: int,
    pr_head_sha: str,
    worktree_path: Path,
    state: MonitorState,
    remote_branch: str,
    base_branch: str,
    operation_id: str | None,
    operation_type: str | None,
    operation_start_head: str | None,
    block_resume_phase: str,
    reason: str,
    extra_state_markers: Mapping[str, str] | None = None,
    repo: RepoRef | None = None,
) -> _GitPushResult:
    """Re-block a still-undeliverable preserved protected commit into ``blocked``.

    Shared by the directive-leak guard and the grant-active non-fast-forward
    rejection branch. Both reach a state where the preserved ungranted/undelivered
    protected commit is still in local history but the workspace is at
    ``monitoring_pr`` — where ``guide_workspace`` rejects ``--grant`` inputs
    (``WorkspaceGuideGrantNotAllowedError``). Rebuild the protected block from the
    violations recorded at the original block (exactly the protected paths the
    preserved commit changed) and reuse the shared WS-1 ``enter_blocked`` pause path
    so the existing blocked-status + operator-grant machinery resolves it
    (``guide --grant`` -> ``blocked -> monitoring_pr`` grant resume -> grant-aware
    push). The resume ORIGIN is preserved exactly as the net-diff re-block branch.

    Defensive fallback: a real protected block always records >=1 violation
    (``_protected_scope_push_block`` only returns a block for non-empty violations),
    so a ``None`` reconstruction is effectively unreachable once the preserved-head
    marker was set by a genuine block. When nothing can be reconstructed, mark the
    hint needs_human and return a NON-failed result so the loop parks the recoverable
    workspace at ``monitoring_pr`` rather than ``_terminate_failed``ing it — a
    failed/terminal row would also reject a later approve-and-keep grant
    (PRRT_kwDOSJAM6s6KHEEU)."""
    # Nothing to re-block for once the PR itself ended: a re-block would enter
    # ``blocked`` and post an operator notification on a merged/closed PR (#910).
    moot_result = await self._post_action_pr_terminal_push_result_if_moot(
        workspace_id=workspace_id,
        pr_number=pr_number,
        context="operator_hint_preserved_leak_reblock",
        operation_id=operation_id,
        operation_type=operation_type,
        repo=repo,
        worktree_path=worktree_path,
    )
    if moot_result is not None:
        return cast(_GitPushResult, moot_result)
    leak_block = await _directive_preserved_leak_protected_block(
        self, workspace_id=workspace_id, message=reason
    )
    if leak_block is not None:
        reblock_resume_phase = monitor_protected_scope_resume_origin(block_resume_phase)
        reblock_result = cast(
            _GitPushResult,
            await self._pause_monitor_for_protected_scope_block(
                workspace_id=workspace_id,
                pr_number=pr_number,
                pr_head_sha=pr_head_sha,
                protected_scope_block=leak_block,
                worktree_path=worktree_path,
                resume_phase=reblock_resume_phase,
                state=state,
                remote_branch=remote_branch,
                base_branch=base_branch,
                operation_id=operation_id,
                operation_type=operation_type,
                source_head_sha=operation_start_head,
                extra_state_markers=extra_state_markers,
                repo=repo,
            ),
        )
        if reblock_result.paused_into_blocked:
            # Mirror the net-diff re-block branch: the bumped block epoch supersedes
            # this hint, so clear the in-memory monitor hint and let the resume
            # re-arm a fresh one.
            state.pending_operator_hint = None
        return reblock_result
    mark_operator_hint_needs_human(state, reason)
    return _GitPushResult(pushed=False, failed=False, returncode=1, stderr=reason)


async def _directive_preserved_leak_protected_block(
    self: Any, *, workspace_id: str, message: str
) -> _ProtectedScopePushBlock | None:
    """Rebuild the protected block from the recorded violations for a leak re-block.

    The directive-leak guard fires when the net diff is clean
    (``_protected_scope_push_block`` returned ``None``) yet the preserved ungranted
    protected commit is still in the unpushed range. To re-block (so ``guide
    --grant`` becomes reachable) the shared ``enter_blocked`` pause path needs the
    protected violations, but the net-diff block carrying them is ``None`` here.
    Reconstruct them from ``workspace.block_violations`` — recorded by the original
    block, i.e. exactly the protected paths the preserved commit changed — using
    ``message`` (the leak reason) as the block message so the pause event and
    operator notification stay informative. Returns ``None`` when no violation can
    be rebuilt so the caller falls back to parking a needs_human hint."""
    async with self._deps.session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        if workspace is None:  # pragma: no cover - the active monitor cycle holds the row
            return None
        recorded = list(workspace.block_violations or [])
    violations: list[QualityGateViolation] = []
    for entry in recorded:
        path = entry.get("path")
        if not path:
            continue
        line = entry.get("line")
        violations.append(
            QualityGateViolation(
                path=str(path),
                protected_pattern=str(entry.get("protected_pattern") or path),
                section=cast("str | None", entry.get("section")),
                line=line if isinstance(line, int) and not isinstance(line, bool) else None,
                reason=str(
                    entry.get("reason")
                    or "protected quality-gate file changed outside declared owned_paths"
                ),
            )
        )
    if not violations:
        return None
    return _ProtectedScopePushBlock(
        message=message,
        reason_code=_PROTECTED_SCOPE_PUSH_BLOCKED_REASON,
        violations=tuple(violations),
    )


async def _terminal_directive_grant_reblock(
    self: Any,
    *,
    workspace_id: str,
    pr_number: int,
    pr_head_sha: str,
    worktree_path: Path,
    state: MonitorState,
    remote_branch: str,
    base_branch: str | None,
    operation_id: str | None,
    operation_type: str | None,
    operation_start_head: str | None,
    preserved_head_sha: str | None,
    active_grant_specs: Any,
    verdict: VerdictResult | MonitorVerdictResult,
    repo: RepoRef | None = None,
) -> _GitPushResult | None:
    """Re-block a TERMINAL combined directive+grant protected-block resume.

    A monitor-origin protected block resumed with a combined ``--directive ...
    --grant ...`` runs the directive CLI; when that pass returns a TERMINAL verdict
    (``agent_failed`` / ``needs_human`` / ``defer`` / ``false_positive``) BEFORE any
    push, the preserved protected commit and the single-use approve-and-keep grant
    are BOTH still unfinalized (the grant is consumed and the marker dropped only on
    a successful resume). Merely parking the hint at ``monitoring_pr`` strands the
    operator: ``decide()`` only surfaces ``NotifyHuman`` while ``guide_workspace``
    rejects new ``--grant`` inputs unless the row is ``blocked`` and a fresh
    ``--directive`` revokes the existing grant — so neither approve-and-keep nor
    consuming the grant to land the preserved commit is reachable. Re-block via the
    shared WS-1 pause path (mirroring the directive-leak guard above) so
    ``guide --grant`` / a new directive can resolve it; the preserved commit is kept
    and the bumped block epoch supersedes the stale grant (PRRT_kwDOSJAM6s6KT0rd).

    Scoped to a grant-bearing preserved-block resume (``preserved_head_sha`` AND
    ``active_grant_specs`` present). Returns ``None`` otherwise — a plain remonitor,
    a directive-only resume, or a grant-only resume (which never runs the CLI) keep
    the existing park-the-hint behavior — and also when no violation can be
    reconstructed, so the caller falls back to parking the terminal hint. Returns
    ``None`` too when the directive CLI moved HEAD away from the preserved commit so
    it is no longer reachable: re-blocking would overwrite the preserved marker with
    the moved HEAD and let a later grant-only resume no-op while consuming the grant
    (PRRT_kwDOSJAM6s6KVCYD). In that dropped-commit case it ALSO clears the stale
    preserved-head marker AND consumes the single-use grant before parking. Clearing
    the marker is required because with a grant still active
    ``_clear_dropped_preserved_marker_after_terminal_directive`` deliberately skips
    clearing, so otherwise a later FRESH directive (which revokes the grant in
    ``guide_workspace``) would leave no active grants plus a stale marker and a local
    HEAD already on the remote — satisfying the directive-drop restart shortcut at the
    top of ``_run_operator_hint_cycle``, which would finalize the new hint as a no-op
    and SKIP the CLI, silently ignoring the operator's follow-up (PRRT_kwDOSJAM6s6KVgwV).
    Consuming the grant is required because clearing the marker drops
    ``has_preserved_protected_block`` to False, so ``decide()`` stops running this
    resume ahead of ``SyncBase``; a surviving grant for the dropped commit would then
    let a base-sync conflict edit push to the granted protected path under a stale
    approval — a grant leak (PRRT_kwDOSJAM6s6KVt_Q)."""
    if not (preserved_head_sha and active_grant_specs):
        return None
    # Re-check the PR before touching any state: once it merged/closed there is
    # nothing left to re-block, and the re-block would enter ``blocked`` plus post
    # an operator notification on a terminal PR (#910). Runs BEFORE the
    # reachability probe so no marker/grant bookkeeping fires either.
    moot_result = await self._post_action_pr_terminal_push_result_if_moot(
        workspace_id=workspace_id,
        pr_number=pr_number,
        context="operator_hint_terminal_directive_reblock",
        operation_id=operation_id,
        operation_type=operation_type,
        repo=repo,
        worktree_path=worktree_path,
    )
    if moot_result is not None:
        return cast(_GitPushResult, moot_result)
    # The combined directive CLI may have MOVED HEAD before returning the terminal
    # verdict — e.g. it reset the worktree back to the remote PR head (dropping the
    # preserved protected commit) and then reported needs_human. Re-blocking here
    # routes through ``_pause_monitor_for_protected_scope_block``, which re-records
    # the CURRENT rev-parse HEAD as the preserved marker, OVERWRITING the original
    # ``preserved_head_sha``. With the marker now pointing at the reset HEAD (already
    # on the remote), a later grant-only resume would see it already pushed, finalize
    # as a no-op, and CONSUME the single-use grant while the approved protected commit
    # never lands. Only re-block while the ORIGINAL preserved commit is still
    # reachable from HEAD (the operator can still land it via approve-and-keep);
    # otherwise fall back to parking the terminal hint so the operator is surfaced
    # without silently burning the grant (PRRT_kwDOSJAM6s6KVCYD).
    if not await self._preserved_commit_reachable_from_head(
        worktree_path=worktree_path,
        preserved_head_sha=preserved_head_sha,
    ):
        # The preserved commit was DROPPED by the combined directive CLI. There is
        # nothing left to protect, so clear the stale marker before parking — the
        # grant-active early return in
        # ``_clear_dropped_preserved_marker_after_terminal_directive`` would otherwise
        # leave it set, and a later grant-revoking directive's drop-restart shortcut
        # would silently no-op the operator's follow-up (PRRT_kwDOSJAM6s6KVgwV).
        # Clearing the marker drops ``state.has_preserved_protected_block`` to False, so
        # ``decide()`` no longer runs this protected resume ahead of ``SyncBase``. The
        # single-use grant approved ONLY the now-dropped preserved commit, yet it stays
        # active until consumed; if the base advanced, the next poll runs ``SyncBase``
        # first and ``_protected_scope_violations_for_sync_base_push`` loads that
        # still-active grant, letting a conflict-resolution edit on the granted
        # protected path push under an approval meant for the dropped commit — a grant
        # leak (PRRT_kwDOSJAM6s6KVt_Q, the KGX2A class). Consume the grant together with
        # the marker so nothing carries the stale approval forward; a re-introduced
        # protected change re-blocks and must be granted again.
        state.threads_addressed_ids.pop(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, None)
        # Make the marker clear DURABLE before parking, mirroring the directive-only
        # drop branch (PRRT_kwDOSJAM6s6KWAIx). Popping only the in-memory copy here is
        # not durable until the loop's later _persist_state, and the follow-up
        # _clear_dropped_preserved_marker_after_terminal_directive early-returns once a
        # grant is present, so nothing else persists the removal. The marker clear and
        # the grant consume must be ATOMIC: clearing the marker drops
        # ``has_preserved_protected_block`` to False so ``decide()`` stops ranking this
        # resume ahead of ``SyncBase``, so a crash BETWEEN two separate commits (marker
        # cleared, grant not yet consumed) would durably leave marker-gone + grant-active
        # — the very KVt_Q grant leak the consume closes. Do both in one transaction so a
        # crash before the commit leaves marker-present + grant-active (re-blocks /
        # re-runs safely) and a crash after leaves marker-gone + grant-consumed.
        await self._clear_preserved_marker_and_consume_grants_durably(workspace_id)
        return None
    message = (
        f"operator directive resume returned '{verdict.verdict}' before landing the "
        f"approved protected change for preserved commit {preserved_head_sha}; "
        "re-blocking so an approve-and-keep grant or a new directive can resolve it"
    )
    leak_block = await _directive_preserved_leak_protected_block(
        self, workspace_id=workspace_id, message=message
    )
    if leak_block is None:
        return None
    # Preserve the resume ORIGIN exactly as the net-diff / leak re-block branches.
    block_resume_phase = await self._resolve_block_resume_phase(workspace_id)
    reblock_resume_phase = monitor_protected_scope_resume_origin(block_resume_phase)
    reblock_result = cast(
        _GitPushResult,
        await self._pause_monitor_for_protected_scope_block(
            workspace_id=workspace_id,
            pr_number=pr_number,
            pr_head_sha=pr_head_sha,
            protected_scope_block=leak_block,
            worktree_path=worktree_path,
            resume_phase=reblock_resume_phase,
            state=state,
            remote_branch=remote_branch,
            base_branch=base_branch or "",
            operation_id=operation_id,
            operation_type=operation_type,
            source_head_sha=operation_start_head,
            repo=repo,
        ),
    )
    if reblock_result.paused_into_blocked:
        # Mirror the leak / net-diff re-block branches: the bumped block epoch
        # supersedes this hint, so clear the in-memory monitor hint and let the
        # resume re-arm a fresh one.
        state.pending_operator_hint = None
    return reblock_result


async def _clear_dropped_preserved_marker_after_terminal_directive(
    self: Any,
    *,
    workspace_id: str,
    worktree_path: Path,
    state: MonitorState,
    hint: OperatorHint,
    active_grant_specs: Any,
    preserved_head_sha: str | None,
) -> None:
    """Drop the preserved-head marker when a directive-only resume RESET the preserved
    commit away before returning a TERMINAL verdict.

    A directive-only protected-block resume can resolve the block by dropping the
    preserved offending commit (resetting the worktree back to the remote PR head) and
    then return a TERMINAL verdict (``agent_failed`` / ``needs_human`` / ``defer`` /
    ``false_positive``) BEFORE any push. A terminal verdict never finalizes, so the
    preserved-head marker — recorded at block time — survives in monitor state even
    though the commit it pointed at is gone. Because the drop leaves the local HEAD
    already contained in the remote, a LATER operator guide carrying a FRESH directive
    and no grants would satisfy the directive-drop restart shortcut at the top of
    ``_run_operator_hint_cycle``: that shortcut only checks the local HEAD is contained
    in the remote, NOT that the pending operation is the one that ran the drop, so it
    would finalize the new guide as a no-op and SKIP the CLI — silently ignoring the
    follow-up instruction (PRRT_kwDOSJAM6s6KVCYE). Once the preserved commit is dropped
    there is nothing left to protect, so clear the marker; a genuinely new protected
    change re-records a fresh marker via ``_protected_scope_push_block``.

    Scoped to directive-only resumes (``hint.directive`` AND no active grants): a
    grant-bearing terminal resume is handled by ``_terminal_directive_grant_reblock``
    (it re-blocks while the preserved commit is reachable, and clears the marker before
    parking when the commit was dropped — PRRT_kwDOSJAM6s6KVgwV), and the
    two restart shortcuts both require ``not active_grant_specs`` so a surviving grant
    already excludes them. Clears ONLY when a resolvable local HEAD proves the preserved
    commit is gone from local history: an unresolvable HEAD is ambiguous (the drop may
    not have landed), so the marker is RETAINED to keep the leak guard armed for the
    corrected resume — mirroring the shortcut's own HEAD-missing skip
    (PRRT_kwDOSJAM6s6KUx2T).

    The clear is made DURABLE before returning, not left to the outer loop's later
    ``_persist_state``. The block-time RECORDING of the marker is durable as soon as
    the block transition commits (PRRT_kwDOSJAM6s6KEtU6); the clear must be symmetric.
    The terminal verdict that drove this drop is marked in memory by the caller AFTER
    this returns and persisted only at the end of the loop cycle, so a crash in that
    window would otherwise reload the pending directive with the STALE marker — and the
    directive-drop restart shortcut at the top of ``_run_operator_hint_cycle`` (which
    only checks the local HEAD is on the remote) would finalize the new hint as a no-op,
    SKIP the CLI, and swallow the terminal verdict (PRRT_kwDOSJAM6s6KWAIx). Removing the
    key durably leaves NO marker after a crash, so the shortcut is skipped and the
    directive CLI re-runs behind the normal guards and re-derives the verdict."""
    if not (hint.directive and preserved_head_sha) or active_grant_specs:
        return
    local_head_sha = await self._rev_parse_head(worktree_path)
    if not local_head_sha:
        return
    if await self._preserved_commit_reachable_from_head(
        worktree_path=worktree_path,
        preserved_head_sha=preserved_head_sha,
    ):
        return
    state.threads_addressed_ids.pop(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, None)
    await self._clear_preserved_head_marker_durably(workspace_id)
