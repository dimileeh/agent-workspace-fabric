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

from sqlalchemy.exc import SQLAlchemyError

from awf.adapters.provider_failures import AGENT_TIMEOUT
from awf.common.logging import get_logger
from awf.db.repositories import WorkspaceRepository
from awf.runtime.pr_monitor_runner.comment_verdict import MonitorVerdictResult, VerdictResult
from awf.runtime.pr_monitor_runner.comment_verdict_timeout_preserve import (
    AGENT_TIMEOUT_REASON_CODES,
)

if TYPE_CHECKING:
    from awf.runtime.pr_monitor import MonitorState, OperatorHint
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

_log = get_logger(__name__)

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


def timeout_retry_reason_code(verdict: VerdictResult | MonitorVerdictResult) -> str:
    """Return the watchdog reason code a granted retry must carry forward.

    ``should_retry_timed_out_hint`` has already proven the verdict is a
    ``MonitorVerdictResult`` carrying a code from ``AGENT_TIMEOUT_REASON_CODES``;
    the fallback only keeps the return type total for other callers.
    """
    reason_code = verdict.reason_code if isinstance(verdict, MonitorVerdictResult) else None
    return reason_code or AGENT_TIMEOUT


def mark_timeout_retry_used(state: MonitorState, hint: OperatorHint) -> None:
    """Spend the hint's single timeout retry."""
    state.mark_addressed(operator_hint_timeout_retry_key(hint), "retried")


async def mark_timeout_retry_used_durably(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    state: MonitorState,
    hint: OperatorHint,
) -> None:
    """Spend the retry in memory *and* on the workspace row.

    In memory alone is not enough for this marker. The hint stays durably
    ``pending`` in ``monitor_threads_addressed`` so ``decide()`` re-issues
    ``AddressOperatorHint``, but the budget that makes the retry *single* only
    reaches the DB through ``run()``'s post-``_execute`` ``_persist_state``. A
    worker killed in between — a shutdown cancellation, a crash, a container stop,
    or a ``_finish_monitor_operation`` failure — resumes against a pending hint
    with no marker and grants another supposedly-single retry, so a hint that keeps
    timing out in that window re-runs the agent instead of escalating to a human
    (PRRT_kwDOSJAM6s6fzBXq).

    Only this one key is written, merged onto the row's own map — never the whole
    ``MonitorState``, which inside a fix cycle still carries unconfirmed addressed
    verdicts a later failure only rolls back in memory (#305). That is the same
    single-key shape ``remember_item_start_head_durably`` already uses.

    Best-effort, like the preserve path it belongs to: a DB fault degrades to the
    in-memory marker and must not replace the timeout's reason code. The clearing
    side needs no durable twin — a crash that loses ``clear_timeout_retry`` also
    loses the terminal hint park, and the surviving marker only makes the next
    resume escalate sooner.
    """
    mark_timeout_retry_used(state, hint)
    session_factory = getattr(getattr(runner, "_deps", None), "session_factory", None)
    if not callable(session_factory):
        return
    try:
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get_for_update(workspace_id)
            if ws is None:
                return
            threads_addressed = dict(ws.monitor_threads_addressed or {})
            threads_addressed[operator_hint_timeout_retry_key(hint)] = "retried"
            ws.monitor_threads_addressed = threads_addressed
            await session.commit()
    except (SQLAlchemyError, OSError) as exc:
        _log.warning(
            "monitor.operator_hint_timeout_retry_durable_write_failed",
            workspace_id=workspace_id,
            operation_id=hint.operation_id,
            error=repr(exc)[:400],
        )


def clear_timeout_retry(state: MonitorState, hint: OperatorHint) -> None:
    """Drop the retry marker once the hint reaches a terminal outcome."""
    state.threads_addressed_ids.pop(operator_hint_timeout_retry_key(hint), None)
