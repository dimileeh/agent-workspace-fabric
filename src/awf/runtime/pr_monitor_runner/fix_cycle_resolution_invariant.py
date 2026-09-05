"""Stranded-thread invariant for the fix cycle's in-cycle resolve step (#925).

Kept separate so ``fix_cycle`` stays under the first-party line budget.

The fix cycle deliberately does not enqueue threads that were already
``isOutdated`` when the AddressComments batch began: outdated hygiene on the next
outer poll owns their resolution (#484). That hand-off only holds while the
thread is *still* outdated. When the outdatedness came from the item's own commit
and that commit is later rolled back (or the forge simply re-activates the
thread), hygiene never sees it, the recorded verdict plus matching body hash keep
``thread_needs_attention`` False, and the conversation stays unresolved forever —
a silent merge blocker with no escalation (issue #925, PR #922).

This module answers one question over the *final settle* feed: for each thread
still awaiting resolution — deferred in this cycle, or stranded the same way by
an earlier one — does another owner demonstrably have it?
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import TYPE_CHECKING

from awf.common.logging import get_logger
from awf.runtime.feedback_policy import (
    RESOLVABLE_THREAD_VERDICTS,
    review_thread_body_hashes,
    thread_resolution_pending,
)
from awf.runtime.pr_monitor_models import ReviewThread

if TYPE_CHECKING:
    from awf.runtime.pr_monitor import MonitorState

_log = get_logger(__name__)

# A thread ended the cycle dispositioned and unresolved with no demonstrable
# owner (no settle status to attribute it to). Escalates to ``needs_human`` so
# the merge gate keeps blocking *visibly* instead of stranding the thread.
RESOLUTION_OWNER_MISSING_REASON = "THREAD_RESOLUTION_OWNER_MISSING"


def _deferred_capture_recorded(state_map: Mapping[str, str], thread: ReviewThread) -> bool:
    """True when a tracking issue is recorded for this ``defer``'s conversation.

    The ``state_map`` view of ``_deferred_issue_already_filed``. Imported lazily
    because ``fix_cycle`` imports this module.
    """
    from awf.runtime.pr_monitor_runner.fix_cycle import _deferred_issue_filed_marker

    return any(
        state_map.get(_deferred_issue_filed_marker(thread.thread_id, body_hash))
        for body_hash in review_thread_body_hashes(thread)
    )


def stranded_resolvable_thread_ids(
    *,
    candidate_ids: Sequence[str],
    state_map: Mapping[str, str],
    settle_threads: Sequence[ReviewThread] | None,
    stale_thread_ids: AbstractSet[str],
    outdated_only_thread_ids: AbstractSet[str],
    queued_resolution_ids: AbstractSet[str] = frozenset(),
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split resolution-pending threads into (resolve in this cycle, owner missing).

    ``candidate_ids`` are the threads whose in-cycle resolution the caller
    deferred. ``settle_threads`` is the canonical unresolved view from the last
    settle poll, or ``None`` when that poll never succeeded; every thread in it
    is a candidate too. A candidate is left alone when another owner is
    demonstrable: it is on the caller's own resolve queue
    (``queued_resolution_ids``), it needs attention (AddressComments re-enters
    it), it is outdated-only (hygiene owns it), or it no longer appears in the
    unresolved feeds at all (already resolved on the forge).

    Sweeping the whole settle feed — not just this cycle's deferred candidates —
    is what catches a thread stranded by an *earlier* cycle: its resolvable
    verdict plus still-matching body hash keep it out of AddressComments, so it
    never becomes a deferred candidate again, yet nobody resolves it either
    (PRRT_kwDOSJAM6s6fmmKc). A swept ``defer`` additionally requires its durable
    capture marker: uncaptured defer state is escalated, never resolved
    (PRRT_kwDOSJAM6s6fmywf).
    """
    resolve_now: list[str] = []
    owner_missing: list[str] = []
    live_by_id = (
        None if settle_threads is None else {thread.thread_id: thread for thread in settle_threads}
    )
    sweep_ids = dict.fromkeys(
        (*candidate_ids, *(() if live_by_id is None else live_by_id)),
    )
    for thread_id in sweep_ids:
        if thread_id in queued_resolution_ids:
            continue
        if thread_id in stale_thread_ids or thread_id in outdated_only_thread_ids:
            continue
        if live_by_id is None:
            # No settle evidence (the settle re-poll never succeeded): the body
            # hash cannot be re-checked and outdatedness cannot be confirmed, so
            # fall back to the recorded verdict alone. Deliberately conservative
            # — outdated hygiene would probably still own these, but "probably"
            # is what stranded PR #922. A visible ``needs_human`` on a thread
            # that is unresolved anyway costs an operator glance; a wrong guess
            # costs a permanently blocked PR with no signal at all.
            if state_map.get(thread_id) in RESOLVABLE_THREAD_VERDICTS:
                owner_missing.append(thread_id)
            continue
        thread = live_by_id.get(thread_id)
        if thread is None:
            continue
        if not thread_resolution_pending(state_map, thread):
            continue
        if state_map.get(thread_id) == "defer" and not _deferred_capture_recorded(
            state_map, thread
        ):
            # Incomplete or legacy ``defer`` state: no ``__deferred_issue_filed__``
            # marker for this conversation, so the deferred work was never durably
            # captured. In-cycle defers only reach ``candidate_ids`` after capture
            # succeeded, and outdated hygiene gates on the same marker; resolving
            # here would be the one path that merges the PR with the follow-up
            # lost (PRRT_kwDOSJAM6s6fmywf). Escalate instead — no capture, no owner.
            owner_missing.append(thread_id)
            continue
        resolve_now.append(thread_id)
    return tuple(resolve_now), tuple(owner_missing)


def escalate_owner_missing_threads(
    state: MonitorState,
    thread_ids: Sequence[str],
    *,
    workspace_id: str,
    pr_number: int,
) -> None:
    """Downgrade unownable dispositioned threads to ``needs_human`` (#925).

    The thread is unresolved either way, so this does not block anything that
    was not already blocked; it converts a silent block into a ``NotifyHuman``
    carrying ``THREAD_RESOLUTION_OWNER_MISSING``. The recorded body hash is
    left in place so a later reviewer reply still re-enters AddressComments.
    """
    from awf.runtime.pr_monitor_runner.comments import VerdictResult
    from awf.runtime.pr_monitor_runner.helpers import _sync_needs_human_reason

    for thread_id in thread_ids:
        _log.warning(
            "monitor.thread_resolution_owner_missing",
            workspace_id=workspace_id,
            pr_number=pr_number,
            thread_id=thread_id,
            reason_code=RESOLUTION_OWNER_MISSING_REASON,
            verdict=state.threads_addressed_ids.get(thread_id),
        )
        _sync_needs_human_reason(
            state,
            thread_id,
            VerdictResult(
                verdict="needs_human",
                reason=(
                    f"{RESOLUTION_OWNER_MISSING_REASON}: the fix cycle could not confirm "
                    "who resolves this dispositioned thread; resolve it manually or "
                    "re-run the monitor."
                ),
            ),
        )
        state.mark_addressed(thread_id, "needs_human")
