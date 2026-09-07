"""Retirement of operator-answered ``needs_human`` feedback.

Split out of ``operator_hints.py`` so that module stays under the first-party
line cap enforced by
``tests/unit/test_core_decomposition_maintainability.py``. Everything here is
monitor-state bookkeeping for a settled operator guide: marking the hint
processed, dropping the protected-block preserved-head marker, and retiring the
review-level ``needs_human`` verdicts the guide explicitly answered.
"""

from __future__ import annotations

from awf.runtime.monitor_state_keys import _operator_decision_key
from awf.runtime.operator_hints import mark_operator_hint_processed
from awf.runtime.pr_monitor import (
    _PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY,
    MonitorState,
    OperatorHint,
)
from awf.runtime.pr_monitor_runner.operator_hint_parsing import (
    _operator_decision_marker_text,
    _operator_hint_feedback_body_hash_key,
    _operator_hint_feedback_id_candidates,
    _operator_hint_feedback_storage_key_candidates,
    _operator_hint_review_thread_id_candidates,
)


def _finalize_processed_operator_hint(
    state: MonitorState,
    *,
    hint: OperatorHint | None = None,
    acted_feedback_text: str | None = None,
) -> None:
    """Mark the operator hint processed and drop the protected-block preserved-head
    marker.

    The marker (``_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY``) is recorded at block
    time and powers the divergence-recovery / restart-after-consume short-circuits
    for THIS resume only. Once the resume is finalized it has served its purpose;
    leaving it in persisted monitor state would let a later plain remonitor (no
    directive, no grant) whose old preserved commit is still on the remote take the
    restart-recovery shortcut and skip the CLI — silently ignoring the operator's
    new repair request (PRRT_kwDOSJAM6s6KE2BX). A fresh block re-records the marker.
    """
    pending_hint = getattr(state, "pending_operator_hint", None)
    active_hint = pending_hint or hint
    _mark_referenced_needs_human_feedback_answered(
        state, hint=active_hint, acted_text=acted_feedback_text
    )
    state.threads_addressed_ids.pop(_PROTECTED_BLOCK_PRESERVED_HEAD_STATE_KEY, None)
    if hasattr(state, "pending_operator_hint") and pending_hint is None and active_hint is not None:
        state.pending_operator_hint = active_hint
    mark_operator_hint_processed(state)


def _mark_referenced_needs_human_feedback_answered(
    state: MonitorState,
    *,
    hint: OperatorHint | None = None,
    acted_text: str | None = None,
) -> None:
    """Retire review-level ``needs_human`` verdicts a guide explicitly answered.

    Operator guides are the sanctioned path for resolving a monitor HUMAN_WAIT.
    Two id classes are recognized, and they are retired differently:

    * **Review comments** (``issue:<id>`` / ``bbcomment:<id>`` / contextual bare
      ids). There is no forge thread to resolve, so a consumed guide that names
      the original feedback id in the acted-on text must itself update the
      persisted verdict, flipping it to ``false_positive``. Otherwise the hint is
      marked processed and the next ``decide()`` poll immediately re-enters the
      same stale HUMAN_WAIT.
    * **Review threads** (``PRRT_...`` and the Bitbucket ``bb:``/``bbtask:``
      keys). Here the verdict is *cleared* rather than flipped: an absent verdict
      makes ``needs_comment_attention`` True, so the thread re-enters
      ``AddressComments`` on the next poll and the agent records the real
      verdict (issue #938). Flipping it to ``false_positive`` would assert a
      verdict on the operator's behalf and let the merge gate pass without the
      agent ever re-reading the thread. The directive is also stashed under
      ``__operator_decision__:<thread id>`` so the re-addressed thread's repair
      prompt quotes the ruling instead of replaying only the reviewer text the
      agent already escalated on (issue #939); ``_mark_review_thread_addressed``
      drops it once a verdict other than ``agent_failed`` answers it. The stash
      also survives PR re-adoption: ``__operator_decision__:`` is on the copied
      marker allowlist in :mod:`awf.service.pr_monitor_adoption_seed`, so a
      successor workspace that adopts the PR before the re-queued thread is
      addressed still quotes the ruling. Like the head-independent verdicts it
      crosses whether or not head continuity is established -- it disposes of
      the *feedback* rather than asserting what the branch contains.

    ``hint.reason`` can be audit context for approve-and-keep grant-only resumes,
    which skip the CLI entirely. Callers pass ``acted_text`` when a directiveless
    reason was actually presented to the agent; otherwise only a directive counts.

    This helper intentionally leaves any stored ``__review_comment_body_hash__`` /
    ``__review_thread_body_hash__`` marker unchanged because it does not receive
    the live ``ReviewComment``/``ReviewThread`` needed to recompute the hash. For a
    cleared thread the snapshot is also what keeps a later ``defer``/``needs_human``
    re-queueable, so it must survive the clear.

    The comment arm requires an existing body-hash sidecar so the ``false_positive``
    it *asserts* is durable across the next stale-state sweep. The thread arm does
    not, because it asserts nothing: a hashless row is exactly what
    ``_drop_stale_review_thread_addressed_state`` would clear on its own, and
    requiring the sidecar would park monitor-seeded rows forever
    (PRRT_kwDOSJAM6s6fw5zx). Outdated-thread hygiene deliberately records bare
    ``needs_human`` verdicts with no snapshot (``outdated_resolution``: post-fix
    reviewer activity, and the mixed-verdict blocking-sibling promotion); an
    unchanged ``needs_human`` never re-enters ``AddressComments``, so no later path
    holds the live thread to add the missing hash, and the guide — already marked
    processed — would leave ``decide`` returning to ``NotifyHuman`` forever. The
    clear is not a merge-gate weakening: it routes the thread back through
    ``AddressComments`` for a real verdict, and ``_operator_decision_for_thread``
    keeps the stashed ruling it cannot compare, so the repair prompt still quotes
    it.
    """
    if hint is None:
        return
    text = acted_text if acted_text is not None else hint.directive
    if not text:
        return
    for referenced_id in _operator_hint_feedback_id_candidates(text):
        for item_id in _operator_hint_feedback_storage_key_candidates(referenced_id):
            if state.threads_addressed_ids.get(item_id) != "needs_human":
                continue
            if not state.threads_addressed_ids.get(_operator_hint_feedback_body_hash_key(item_id)):
                continue
            state.mark_addressed(item_id, "false_positive")
            state.threads_addressed_ids.pop(f"__needs_human_reason__:{item_id}", None)
            break
    for thread_id in _operator_hint_review_thread_id_candidates(text):
        if state.threads_addressed_ids.get(thread_id) != "needs_human":
            continue
        state.threads_addressed_ids.pop(thread_id, None)
        state.threads_addressed_ids.pop(f"__needs_human_reason__:{thread_id}", None)
        state.mark_addressed(
            _operator_decision_key(thread_id), _operator_decision_marker_text(text)
        )
