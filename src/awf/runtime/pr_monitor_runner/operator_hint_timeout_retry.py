"""One retry for an operator hint whose agent timed out with work preserved (#932).

Before #932 an ``AGENT_IDLE_TIMEOUT`` on an operator-hint run rolled the agent's
commit away and parked the monitor at ``NotifyHuman`` on the very first failure
(ws_84fddb4a98c94f7b8d6aa0d3 / PR #922). The timeout now preserves the work, so
the hint is worth one more pass before a human is called: leave it ``pending``
so ``decide()`` returns ``AddressOperatorHint`` again, and record a marker. A
second timeout finds the marker and parks exactly as before.

The retry is deliberately narrow — it only applies when the watchdog fired *and*
something was preserved. A timeout that salvaged nothing, or any other
``agent_failed``, keeps today's park-immediately behaviour.

Kept in a sibling module so ``operator_hints`` stays under the line budget.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from awf.runtime.pr_monitor_runner.comment_verdict import MonitorVerdictResult, VerdictResult
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    AGENT_TIMEOUT_REASON_CODES,
)

if TYPE_CHECKING:
    from awf.runtime.pr_monitor import MonitorState, OperatorHint

_OPERATOR_HINT_TIMEOUT_RETRY_KEY_PREFIX = "__awf_operator_hint_timeout_retry__:"


def operator_hint_timeout_retry_key(hint: OperatorHint) -> str:
    """Reserved ``MonitorState.threads_addressed_ids`` key for this hint's retry budget."""
    return f"{_OPERATOR_HINT_TIMEOUT_RETRY_KEY_PREFIX}{hint.operation_id or 'pending'}"


def should_retry_timed_out_hint(
    state: MonitorState,
    hint: OperatorHint,
    verdict: VerdictResult | MonitorVerdictResult,
) -> bool:
    """True when this hint earned its single timeout retry."""
    # Only the wider monitor result carries a reason code; an in-protocol
    # ``VerdictResult`` never reports ``agent_failed`` in the first place.
    if not isinstance(verdict, MonitorVerdictResult):
        return False
    if verdict.verdict != "agent_failed":
        return False
    if verdict.reason_code not in AGENT_TIMEOUT_REASON_CODES:
        return False
    if not verdict.preserved_head_sha:
        # Nothing survived the timeout, so a retry resumes from nowhere.
        return False
    return not state.threads_addressed_ids.get(operator_hint_timeout_retry_key(hint))


def mark_timeout_retry_used(state: MonitorState, hint: OperatorHint) -> None:
    """Spend the hint's single timeout retry."""
    state.mark_addressed(operator_hint_timeout_retry_key(hint), "retried")


def clear_timeout_retry(state: MonitorState, hint: OperatorHint) -> None:
    """Drop the retry marker once the hint reaches a terminal outcome."""
    state.threads_addressed_ids.pop(operator_hint_timeout_retry_key(hint), None)
