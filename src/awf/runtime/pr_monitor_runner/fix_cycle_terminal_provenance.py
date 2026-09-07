"""Terminal fix-cycle result shaping for ``fix_cycle``.

Kept separate so ``fix_cycle`` stays under the first-party line budget.

Every terminal (non-retryable) fix-cycle exit has to answer one question for the
operator: where did the work that never reached the remote end up? These helpers
own that answer — they stamp the unpushed local HEAD onto a failed
``_GitPushResult``, or record that the HEAD could not be fingerprinted at all —
and they build the terminal result for an agent verdict-protocol breach, which
is an agent failure rather than something a human is asked to look at.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from awf.db.enums import FailureReason
from awf.runtime.pr_monitor_runner.comment_verdict import AgentVerdictProtocolError
from awf.runtime.pr_monitor_runner.constants import (
    _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
)
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.remote_ops import (
    _GitPushResult,
)


def _agent_verdict_protocol_failure_result(
    exc: AgentVerdictProtocolError,
) -> _GitPushResult:
    """Return a terminal agent failure without creating human-attention state."""
    return _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr=str(exc),
        reason_code=exc.reason_code,
        failure_reason=FailureReason.agent_failure,
    )


def _git_push_result_with_terminal_head_provenance_unavailable(
    push_result: _GitPushResult,
) -> _GitPushResult:
    """Mark terminal failures whose unpushed HEAD could not be fingerprinted."""
    if not push_result.failed:
        return push_result
    details = dict(push_result.details or {})
    if details.get("local_terminal_head_provenance_unavailable"):
        return push_result
    if details.get("local_terminal_head_sha"):
        return push_result
    details["local_terminal_head_provenance_unavailable"] = True
    return _GitPushResult(
        pushed=push_result.pushed,
        failed=push_result.failed,
        returncode=push_result.returncode,
        stdout=push_result.stdout,
        stderr=push_result.stderr,
        recovered_by_resync=push_result.recovered_by_resync,
        reason_code=push_result.reason_code,
        failure_reason=push_result.failure_reason,
        details=details,
        paused_into_blocked=push_result.paused_into_blocked,
        parked_needs_human=push_result.parked_needs_human,
    )


def _git_push_result_with_local_terminal_head(
    push_result: _GitPushResult,
    *,
    operation_start_head: str,
    local_head: str | None,
) -> _GitPushResult:
    """Attach unpushed local HEAD provenance to a failed fix-cycle result."""
    if not push_result.failed:
        return push_result
    if not local_head or local_head.lower() == operation_start_head.lower():
        return push_result
    details = dict(push_result.details or {})
    if details.get("local_terminal_head_sha"):
        return push_result
    details["local_terminal_head_sha"] = local_head
    return _GitPushResult(
        pushed=push_result.pushed,
        failed=push_result.failed,
        returncode=push_result.returncode,
        stdout=push_result.stdout,
        stderr=push_result.stderr,
        recovered_by_resync=push_result.recovered_by_resync,
        reason_code=push_result.reason_code,
        failure_reason=push_result.failure_reason,
        details=details,
        paused_into_blocked=push_result.paused_into_blocked,
        parked_needs_human=push_result.parked_needs_human,
    )


async def _enrich_failed_fix_cycle_result(
    self: Any,
    push_result: _GitPushResult,
    *,
    worktree_path: Path,
    operation_start_head: str,
) -> _GitPushResult:
    """Record unpushed local HEAD on terminal failed fix-cycle exits for provenance."""
    if not push_result.failed or not push_result.terminal_monitor_failure:
        return push_result
    if push_result.reason_code == _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON:
        return _git_push_result_with_terminal_head_provenance_unavailable(push_result)
    try:
        local_head = await self._rev_parse_head(worktree_path)
    except (TimeoutError, OSError, subprocess.SubprocessError):
        _log.warning(
            "monitor.fix_cycle_terminal_head_provenance_unavailable",
            reason_code=push_result.reason_code,
        )
        return _git_push_result_with_terminal_head_provenance_unavailable(push_result)
    if not local_head:
        _log.warning(
            "monitor.fix_cycle_terminal_head_provenance_unavailable",
            reason_code=push_result.reason_code,
        )
        return _git_push_result_with_terminal_head_provenance_unavailable(push_result)
    return _git_push_result_with_local_terminal_head(
        push_result,
        operation_start_head=operation_start_head,
        local_head=local_head,
    )
