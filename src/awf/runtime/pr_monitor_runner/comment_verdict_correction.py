"""Correction-path helpers for the comment verdict protocol (#925).

Kept separate so ``comment_verdict`` stays under the first-party line budget;
re-exported from ``comment_verdict`` for callers and tests.

One policy lives here, scoped to the retry that follows an explicit
``AGENT_FIXED_WITHOUT_EVIDENCE`` rejection:

* **No rollback on a self-citing non-fix** — the correction prompt puts the
  item's own attempt-0 commit at HEAD, so an agent can answer ``FALSE POSITIVE:
  already addressed by commit <its own sha>``. Accepting that as a non-fix and
  rolling the commit back discards the change and strands the review thread
  (issue #925, observed six times on PR #922). Keep the commit; accept
  ``fix_committed`` only when item-scoped related-line evidence already exists
  (near-anchor / callee), otherwise escalate to ``needs_human``. Path membership
  alone is not item-scoped FIXED evidence (issue:5558086911).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from awf.common.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner
    from awf.runtime.pr_monitor_runner.comment_verdict import VerdictResult

_log = get_logger(__name__)

# A non-FIXED correction verdict whose reason cites the commit this item just
# made. Never a rollback trigger; either the fix is kept as FIXED (item-scoped
# related-line evidence) or the item escalates to ``needs_human`` with the
# commit preserved.
AGENT_NON_FIX_CITES_OWN_COMMIT = "AGENT_NON_FIX_CITES_OWN_COMMIT"

# Abbreviated-or-full commit references. Seven hex chars is Git's own minimum
# abbreviation, which keeps ordinary prose ("fix 1234ab") from matching.
_COMMIT_REFERENCE = re.compile(r"[0-9a-fA-F]{7,40}")

_MAX_CORRECTION_REASON_LENGTH = 500

# Upper bound on the attempt-0 commit range listing. An item attempt makes one
# or two commits (agent self-commit, dirty-worktree sink); the cap only stops a
# pathological range from producing unbounded output.
_MAX_ATTEMPT_COMMITS = 50


def verdict_reason_cites_own_commit(
    reason: str | None,
    *,
    attempt_tip: str | None,
    item_start_head: str | None,
    attempt_commits: Sequence[str] | None = None,
) -> bool:
    """True when ``reason`` names a commit attempt 0 made for this item.

    ``attempt_tip`` is the HEAD verified after attempt 0. When it is unknown, or
    equals ``item_start_head`` (attempt 0 never advanced HEAD), there is no own
    commit to cite and today's behaviour stands. Citing ``item_start_head`` — a
    genuinely earlier commit — is deliberately not self-citation.

    ``attempt_commits`` carries the rest of the ``item_start_head``..tip range
    (PRRT_kwDOSJAM6s6fmmKY): an attempt can leave an agent-authored fix commit
    *under* the dirty-worktree sink commit, and citing that earlier own commit
    is still self-citation.
    """
    if not reason or not attempt_tip:
        return False
    tip = attempt_tip.lower()
    if item_start_head is not None and tip == item_start_head.lower():
        return False
    start = item_start_head.lower() if item_start_head is not None else None
    candidates = [tip]
    for commit in attempt_commits or ():
        candidate = commit.strip().lower()
        if candidate and candidate != start and candidate not in candidates:
            candidates.append(candidate)
    for match in _COMMIT_REFERENCE.finditer(reason):
        token = match.group(0).lower()
        if any(
            candidate.startswith(token) or token.startswith(candidate) for candidate in candidates
        ):
            return True
    return False


async def attempt_commit_shas(
    runner: PullRequestMonitorRunner,
    *,
    worktree_path: Path,
    item_start_head: str | None,
    attempt_tip: str | None,
) -> list[str]:
    """Commits the attempt added in ``item_start_head``..``attempt_tip``.

    Returns ``[]`` when the range is empty, unknown, or Git cannot list it. The
    caller always compares against ``attempt_tip`` itself, so a failed listing
    only restores the previous tip-only comparison rather than widening it.
    """
    from awf.runtime.pr_monitor_runner.comment_verdict_residue import (
        _RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS,
    )
    from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _git_env_for_merge_safety_object_lookup,
    )

    if not attempt_tip or not item_start_head:
        return []
    if attempt_tip.lower() == item_start_head.lower():
        return []
    if not worktree_path.exists():
        return []
    try:
        result = await runner._deps.runner.run(
            git_worktree_command(
                worktree_path,
                "rev-list",
                "--first-parent",
                f"--max-count={_MAX_ATTEMPT_COMMITS}",
                f"{item_start_head}..{attempt_tip}",
            ),
            env=_git_env_for_merge_safety_object_lookup(),
            timeout_seconds=_RESIDUE_ORDINARY_GIT_TIMEOUT_SECONDS,
        )
    except (TimeoutError, OSError, RuntimeError) as exc:
        _log.warning(
            "monitor.agent_verdict_attempt_commit_range_unreadable",
            item_start_head=item_start_head,
            attempt_tip=attempt_tip,
            exc_type=type(exc).__name__,
        )
        return []
    if not result.ok:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


async def correction_reason_cites_own_item_commit(
    runner: PullRequestMonitorRunner,
    *,
    reason: str | None,
    worktree_path: Path,
    item_start_head: str | None,
    attempt_tip: str | None,
) -> bool:
    """True when ``reason`` cites any commit this item made (#925 D2).

    Checks the verified tip first — the common shape and the only case that
    needs no Git — then widens to the whole ``item_start_head``..tip range so a
    non-tip agent commit is not mistaken for an earlier, foreign one
    (PRRT_kwDOSJAM6s6fmmKY).
    """
    if not reason or not attempt_tip:
        return False
    if verdict_reason_cites_own_commit(
        reason,
        attempt_tip=attempt_tip,
        item_start_head=item_start_head,
    ):
        return True
    attempt_commits = await attempt_commit_shas(
        runner,
        worktree_path=worktree_path,
        item_start_head=item_start_head,
        attempt_tip=attempt_tip,
    )
    if not attempt_commits:
        return False
    return verdict_reason_cites_own_commit(
        reason,
        attempt_tip=attempt_tip,
        item_start_head=item_start_head,
        attempt_commits=attempt_commits,
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

    With item-scoped related-line FIXED evidence the item is what the agent said
    it was on its first attempt — FIXED — so the commit stays and the thread can
    be resolved. Without it, the commit is still preserved (rolling back a
    change the agent points at as the fix is exactly the #925 defect) and the
    item escalates to ``needs_human`` so the merge gate keeps blocking with a
    reason code. Path membership alone is not enough for ``fix_committed``
    (issue:5558086911).
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
            f"Accepted as FIXED: the correction verdict cited a commit this item "
            f"made (attempt tip {short_tip}), with item-scoped related-line "
            f"evidence. Agent reason: {reason}"
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
