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
* **No workspace failure once a rollback succeeded** — a correction that still
  ends unaccepted parks the item as ``needs_human`` instead of raising. A
  contentful commit is kept, as in ``correction_unscoped_fix_outcome``.
  Otherwise the residue is rolled back first, as in
  ``resolve_correction_non_fixed_mutation`` (#1020). The batch then reaches its
  end-of-batch push. Only a rollback that itself fails still raises.

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
    included, blocks inheritance. Everything else re-raises the strict error.
    """
    from awf.runtime.pr_monitor_runner import helpers
    from awf.runtime.pr_monitor_runner.helpers_verdict import (
        _count_exact_verdict_records,
        _last_valid_verdict_record,
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
        if inherited is not None and _count_exact_verdict_records(stdout) == 0:
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
    (PRRT_kwDOSJAM6s6fpjBu). So on failure, roll back first and then re-raise,
    so reason-coded causes reach ``fix_cycle`` unmasked. A rollback that itself
    fails raises the typed protocol error, chained to the probe failure.
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
        raise


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
    reason code. With no such commit, the residue is rolled back to the floor,
    just as the raise did before. Either way the item returns ``needs_human``.
    ``_sync_needs_human_reason`` persists the reason on the thread, the merge
    gate keeps blocking, ``decide()`` answers ``NotifyHuman``, and the batch's
    accepted commits reach the remote through the ordinary end-of-batch push. A
    rollback that fails still raises, unchanged: the worktree state is then
    unknown.
    """
    from awf.runtime.pr_monitor_runner import comment_verdict as _comment_verdict
    from awf.runtime.pr_monitor_runner.comment_verdict_correction import _bounded

    reason_code = protocol_error.reason_code
    if await guarded_unscoped_fix_evidence(
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
        preserved_tip = await _comment_verdict.preserved_correction_tip(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            rev_parse_head=rev_parse_head,
            fallback=fallback_tip,
        )
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
        )
    rollback_ok = await _comment_verdict._rollback_or_classify_failure(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        item_start_head=rollback_floor_head,
        item_start_last_push_sha=item_start_last_push_sha,
        state=state,
    )
    if not rollback_ok:
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
            f"{(rollback_floor_head or '')[:12] or '<unknown>'}; the item is parked "
            f"for human review."
        ),
    )
