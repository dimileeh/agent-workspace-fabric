"""Automatic reopen of review threads the disposition gate could not verify (#1138).

The #1018 gate refuses READY_TO_MERGE / merge on a thread AWF's identity
resolved without a live disposition. A review bot replying in the thread after
AWF's reply, or a moved-head re-adoption retiring that reply, makes a thread AWF
did handle unverified; resolved, ``decide()`` never re-triages it. The remedy is
a reopen: the next poll that lists the thread open re-triages it through the
normal fix cycle, which posts a fresh disposition before resolving it
(:func:`~awf.runtime.pr_monitor_runner.thread_disposition.retire_reopened_dispositions`).

The runner performs that reopen itself, here, after the gate returned: the gate
keeps deciding on one audit with no forge write, and ``decide()`` stays pure.
Each thread is reopened at most :data:`MAX_AUTO_REOPENS_PER_THREAD` times over
the PR's life (the count crosses re-adoption). A thread over the cap, a forge
with no reopen API, or a failed reopen falls back to the gate's ``NotifyHuman``
blocker for that thread, never a workspace failure. Callers never run this
holding the merge lock.
"""

from __future__ import annotations

from typing import Any

from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import RepoRef
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.monitor_state_keys import _disposition_auto_reopen_key
from awf.runtime.pr_monitor import MonitorState, PRStatus
from awf.runtime.pr_monitor_runner.constants import _AUDIT_COMMENT_RESOLUTION_EVENT
from awf.runtime.pr_monitor_runner.helpers import _redact_and_truncate_forge_error
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.thread_disposition import (
    REVIEW_THREAD_DISPOSITION_AUTO_REOPEN_EXHAUSTED,
    REVIEW_THREAD_DISPOSITION_AUTO_REOPEN_FAILED,
    REVIEW_THREAD_DISPOSITION_AUTO_REOPEN_UNSUPPORTED,
    REVIEW_THREAD_DISPOSITION_AUTO_REOPENED,
    unverified_blocker_message,
)
from awf.runtime.pr_monitor_runner.thread_disposition_gate import (
    DispositionGateResult,
    review_thread_disposition_gate,
)

# The blocker's EXHAUSTED text says "already reopened twice".
MAX_AUTO_REOPENS_PER_THREAD = 2
_AUDIT_ACTION = "review_thread_disposition_auto_reopen"


async def review_thread_disposition_gate_with_reopen(
    self: Any,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    status: PRStatus,
    state: MonitorState,
    base_branch: str,
    remote_branch: str,
    monitor_log: WorkspaceLogSink | None,
) -> DispositionGateResult:
    """Run the disposition gate, then reopen the threads it flags (outside the merge lock)."""
    gate = await review_thread_disposition_gate(
        self,
        workspace_id=workspace_id,
        repo=repo,
        pr_number=pr_number,
        status=status,
        state=state,
        base_branch=base_branch,
        remote_branch=remote_branch,
        monitor_log=monitor_log,
    )
    return await reopen_unverified_threads(
        self,
        gate,
        workspace_id=workspace_id,
        pr_number=pr_number,
        state=state,
        base_branch=base_branch,
        remote_branch=remote_branch,
        monitor_log=monitor_log,
    )


async def reopen_unverified_threads(
    self: Any,
    gate: DispositionGateResult,
    *,
    workspace_id: str,
    pr_number: int,
    state: MonitorState,
    base_branch: str,
    remote_branch: str,
    monitor_log: WorkspaceLogSink | None,
) -> DispositionGateResult:
    """Reopen ``gate``'s unverified threads; re-poll, or block on those left resolved.

    The gate's unverified flag is kept, so the poll that lists a reopened thread
    open re-triages it. Only a reopen that landed counts toward the cap, and its
    count is persisted as soon as it lands. A malformed count reads as exhausted, so the monitor never loops on it. Once
    the forge reports no reopen API, no further thread is tried.
    """
    if not gate.unverified:
        return gate
    reopened: dict[str, int] = {}
    left: dict[str, str] = {}  # thread id -> why it stays resolved
    supported = True
    for thread_id in gate.unverified:
        count = _reopen_count(state, thread_id)
        if count is None or count >= MAX_AUTO_REOPENS_PER_THREAD:
            left[thread_id] = REVIEW_THREAD_DISPOSITION_AUTO_REOPEN_EXHAUSTED
            continue
        if not supported:
            left[thread_id] = REVIEW_THREAD_DISPOSITION_AUTO_REOPEN_UNSUPPORTED
            continue
        try:
            supported = await self._deps.gh.unresolve_thread(thread_id=thread_id)
        except ForgeClientError as exc:
            _log.warning(
                "monitor.review_thread_disposition_auto_reopen_failed",
                workspace_id=workspace_id,
                pr_number=pr_number,
                thread_id=thread_id,
                reason_code=REVIEW_THREAD_DISPOSITION_AUTO_REOPEN_FAILED,
                forge_reason_code=exc.reason_code,
                detail=_redact_and_truncate_forge_error(str(exc)),
            )
            left[thread_id] = REVIEW_THREAD_DISPOSITION_AUTO_REOPEN_FAILED
            continue
        if not supported:
            _log.info(
                "monitor.review_thread_disposition_auto_reopen_unsupported",
                workspace_id=workspace_id,
                pr_number=pr_number,
                reason_code=REVIEW_THREAD_DISPOSITION_AUTO_REOPEN_UNSUPPORTED,
            )
            left[thread_id] = REVIEW_THREAD_DISPOSITION_AUTO_REOPEN_UNSUPPORTED
            continue
        reopened[thread_id] = count + 1
        state.mark_addressed(_disposition_auto_reopen_key(thread_id), str(count + 1))
        # The thread is open on the forge now: save its count before any further
        # forge call, audit write, or poll wait, so a restart cannot exceed the cap.
        await self._persist_state(workspace_id, state)
    exhausted = [
        tid for tid, why in left.items() if why == REVIEW_THREAD_DISPOSITION_AUTO_REOPEN_EXHAUSTED
    ]
    if exhausted:
        _log.warning(
            "monitor.review_thread_disposition_auto_reopen_exhausted",
            workspace_id=workspace_id,
            pr_number=pr_number,
            thread_ids=exhausted,
            reason_code=REVIEW_THREAD_DISPOSITION_AUTO_REOPEN_EXHAUSTED,
        )
    if reopened:
        await _record_reopened(
            self,
            reopened,
            workspace_id=workspace_id,
            pr_number=pr_number,
            base_branch=base_branch,
            remote_branch=remote_branch,
            monitor_log=monitor_log,
        )
    if not left:
        # Callers wait one poll interval (outside the merge lock) and re-poll.
        return DispositionGateResult(retry=True, feedback_changed=True)
    return DispositionGateResult(
        blocker=unverified_blocker_message(list(left), auto_reopen_reasons=list(left.values())),
        unverified=tuple(left),
    )


def _reopen_count(state: MonitorState, thread_id: str) -> int | None:
    """The thread's landed automatic reopens so far; ``None`` when malformed."""
    raw = state.threads_addressed_ids.get(_disposition_auto_reopen_key(thread_id))
    if raw is None:
        return 0
    return int(raw) if raw.isdecimal() and int(raw) > 0 else None


async def _record_reopened(
    self: Any,
    reopened: dict[str, int],
    *,
    workspace_id: str,
    pr_number: int,
    base_branch: str,
    remote_branch: str,
    monitor_log: WorkspaceLogSink | None,
) -> None:
    """One structured log line and ONE audit event for the reopened set."""
    thread_ids = list(reopened)
    _log.info(
        "monitor.review_thread_disposition_auto_reopened",
        workspace_id=workspace_id,
        pr_number=pr_number,
        thread_ids=thread_ids,
        reopen_counts=reopened,
        reason_code=REVIEW_THREAD_DISPOSITION_AUTO_REOPENED,
    )
    await self._record_pr_monitor_audit_event(
        workspace_id=workspace_id,
        event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
        action=_AUDIT_ACTION,
        outcome="reopened",
        reason_code=REVIEW_THREAD_DISPOSITION_AUTO_REOPENED,
        pr_number=pr_number,
        status=None,
        base_branch=base_branch,
        remote_branch=remote_branch,
        monitor_log=monitor_log,
        evidence={"thread_ids": thread_ids, "reopen_counts": reopened},
    )
