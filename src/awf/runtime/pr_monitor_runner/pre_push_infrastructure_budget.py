"""Attempt budget for repeated infrastructure pre-push validation fix failures (#1182).

The fix-pass agent can repair product failures (lint, tests), but not a validation
command that failed for an execution-context reason: not executable (126), invalid
invocation or usage (128+, e.g. ``git diff`` exiting 129 outside a work tree), or
killed by a signal. Before this budget, such a ``PRE_PUSH_VALIDATION_FIX_FAILED``
re-queued ``AddressComments`` and the next poll re-ran the whole batch with fresh
agents, forever.

The budget follows the sync-base no-progress pattern (signature + count) and the
#935 park pattern (row stays ``monitoring_pr``, attention flag set, a reserved
state key, a gate before any item work):

* every comment-repair cycle result is recorded: an infrastructure-signature
  ``PRE_PUSH_VALIDATION_FIX_FAILED`` counts, anything else ends the episode;
* the signature includes the *remote* PR head, which stays fixed while nothing is
  published, so a human push to the PR resets the budget;
* at the cap the cycle result becomes a ``parked_needs_human`` envelope carrying
  the failing command, return code and validation run id, and the gate at the top
  of ``_run_fix_cycle`` returns that envelope on later polls without running agents.

Genuine product failures (any other return code) keep today's retry behavior.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.pre_push_validation_constants import (
    _PRE_PUSH_INFRASTRUCTURE_FAILURE_PARK_AFTER,
    _PRE_PUSH_VALIDATION_FIX_FAILED_REASON,
    _PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILURE_PARKED_REASON,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from awf.runtime.pr_monitor_runner.remote_repair_unpublished_provenance import (
    _append_disposition_event,
)

_PARKED_EVENT = "monitor.pre_push_validation_infrastructure_parked"

# Shell statuses that mean the command never ran as intended: 126 "not
# executable", 128+ invalid invocation/usage or signal death, and a negative
# status for a signal reported by Python. 127 is excluded on purpose: it is the
# already-terminal ``PRE_PUSH_VALIDATION_TOOLCHAIN_MISSING``.
_NOT_EXECUTABLE_RETURNCODE = 126
_RESERVED_RETURNCODE_FLOOR = 128

_EVIDENCE_KEYS = (
    "failing_command",
    "failing_returncode",
    "validation_run_id",
    "validation_reason_code",
)


def _is_infrastructure_returncode(returncode: object) -> bool:
    """Return whether a validation exit status is a shell execution-context failure."""
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        return False
    return (
        returncode == _NOT_EXECUTABLE_RETURNCODE
        or returncode >= _RESERVED_RETURNCODE_FLOOR
        or returncode < 0
    )


def pre_push_infrastructure_failure_signature(
    push_result: _GitPushResult,
    *,
    pr_head_sha: str,
) -> str | None:
    """Return the budget signature for an infrastructure fix failure, else ``None``."""
    if not push_result.failed or push_result.reason_code != _PRE_PUSH_VALIDATION_FIX_FAILED_REASON:
        return None
    details = push_result.details or {}
    returncode = details.get("failing_returncode")
    if not _is_infrastructure_returncode(returncode):
        return None
    material = json.dumps(
        [
            push_result.reason_code,
            details.get("validation_reason_code"),
            details.get("failing_command"),
            returncode,
            pr_head_sha,
        ],
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def _load_record(state: MonitorState) -> dict[str, Any] | None:
    """Parse the persisted budget record, ignoring malformed values."""
    raw = state.pre_push_infra_failure
    if raw is None:
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    count = record.get("count")
    if isinstance(count, bool) or not isinstance(count, int):
        return None
    if not isinstance(record.get("signature"), str) or not isinstance(
        record.get("pr_head_sha"), str
    ):
        return None
    return record


def _store_record(state: MonitorState, record: dict[str, Any]) -> None:
    """Persist the budget record on the live monitor state."""
    state.mark_pre_push_infra_failure(json.dumps(record, sort_keys=True))


def record_pre_push_infrastructure_failure(
    state: MonitorState,
    push_result: _GitPushResult,
    *,
    pr_head_sha: str,
) -> int:
    """Count this cycle against the budget; return the attempt count (0 when cleared)."""
    signature = pre_push_infrastructure_failure_signature(push_result, pr_head_sha=pr_head_sha)
    if signature is None:
        state.clear_pre_push_infra_failure()
        return 0
    previous = _load_record(state)
    count = 1
    if previous is not None and previous["signature"] == signature:
        count = previous["count"] + 1
    details = push_result.details or {}
    _store_record(
        state,
        {
            "signature": signature,
            "count": count,
            "pr_head_sha": pr_head_sha,
            "evidence": {key: details[key] for key in _EVIDENCE_KEYS if key in details},
            "event_recorded": False,
        },
    )
    return count


def _active_record(state: MonitorState, *, pr_head_sha: str) -> dict[str, Any] | None:
    """Return the budget record when it has reached the cap on this PR head."""
    record = _load_record(state)
    if record is None:
        return None
    if record["count"] < _PRE_PUSH_INFRASTRUCTURE_FAILURE_PARK_AFTER:
        return None
    if record["pr_head_sha"] != pr_head_sha:
        return None
    return record


def pre_push_infrastructure_park_active(state: MonitorState, *, pr_head_sha: str) -> bool:
    """Return whether the comment-repair arm is parked on this PR head."""
    return _active_record(state, pr_head_sha=pr_head_sha) is not None


def pre_push_infrastructure_park_result(
    state: MonitorState,
    *,
    pr_head_sha: str,
) -> _GitPushResult | None:
    """Return the parked envelope once the cap is reached on this PR head."""
    record = _active_record(state, pr_head_sha=pr_head_sha)
    if record is None:
        return None
    evidence = record.get("evidence")
    evidence = dict(evidence) if isinstance(evidence, dict) else {}
    command = evidence.get("failing_command", "<unknown>")
    returncode = evidence.get("failing_returncode", "<unknown>")
    message = (
        f"Pre-push validation command `{command}` failed with exit {returncode} "
        f"{record['count']} times on PR head {pr_head_sha[:10]}; this looks like a "
        "validation environment failure the repair agent cannot fix, so AWF parked "
        "comment repair for a human instead of re-running the agent."
    )
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr=message,
        reason_code=_PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILURE_PARKED_REASON,
        parked_needs_human=True,
        details={
            "phase": "pre_push_validation",
            "pushed": False,
            **evidence,
            "underlying_reason_code": _PRE_PUSH_VALIDATION_FIX_FAILED_REASON,
            "attempts": record["count"],
            "cap": _PRE_PUSH_INFRASTRUCTURE_FAILURE_PARK_AFTER,
            "infrastructure_signature": record["signature"],
            "pr_head_sha": pr_head_sha,
        },
    )


async def bound_pre_push_infrastructure_failure(
    runner: Any,
    *,
    workspace_id: str,
    state: MonitorState,
    push_result: _GitPushResult,
    pr_head_sha: str,
) -> _GitPushResult:
    """Apply the budget to one comment-repair cycle result.

    Returns the parked envelope once the cap is reached (or when the fix-cycle gate
    already returned it), otherwise ``push_result`` unchanged. Pauses and other parks
    leave the budget untouched. The operator-facing event is appended once per
    episode; a failed best-effort append is retried on the next parked poll.
    """
    parked: _GitPushResult | None = push_result
    if push_result.reason_code != _PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILURE_PARKED_REASON:
        if push_result.paused_into_blocked or push_result.parked_needs_human:
            return push_result
        record_pre_push_infrastructure_failure(state, push_result, pr_head_sha=pr_head_sha)
        parked = pre_push_infrastructure_park_result(state, pr_head_sha=pr_head_sha)
    if parked is None:
        return push_result
    record = _load_record(state)
    if record is not None and not record.get("event_recorded"):
        recorded = await _append_disposition_event(
            runner,
            workspace_id=workspace_id,
            event_type=_PARKED_EVENT,
            reason_code=_PRE_PUSH_VALIDATION_INFRASTRUCTURE_FAILURE_PARKED_REASON,
            payload=dict(parked.details or {}),
        )
        if recorded:
            record["event_recorded"] = True
            _store_record(state, record)
    return parked
