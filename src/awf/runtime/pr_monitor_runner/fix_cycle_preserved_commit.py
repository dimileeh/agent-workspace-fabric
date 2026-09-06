"""Unpublished-commit provenance bookkeeping for the fix cycle.

A failed fix-cycle push leaves the agent's repair commit authored but
unpublished in the worktree. The next cycle's
``_abandon_unpublished_comment_repairs`` only resets local-ahead commits a prior
operation *provably* owns, and this module records that proof onto the
``_GitPushResult`` — either the local HEAD sha or an explicit
"provenance unavailable" marker. Split out of ``fix_cycle`` so the orchestration
module stays under the first-party line limit; behaviour is unchanged and
``fix_cycle`` re-exports these helpers.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from awf.runtime.pr_monitor_runner.constants import (
    _HEAD_OBJECT_MISSING_UNRECOVERABLE_REASON,
)
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.remote_ops import (
    _GitPushResult,
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
    )


async def _enrich_failed_fix_cycle_result(
    self: Any,
    push_result: _GitPushResult,
    *,
    worktree_path: Path,
    operation_start_head: str,
) -> _GitPushResult:
    """Record unpushed local HEAD on failed fix-cycle exits for provenance.

    A retryable exit (an ordinary ``GIT_PUSH_FAILED`` that never resynced) leaves
    the same authored-but-unpublished commit behind as a terminal one, and the
    next cycle's ``_abandon_unpublished_comment_repairs`` only resets local-ahead
    commits a prior operation *provably* owns — that proof is this recorded HEAD.
    Without it the retry the push-dependency requeue is meant to enable instead
    fails closed with ``COMMENT_REPAIR_UNPUBLISHED_PROVENANCE_MISSING``
    (PRRT_kwDOSJAM6s6fp2uF). A protected-scope pause is excluded: it preserves
    its commit for an operator decision and the abandon path already skips it.
    """
    if not push_result.failed or push_result.paused_into_blocked:
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
    enriched = _git_push_result_with_local_terminal_head(
        push_result,
        operation_start_head=operation_start_head,
        local_head=local_head,
    )
    if enriched is not push_result:
        # A retryable exit keeps the workspace in ``monitoring_pr``, so — unlike a
        # terminal one, whose failure details reach the workspace event — this
        # recorded head is otherwise only visible inside the operation result. Emit
        # it so the unpublished repair commit the next cycle must abandon before
        # re-addressing stays traceable across cycles (AGENTS.md: retries preserve
        # reason codes, logs, and events).
        _log.info(
            "monitor.fix_cycle_unpublished_repair_head_recorded",
            reason_code=push_result.reason_code,
            terminal=push_result.terminal_monitor_failure,
            local_head_sha=local_head,
        )
    return enriched
