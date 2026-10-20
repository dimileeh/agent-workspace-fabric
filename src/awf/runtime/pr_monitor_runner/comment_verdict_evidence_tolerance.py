"""Evidence decides, not stdout formatting, for the comment verdict protocol (#1074).

Kept separate so ``comment_verdict`` stays under the first-party line budget;
re-exported from ``comment_verdict`` for callers and tests.

The strict record contract (``helpers_verdict._parse_verdict_result``: the record
is the final non-empty line and the only record) is still what agents are told
and still the first check. It stopped being the *only* check, because agents
keep talking after a valid record. In ws_6fc04496 an operator-hint pass printed
one valid ``FIXED`` record and then a test-result paragraph, twice. In
ws_a0f7fcd8 a correction attempt committed the real fix but printed no record at
all. Both times AWF rolled back verified work and failed the workspace, which
also stranded the batch's accepted commits. The rule here: evidence decides, and
AWF never accepts a verdict it has no evidence to check.

* **Extra output around a FIXED record** — when the strict parse fails, the last
  valid record is accepted only if it is ``FIXED`` *and* the attempt's own
  item-scoped evidence gate passed. Non-FIXED records cannot be checked against
  evidence, so they keep the strict outcome. Operator hints call the protocol
  with ``require_fix_evidence=False`` because a hint may be a no-code directive,
  so for a commit-less hint ``FIXED`` the strict format is the only guard. The
  gate here therefore still needs a contentful commit since the hint started.
* **Record-less correction** — when attempt 0's strictly parsed ``FIXED`` was
  refused only for ``AGENT_FIXED_WITHOUT_EVIDENCE`` and the correction prints no
  exact record, the correction inherits that ``FIXED``. The caller's existing
  correction gates (line, path, package, callee; keep-and-escalate for
  contentful-but-unscoped work) then judge it.
* **The agent's own word still counts** — a decorated, indented or wrong-case
  ``NEEDS_HUMAN`` / ``DEFER`` / ``FALSE POSITIVE`` is never a record, but
  after a ``FIXED`` (or anywhere on a record-less correction) it overrules
  that ``FIXED``. Evidence must never turn a request for a human into a fix.
* **No workspace failure once a rollback succeeded** — a correction that still
  ends unaccepted parks the item as ``needs_human`` instead of raising. A
  contentful commit is kept, as in ``correction_unscoped_fix_outcome``, and
  edits left uncommitted above it are cleaned so the next item cannot absorb
  them. Otherwise the residue is rolled back first, as in
  ``resolve_correction_non_fixed_mutation`` (#1020). The batch then reaches its
  end-of-batch push; an operator hint pushes a kept commit through its own
  guarded push and stays parked. A rollback that itself fails still raises, and
  so does an item with no known floor or no local worktree, where no rollback
  could run. A no-commit rollback without a saved item-start Git config baseline
  raises too: restore would report success without rewriting config
  (PRRT_kwDOSJAM6s6uXI6H). An unexpected error after a rollback that did restore
  that baseline parks the item (``comment_verdict_item_unexpected_error``,
  #1076).

Sibling modules (``helpers_verdict``, ``helpers``, ``comment_verdict``) are
resolved at call time. ``comment_verdict`` imports this module at load time, and
callers and tests monkeypatch the rollback and evidence helpers on
``comment_verdict``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from awf.common.logging import get_logger
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    AGENT_FIXED_WITHOUT_EVIDENCE,
    AGENT_VERDICT_PROTOCOL_VIOLATION,
    AgentVerdictProtocolError,
    VerdictResult,
)

if TYPE_CHECKING:
    from awf.runtime.pr_monitor import MonitorState
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

_log = get_logger(__name__)

_PARK_ROLLBACK_FAILURE_MESSAGE = (
    "Could not roll back unaccepted edits before terminating after protocol violation."
)


def parse_verdict_with_fix_evidence(
    stdout: str,
    *,
    workspace_id: str,
    fix_evidence: bool,
    inherited: VerdictResult | None,
) -> VerdictResult:
    """Decode a verdict strictly, then fall back only where evidence can decide.

    ``fix_evidence`` is the attempt's own item-scoped evidence gate, as already
    computed by the caller. ``inherited`` is attempt 0's evidence-refused
    ``FIXED``, passed only on the correction attempt. A tolerated ``FIXED`` wins
    over inheritance. Any exact record line on the correction, placeholder
    included, blocks inheritance, and so does any non-FIXED mention. Everything
    else re-raises the strict error.
    """
    from awf.runtime.pr_monitor_runner import helpers
    from awf.runtime.pr_monitor_runner.helpers_verdict import (
        _count_exact_verdict_records,
        _last_valid_verdict_record,
        _mentions_non_fixed_verdict,
    )

    try:
        return helpers._parse_verdict_result(stdout)
    except AgentVerdictProtocolError as strict_error:
        record = _last_valid_verdict_record(stdout)
        if record is not None and record.verdict == "fix_committed" and fix_evidence:
            _log.warning(
                "monitor.agent_verdict_trailing_output_accepted",
                workspace_id=workspace_id,
                reason_code=strict_error.reason_code,
                verdict=record.verdict,
            )
            return record
        if (
            inherited is not None
            and _count_exact_verdict_records(stdout) == 0
            and not _mentions_non_fixed_verdict(stdout)
        ):
            _log.warning(
                "monitor.agent_verdict_correction_inherited_fixed",
                workspace_id=workspace_id,
                reason_code=AGENT_FIXED_WITHOUT_EVIDENCE,
                verdict=inherited.verdict,
            )
            return inherited
        raise


async def guarded_unscoped_fix_evidence(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
    item_start_head: str | None,
    rollback_floor_head: str | None,
    item_start_last_push_sha: str | None,
    state: MonitorState | None,
    dirty_changes_committed: bool,
    protocol_attempt: int,
    rollback_failure_message: str,
) -> bool:
    """Ask "did this item commit anything contentful at all?" without stranding edits.

    This is an anchor-free probe, never "is that commit the fix". It only decides
    between keeping the commit and discarding it. Path membership still cannot
    buy ``fix_committed`` (issue:5558086911).

    It re-runs Git ancestry and tree checks after the commit-sink evidence
    handler has ended. Unguarded, an ordinary rev-parse, ancestry or repository
    failure would escape without rollback or reason-code classification, and it
    would strand the unaccepted correction commit in the worktree
    (PRRT_kwDOSJAM6s6fpjBu). So on failure, roll back first. A cause with a
    dedicated upstream handler is then re-raised unchanged, so its reason code
    still reaches that handler; any other cause parks the item (#1076). A
    rollback that itself fails raises the typed protocol error, chained to the
    probe failure.
    """
    from awf.runtime.pr_monitor_runner import comment_verdict as _comment_verdict

    try:
        return await _comment_verdict._item_fix_evidence(
            runner,
            worktree_path=worktree_path,
            item_start_head=item_start_head,
            item_path=None,
            item_line=None,
            state=state,
            dirty_changes_committed=dirty_changes_committed,
        )
    except Exception as unscoped_exc:
        rollback_ok = await _comment_verdict._rollback_or_classify_failure(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            item_start_head=rollback_floor_head,
            item_start_last_push_sha=item_start_last_push_sha,
            state=state,
        )
        if not rollback_ok:
            _log.warning(
                "monitor.agent_verdict_unscoped_evidence_rollback_failed",
                workspace_id=workspace_id,
                item_start_head=item_start_head,
                protocol_attempt=protocol_attempt,
                exc_type=type(unscoped_exc).__name__,
            )
            raise AgentVerdictProtocolError(
                reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
                message=rollback_failure_message,
            ) from unscoped_exc
        _comment_verdict.reraise_or_park_item(
            unscoped_exc, "unscoped_fix_evidence_probe", rollback_floor_head, worktree_path
        )


async def park_unaccepted_correction(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
    item_start_head: str | None,
    rollback_floor_head: str | None,
    item_start_last_push_sha: str | None,
    state: MonitorState | None,
    protocol_error: AgentVerdictProtocolError,
    dirty_changes_committed: bool,
    rev_parse_head: object,
    fallback_tip: str | None,
) -> VerdictResult:
    """Park an item whose correction attempt still ended without an accepted verdict.

    This replaces the raise that failed the workspace. A contentful commit in the
    item's own range is kept, and the reason cites the preserved tip and the
    reason code; uncommitted edits above it are cleaned first. With no such
    commit, the residue is rolled back to the floor, just as the raise did
    before. Either way the item returns ``needs_human``.
    ``_sync_needs_human_reason`` persists the reason on the thread, the merge
    gate keeps blocking, ``decide()`` answers ``NotifyHuman``, and the batch's
    accepted commits reach the remote through the ordinary end-of-batch push. A
    kept commit is flagged ``kept_commit``, so an operator hint, which returns
    before its push for other parked verdicts, pushes it too and stays parked. A
    rollback that fails still raises, unchanged: the worktree state is then
    unknown. With no known floor or no local worktree nothing can be measured or
    rolled back, so ``protocol_error`` is raised exactly as before. A no-commit
    rollback without a saved item-start Git config baseline raises the same
    typed terminal error (PRRT_kwDOSJAM6s6uXI6H).
    """
    from awf.runtime.pr_monitor_runner import comment_verdict as _comment_verdict
    from awf.runtime.pr_monitor_runner.comment_verdict_correction import _bounded
    from awf.runtime.pr_monitor_runner.comment_verdict_residue_fingerprint import (
        item_start_has_local_git_config_snapshot,
    )

    reason_code = protocol_error.reason_code
    if rollback_floor_head is None or not worktree_path.exists():
        _log.warning(
            "monitor.agent_verdict_correction_unaccepted_unmeasurable",
            workspace_id=workspace_id,
            reason_code=reason_code,
            rollback_floor_head=rollback_floor_head,
        )
        raise protocol_error
    if await _comment_verdict.guarded_unscoped_fix_evidence(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        item_start_head=item_start_head,
        rollback_floor_head=rollback_floor_head,
        item_start_last_push_sha=item_start_last_push_sha,
        state=state,
        dirty_changes_committed=dirty_changes_committed,
        protocol_attempt=1,
        rollback_failure_message=_PARK_ROLLBACK_FAILURE_MESSAGE,
    ):
        live_tip = await _comment_verdict.preserved_correction_tip(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            rev_parse_head=rev_parse_head,
            fallback=None,
        )
        await _clean_edits_above_kept_commit(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            kept_tip=live_tip,
            reason_code=reason_code,
            rev_parse_head=rev_parse_head,
        )
        preserved_tip = live_tip or fallback_tip
        _log.warning(
            "monitor.agent_verdict_correction_unaccepted_commit_preserved",
            workspace_id=workspace_id,
            reason_code=reason_code,
            attempt_tip=preserved_tip,
        )
        return VerdictResult(
            verdict="needs_human",
            reason=_bounded(
                f"{reason_code}: {protocol_error} The correction attempt ended "
                f"without an accepted AWF-VERDICT; this item's commit (attempt tip "
                f"{(preserved_tip or '')[:12] or '<unknown>'}) is preserved for "
                f"human review."
            ),
            kept_commit=True,
        )
    # Without a saved baseline the item-start Git config restore is a no-op that
    # still reports success, so a True rollback proves nothing and must not park
    # (PRRT_kwDOSJAM6s6uXI6H). Same gate as the kept-commit clean
    # (PRRT_kwDOSJAM6s6uPByi).
    has_git_config_baseline = item_start_has_local_git_config_snapshot(worktree_path)
    rollback_ok = await _comment_verdict._rollback_or_classify_failure(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        item_start_head=rollback_floor_head,
        item_start_last_push_sha=item_start_last_push_sha,
        state=state,
    )
    if not has_git_config_baseline:
        _log.warning(
            "monitor.agent_verdict_correction_unaccepted_rollback_without_git_config_baseline",
            workspace_id=workspace_id,
            reason_code=reason_code,
            rollback_floor_head=rollback_floor_head,
        )
    if not rollback_ok or not has_git_config_baseline:
        raise AgentVerdictProtocolError(
            reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
            message=_PARK_ROLLBACK_FAILURE_MESSAGE,
        )
    _log.warning(
        "monitor.agent_verdict_correction_unaccepted_item_parked",
        workspace_id=workspace_id,
        reason_code=reason_code,
        rollback_floor_head=rollback_floor_head,
    )
    return VerdictResult(
        verdict="needs_human",
        reason=_bounded(
            f"{reason_code}: {protocol_error} The correction attempt ended without "
            f"an accepted AWF-VERDICT; its edits were rolled back to "
            f"{rollback_floor_head[:12]}; the item is parked "
            f"for human review."
        ),
    )


async def _clean_edits_above_kept_commit(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
    kept_tip: str | None,
    reason_code: str,
    rev_parse_head: object,
) -> None:
    """Discard uncommitted edits left above a kept commit, keeping the commit.

    ``_commit_dirty_worktree`` can return False after ``git add`` or ``git
    commit`` fails, even when the item already made a contentful commit. The
    batch starts the next item at this HEAD, so those edits would be absorbed by
    that item's agent or commit sink and counted as its fix
    (PRRT_kwDOSJAM6s6uOQn5). They are unaccepted residue, so they go, as on every
    other unaccepted path. The rollback targets the live kept tip with no
    ``state``: HEAD does not move, and neither the hosted remote nor push
    tracking is rewound past the kept commit. An unreadable or raising residue
    probe fails closed into the clean (PRRT_kwDOSJAM6s6uOogH). With no path
    residue, the item-start Git config is still restored (PRRT_kwDOSJAM6s6uO5Xk),
    and HEAD is re-read after it, because restored linkage can retarget HEAD
    (PRRT_kwDOSJAM6s6uPDnk). Both the clean and that restore need a saved
    item-start baseline (PRRT_kwDOSJAM6s6uPByi). An unreadable tip, a failed
    clean, a missing baseline, a failed config restore or a HEAD that left the
    kept tip raises the typed terminal error, because the worktree state is then
    unknown.
    """
    from awf.runtime.pr_monitor_runner import comment_verdict as _comment_verdict
    from awf.runtime.pr_monitor_runner.comment_verdict_residue_fingerprint import (
        _fingerprint_has_pr_worthy_path_residue,
        item_start_has_local_git_config_snapshot,
        restore_item_start_local_git_configs,
    )

    residue_fp: str | None
    try:
        residue_fp = await _comment_verdict._read_correction_pr_worthy_residue_fingerprint(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
        )
    except Exception as probe_exc:
        # Broad on purpose, like every other residue probe on this path: it
        # spawns Git and can raise outside the git-spawn error set. Escaping here
        # would leave a kept commit as an untyped failure instead of a park.
        # ``asyncio.CancelledError`` is a ``BaseException`` and still propagates.
        _log.warning(
            "monitor.agent_verdict_correction_unaccepted_leftover_residue_probe_failed",
            workspace_id=workspace_id,
            reason_code=reason_code,
            exc_type=type(probe_exc).__name__,
        )
        residue_fp = None
    # Without a saved baseline the item-start Git config restore is a no-op that
    # still reports success, on its own and inside the clean, so it proves
    # nothing and fails closed (PRRT_kwDOSJAM6s6uPByi).
    has_git_config_baseline = item_start_has_local_git_config_snapshot(worktree_path)
    if residue_fp is not None and not _fingerprint_has_pr_worthy_path_residue(residue_fp):
        # Config-only residue is just a ``git-meta:`` line, which the path check
        # ignores, so no clean runs. Restore the item-start Git config anyway so
        # ``include.path`` or ``url.*.insteadOf`` cannot reach the next item or
        # the push (PRRT_kwDOSJAM6s6uO5Xk). The restore also rewrites the
        # gitfile, ``commondir`` and nested linkage, so HEAD can then resolve in
        # a git-dir without the kept commit. Re-read it under the same lock, as
        # the rollback does, and keep the commit only while HEAD is still the
        # kept tip (PRRT_kwDOSJAM6s6uPDnk).
        restored_tip: str | None = None
        async with _comment_verdict.hold_exclusive_worktree_writer_lock(worktree_path):
            if has_git_config_baseline and restore_item_start_local_git_configs(worktree_path):
                restored_tip = await _comment_verdict.preserved_correction_tip(
                    runner,
                    workspace_id=workspace_id,
                    worktree_path=worktree_path,
                    rev_parse_head=rev_parse_head,
                    fallback=None,
                )
        if kept_tip is not None and restored_tip is not None:
            if restored_tip.lower() == kept_tip.lower():
                return
            _log.warning(
                "monitor.agent_verdict_correction_unaccepted_git_config_restore_moved_head",
                workspace_id=workspace_id,
                reason_code=reason_code,
                attempt_tip=kept_tip,
                live_head=restored_tip,
            )
    elif (
        kept_tip is not None
        and has_git_config_baseline
        and await _comment_verdict._rollback_or_classify_failure(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            item_start_head=kept_tip,
            state=None,
        )
    ):
        _log.warning(
            "monitor.agent_verdict_correction_unaccepted_leftover_edits_cleaned",
            workspace_id=workspace_id,
            reason_code=reason_code,
            attempt_tip=kept_tip,
        )
        return
    _log.warning(
        "monitor.agent_verdict_correction_unaccepted_leftover_edits_clean_failed",
        workspace_id=workspace_id,
        reason_code=reason_code,
        attempt_tip=kept_tip,
    )
    raise AgentVerdictProtocolError(
        reason_code=AGENT_VERDICT_PROTOCOL_VIOLATION,
        message=_PARK_ROLLBACK_FAILURE_MESSAGE,
    )
