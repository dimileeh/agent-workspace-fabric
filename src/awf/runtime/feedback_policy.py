"""State-independent PR feedback policy helpers.

Centralizes verdict taxonomy, full-conversation body hashing/freshness, and the
canonical unresolved-thread combination used by merge safety gates.

This module may depend on review wire models, state-key helpers, and
``Mapping[str, str]``, but must not import ``MonitorState``, runner modules,
forge clients, or persistence.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from awf.runtime.pr_monitor_models import ReviewThread

_FRESHNESS_EPOCH = datetime.min.replace(tzinfo=UTC)

# Verdicts that mean "AWF closed this thread; it should stay resolved" for
# outdated-thread hygiene / fresh-feedback detection. Mirrors the runner's
# outdated-resolvable set (closed only — durable defer is handled separately).
CLOSED_OUTDATED_THREAD_VERDICTS = frozenset({"false_positive", "fix_committed"})

# Verdicts that re-enter AddressComments when a recorded full-conversation body
# hash no longer matches. Closed dispositions plus ``defer`` / ``needs_human``
# (a reviewer reply after escalation or durable capture must be re-triaged —
# otherwise hygiene refuses resolve on marker/hash mismatch and decide strands
# at NotifyHuman without feeding the clarifying reply to the agent).
_REQUEUE_ON_BODY_CHANGE_VERDICTS = CLOSED_OUTDATED_THREAD_VERDICTS | frozenset(
    {"defer", "needs_human"}
)


def needs_comment_attention(verdict: str | None) -> bool:
    """Return True when an unresolved PR comment still needs the agent.

    ``agent_failed`` is deliberately not treated as addressed. PR #35
    showed why: Codex exited non-zero while handling a Gemini review
    thread, left the worktree dirty, and the old decision core then let
    the PR merge because bot defers do not block. Agent failure is not a
    reviewer defer; it means AWF still owes the thread another attempt.
    """

    return verdict is None or verdict == "agent_failed"


def review_thread_body_state_key(thread_id: str) -> str:
    return f"__review_thread_body_hash__:{thread_id}"


def review_thread_resolution_body(thread: ReviewThread) -> str:
    """Hash only inline conversation for attention tracking.

    Review bodies are bundled onto exactly one live inline thread for prompt
    context and are triaged independently; when that anchor thread is resolved
    the same body may attach to another live thread without any inline feedback
    changing. Including review_context here would spuriously re-queue those
    threads.
    """
    # Hash conversation content only. ``comment_id`` / ``created_at`` are
    # representation-specific: the fallback (no ``comments``) form always
    # stores them as None, while a populated one-comment node carries real
    # forge IDs and timestamps for the same author+body. Including them made
    # equivalent bodies mismatch across transports, so a thread closed under
    # the fallback form requeued when the forge later returned the populated
    # form (and outdated hygiene skipped resolve) — PRRT_kwDOSJAM6s6dcFNZ.
    payload: list[dict[str, str | None]] = []
    if thread.comments:
        payload.extend(
            {
                "author": comment.author,
                "body": comment.body,
            }
            for comment in thread.comments
        )
    else:
        payload.append(
            {
                "author": thread.author,
                "body": thread.body_excerpt,
            }
        )
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _legacy_review_thread_resolution_body(thread: ReviewThread) -> str:
    """Pre-normalize serializer that included ``comment_id`` / ``created_at``.

    Persisted ``__review_thread_body_hash__`` and deferred-issue markers from
    parent monitors still use this payload. Matching must accept it for an
    unchanged conversation so resume does not requeue or refile — PRRT_kwDOSJAM6s6dfH8h.
    """
    payload: list[dict[str, str | None]] = []
    if thread.comments:
        payload.extend(
            {
                "author": comment.author,
                "body": comment.body,
                "comment_id": comment.comment_id,
                "created_at": (
                    comment.created_at.isoformat() if comment.created_at is not None else None
                ),
            }
            for comment in thread.comments
        )
    else:
        payload.append(
            {
                "author": thread.author,
                "body": thread.body_excerpt,
                "comment_id": None,
                "created_at": None,
            }
        )
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def review_thread_body_hash(thread: ReviewThread) -> str:
    return hashlib.sha256(review_thread_resolution_body(thread).encode("utf-8")).hexdigest()


def _legacy_review_thread_body_hash(thread: ReviewThread) -> str:
    return hashlib.sha256(_legacy_review_thread_resolution_body(thread).encode("utf-8")).hexdigest()


def review_thread_body_hashes(thread: ReviewThread) -> frozenset[str]:
    """Current and pre-normalize hashes that all mean this conversation."""
    return frozenset({review_thread_body_hash(thread), _legacy_review_thread_body_hash(thread)})


def recorded_review_thread_body_matches(recorded: str | None, thread: ReviewThread) -> bool:
    """True when ``recorded`` is the current or legacy hash of ``thread``."""
    return recorded is not None and recorded in review_thread_body_hashes(thread)


def thread_needs_attention(state_map: Mapping[str, str], thread: ReviewThread) -> bool:
    """True when a thread still needs ordinary comment repair.

    Missing evidence / ``agent_failed`` always need attention. A recorded
    disposition whose full-conversation body hash no longer matches also
    re-enters repair — ``isOutdated`` alone never suppresses this.
    """
    verdict = state_map.get(thread.thread_id)
    if needs_comment_attention(verdict):
        return True
    recorded = state_map.get(review_thread_body_state_key(thread.thread_id))
    return not recorded_review_thread_body_matches(recorded, thread)


def thread_enters_address_comments(state_map: Mapping[str, str], thread: ReviewThread) -> bool:
    """True when ``decide`` should batch this thread into ``AddressComments``.

    Never-addressed / ``agent_failed`` always enter repair. A closed disposition
    (``fix_committed`` / ``false_positive``), ``defer``, or ``needs_human`` with
    a recorded body hash that no longer matches also re-enters repair so fresh
    reviewer replies are re-triaged. Unchanged ``defer`` and ``needs_human`` stay
    on the NotifyHuman merge gate; a requeue-eligible verdict without any body
    snapshot does not re-queue (legacy / incomplete state falls through to
    existing gates).
    """
    verdict = state_map.get(thread.thread_id)
    if needs_comment_attention(verdict):
        return True
    if verdict not in _REQUEUE_ON_BODY_CHANGE_VERDICTS:
        return False
    recorded = state_map.get(review_thread_body_state_key(thread.thread_id))
    if recorded is None:
        return False
    return not recorded_review_thread_body_matches(recorded, thread)


def outdated_thread_has_fresh_feedback(state_map: Mapping[str, str], thread: ReviewThread) -> bool:
    """True when an AWF-closed OUTDATED thread has gained fresh reviewer feedback.

    Restricted to the closed-verdict set so never-addressed / ``needs_human`` /
    ``agent_failed`` outdated threads are routed by their own gates rather than
    this freshness helper.
    """
    if state_map.get(thread.thread_id) not in CLOSED_OUTDATED_THREAD_VERDICTS:
        return False
    return thread_needs_attention(state_map, thread)


def review_thread_conversation_rank(thread: ReviewThread) -> tuple[int, datetime]:
    """Monotonic richness key for same-ID transport copies (higher is richer).

    Latest activity is the max of each comment's ``created_at`` and
    ``updated_at`` so a same-ID post-edit copy outranks its pre-edit sibling
    when comment counts tie.
    """
    if thread.comments:
        stamps = (
            stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
            for c in thread.comments
            for stamp in (c.created_at, c.updated_at)
            if stamp is not None
        )
        latest = max(stamps, default=_FRESHNESS_EPOCH)
        return (len(thread.comments), latest)
    # Fallback body_excerpt representation counts as a single undated comment.
    return (1, _FRESHNESS_EPOCH)


def _body_matches_recorded(state_map: Mapping[str, str], thread: ReviewThread) -> bool:
    recorded = state_map.get(review_thread_body_state_key(thread.thread_id))
    return recorded_review_thread_body_matches(recorded, thread)


def prefer_duplicate_review_thread(
    existing: ReviewThread,
    candidate: ReviewThread,
    state_map: Mapping[str, str] | None = None,
) -> ReviewThread:
    """Choose one representation among same-ID transport duplicates.

    Richer conversations win (comment count, then latest ``created_at`` /
    ``updated_at``). On equal rank, never demote a body that matches the
    recorded hash to a mismatch (anti-oscillation after repair). Otherwise
    keep the later feed occurrence when bodies differ.
    """
    exist_rank = review_thread_conversation_rank(existing)
    cand_rank = review_thread_conversation_rank(candidate)
    if cand_rank > exist_rank:
        return candidate
    if cand_rank < exist_rank:
        return existing
    if state_map is not None:
        exist_match = _body_matches_recorded(state_map, existing)
        cand_match = _body_matches_recorded(state_map, candidate)
        if exist_match and not cand_match:
            return existing
        if cand_match and not exist_match:
            return candidate
    if review_thread_body_hash(candidate) != review_thread_body_hash(existing):
        return candidate
    return existing


def preferred_duplicate_review_thread(
    copies: Sequence[ReviewThread],
    state_map: Mapping[str, str] | None = None,
) -> ReviewThread:
    """Reduce same-ID transport copies to a single preferred representation."""
    if not copies:
        raise ValueError("preferred_duplicate_review_thread requires at least one copy")
    preferred = copies[0]
    for copy in copies[1:]:
        preferred = prefer_duplicate_review_thread(preferred, copy, state_map)
    return preferred


def canonical_unresolved_inline_threads(
    active: Sequence[ReviewThread],
    outdated: Sequence[ReviewThread],
    state_map: Mapping[str, str] | None = None,
) -> tuple[ReviewThread, ...]:
    """Deterministic merge-authoritative unresolved view.

    Active threads first (preserving relative order), then outdated IDs not
    already seen. When the same ID appears in both feeds, the active
    representation wins.

    Within a single feed, same-ID transport copies coalesce to a preferred
    representation (richer conversation; anti-oscillation hash match; else
    AddressComments-entering / later distinct body) so ``decide()`` sees one
    conversation and hygiene is not wedged by a stale sibling.
    """
    index_by_id: dict[str, int] = {}
    active_ids: set[str] = set()
    combined: list[ReviewThread] = []

    def _consider(thread: ReviewThread, *, from_active: bool) -> None:
        tid = thread.thread_id
        if tid not in index_by_id:
            index_by_id[tid] = len(combined)
            combined.append(thread)
            if from_active:
                active_ids.add(tid)
            return
        # Cross-feed: active already owns this ID — never replace with outdated.
        if not from_active and tid in active_ids:
            return
        existing = combined[index_by_id[tid]]
        combined[index_by_id[tid]] = prefer_duplicate_review_thread(existing, thread, state_map)

    for thread in active:
        _consider(thread, from_active=True)
    for thread in outdated:
        _consider(thread, from_active=False)
    return tuple(combined)


def unresolved_active_count(active: Sequence[ReviewThread]) -> int:
    """Count distinct active thread IDs (dedupe within the active feed)."""
    return len({thread.thread_id for thread in active})


def unresolved_outdated_unique_count(
    active: Sequence[ReviewThread],
    outdated: Sequence[ReviewThread],
) -> int:
    """Count distinct outdated unresolved IDs not already present in the active feed."""
    active_ids = {thread.thread_id for thread in active}
    outdated_ids = {thread.thread_id for thread in outdated}
    return len(outdated_ids - active_ids)


def unresolved_canonical_count(
    active: Sequence[ReviewThread],
    outdated: Sequence[ReviewThread],
) -> int:
    return len(canonical_unresolved_inline_threads(active, outdated))


def unresolved_thread_counts(
    active: Sequence[ReviewThread],
    outdated: Sequence[ReviewThread],
) -> dict[str, int]:
    """Operator/log counters: canonical total plus active/outdated breakdowns."""
    return {
        "unresolved_threads": unresolved_canonical_count(active, outdated),
        "unresolved_active_threads": unresolved_active_count(active),
        "unresolved_outdated_threads": unresolved_outdated_unique_count(active, outdated),
    }
