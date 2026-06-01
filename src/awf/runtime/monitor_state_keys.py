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
    return f"__awf_merge_method_blocked__:{pr_number}:{head_sha}"


def _initial_review_grace_wall_started_value(started_wall_seconds: float) -> str:
    return f"{started_wall_seconds:.6f}"


def _initial_review_grace_wall_started_value_from_datetime(started_at: datetime) -> str:
    started_dt = started_at
    if started_dt.tzinfo is None:
        started_dt = started_dt.replace(tzinfo=UTC)
    return _initial_review_grace_wall_started_value(started_dt.timestamp())
