"""Correction-attempt mutation gates for the comment verdict protocol.

Kept separate so ``comment_verdict`` stays under the first-party line budget;
re-exported from ``comment_verdict`` for callers and tests.

Both helpers here serve the same rule: a *correction* attempt may only be
credited with a non-FIXED verdict when AWF can prove that attempt did not
mutate the worktree. That needs an end-of-attempt HEAD (``read_correction_end_head``);
when the measurement says the attempt did mutate, the verdict is refused after a
safe rollback (``resolve_correction_non_fixed_mutation``). Both fail closed: an
unreadable HEAD is a refusal, never an assumed-clean acceptance.

One mutating shape is not a non-fix at all (#1105): a verdict that cites a
commit in this item's own range is a FIXED claim backed by that commit.
``keep_self_cited_item_commit`` keeps it, rolls back only what came after it,
and judges it by the ordinary evidence chain before the mutation park is
considered.

Refusing the verdict is not the same as failing the workspace. Once the rollback
has succeeded, the offending *item* is parked as ``needs_human`` with its reason
code preserved — the #925/#928 escalation shape — and the comment-repair batch
carries on to its end-of-batch push, so the items already accepted before it are
published instead of stranded in the worktree (#1020). Only a rollback that
itself fails still terminates: the worktree state is then unknown, and failing
closed is the only safe answer.

The rollback helpers and the trusted HEAD probe are resolved through
``comment_verdict`` at call time so monkeypatches on that module (and the
``comments`` forwarding shim) still apply.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from awf.common.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from awf.runtime.pr_monitor import MonitorState
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner
    from awf.runtime.pr_monitor_runner.comment_verdict import VerdictResult

_log = get_logger(__name__)


async def read_correction_end_head(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
    rev_parse_head: Any,
    attempt_start_head: str,
    item_start_head: str | None,
    rollback_floor_head: str | None,
    item_start_last_push_sha: str | None,
    state: MonitorState | None,
    protocol_attempt: int,
    verdict: str,
) -> str:
    """Read the correction attempt's end HEAD, or refuse the non-FIXED verdict.

    Returns ``attempt_start_head`` unchanged when there is no worktree to probe.
    Otherwise the live HEAD is read through the trusted item-start Git config +
    timeout so an ``include.path`` → FIFO cannot hang the worker
    (PRRT_kwDOSJAM6s6e4egQ), and both failure shapes roll back first:

    * an ordinary probe failure (``OSError``/``RuntimeError`` while spawning Git)
      is outside the caller's ``Exception`` handlers, whose surrounding handler
      catches only ``CancelledError``, so the correction attempt's edits would be
      stranded (PRRT_kwDOSJAM6s6eJ2Tg, matching PRRT_kwDOSJAM6s6eJUbE). After
      that rollback, a cause without a dedicated upstream handler parks the item
      instead of failing the workspace (#1076);
    * a transient ``None`` must not leave the end HEAD equal to
      ``attempt_start_head``: a clean self-commit would then miss mutation and a
      later successful rollback could accept FALSE POSITIVE / DEFER /
      NEEDS_HUMAN (PRRT_kwDOSJAM6s6eIz5m).
    """
    from awf.runtime.pr_monitor_runner import comment_verdict as _comment_verdict

    if not worktree_path.exists():
        return attempt_start_head
    try:
        live_head = await _comment_verdict.read_protocol_attempt_start_head(
            runner,
            worktree_path=worktree_path,
            rev_parse_head=(rev_parse_head if callable(rev_parse_head) else None),
        )
    except Exception as end_head_exc:
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
                "monitor.agent_verdict_correction_end_head_rollback_failed",
                workspace_id=workspace_id,
                item_start_head=item_start_head,
                protocol_attempt=protocol_attempt,
                exc_type=type(end_head_exc).__name__,
            )
            raise _comment_verdict.AgentVerdictProtocolError(
                reason_code=_comment_verdict.AGENT_VERDICT_PROTOCOL_VIOLATION,
                message=(
                    "Could not roll back unaccepted edits after correction-end HEAD probe failure."
                ),
            ) from end_head_exc
        _comment_verdict.reraise_or_park_item(
            end_head_exc, "correction_end_head_probe", rollback_floor_head, worktree_path
        )
    if live_head:
        return live_head
    _log.warning(
        "monitor.agent_verdict_correction_end_head_unreadable",
        workspace_id=workspace_id,
        reason_code=_comment_verdict.AGENT_VERDICT_PROTOCOL_VIOLATION,
        protocol_attempt=protocol_attempt,
        attempt_start_head=attempt_start_head,
        verdict=verdict,
    )
    rollback_ok = await _comment_verdict._rollback_unaccepted_protocol_retry_changes(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        item_start_head=rollback_floor_head,
        item_start_last_push_sha=item_start_last_push_sha,
        state=state,
    )
    if not rollback_ok:
        raise _comment_verdict.AgentVerdictProtocolError(
            reason_code=_comment_verdict.AGENT_VERDICT_PROTOCOL_VIOLATION,
            message=(
                "Could not roll back unaccepted edits after "
                "correction attempt with unreadable end HEAD."
            ),
        )
    raise _comment_verdict.AgentVerdictProtocolError(
        reason_code=_comment_verdict.AGENT_VERDICT_PROTOCOL_VIOLATION,
        message=(
            "Correction attempt end HEAD was unreadable; cannot accept a "
            "non-FIXED verdict without measuring whether the worktree advanced."
        ),
    )


async def keep_self_cited_item_commit(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
    item_start_head: str | None,
    rollback_floor_head: str | None,
    item_start_last_push_sha: str | None,
    state: MonitorState | None,
    post_attempt_head: str | None,
    known_item_heads: Sequence[str | None],
    verdict: str,
    reason: str | None,
    item_path: str | None,
    item_line: int | None,
    comment_text: str | None,
    anchor_head: str | None,
) -> VerdictResult | None:
    """Keep the item commit a mutating non-FIXED correction cites (#1105).

    "Already addressed by earlier commit X", where X lies in this item's own
    ``item_start_head``..HEAD range — attempt 0's commit, or one the correction
    itself made — is a FIXED claim backed by X, not a non-fix. Rolling the whole
    item back discarded correct fixes. Instead, reset to the newest cited item
    commit (never below the #934 floor), which drops only the uncited later
    commits — AWF's sink of the correction's edits included — and uncommitted
    residue, then judge what is kept by the unchanged evidence chain:
    ``fix_committed`` with evidence, otherwise ``needs_human`` with the commit
    preserved (#928).

    Returns ``None`` — the caller's ordinary mutation park — when nothing in the
    item's range is cited (a pre-existing or foreign commit, or no citation),
    when the floor ancestry probe fails, when the cited commit does not descend
    from the item start (a merged side branch), or when a hosted remote already
    advanced: the rollback would restore the item-start push state while the
    remote sits at the kept commit. Cited commits on two merge sides keep only
    the newest one's ancestry. A failed rollback raises the mutation reason code.
    """
    from awf.runtime.pr_monitor_runner import comment_verdict as _comment_verdict
    from awf.runtime.pr_monitor_runner.comment_verdict_correction import (
        AGENT_NON_FIX_CITES_OWN_COMMIT,
        attempt_commit_shas,
        cited_item_commit,
        correction_item_fix_evidence,
        correction_self_citation_outcome,
    )

    if item_start_head is None or not post_attempt_head or not reason:
        return None
    if state is not None and getattr(runner._deps.adapter, "is_hosted", False):
        saved_push = (item_start_last_push_sha or "").strip().lower()
        if state.hosted_terminal_head_advanced or (
            (state.last_push_sha or "").strip().lower() != saved_push
        ):
            return None
    item_commits = await attempt_commit_shas(
        runner,
        worktree_path=worktree_path,
        item_start_head=item_start_head,
        attempt_tip=post_attempt_head,
    )
    if not item_commits:
        # Same fallback as the tip-only self-citation check: the heads this
        # protocol itself read inside the item, newest first.
        item_commits = [head for head in (post_attempt_head, *known_item_heads) if head]
    cited = cited_item_commit(reason, candidates=item_commits, item_start_head=item_start_head)
    if cited is None:
        return None
    kept_head = cited
    # The full-DAG item range can list a merged side-branch commit that forked
    # before the item start, so the ancestry probe runs even when the floor is
    # the item start (PRRT_kwDOSJAM6s63brGg).
    floor = rollback_floor_head or item_start_head
    if floor.lower() != cited.lower():
        try:
            cited_above_floor = await runner._head_descends_from(
                worktree_path=worktree_path,
                ancestor=floor,
                descendant=cited,
            )
        except (TimeoutError, OSError, RuntimeError) as exc:
            _log.warning(
                "monitor.agent_verdict_correction_self_cite_floor_unreadable",
                workspace_id=workspace_id,
                rollback_floor_head=floor,
                cited_commit=cited,
                exc_type=type(exc).__name__,
            )
            return None
        if not cited_above_floor and floor.lower() == item_start_head.lower():
            # Resetting onto a side-branch commit would discard the item start's
            # history — earlier accepted items' fixes included.
            _log.warning(
                "monitor.agent_verdict_correction_self_cite_off_item_start",
                workspace_id=workspace_id,
                item_start_head=item_start_head,
                cited_commit=cited,
            )
            return None
        if not cited_above_floor:
            # #934: the preserved floor already contains the cited commit.
            kept_head = floor
    _log.warning(
        "monitor.agent_verdict_correction_self_cite_partial_rollback",
        workspace_id=workspace_id,
        reason_code=AGENT_NON_FIX_CITES_OWN_COMMIT,
        verdict=verdict,
        cited_commit=cited,
        kept_head=kept_head,
        rolled_back_from=post_attempt_head,
    )
    rollback_ok = await _comment_verdict._rollback_or_classify_failure(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        item_start_head=kept_head,
        item_start_last_push_sha=item_start_last_push_sha,
        state=state,
    )
    if not rollback_ok:
        raise _comment_verdict.AgentVerdictProtocolError(
            reason_code=_comment_verdict.AGENT_NON_FIXED_WITH_MUTATION,
            message=(
                "Could not roll back the correction attempt's uncited edits after "
                "it cited this item's own commit."
            ),
        )
    has_evidence = await correction_item_fix_evidence(
        runner,
        protocol_attempt=1,
        worktree_path=worktree_path,
        item_start_head=item_start_head,
        item_path=item_path,
        item_line=item_line,
        state=state,
        dirty_changes_committed=False,
        comment_text=comment_text,
        anchor_head=anchor_head,
    )
    return correction_self_citation_outcome(
        workspace_id=workspace_id,
        verdict=verdict,
        reason=reason,
        attempt_tip=kept_head,
        has_path_evidence=has_evidence,
    )


async def resolve_correction_non_fixed_mutation(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
    rollback_floor_head: str | None,
    item_start_last_push_sha: str | None,
    state: MonitorState | None,
    protocol_attempt: int,
    attempt_start_head: str | None,
    post_attempt_head: str | None,
    verdict: str,
    agent_reason: str | None,
    dirty_changes_committed: bool,
    stranded_dirty_residue: bool,
    pre_sink_head_unreadable: bool,
    pre_sink_probe_exc: Exception | None,
) -> VerdictResult:
    """Roll back a mutating correction attempt, then park its non-FIXED verdict.

    ``pre_sink_head_unreadable`` distinguishes "the attempt demonstrably mutated"
    (``AGENT_NON_FIXED_WITH_MUTATION``) from "mutation could not be measured, so
    fail closed" (``AGENT_VERDICT_PROTOCOL_VIOLATION``).

    On a successful rollback the refused verdict becomes a ``needs_human`` park
    for this item alone (#1020): the reason code, the agent's own verdict and its
    reason travel in the returned reason, which ``_sync_needs_human_reason``
    persists on the thread, so the merge gate keeps blocking and ``decide()``
    answers ``NotifyHuman`` — while the batch's already-accepted item commits
    reach the remote through the ordinary end-of-batch push. Raising here instead
    killed the monitor mid-batch and stranded those commits three times in three
    days (``ws_8cd0de10``, ``ws_6a5514fb``, ``ws_6e4081d1``).

    A rollback that fails still raises: the worktree holds unaccepted edits AWF
    could not remove, so there is nothing safe to continue with. The originating
    probe failure is chained as the cause so it is not lost.
    """
    from awf.runtime.pr_monitor_runner import comment_verdict as _comment_verdict
    from awf.runtime.pr_monitor_runner.comment_verdict_correction import _bounded

    if pre_sink_head_unreadable:
        mutation_reason_code = _comment_verdict.AGENT_VERDICT_PROTOCOL_VIOLATION
        mutation_log_event = "monitor.agent_verdict_correction_pre_sink_head_unreadable"
        mutation_message = (
            "Pre-sink HEAD was unreadable; cannot accept a "
            "non-FIXED verdict without measuring whether the "
            "correction attempt self-committed."
        )
        rollback_failure_message = (
            "Could not roll back unaccepted edits after "
            "correction attempt with unreadable pre-sink HEAD."
        )
    else:
        mutation_reason_code = _comment_verdict.AGENT_NON_FIXED_WITH_MUTATION
        mutation_log_event = "monitor.agent_verdict_correction_non_fixed_with_mutation"
        mutation_message = (
            "Correction attempt mutated the worktree then reported a non-FIXED verdict."
        )
        rollback_failure_message = (
            "Could not roll back unaccepted edits after "
            "correction attempt mutated state then "
            "reported a non-FIXED verdict."
        )
    _log.warning(
        mutation_log_event,
        workspace_id=workspace_id,
        reason_code=mutation_reason_code,
        protocol_attempt=protocol_attempt,
        attempt_start_head=attempt_start_head,
        current_head=post_attempt_head,
        verdict=verdict,
        dirty_changes_committed=dirty_changes_committed,
        stranded_dirty_residue=stranded_dirty_residue,
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
        rollback_error = _comment_verdict.AgentVerdictProtocolError(
            reason_code=mutation_reason_code,
            message=rollback_failure_message,
        )
        if pre_sink_probe_exc is not None:
            raise rollback_error from pre_sink_probe_exc
        raise rollback_error
    _log.warning(
        "monitor.agent_verdict_correction_mutation_item_parked",
        workspace_id=workspace_id,
        reason_code=mutation_reason_code,
        protocol_attempt=protocol_attempt,
        rollback_floor_head=rollback_floor_head,
        verdict=verdict,
    )
    short_floor = (rollback_floor_head or "")[:12]
    # A mutation park records the rollback commit it was rendered on, so the
    # park is re-evaluated once the PR head moves past it (#1213). An
    # unmeasurable attempt is not tied to any head and stays parked.
    is_mutation_park = mutation_reason_code == _comment_verdict.AGENT_NON_FIXED_WITH_MUTATION
    return _comment_verdict.VerdictResult(
        verdict="needs_human",
        reason=_bounded(
            f"{mutation_reason_code}: {mutation_message} The correction "
            f"attempt's edits were rolled back to {short_floor or '<unknown>'}; "
            f"the item is parked for human review. Agent verdict: {verdict}. "
            f"Agent reason: {agent_reason}"
        ),
        reason_code=mutation_reason_code if is_mutation_park else None,
        rollback_head=rollback_floor_head if is_mutation_park else None,
    )
