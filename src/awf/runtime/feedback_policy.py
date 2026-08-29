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

from awf.runtime.pr_monitor_models import ReviewThread

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


def thread_needs_attention(state_map: Mapping[str, str], thread: ReviewThread) -> bool:
    """True when a thread still needs ordinary comment repair.

    Missing evidence / ``agent_failed`` always need attention. A recorded
    disposition whose full-conversation body hash no longer matches also
    re-enters repair — ``isOutdated`` alone never suppresses this.
    """
    verdict = state_map.get(thread.thread_id)
    if needs_comment_attention(verdict):
        return True
    return state_map.get(review_thread_body_state_key(thread.thread_id)) != review_thread_body_hash(
        thread
    )


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
    return recorded != review_thread_body_hash(thread)


def outdated_thread_has_fresh_feedback(state_map: Mapping[str, str], thread: ReviewThread) -> bool:
    """True when an AWF-closed OUTDATED thread has gained fresh reviewer feedback.

    Restricted to the closed-verdict set so never-addressed / ``needs_human`` /
    ``agent_failed`` outdated threads are routed by their own gates rather than
    this freshness helper.
    """
    if state_map.get(thread.thread_id) not in CLOSED_OUTDATED_THREAD_VERDICTS:
        return False
    return thread_needs_attention(state_map, thread)


def canonical_unresolved_inline_threads(
    active: Sequence[ReviewThread],
    outdated: Sequence[ReviewThread],
) -> tuple[ReviewThread, ...]:
    """Deterministic merge-authoritative unresolved view.

    Active threads first (preserving relative order), then outdated IDs not
    already seen. When the same ID appears in both feeds, the active
    representation wins.
    """
    seen: set[str] = set()
    combined: list[ReviewThread] = []
    for thread in active:
        if thread.thread_id in seen:
            continue
        seen.add(thread.thread_id)
        combined.append(thread)
    for thread in outdated:
        if thread.thread_id in seen:
            continue
        seen.add(thread.thread_id)
        combined.append(thread)
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
