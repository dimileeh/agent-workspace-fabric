"""Shared PR monitor persisted-state key helpers."""

from __future__ import annotations

from datetime import UTC, datetime


def _non_check_reviewer_settle_started_key(
    *,
    pr_number: int,
    head_sha: str,
    activity_signature: str | None = None,
) -> str:
    """Build state key for a non-check reviewer settle start marker."""
    key = f"{_non_check_reviewer_settle_started_prefix(pr_number=pr_number)}{head_sha}"
    if activity_signature is not None:
        return f"{key}:{activity_signature}"
    return key


def _non_check_reviewer_settle_started_prefix(*, pr_number: int) -> str:
    """Build namespace prefix for non-check reviewer settle state keys."""
    return f"__awf_non_check_reviewer_settle_started__:{pr_number}:"


def _non_check_reviewer_settle_done_key(
    *,
    pr_number: int,
    head_sha: str,
    activity_signature: str | None = None,
) -> str:
    """Build state key for a completed non-check reviewer settle window."""
    key = f"__awf_non_check_reviewer_settle_done__:{pr_number}:{head_sha}"
    if activity_signature is not None:
        return f"{key}:{activity_signature}"
    return key


def _non_check_reviewer_settle_freeze_key(*, pr_number: int, head_sha: str) -> str:
    """Build state key for a remonitor-armed head settle freeze."""
    return f"__awf_non_check_reviewer_settle_freeze__:{pr_number}:{head_sha}"


def _initial_review_grace_started_key(pr_number: int) -> str:
    return f"__awf_initial_review_grace_started__:{pr_number}"


def _initial_review_grace_done_key(pr_number: int) -> str:
    return f"__awf_initial_review_grace_done__:{pr_number}"


def _merge_method_blocked_key(*, pr_number: int, head_sha: str) -> str:
    """Build state key for a PR-head merge-method blocker."""
    return f"__awf_merge_method_blocked__:{pr_number}:{head_sha}"


def _outdated_resolve_requeued_key(thread_id: str) -> str:
    """Build state key flagging an addressed-OUTDATED thread whose resolve was
    requeued after a TRANSIENT forge fault on this poll.

    The transient resolve path keeps the fix verdict (``fix_committed`` /
    ``false_positive``) intact so the next poll retries — but those verdicts do
    NOT block merge, and ``_resolve_addressed_outdated_threads`` runs immediately
    before ``decide`` in the same iteration. Without this flag ``decide`` could
    return ``Merge`` on that very poll, merging over the addressed-but-unresolved
    outdated thread before the promised retry runs. The flag makes ``decide`` hold
    that thread at ``NotifyHuman`` until the resolve succeeds (flag cleared) or a
    permanent fault escalates the verdict to ``needs_human``."""
    return f"__awf_outdated_resolve_requeued__:{thread_id}"


def _salvaged_fix_head_state_key(item_id: str) -> str:
    """Persisted SHA of a dirty-salvage commit after a nonzero CLI exit.

    When the comment agent crashes after leaving a valid dirty fix,
    ``_commit_dirty_worktree`` still commits it and the invocation returns
    ``agent_failed``. The settle loop retries the same item; a successful FIXED
    without another tree change must be able to confirm the prior salvage when
    HEAD still equals or descends from that SHA — without accepting the failed
    run's verdict. Descent matters in multi-item bursts where a later thread
    advances HEAD past the salvage tip while leaving it in history.
    """
    return f"__salvaged_fix_head__:{item_id}"


def _initial_review_grace_wall_started_value(started_wall_seconds: float) -> str:
    return f"{started_wall_seconds:.6f}"


def _initial_review_grace_wall_started_value_from_datetime(started_at: datetime) -> str:
    started_dt = started_at
    if started_dt.tzinfo is None:
        started_dt = started_dt.replace(tzinfo=UTC)
    return _initial_review_grace_wall_started_value(started_dt.timestamp())
