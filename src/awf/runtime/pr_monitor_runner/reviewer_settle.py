"""Non-check reviewer settle helper functions."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta

from awf.runtime.monitor_state_keys import (
    _non_check_reviewer_settle_done_key,
    _non_check_reviewer_settle_freeze_key,
    _non_check_reviewer_settle_started_key,
)
from awf.runtime.pr_monitor import CheckTiming, MonitorConfig, MonitorState, PRStatus
from awf.runtime.pr_monitor_runner.gates import (
    _NonCheckReviewerSettleDecision,
    _NonCheckReviewerSettleWaitOperationContext,
)


def _non_check_reviewer_settle_skip_visible_key(*, pr_number: int, head_sha: str) -> str:
    """Build skip marker key for missing non-check reviewer visibility checks."""
    return f"__awf_non_check_reviewer_settle_skipped_visible__:{pr_number}:{head_sha}"


def _non_check_reviewer_settle_decision(
    status: PRStatus,
    state: MonitorState,
    config: MonitorConfig,
    *,
    pr_number: int,
    now: float,
    now_wall: datetime | None = None,
) -> _NonCheckReviewerSettleDecision:
    """Return settle decision for non-check reviewers, preferring activity clock when available."""
    configured_reviewers = _normalize_non_check_reviewer_logins(config.non_check_reviewer_logins)
    if not config.auto_merge:
        return _NonCheckReviewerSettleDecision(
            action="not_auto_merge",
            configured_reviewers=configured_reviewers,
        )
    if config.non_check_reviewer_settle_seconds <= 0:
        return _NonCheckReviewerSettleDecision(
            action="disabled",
            configured_reviewers=configured_reviewers,
        )
    if not configured_reviewers:
        return _NonCheckReviewerSettleDecision(action="no_configured_reviewers")

    visible_reviewers, missing_reviewers = _non_check_reviewer_visibility(
        configured_reviewers=configured_reviewers,
        checks=status.checks,
    )
    done_key = _non_check_reviewer_settle_done_key(
        pr_number=pr_number,
        head_sha=status.head_sha,
    )
    started_key = _non_check_reviewer_settle_started_key(
        pr_number=pr_number,
        head_sha=status.head_sha,
    )
    freeze_key = _non_check_reviewer_settle_freeze_key(
        pr_number=pr_number,
        head_sha=status.head_sha,
    )
    if not missing_reviewers:
        if (
            state.threads_addressed_ids.get(done_key) != "elapsed"
            and state.threads_addressed_ids.get(freeze_key) == "armed"
            and started_key in state.threads_addressed_ids
        ):
            return _non_check_reviewer_head_settle_decision(
                state,
                config,
                pr_number=pr_number,
                head_sha=status.head_sha,
                now=now,
                configured_reviewers=configured_reviewers,
                missing_reviewers=missing_reviewers,
                visible_reviewers=visible_reviewers,
            )
        freeze_cleared = state.threads_addressed_ids.pop(freeze_key, None) is not None
        stale_wait_cleared = (
            state.threads_addressed_ids.get(done_key) != "elapsed"
            and state.threads_addressed_ids.pop(started_key, None) is not None
        )
        skip_key = _non_check_reviewer_settle_skip_visible_key(
            pr_number=pr_number,
            head_sha=status.head_sha,
        )
        state_changed = (
            state.threads_addressed_ids.get(skip_key) != "visible_check"
            or freeze_cleared
            or stale_wait_cleared
        )
        if state_changed:
            state.mark_addressed(skip_key, "visible_check")
        return _NonCheckReviewerSettleDecision(
            action="visible_check",
            configured_reviewers=configured_reviewers,
            visible_reviewers=visible_reviewers,
            state_changed=state_changed,
        )

    if status.quiet_period_anchor_at is not None:
        return _non_check_reviewer_activity_settle_decision(
            status,
            state,
            config,
            pr_number=pr_number,
            now=now,
            now_wall=now_wall or datetime.now(UTC),
            configured_reviewers=configured_reviewers,
            missing_reviewers=missing_reviewers,
            visible_reviewers=visible_reviewers,
        )

    return _non_check_reviewer_head_settle_decision(
        state,
        config,
        pr_number=pr_number,
        head_sha=status.head_sha,
        now=now,
        configured_reviewers=configured_reviewers,
        missing_reviewers=missing_reviewers,
        visible_reviewers=visible_reviewers,
    )


def _non_check_reviewer_head_settle_decision(
    state: MonitorState,
    config: MonitorConfig,
    *,
    pr_number: int,
    head_sha: str,
    now: float,
    configured_reviewers: tuple[str, ...],
    missing_reviewers: tuple[str, ...],
    visible_reviewers: tuple[str, ...],
) -> _NonCheckReviewerSettleDecision:
    """Return the head-scoped settle decision for a running wait marker."""
    done_key = _non_check_reviewer_settle_done_key(
        pr_number=pr_number,
        head_sha=head_sha,
    )
    if state.threads_addressed_ids.get(done_key) == "elapsed":
        return _NonCheckReviewerSettleDecision(
            action="already_elapsed",
            configured_reviewers=configured_reviewers,
            missing_reviewers=missing_reviewers,
            visible_reviewers=visible_reviewers,
        )

    started_key = _non_check_reviewer_settle_started_key(
        pr_number=pr_number,
        head_sha=head_sha,
    )
    started_raw = state.threads_addressed_ids.get(started_key)
    started_now = False
    if started_raw is None:
        started_at = now
        state.mark_addressed(started_key, f"{started_at:.6f}")
        started_now = True
    else:
        try:
            started_at = float(started_raw)
        except (TypeError, ValueError):
            started_at = now
            state.mark_addressed(started_key, f"{started_at:.6f}")
            started_now = True

    elapsed_seconds = max(now - started_at, 0.0)
    remaining_seconds = config.non_check_reviewer_settle_seconds - elapsed_seconds
    if remaining_seconds <= 0:
        state.mark_addressed(done_key, "elapsed")
        state.threads_addressed_ids.pop(
            _non_check_reviewer_settle_freeze_key(
                pr_number=pr_number,
                head_sha=head_sha,
            ),
            None,
        )
        return _NonCheckReviewerSettleDecision(
            action="elapsed",
            configured_reviewers=configured_reviewers,
            missing_reviewers=missing_reviewers,
            visible_reviewers=visible_reviewers,
            started_at=started_at,
            elapsed_seconds=elapsed_seconds,
            remaining_seconds=0.0,
            state_changed=True,
        )

    wait_seconds = (
        remaining_seconds
        if config.poll_interval_seconds <= 0
        else min(config.poll_interval_seconds, remaining_seconds)
    )

    return _NonCheckReviewerSettleDecision(
        action="started" if started_now else "waiting",
        wait_seconds=wait_seconds,
        configured_reviewers=configured_reviewers,
        missing_reviewers=missing_reviewers,
        visible_reviewers=visible_reviewers,
        started_at=started_at,
        elapsed_seconds=elapsed_seconds,
        remaining_seconds=remaining_seconds,
        state_changed=started_now,
    )


def _non_check_reviewer_activity_settle_decision(
    status: PRStatus,
    state: MonitorState,
    config: MonitorConfig,
    *,
    pr_number: int,
    now: float,
    now_wall: datetime,
    configured_reviewers: tuple[str, ...],
    missing_reviewers: tuple[str, ...],
    visible_reviewers: tuple[str, ...],
) -> _NonCheckReviewerSettleDecision:
    """Return a settle decision anchored to the latest external review activity."""
    assert status.quiet_period_anchor_at is not None
    anchor_at = _utc_datetime(status.quiet_period_anchor_at)
    now_dt = _utc_datetime(now_wall)
    quiet_until = anchor_at + timedelta(seconds=config.non_check_reviewer_settle_seconds)
    elapsed_seconds = max((now_dt - anchor_at).total_seconds(), 0.0)
    remaining_seconds = max((quiet_until - now_dt).total_seconds(), 0.0)
    signature = _non_check_reviewer_activity_signature(
        status,
        anchor_at=anchor_at,
    )
    done_key = _non_check_reviewer_settle_done_key(
        pr_number=pr_number,
        head_sha=status.head_sha,
        activity_signature=signature,
    )
    started_key = _non_check_reviewer_settle_started_key(
        pr_number=pr_number,
        head_sha=status.head_sha,
        activity_signature=signature,
    )
    activity_freeze_seeded = _seed_non_check_reviewer_activity_freeze_marker(
        state,
        pr_number=pr_number,
        head_sha=status.head_sha,
        activity_started_key=started_key,
    )
    freeze_elapsed_seconds = _non_check_reviewer_activity_freeze_elapsed_seconds(
        state,
        started_key=started_key,
        now=now,
        now_wall=now_dt,
    )
    if freeze_elapsed_seconds is not None:
        remaining_seconds = max(
            config.non_check_reviewer_settle_seconds - freeze_elapsed_seconds,
            0.0,
        )
        freeze_started_wall = now_dt - timedelta(seconds=freeze_elapsed_seconds)
        quiet_until = freeze_started_wall + timedelta(
            seconds=config.non_check_reviewer_settle_seconds
        )
        if remaining_seconds <= 0:
            head_done_key = _non_check_reviewer_settle_done_key(
                pr_number=pr_number,
                head_sha=status.head_sha,
            )
            freeze_cleared = (
                state.threads_addressed_ids.pop(
                    _non_check_reviewer_settle_freeze_key(
                        pr_number=pr_number,
                        head_sha=status.head_sha,
                    ),
                    None,
                )
                is not None
            )
            head_done_recorded = state.threads_addressed_ids.get(head_done_key) == "elapsed"
            if not head_done_recorded:
                state.mark_addressed(head_done_key, "elapsed")
            if state.threads_addressed_ids.get(done_key) == "elapsed":
                return _NonCheckReviewerSettleDecision(
                    action="already_elapsed",
                    configured_reviewers=configured_reviewers,
                    missing_reviewers=missing_reviewers,
                    visible_reviewers=visible_reviewers,
                    elapsed_seconds=freeze_elapsed_seconds,
                    remaining_seconds=0.0,
                    activity_anchor_at=anchor_at,
                    activity_anchor_source=status.quiet_period_anchor_source,
                    quiet_until=quiet_until,
                    latest_external_review_activity_at=(status.latest_external_review_activity_at),
                    latest_external_review_activity_source=(
                        status.latest_external_review_activity_source
                    ),
                    activity_signature=signature,
                    state_changed=(freeze_cleared or not head_done_recorded),
                )
            state.mark_addressed(done_key, "elapsed")
            return _NonCheckReviewerSettleDecision(
                action="elapsed",
                configured_reviewers=configured_reviewers,
                missing_reviewers=missing_reviewers,
                visible_reviewers=visible_reviewers,
                elapsed_seconds=freeze_elapsed_seconds,
                remaining_seconds=0.0,
                activity_anchor_at=anchor_at,
                activity_anchor_source=status.quiet_period_anchor_source,
                quiet_until=quiet_until,
                latest_external_review_activity_at=status.latest_external_review_activity_at,
                latest_external_review_activity_source=(
                    status.latest_external_review_activity_source
                ),
                activity_signature=signature,
                state_changed=True,
            )
        wait_seconds = (
            remaining_seconds
            if config.poll_interval_seconds <= 0
            else min(config.poll_interval_seconds, remaining_seconds)
        )
        return _NonCheckReviewerSettleDecision(
            action="waiting",
            wait_seconds=wait_seconds,
            configured_reviewers=configured_reviewers,
            missing_reviewers=missing_reviewers,
            visible_reviewers=visible_reviewers,
            elapsed_seconds=freeze_elapsed_seconds,
            remaining_seconds=remaining_seconds,
            activity_anchor_at=anchor_at,
            activity_anchor_source=status.quiet_period_anchor_source,
            quiet_until=quiet_until,
            latest_external_review_activity_at=status.latest_external_review_activity_at,
            latest_external_review_activity_source=status.latest_external_review_activity_source,
            activity_signature=signature,
            state_changed=activity_freeze_seeded,
        )
    if state.threads_addressed_ids.get(done_key) == "elapsed":
        return _NonCheckReviewerSettleDecision(
            action="already_elapsed",
            configured_reviewers=configured_reviewers,
            missing_reviewers=missing_reviewers,
            visible_reviewers=visible_reviewers,
            elapsed_seconds=elapsed_seconds,
            remaining_seconds=0.0,
            activity_anchor_at=anchor_at,
            activity_anchor_source=status.quiet_period_anchor_source,
            quiet_until=quiet_until,
            latest_external_review_activity_at=status.latest_external_review_activity_at,
            latest_external_review_activity_source=status.latest_external_review_activity_source,
            activity_signature=signature,
        )
    if remaining_seconds <= 0:
        state.mark_addressed(done_key, "elapsed")
        return _NonCheckReviewerSettleDecision(
            action="elapsed",
            configured_reviewers=configured_reviewers,
            missing_reviewers=missing_reviewers,
            visible_reviewers=visible_reviewers,
            elapsed_seconds=elapsed_seconds,
            remaining_seconds=0.0,
            activity_anchor_at=anchor_at,
            activity_anchor_source=status.quiet_period_anchor_source,
            quiet_until=quiet_until,
            latest_external_review_activity_at=status.latest_external_review_activity_at,
            latest_external_review_activity_source=status.latest_external_review_activity_source,
            activity_signature=signature,
            state_changed=True,
        )

    wait_seconds = (
        remaining_seconds
        if config.poll_interval_seconds <= 0
        else min(config.poll_interval_seconds, remaining_seconds)
    )
    state_changed = state.threads_addressed_ids.get(started_key) != "activity_wait"
    if state_changed:
        state.mark_addressed(started_key, "activity_wait")
    return _NonCheckReviewerSettleDecision(
        action="started" if state_changed else "waiting",
        wait_seconds=wait_seconds,
        configured_reviewers=configured_reviewers,
        missing_reviewers=missing_reviewers,
        visible_reviewers=visible_reviewers,
        elapsed_seconds=elapsed_seconds,
        remaining_seconds=remaining_seconds,
        activity_anchor_at=anchor_at,
        activity_anchor_source=status.quiet_period_anchor_source,
        quiet_until=quiet_until,
        latest_external_review_activity_at=status.latest_external_review_activity_at,
        latest_external_review_activity_source=status.latest_external_review_activity_source,
        activity_signature=signature,
        state_changed=state_changed,
    )


def _seed_non_check_reviewer_activity_freeze_marker(
    state: MonitorState,
    *,
    pr_number: int,
    head_sha: str,
    activity_started_key: str,
) -> bool:
    """Copy an armed head-scoped remonitor freeze marker to the activity key."""
    if activity_started_key in state.threads_addressed_ids:
        return False
    freeze_key = _non_check_reviewer_settle_freeze_key(
        pr_number=pr_number,
        head_sha=head_sha,
    )
    if state.threads_addressed_ids.get(freeze_key) != "armed":
        return False
    head_started_key = _non_check_reviewer_settle_started_key(
        pr_number=pr_number,
        head_sha=head_sha,
    )
    started_raw = state.threads_addressed_ids.get(head_started_key)
    if started_raw is None:
        return False
    state.mark_addressed(activity_started_key, started_raw)
    return True


def _non_check_reviewer_activity_freeze_elapsed_seconds(
    state: MonitorState,
    *,
    started_key: str,
    now: float,
    now_wall: datetime,
) -> float | None:
    """Return elapsed seconds for a remonitor-armed activity freeze marker."""
    started_raw = state.threads_addressed_ids.get(started_key)
    started_wall_seconds = _settle_wall_seconds(started_raw)
    if started_wall_seconds is not None:
        return max(now_wall.timestamp() - started_wall_seconds, 0.0)
    if started_raw is None or started_raw == "activity_wait":
        return None
    try:
        started_monotonic = float(started_raw)
    except (TypeError, ValueError):
        return None
    return max(now - started_monotonic, 0.0)


def _settle_wall_seconds(raw: object) -> float | None:
    if not isinstance(raw, (str, bytes, bytearray, int, float)):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value >= 1_000_000_000:
        return value
    return None


def _non_check_reviewer_activity_signature(status: PRStatus, *, anchor_at: datetime) -> str:
    """Return a stable signature for the current settle activity anchor."""
    payload = "|".join(
        (
            status.head_sha,
            status.quiet_period_anchor_source or "",
            anchor_at.isoformat(),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _utc_datetime(value: datetime) -> datetime:
    """Normalize datetimes to timezone-aware UTC values."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _datetime_iso(value: datetime | None) -> str | None:
    """Serialize an optional datetime to ISO-8601 UTC, or ``None``."""
    if value is None:
        return None
    return _utc_datetime(value).isoformat()


def _non_check_reviewer_settle_wait_operation_context(
    config: MonitorConfig,
    decision: _NonCheckReviewerSettleDecision,
) -> _NonCheckReviewerSettleWaitOperationContext:
    """Build persisted wait-operation context for non-check reviewer settle state."""
    return _NonCheckReviewerSettleWaitOperationContext(
        extra_payload={
            "settle_seconds": config.non_check_reviewer_settle_seconds,
            "configured_reviewers": list(decision.configured_reviewers),
            "missing_reviewers": list(decision.missing_reviewers),
            "visible_reviewers": list(decision.visible_reviewers),
            "elapsed_seconds": decision.elapsed_seconds,
            "remaining_seconds": decision.remaining_seconds,
            "activity_anchor_at": _datetime_iso(decision.activity_anchor_at),
            "activity_anchor_source": decision.activity_anchor_source,
            "quiet_until": _datetime_iso(decision.quiet_until),
            "latest_external_review_activity_at": _datetime_iso(
                decision.latest_external_review_activity_at
            ),
            "latest_external_review_activity_source": (
                decision.latest_external_review_activity_source
            ),
        },
        extra_identity=(
            *decision.configured_reviewers,
            *decision.missing_reviewers,
            decision.started_at,
            decision.activity_signature,
        ),
    )


def _normalize_non_check_reviewer_logins(logins: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Normalize and dedupe configured reviewer logins."""
    normalized: list[str] = []
    seen: set[str] = set()
    for login in logins:
        value = _normalize_non_check_reviewer_identity(login)
        if not value or value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return tuple(normalized)


def _non_check_reviewer_visibility(
    *,
    configured_reviewers: tuple[str, ...],
    checks: tuple[CheckTiming, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Separate configured reviewers into visible and missing based on checks."""
    visible_identities = _visible_check_identities(checks)
    visible_reviewers: list[str] = []
    missing_reviewers: list[str] = []
    for reviewer in configured_reviewers:
        if _reviewer_has_visible_check(reviewer, visible_identities=visible_identities):
            visible_reviewers.append(reviewer)
        else:
            missing_reviewers.append(reviewer)
    return tuple(visible_reviewers), tuple(missing_reviewers)


def _visible_check_identities(checks: tuple[CheckTiming, ...]) -> frozenset[str]:
    """Extract normalized identities from check metadata and creator fields."""
    values: set[str] = set()
    for check in checks:
        for raw in (
            check.name,
            getattr(check, "app_slug", None),
            getattr(check, "app_name", None),
            getattr(check, "creator_login", None),
        ):
            normalized = _normalize_non_check_reviewer_identity(raw)
            if normalized:
                values.add(normalized)
    return frozenset(values)


def _reviewer_has_visible_check(
    reviewer: str,
    *,
    visible_identities: frozenset[str],
) -> bool:
    """Return whether a reviewer has a corresponding visible check identity."""
    aliases = _non_check_reviewer_visible_aliases(reviewer)
    for identity in visible_identities:
        for alias in aliases:
            if identity == alias or identity.startswith(f"{alias}-"):
                return True
            if alias == "greptile" and identity.endswith("-greptile"):
                return True
    return False


def _non_check_reviewer_visible_aliases(reviewer: str) -> frozenset[str]:
    """Expand reviewer identity variants used for check-name matching."""
    aliases = {reviewer}
    if reviewer == "greptile-apps" or reviewer.startswith("greptile-"):
        aliases.update({"greptile", "greptile-apps"})
    return frozenset(aliases)


def _normalize_non_check_reviewer_identity(value: object) -> str:
    """Normalize a reviewer/caller identity into lowercase token form."""
    if not isinstance(value, str):
        return ""
    text = value.strip().lower()
    if text.endswith("[bot]"):
        text = text[: -len("[bot]")]
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")
