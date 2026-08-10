"""Notification item collection and content-sensitive deduplication helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from awf.runtime.pr_monitor import (
    MonitorState,
    PRStatus,
    _is_bot_author,
    _is_bot_review_thread,
)


def _needs_human_reason_state_key(item_id: str) -> str:
    """Return the persisted state key for a blocker item's human reason."""
    return f"__needs_human_reason__:{item_id}"


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
    the two apart.
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
    """Return an order-independent digest of public blocking-item content."""
    item_details = sorted(
        json.dumps(
            {
                field: item.get(field)
                for field in (
                    "kind",
                    "id",
                    "author",
                    "is_bot",
                    "path",
                    "line",
                    "url",
                    "body",
                    "verdict",
                    "agent_verdict_reason",
                )
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        for item in items
    )
    payload = json.dumps(item_details, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
