"""Resolve addressed review threads that became OUTDATED (#473).

When the PR monitor addresses a review thread by changing code ELSEWHERE (a
different file/line than the comment anchor), the forge marks the original thread
``isOutdated=true``. Both forge clients drop outdated threads from
``PRStatus.unresolved_inline_threads`` because they are non-blocking for merge,
so ``decide()`` sees zero unresolved threads and proceeds to ``Merge`` — correct.
But the fix-cycle resolve loop only iterates that actionable feed, so the
now-outdated thread is never resolved. The result is a merged PR with a lingering
"unresolved" comment even though the monitor recorded a fix verdict in
``state.threads_addressed_ids`` — eroding the operator-visible "did AWF handle
everything?" signal.

This focused step closes that gap forge-neutrally. The forge clients surface the
addressed-but-outdated threads in ``PRStatus.outdated_unresolved_inline_threads``;
here we resolve only the ones the monitor already recorded with a fix verdict.
``defer`` / ``needs_human`` / ``agent_failed`` threads legitimately stay open.
"""

from __future__ import annotations

from typing import Any

from awf.common.bitbucket_client import BitBucketClientError
from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import RepoRef
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.pr_monitor import MonitorState, PRStatus
from awf.runtime.pr_monitor_runner.constants import (
    _AUDIT_COMMENT_RESOLUTION_EVENT,
    _BITBUCKET_TRANSIENT_RETRY_REASON,
    _GITHUB_TRANSIENT_RETRY_REASON,
)
from awf.runtime.pr_monitor_runner.fix_cycle import _RESOLVABLE_THREAD_VERDICTS
from awf.runtime.pr_monitor_runner.logging import _log

# Verdicts whose now-OUTDATED threads this hygiene step may resolve. This is the
# fix-cycle's ``_RESOLVABLE_THREAD_VERDICTS`` MINUS ``defer``: a defer's thread
# is only resolved after its follow-up work is durably captured (an explanatory
# comment + tracking issue), which the fix cycle gates on at address time. That
# capture cannot be re-verified here, so an outdated defer thread is left open
# rather than silently resolved without its tracking issue. The kept verdicts
# (``fix_committed`` / ``false_positive``) both mean "handled, thread should
# close, no human follow-up".
_OUTDATED_RESOLVABLE_THREAD_VERDICTS = _RESOLVABLE_THREAD_VERDICTS - frozenset({"defer"})


async def _resolve_addressed_outdated_threads(
    self: Any,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    status: PRStatus,
    state: MonitorState,
    base_branch: str | None,
    remote_branch: str | None,
    monitor_log: WorkspaceLogSink | None = None,
) -> None:
    """Resolve threads the monitor addressed that have since gone OUTDATED (#473).

    Iterates ``status.outdated_unresolved_inline_threads`` and resolves only the
    threads whose latest recorded verdict is in
    ``_OUTDATED_RESOLVABLE_THREAD_VERDICTS``. Error handling mirrors the fix-cycle
    resolve loop and is fully self-contained so a forge fault never escapes into
    the monitor's outer loop: a transient fault waits then leaves the thread for
    the next poll; a permanent fault is logged + audited and skipped. The thread
    is non-blocking for merge, so the addressed marker is preserved either way
    (the resolve is simply retried next poll) — there is nothing to wedge. No
    ``state`` mutation is needed: once resolved, the thread drops out of the
    outdated feed on the next fetch.
    """
    del repo  # repo is recovered from the neutral thread_id by the forge client
    for thread in status.outdated_unresolved_inline_threads:
        tid = thread.thread_id
        if state.threads_addressed_ids.get(tid) not in _OUTDATED_RESOLVABLE_THREAD_VERDICTS:
            continue
        try:
            await self._deps.gh.resolve_thread(thread_id=tid)
        except ForgeClientError as exc:
            # Both forges resolve threads through ``self._deps.gh`` (GitHub or
            # BitBucket), each raising a ``ForgeClientError`` subclass. Catching
            # the shared base keeps either fault from escaping into the runner's
            # poll loop. Transient blips wait and leave the thread for the next
            # poll (already-resolved races are tolerated by the same broad catch
            # — idempotent, never surfaced as a hard failure). The transient and
            # permanent audit reason codes stay forge-specific.
            transient_retry_reason = (
                _BITBUCKET_TRANSIENT_RETRY_REASON
                if isinstance(exc, BitBucketClientError)
                else _GITHUB_TRANSIENT_RETRY_REASON
            )
            if await self._wait_after_transient_forge_error(
                exc,
                workspace_id=workspace_id,
                pr_number=pr_number,
                context="resolve_outdated_thread",
                monitor_log=monitor_log,
            ):
                await self._record_pr_monitor_audit_event(
                    workspace_id=workspace_id,
                    event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
                    action="resolve_outdated_thread",
                    outcome="requeued",
                    reason_code=transient_retry_reason,
                    pr_number=pr_number,
                    status=None,
                    base_branch=base_branch or "",
                    remote_branch=remote_branch,
                    monitor_log=monitor_log,
                    evidence={
                        "thread_ids": [tid],
                        "resolved_thread_count": 0,
                        "requeued_thread_count": 1,
                        "error_message": str(exc),
                    },
                )
                continue
            # Permanent fault: log + audit and skip. The thread is non-blocking,
            # so we keep the addressed marker and simply retry next poll rather
            # than wedging the merge gate. ``redacted_detail()`` normalizes the
            # human detail across forges (gh stderr / BitBucket body).
            _log.warning(
                "monitor.resolve_outdated_thread_failed",
                thread_id=tid,
                stderr=exc.redacted_detail(),
            )
            await self._record_pr_monitor_audit_event(
                workspace_id=workspace_id,
                event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
                action="resolve_outdated_thread",
                outcome="failed",
                reason_code=exc.reason_code,
                pr_number=pr_number,
                status=None,
                base_branch=base_branch or "",
                remote_branch=remote_branch,
                monitor_log=monitor_log,
                evidence={
                    "thread_ids": [tid],
                    "resolved_thread_count": 0,
                    "failed_thread_count": 1,
                    "error_message": str(exc),
                },
            )
            continue
        await self._record_pr_monitor_audit_event(
            workspace_id=workspace_id,
            event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
            action="resolve_outdated_thread",
            outcome="succeeded",
            reason_code="COMMENT_REPAIR",
            pr_number=pr_number,
            status=None,
            base_branch=base_branch or "",
            remote_branch=remote_branch,
            monitor_log=monitor_log,
            evidence={
                "thread_ids": [tid],
                "resolved_thread_count": 1,
            },
        )
