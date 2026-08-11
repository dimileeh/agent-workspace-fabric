"""Notification item collection and content-sensitive deduplication helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from awf.runtime.monitor_state_keys import _outdated_resolve_requeued_key
from awf.runtime.pr_monitor import (
    MonitorState,
    PRStatus,
    ReviewThread,
    _is_bot_author,
    _is_bot_review_thread,
    _outdated_thread_has_fresh_feedback,
)


def _needs_human_reason_state_key(item_id: str) -> str:
    """Return the persisted state key for a blocker item's human reason."""
    return f"__needs_human_reason__:{item_id}"


def _outdated_thread_blocker_reason(state: MonitorState, thread: ReviewThread) -> str | None:
    """Return why an outdated thread currently blocks the merge decision."""
    if state.threads_addressed_ids.get(thread.thread_id) == "needs_human":
        return "AWF could not resolve this outdated thread and needs human input"
    if state.threads_addressed_ids.get(_outdated_resolve_requeued_key(thread.thread_id)):
        return "AWF could not yet resolve this outdated thread and will retry before merging"
    if _outdated_thread_has_fresh_feedback(state, thread):
        return "new feedback was added to this outdated thread after AWF addressed it"
    return None


def _collect_defer_items(
    status: PRStatus, state: MonitorState
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Collect deferred / needs-human threads/comments for notification groups.

    Returns ``(bot_items, human_items)``. Inline threads are classified using
    their complete comment history via ``pr_monitor._is_bot_review_thread``;
    review comments use ``pr_monitor._is_bot_author``. All other items
    (including unknown-author items, which the merge gate treats as human for
    safety) go into the second list — the artifact mirrors that classification
    so orchestrators see the same picture.

    Both ``defer`` and ``needs_human`` verdicts are collected (#305): a
    ``needs_human`` item blocks the merge just as a ``defer`` one does, so
    dropping it would let the terminal artifact and notification under-report
    the open feedback. Each item carries its ``verdict`` so consumers can tell
    the two apart. Outdated threads are included only when they match one of
    the decision core's merge-blocking states. Their merge-state fallback
    explains why they are included, but is not an agent-provided verdict
    reason.
    """
    bot_items: list[dict[str, object]] = []
    human_items: list[dict[str, object]] = []
    for thread in status.unresolved_inline_threads:
        verdict = state.threads_addressed_ids.get(thread.thread_id)
        if verdict not in {"defer", "needs_human"}:
            continue
        is_bot = _is_bot_review_thread(thread)
        bucket = bot_items if is_bot else human_items
        bucket.append(
            {
                "kind": "thread",
                "id": thread.thread_id,
                "author": thread.author,
                "is_bot": is_bot,
                "path": thread.path,
                "line": thread.line,
                "url": thread.url,
                "body": thread.body_excerpt,
                "verdict": verdict,
                "agent_verdict_reason": state.threads_addressed_ids.get(
                    _needs_human_reason_state_key(thread.thread_id)
                ),
            }
        )
    for thread in status.outdated_unresolved_inline_threads:
        if _outdated_thread_blocker_reason(state, thread) is None:
            continue
        is_bot = _is_bot_review_thread(thread)
        bucket = bot_items if is_bot else human_items
        bucket.append(
            {
                "kind": "thread",
                "id": thread.thread_id,
                "author": thread.author,
                "is_bot": is_bot,
                "path": thread.path,
                "line": thread.line,
                "url": thread.url,
                "body": thread.body_excerpt,
                "verdict": "needs_human",
                "agent_verdict_reason": state.threads_addressed_ids.get(
                    _needs_human_reason_state_key(thread.thread_id)
                ),
            }
        )
    for comment in status.unresolved_review_comments:
        verdict = state.threads_addressed_ids.get(comment.comment_id)
        if verdict not in {"defer", "needs_human"}:
            continue
        is_bot = _is_bot_author(comment.author)
        bucket = bot_items if is_bot else human_items
        bucket.append(
            {
                "kind": "review",
                "id": comment.comment_id,
                "author": comment.author,
                "is_bot": is_bot,
                "path": None,
                "line": None,
                "url": comment.url,
                "body": comment.body_excerpt,
                "verdict": verdict,
                "agent_verdict_reason": state.threads_addressed_ids.get(
                    _needs_human_reason_state_key(comment.comment_id)
                ),
            }
        )
    return bot_items, human_items


def _notification_items_digest(items: Sequence[Mapping[str, object]]) -> str:
    """Return a stable, order-independent notification identity for item IDs."""
    item_ids = sorted(str(item.get("id", "")) for item in items)
    payload = json.dumps(item_ids, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
