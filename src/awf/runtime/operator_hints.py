"""Pure operator remonitor hint state helpers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal, cast

from awf.runtime.monitor_state_keys import (
    _initial_review_grace_done_key,
    _initial_review_grace_started_key,
    _initial_review_grace_wall_started_value_from_datetime,
    _non_check_reviewer_settle_done_key,
    _non_check_reviewer_settle_freeze_key,
    _non_check_reviewer_settle_started_key,
)
from awf.runtime.pr_monitor import MonitorState, OperatorHint

OPERATOR_HINT_STATE_KEY = "__awf_pending_operator_hint__"
OPERATOR_HINT_PROCESSED_KEY_PREFIX = "__awf_operator_hint_processed__:"


def operator_hint_from_threads(threads_addressed: dict[str, str]) -> OperatorHint | None:
    """Read a pending operator hint from persisted monitor state."""
    raw = threads_addressed.get(OPERATOR_HINT_STATE_KEY)
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return None
    status = payload.get("status")
    if status not in {"pending", "needs_human", "agent_failed"}:
        status = "pending"
    return OperatorHint(
        reason=reason,
        operation_id=_optional_string(payload.get("operation_id")),
        requested_at=_optional_string(payload.get("requested_at")),
        reason_code=_optional_string(payload.get("reason_code")) or "OPERATOR_REMONITOR",
        status=cast(Literal["pending", "needs_human", "agent_failed"], status),
        status_reason=_optional_string(payload.get("status_reason")),
    )


def persist_operator_hint(
    threads_addressed: dict[str, str],
    hint: OperatorHint | None,
) -> dict[str, str]:
    """Store or clear the operator hint in persisted monitor state."""
    if hint is None:
        threads_addressed.pop(OPERATOR_HINT_STATE_KEY, None)
        return threads_addressed
    payload = {
        "reason": hint.reason,
        "operation_id": hint.operation_id,
        "requested_at": hint.requested_at,
        "reason_code": hint.reason_code,
        "status": hint.status,
        "status_reason": hint.status_reason,
    }
    threads_addressed[OPERATOR_HINT_STATE_KEY] = json.dumps(
        {key: value for key, value in payload.items() if value is not None},
        sort_keys=True,
        separators=(",", ":"),
    )
    return threads_addressed


def operator_hint_processed_key(operation_id: str) -> str:
    return f"{OPERATOR_HINT_PROCESSED_KEY_PREFIX}{operation_id}"


def mark_operator_hint_processed(state: MonitorState) -> None:
    """Clear a pending hint after a repair pass produced/pushed work."""
    hint = state.pending_operator_hint
    if hint is not None and hint.operation_id:
        state.mark_addressed(operator_hint_processed_key(hint.operation_id), "processed")
    state.pending_operator_hint = None


def mark_operator_hint_needs_human(state: MonitorState, reason: str) -> None:
    hint = state.pending_operator_hint
    if hint is None:
        return
    state.pending_operator_hint = OperatorHint(
        reason=hint.reason,
        operation_id=hint.operation_id,
        requested_at=hint.requested_at,
        reason_code=hint.reason_code,
        status="needs_human",
        status_reason=reason,
    )


def mark_operator_hint_agent_failed(state: MonitorState, reason: str) -> None:
    hint = state.pending_operator_hint
    if hint is None:
        return
    state.pending_operator_hint = OperatorHint(
        reason=hint.reason,
        operation_id=hint.operation_id,
        requested_at=hint.requested_at,
        reason_code=hint.reason_code,
        status="agent_failed",
        status_reason=reason,
    )


def remonitor_has_elapsed_settle(
    threads_addressed: dict[str, str],
    *,
    pr_number: int | None,
    head_sha: str | None,
) -> bool:
    """Return whether persisted state shows reviewer-settle already elapsed."""
    if pr_number is None or not head_sha:
        return False
    done_key = _non_check_reviewer_settle_done_key(pr_number=pr_number, head_sha=head_sha)
    done_prefix = f"{done_key}:"
    return any(
        (key == done_key or key.startswith(done_prefix)) and value == "elapsed"
        for key, value in threads_addressed.items()
    )


def remonitor_elapsed_settle_head_shas(
    threads_addressed: dict[str, str],
    *,
    pr_number: int | None,
    preferred_head_sha: str | None,
    current_head_sha: str | None = None,
) -> tuple[str, ...]:
    """Return PR head SHAs that need reviewer-settle freeze for remonitor."""
    if pr_number is None:
        return ()
    if current_head_sha:
        if remonitor_has_elapsed_settle(
            threads_addressed,
            pr_number=pr_number,
            head_sha=current_head_sha,
        ):
            return (current_head_sha,)
        return ()
    head_shas: list[str] = []
    if remonitor_has_elapsed_settle(
        threads_addressed,
        pr_number=pr_number,
        head_sha=preferred_head_sha,
    ):
        head_shas.append(cast(str, preferred_head_sha))
    done_prefix = _non_check_reviewer_settle_done_key(
        pr_number=pr_number,
        head_sha="",
    )
    for key, value in threads_addressed.items():
        if value != "elapsed" or not key.startswith(done_prefix):
            continue
        head_sha = key.removeprefix(done_prefix).split(":", 1)[0]
        if head_sha and head_sha not in head_shas:
            head_shas.append(head_sha)
    return tuple(head_shas)


def arm_operator_hint_freeze(
    threads_addressed: dict[str, str],
    *,
    pr_number: int,
    head_sha: str,
    now: datetime,
) -> None:
    """Re-arm existing grace/settle state after a past-settle remonitor."""
    started_at = _initial_review_grace_wall_started_value_from_datetime(now)
    threads_addressed.pop(_initial_review_grace_done_key(pr_number), None)
    threads_addressed[_initial_review_grace_started_key(pr_number)] = started_at

    settle_done_key = _non_check_reviewer_settle_done_key(
        pr_number=pr_number,
        head_sha=head_sha,
    )
    settle_done_activity_prefix = f"{settle_done_key}:"
    settle_started_key = _non_check_reviewer_settle_started_key(
        pr_number=pr_number,
        head_sha=head_sha,
    )
    settle_started_activity_signatures: list[str] = []
    for key, value in list(threads_addressed.items()):
        if key == settle_done_key or key.startswith(settle_done_activity_prefix):
            if key.startswith(settle_done_activity_prefix) and value == "elapsed":
                activity_signature = key.removeprefix(settle_done_activity_prefix)
                if (
                    activity_signature
                    and activity_signature not in settle_started_activity_signatures
                ):
                    settle_started_activity_signatures.append(activity_signature)
            threads_addressed.pop(key, None)
        if key == settle_started_key or key.startswith(f"{settle_started_key}:"):
            threads_addressed.pop(key, None)
    threads_addressed[settle_started_key] = started_at
    threads_addressed[
        _non_check_reviewer_settle_freeze_key(
            pr_number=pr_number,
            head_sha=head_sha,
        )
    ] = "armed"
    for activity_signature in settle_started_activity_signatures:
        threads_addressed[
            _non_check_reviewer_settle_started_key(
                pr_number=pr_number,
                head_sha=head_sha,
                activity_signature=activity_signature,
            )
        ] = started_at


def build_pending_operator_hint_payload(hint: OperatorHint) -> dict[str, object]:
    return {
        key: value
        for key, value in {
            "reason": hint.reason,
            "operation_id": hint.operation_id,
            "requested_at": hint.requested_at,
            "reason_code": hint.reason_code,
            "status": hint.status,
            "status_reason": hint.status_reason,
        }.items()
        if value is not None
    }


def utcnow() -> datetime:
    return datetime.now(UTC)


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
