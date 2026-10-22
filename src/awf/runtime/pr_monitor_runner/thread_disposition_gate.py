"""READY_TO_MERGE / merge gate: every AWF resolution must carry its disposition (#1018).

``decide()`` sees only *unresolved* threads, so "inline comments resolved" used
to mean "zero unresolved threads" — a thread AWF resolved silently looked the
same as one it handled. Before the release cert is posted, and before a feature
monitor merges, this gate re-enumerates EVERY review thread (exhaustive
pagination, failing closed) and checks each thread resolved by AWF's own forge
identity for an AWF disposition marker in a comment that identity authored.

* The marker, not the login, is the evidence: AWF's identity can be the human
  operator's own account, so a resolution by that login proves nothing alone.
* Threads resolved by any other login (or a deleted / app account) are that
  person's or bot's own disposition and never block; they are counted.
* A failing thread turns into ``NotifyHuman`` with
  ``REVIEW_THREAD_DISPOSITION_UNVERIFIED`` listing the ids — never a workspace
  failure. An operator guide naming a flagged id acknowledges it; this gate then
  posts the acknowledgement (with its marker) into the thread itself.

``decide()`` stays pure; this runs at the runner level only.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from typing import Any

from awf.common.forge_errors import ForgeClientError
from awf.common.github_client import RepoRef
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.monitor_state_keys import (
    _REVIEW_THREAD_AUDIT_UNAVAILABLE_STATE_KEY,
    _disposition_unverified_key,
)
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.constants import _AUDIT_COMMENT_RESOLUTION_EVENT
from awf.runtime.pr_monitor_runner.helpers import _redact_and_truncate_forge_error
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.thread_disposition import (
    RESOLVED_BY_OTHERS,
    REVIEW_THREAD_AUDIT_UNAVAILABLE,
    REVIEW_THREAD_DISPOSITION_UNVERIFIED,
    THREAD_DISPOSITION_REPLY_FAILED,
    DispositionClass,
    disposition_count_rows,
    disposition_marker_class,
    read_item_disposition,
    record_item_disposition,
    render_disposition_reply,
    unverified_blocker_message,
)

_AUDIT_CONTEXT = "review_thread_audit"
_AUDIT_ACTION = "review_thread_disposition_audit"


@dataclass(frozen=True)
class DispositionGateResult:
    """Outcome of one gate run."""

    retry: bool = False  # transient audit fault: re-poll
    blocker: str | None = None  # NotifyHuman message (reason-coded)
    count_rows: tuple[tuple[str, int], ...] | None = None  # cert line 1; None = unsupported
    audit_unavailable: bool = False  # ``blocker`` is REVIEW_THREAD_AUDIT_UNAVAILABLE


async def review_thread_disposition_gate(
    self: Any,
    *,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    state: MonitorState,
    base_branch: str,
    remote_branch: str,
    monitor_log: WorkspaceLogSink | None,
    defer_forge_errors: bool = False,
) -> DispositionGateResult:
    """Verify every AWF-resolved thread carries a disposition marker.

    ``defer_forge_errors`` re-raises an audit fetch fault instead of backing off:
    the merge loop re-audits under ``serialized_merge`` and must not sleep while
    holding the lock, so it passes the fault to :func:`review_thread_audit_fault`
    after release.
    """
    try:
        audit = await self._deps.gh.fetch_review_thread_audit(repo=repo, pr_number=pr_number)
    except ForgeClientError as exc:
        if defer_forge_errors:
            raise
        return await review_thread_audit_fault(
            self,
            exc,
            workspace_id=workspace_id,
            pr_number=pr_number,
            state=state,
            base_branch=base_branch,
            remote_branch=remote_branch,
            monitor_log=monitor_log,
        )
    await self._clear_forge_transient_retry_state_on_success(
        workspace_id=workspace_id,
        state=state,
        context=_AUDIT_CONTEXT,
    )
    state.threads_addressed_ids.pop(_REVIEW_THREAD_AUDIT_UNAVAILABLE_STATE_KEY, None)
    if not audit.supported:
        _log.info(
            "monitor.review_thread_audit_unsupported_forge",
            workspace_id=workspace_id,
            pr_number=pr_number,
            forge=repo.forge,
        )
        return DispositionGateResult()
    viewer = (audit.viewer_login or "").strip().casefold()
    if not viewer:
        missing_login = "the forge did not report AWF's own login"
        return await _audit_unavailable(
            self,
            workspace_id=workspace_id,
            pr_number=pr_number,
            state=state,
            base_branch=base_branch,
            remote_branch=remote_branch,
            monitor_log=monitor_log,
            summary=missing_login,
            detail=missing_login,
        )
    counts: Counter[str] = Counter()
    unverified: list[str] = []
    for thread in audit.threads:
        if not thread.is_resolved:
            continue
        if (thread.resolved_by or "").strip().casefold() != viewer:
            counts[RESOLVED_BY_OTHERS] += 1  # that person's or bot's own disposition
            continue
        disposition_class: DispositionClass | None = disposition_marker_class(
            thread.viewer_comment_bodies, thread_id=thread.thread_id
        )
        if disposition_class is None:
            disposition_class = await _post_operator_acknowledgement(
                self, state=state, thread_id=thread.thread_id, workspace_id=workspace_id
            )
        if disposition_class is None:
            unverified.append(thread.thread_id)
            continue
        counts[disposition_class] += 1
        state.threads_addressed_ids.pop(_disposition_unverified_key(thread.thread_id), None)
    if not unverified:
        return DispositionGateResult(count_rows=disposition_count_rows(counts))
    newly_flagged = [
        thread_id
        for thread_id in unverified
        if not state.threads_addressed_ids.get(_disposition_unverified_key(thread_id))
    ]
    for thread_id in unverified:
        state.mark_addressed(
            _disposition_unverified_key(thread_id), REVIEW_THREAD_DISPOSITION_UNVERIFIED
        )
    _log.warning(
        "monitor.review_thread_disposition_unverified",
        workspace_id=workspace_id,
        pr_number=pr_number,
        thread_ids=unverified,
        reason_code=REVIEW_THREAD_DISPOSITION_UNVERIFIED,
    )
    if newly_flagged:
        # One audit event per newly flagged set, not one per poll.
        await self._record_pr_monitor_audit_event(
            workspace_id=workspace_id,
            event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
            action=_AUDIT_ACTION,
            outcome="blocked",
            reason_code=REVIEW_THREAD_DISPOSITION_UNVERIFIED,
            pr_number=pr_number,
            status=None,
            base_branch=base_branch,
            remote_branch=remote_branch,
            monitor_log=monitor_log,
            evidence={"thread_ids": unverified, "disposition_counts": dict(counts)},
        )
    return DispositionGateResult(blocker=unverified_blocker_message(unverified))


async def review_thread_audit_fault(
    self: Any,
    exc: ForgeClientError,
    *,
    workspace_id: str,
    pr_number: int,
    state: MonitorState,
    base_branch: str,
    remote_branch: str,
    monitor_log: WorkspaceLogSink | None,
) -> DispositionGateResult:
    """Back off on a transient audit fault (retry); fail closed on a permanent one."""
    if await self._wait_after_transient_forge_error(
        exc,
        workspace_id=workspace_id,
        pr_number=pr_number,
        context=_AUDIT_CONTEXT,
        state=state,
        monitor_log=monitor_log,
    ):
        return DispositionGateResult(retry=True)
    return await _audit_unavailable(
        self,
        workspace_id=workspace_id,
        pr_number=pr_number,
        state=state,
        base_branch=base_branch,
        remote_branch=remote_branch,
        monitor_log=monitor_log,
        summary=f"forge error {exc.reason_code}",
        detail=_redact_and_truncate_forge_error(str(exc)),
        forge_reason_code=exc.reason_code,
    )


async def _post_operator_acknowledgement(
    self: Any,
    *,
    state: MonitorState,
    thread_id: str,
    workspace_id: str,
) -> DispositionClass | None:
    """Post a guide's acknowledgement into the resolved thread; ``None`` = still unverified."""
    disposition = read_item_disposition(state, thread_id)
    if disposition is None or disposition.disposition_class != "operator_acknowledged":
        return None
    if disposition.reply_comment_id:
        # Posted on an earlier run, yet this audit (fetched after it) finds no
        # marker: the reply was deleted or edited away. The saved id only prevents
        # a duplicate post, it is not evidence; a new guide re-acknowledges.
        return None
    try:
        reply_id = await self._deps.gh.reply_to_thread(
            thread_id=thread_id,
            body=render_disposition_reply(disposition),
        )
    except ForgeClientError as exc:
        _log.warning(
            "monitor.operator_acknowledgement_reply_failed",
            workspace_id=workspace_id,
            thread_id=thread_id,
            reason_code=THREAD_DISPOSITION_REPLY_FAILED,
            forge_reason_code=exc.reason_code,
        )
        return None
    record_item_disposition(state, replace(disposition, reply_comment_id=reply_id))
    return "operator_acknowledged"


async def _audit_unavailable(
    self: Any,
    *,
    workspace_id: str,
    pr_number: int,
    state: MonitorState,
    base_branch: str,
    remote_branch: str,
    monitor_log: WorkspaceLogSink | None,
    summary: str,
    detail: str,
    forge_reason_code: str | None = None,
) -> DispositionGateResult:
    """Fail closed: no cert (or merge) without a complete thread enumeration.

    The NotifyHuman text carries only the stable ``summary``: it keys the
    once-per-(head, reason) notification dedupe, and raw forge errors vary per
    request (request ids, addresses), so including them would post a new public
    comment every poll of an outage. The redacted ``detail`` goes to the log and
    to an audit event recorded once per outage.
    """
    _log.warning(
        "monitor.review_thread_audit_unavailable",
        workspace_id=workspace_id,
        pr_number=pr_number,
        reason_code=REVIEW_THREAD_AUDIT_UNAVAILABLE,
        forge_reason_code=forge_reason_code,
        detail=detail,
    )
    if state.threads_addressed_ids.get(_REVIEW_THREAD_AUDIT_UNAVAILABLE_STATE_KEY) != summary:
        state.mark_addressed(_REVIEW_THREAD_AUDIT_UNAVAILABLE_STATE_KEY, summary)
        await self._record_pr_monitor_audit_event(
            workspace_id=workspace_id,
            event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
            action=_AUDIT_ACTION,
            outcome="failed",
            reason_code=REVIEW_THREAD_AUDIT_UNAVAILABLE,
            pr_number=pr_number,
            status=None,
            base_branch=base_branch,
            remote_branch=remote_branch,
            monitor_log=monitor_log,
            evidence={"error_message": detail, "forge_reason_code": forge_reason_code},
        )
    return DispositionGateResult(
        blocker=(
            f"{REVIEW_THREAD_AUDIT_UNAVAILABLE}: AWF could not enumerate this PR's review "
            f"threads to verify thread dispositions ({summary})."
        ),
        audit_unavailable=True,
    )
