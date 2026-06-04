"""Worker heartbeat freshness helpers."""

from __future__ import annotations

from datetime import UTC, datetime

WORKER_HEARTBEAT_FRESH_REASON = "WORKER_HEARTBEAT_FRESH"
WORKER_HEARTBEAT_MISSING_REASON = "WORKER_HEARTBEAT_MISSING"
WORKER_HEARTBEAT_STALE_REASON = "WORKER_HEARTBEAT_STALE"
WORKER_HEARTBEAT_UNAVAILABLE_REASON = "WORKER_HEARTBEAT_UNAVAILABLE"
WORKER_HEARTBEAT_WRITE_FAILED_REASON = "WORKER_HEARTBEAT_WRITE_FAILED"

WORKER_HEARTBEAT_MIN_STALE_AFTER_SECONDS = 15.0
WORKER_HEARTBEAT_STALE_POLL_MULTIPLIER = 3.0
WORKER_HEARTBEAT_MIN_WRITE_INTERVAL_SECONDS = 1.0


def worker_heartbeat_stale_after_seconds(poll_interval_seconds: float | None) -> float:
    try:
        poll_interval = float(poll_interval_seconds or 0.0)
    except (TypeError, ValueError):
        poll_interval = 0.0
    return max(
        WORKER_HEARTBEAT_MIN_STALE_AFTER_SECONDS,
        poll_interval * WORKER_HEARTBEAT_STALE_POLL_MULTIPLIER,
    )


def worker_heartbeat_write_interval_seconds(poll_interval_seconds: float | None) -> float:
    try:
        poll_interval = float(poll_interval_seconds or 0.0)
    except (TypeError, ValueError):
        poll_interval = 0.0
    if poll_interval <= 0:
        poll_interval = WORKER_HEARTBEAT_MIN_WRITE_INTERVAL_SECONDS
    return max(
        WORKER_HEARTBEAT_MIN_WRITE_INTERVAL_SECONDS,
        min(poll_interval, worker_heartbeat_stale_after_seconds(poll_interval) / 3.0),
    )


def worker_heartbeat_age_seconds(last_heartbeat_at: datetime, *, now: datetime) -> float:
    return max(0.0, (_as_aware_utc(now) - _as_aware_utc(last_heartbeat_at)).total_seconds())


def worker_heartbeat_is_fresh(
    last_heartbeat_at: datetime,
    *,
    now: datetime,
    poll_interval_seconds: float | None,
) -> bool:
    return worker_heartbeat_age_seconds(last_heartbeat_at, now=now) <= (
        worker_heartbeat_stale_after_seconds(poll_interval_seconds)
    )


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
