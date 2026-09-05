"""Correction-path helpers for the comment verdict protocol (#925).

Kept separate so ``comment_verdict`` stays under the first-party line budget;
re-exported from ``comment_verdict`` for callers and tests.

Two policies live here, both scoped to the retry that follows an explicit
``AGENT_FIXED_WITHOUT_EVIDENCE`` rejection:

* **Escalating evidence** — attempt 0 keeps the strict line-anchored gate, but
  once that gate has already rejected a FIXED claim, a contentful attempt-0
  commit touching the anchored *path* is accepted as evidence. A legitimate fix
  that lands elsewhere in the reviewed file (a helper above the caller, a guard
  at the call site) is a real fix, not an unsubstantiated claim.
* **No rollback on a self-citing non-fix** — the correction prompt puts the
  item's own attempt-0 commit at HEAD, so an agent can answer ``FALSE POSITIVE:
  already addressed by commit <its own sha>``. Accepting that as a non-fix and
  rolling the commit back discards the fix and strands the review thread
  (issue #925, observed six times on PR #922).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from awf.common.logging import get_logger

if TYPE_CHECKING:
    from awf.runtime.pr_monitor import MonitorState
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner
    from awf.runtime.pr_monitor_runner.comment_verdict import VerdictResult

_log = get_logger(__name__)

# A non-FIXED correction verdict whose reason cites the commit this item just
# made. Never a rollback trigger; either the fix is kept as FIXED (path-level
# evidence) or the item escalates to ``needs_human`` with the commit preserved.
AGENT_NON_FIX_CITES_OWN_COMMIT = "AGENT_NON_FIX_CITES_OWN_COMMIT"

# Abbreviated-or-full commit references. Seven hex chars is Git's own minimum
# abbreviation, which keeps ordinary prose ("fix 1234ab") from matching.
_COMMIT_REFERENCE = re.compile(r"[0-9a-fA-F]{7,40}")

_MAX_CORRECTION_REASON_LENGTH = 500


def verdict_reason_cites_own_commit(
    reason: str | None,
    *,
    attempt_tip: str | None,
    item_start_head: str | None,
) -> bool:
    """True when ``reason`` names the commit attempt 0 made for this item.

    ``attempt_tip`` is the HEAD verified after attempt 0. When it is unknown, or
    equals ``item_start_head`` (attempt 0 never advanced HEAD), there is no own
    commit to cite and today's behaviour stands. Citing ``item_start_head`` — a
    genuinely earlier commit — is deliberately not self-citation.
    """
    if not reason or not attempt_tip:
        return False
    tip = attempt_tip.lower()
    if item_start_head is not None and tip == item_start_head.lower():
        return False
    for match in _COMMIT_REFERENCE.finditer(reason):
        token = match.group(0).lower()
        if tip.startswith(token) or token.startswith(tip):
            return True
    return False


async def path_level_item_fix_evidence(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
    item_start_head: str | None,
    item_path: str | None,
    state: MonitorState | None,
    dirty_changes_committed: bool,
) -> bool:
    """Re-run the FIXED evidence check without the line anchor (#925 D1).

    Resolved through ``comment_verdict`` at call time, like the rest of the
    split-out verdict helpers, so monkeypatches on that module still reach the
    escalated check as well as the line-anchored one.
    """
    from awf.runtime.pr_monitor_runner import comment_verdict

    return await comment_verdict._item_fix_evidence(
        runner,
        worktree_path=worktree_path,
        item_start_head=item_start_head,
        item_path=item_path,
        item_line=None,
        state=state,
        dirty_changes_committed=dirty_changes_committed,
    )


def correction_self_citation_outcome(
    *,
    workspace_id: str,
    verdict: str,
    reason: str | None,
    attempt_tip: str | None,
    has_path_evidence: bool,
) -> VerdictResult:
    """Disposition for a self-citing non-FIXED correction verdict (#925 D2).

    With path-level evidence the item is what the agent said it was on its first
    attempt — FIXED — so the commit stays and the thread can be resolved. Without
    it, the commit is still preserved (rolling back a change the agent points at
    as the fix is exactly the #925 defect) and the item escalates to
    ``needs_human`` so the merge gate keeps blocking with a reason code.
    """
    from awf.runtime.pr_monitor_runner.comment_verdict import VerdictResult

    short_tip = (attempt_tip or "")[:12]
    _log.warning(
        "monitor.agent_verdict_correction_cites_own_commit",
        workspace_id=workspace_id,
        reason_code=AGENT_NON_FIX_CITES_OWN_COMMIT,
        verdict=verdict,
        attempt_tip=attempt_tip,
        has_path_evidence=has_path_evidence,
    )
    if has_path_evidence:
        outcome = (
            f"Accepted as FIXED: the correction verdict cited this item's own "
            f"commit {short_tip}, which changes the reviewed file. "
            f"Agent reason: {reason}"
        )
        return VerdictResult(verdict="fix_committed", reason=_bounded(outcome))
    outcome = (
        f"Correction verdict cited this item's own commit {short_tip} without "
        f"item-scoped fix evidence; the commit is preserved for human review. "
        f"Agent reason: {reason}"
    )
    return VerdictResult(verdict="needs_human", reason=_bounded(outcome))


def _bounded(reason: str) -> str:
    if len(reason) <= _MAX_CORRECTION_REASON_LENGTH:
        return reason
    return f"{reason[: _MAX_CORRECTION_REASON_LENGTH - 1].rstrip()}…"
