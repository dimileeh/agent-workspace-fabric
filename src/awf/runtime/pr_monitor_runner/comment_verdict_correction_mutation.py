"""Correction-attempt mutation gates for the comment verdict protocol.

Kept separate so ``comment_verdict`` stays under the first-party line budget;
re-exported from ``comment_verdict`` for callers and tests.

Both helpers here serve the same rule: a *correction* attempt may only be
credited with a non-FIXED verdict when AWF can prove that attempt did not
mutate the worktree. That needs an end-of-attempt HEAD (``read_correction_end_head``);
when the measurement says the attempt did mutate, the verdict is refused after a
safe rollback (``raise_correction_non_fixed_mutation``). Both fail closed: an
unreadable HEAD is a refusal, never an assumed-clean acceptance.

The rollback helpers and the trusted HEAD probe are resolved through
``comment_verdict`` at call time so monkeypatches on that module (and the
``comments`` forwarding shim) still apply.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from awf.common.logging import get_logger

if TYPE_CHECKING:
    from awf.runtime.pr_monitor import MonitorState
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

_log = get_logger(__name__)


async def read_correction_end_head(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
    rev_parse_head: Any,
    attempt_start_head: str,
    item_start_head: str | None,
    rollback_floor_head: str | None,
    item_start_last_push_sha: str | None,
    state: MonitorState | None,
    protocol_attempt: int,
    verdict: str,
) -> str:
    """Read the correction attempt's end HEAD, or refuse the non-FIXED verdict.

    Returns ``attempt_start_head`` unchanged when there is no worktree to probe.
    Otherwise the live HEAD is read through the trusted item-start Git config +
    timeout so an ``include.path`` → FIFO cannot hang the worker
    (PRRT_kwDOSJAM6s6e4egQ), and both failure shapes roll back first:

    * an ordinary probe failure (``OSError``/``RuntimeError`` while spawning Git)
      is outside the caller's ``Exception`` handlers, whose surrounding handler
      catches only ``CancelledError``, so the correction attempt's edits would be
      stranded (PRRT_kwDOSJAM6s6eJ2Tg, matching PRRT_kwDOSJAM6s6eJUbE);
    * a transient ``None`` must not leave the end HEAD equal to
      ``attempt_start_head``: a clean self-commit would then miss mutation and a
      later successful rollback could accept FALSE POSITIVE / DEFER /
      NEEDS_HUMAN (PRRT_kwDOSJAM6s6eIz5m).
    """
    from awf.runtime.pr_monitor_runner import comment_verdict as _comment_verdict

    if not worktree_path.exists():
        return attempt_start_head
    try:
        live_head = await _comment_verdict.read_protocol_attempt_start_head(
            runner,
            worktree_path=worktree_path,
            rev_parse_head=(rev_parse_head if callable(rev_parse_head) else None),
        )
    except Exception as end_head_exc:
        rollback_ok = await _comment_verdict._rollback_or_classify_failure(
            runner,
            workspace_id=workspace_id,
            worktree_path=worktree_path,
            item_start_head=rollback_floor_head,
            item_start_last_push_sha=item_start_last_push_sha,
            state=state,
        )
        if not rollback_ok:
            _log.warning(
                "monitor.agent_verdict_correction_end_head_rollback_failed",
                workspace_id=workspace_id,
                item_start_head=item_start_head,
                protocol_attempt=protocol_attempt,
                exc_type=type(end_head_exc).__name__,
            )
            raise _comment_verdict.AgentVerdictProtocolError(
                reason_code=_comment_verdict.AGENT_VERDICT_PROTOCOL_VIOLATION,
                message=(
                    "Could not roll back unaccepted edits after correction-end HEAD probe failure."
                ),
            ) from end_head_exc
        raise
    if live_head:
        return live_head
    _log.warning(
        "monitor.agent_verdict_correction_end_head_unreadable",
        workspace_id=workspace_id,
        reason_code=_comment_verdict.AGENT_VERDICT_PROTOCOL_VIOLATION,
        protocol_attempt=protocol_attempt,
        attempt_start_head=attempt_start_head,
        verdict=verdict,
    )
    rollback_ok = await _comment_verdict._rollback_unaccepted_protocol_retry_changes(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        item_start_head=rollback_floor_head,
        item_start_last_push_sha=item_start_last_push_sha,
        state=state,
    )
    if not rollback_ok:
        raise _comment_verdict.AgentVerdictProtocolError(
            reason_code=_comment_verdict.AGENT_VERDICT_PROTOCOL_VIOLATION,
            message=(
                "Could not roll back unaccepted edits after "
                "correction attempt with unreadable end HEAD."
            ),
        )
    raise _comment_verdict.AgentVerdictProtocolError(
        reason_code=_comment_verdict.AGENT_VERDICT_PROTOCOL_VIOLATION,
        message=(
            "Correction attempt end HEAD was unreadable; cannot accept a "
            "non-FIXED verdict without measuring whether the worktree advanced."
        ),
    )


async def raise_correction_non_fixed_mutation(
    runner: PullRequestMonitorRunner,
    *,
    workspace_id: str,
    worktree_path: Path,
    rollback_floor_head: str | None,
    item_start_last_push_sha: str | None,
    state: MonitorState | None,
    protocol_attempt: int,
    attempt_start_head: str | None,
    post_attempt_head: str | None,
    verdict: str,
    dirty_changes_committed: bool,
    stranded_dirty_residue: bool,
    pre_sink_head_unreadable: bool,
    pre_sink_probe_exc: Exception | None,
) -> NoReturn:
    """Roll back a mutating correction attempt, then refuse its non-FIXED verdict.

    ``pre_sink_head_unreadable`` distinguishes "the attempt demonstrably mutated"
    (``AGENT_NON_FIXED_WITH_MUTATION``) from "mutation could not be measured, so
    fail closed" (``AGENT_VERDICT_PROTOCOL_VIOLATION``); the originating probe
    failure is chained as the cause so it is not lost. Never returns.
    """
    from awf.runtime.pr_monitor_runner import comment_verdict as _comment_verdict

    if pre_sink_head_unreadable:
        mutation_reason_code = _comment_verdict.AGENT_VERDICT_PROTOCOL_VIOLATION
        mutation_log_event = "monitor.agent_verdict_correction_pre_sink_head_unreadable"
        mutation_message = (
            "Pre-sink HEAD was unreadable; cannot accept a "
            "non-FIXED verdict without measuring whether the "
            "correction attempt self-committed."
        )
        rollback_failure_message = (
            "Could not roll back unaccepted edits after "
            "correction attempt with unreadable pre-sink HEAD."
        )
    else:
        mutation_reason_code = _comment_verdict.AGENT_NON_FIXED_WITH_MUTATION
        mutation_log_event = "monitor.agent_verdict_correction_non_fixed_with_mutation"
        mutation_message = (
            "Correction attempt mutated the worktree then reported a non-FIXED verdict."
        )
        rollback_failure_message = (
            "Could not roll back unaccepted edits after "
            "correction attempt mutated state then "
            "reported a non-FIXED verdict."
        )
    _log.warning(
        mutation_log_event,
        workspace_id=workspace_id,
        reason_code=mutation_reason_code,
        protocol_attempt=protocol_attempt,
        attempt_start_head=attempt_start_head,
        current_head=post_attempt_head,
        verdict=verdict,
        dirty_changes_committed=dirty_changes_committed,
        stranded_dirty_residue=stranded_dirty_residue,
    )
    rollback_ok = await _comment_verdict._rollback_or_classify_failure(
        runner,
        workspace_id=workspace_id,
        worktree_path=worktree_path,
        item_start_head=rollback_floor_head,
        item_start_last_push_sha=item_start_last_push_sha,
        state=state,
    )
    if not rollback_ok:
        rollback_error = _comment_verdict.AgentVerdictProtocolError(
            reason_code=mutation_reason_code,
            message=rollback_failure_message,
        )
        if pre_sink_probe_exc is not None:
            raise rollback_error from pre_sink_probe_exc
        raise rollback_error
    mutation_error = _comment_verdict.AgentVerdictProtocolError(
        reason_code=mutation_reason_code,
        message=mutation_message,
    )
    if pre_sink_probe_exc is not None:
        raise mutation_error from pre_sink_probe_exc
    raise mutation_error
