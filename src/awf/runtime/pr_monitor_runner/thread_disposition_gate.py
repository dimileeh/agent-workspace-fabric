"""READY_TO_MERGE / merge gate: every AWF resolution must carry its disposition (#1018).

``decide()`` sees only *unresolved* threads, so "inline comments resolved" used
to mean "zero unresolved threads" — a thread AWF resolved silently looked the
same as one it handled. Before the release cert is posted, and before a feature
monitor merges, this gate re-enumerates EVERY review thread (exhaustive
pagination, failing closed) and checks each thread resolved by AWF's own forge
identity for an AWF disposition marker in a comment that identity authored
after the thread's newest feedback (an older marker answered earlier feedback).

* The marker, not the login, is the evidence: AWF's identity can be the human
  operator's own account, so a resolution by that login proves nothing alone.
* Threads resolved by any other login (or a deleted / app account) are that
  person's or bot's own disposition and never block; they are counted.
* A failing thread turns into ``NotifyHuman`` with
  ``REVIEW_THREAD_DISPOSITION_UNVERIFIED`` listing the ids — never a workspace
  failure. An operator guide naming a flagged id acknowledges it; this gate then
  posts the acknowledgement (with its marker) into the thread itself, only while
  the thread keeps the conversation that was flagged, and the marker counts only
  while that conversation is what the reply followed.

``decide()`` stays pure; this runs at the runner level only.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, replace
from typing import Any

from awf.common.forge_errors import ForgeClientError
from awf.common.forge_thread_audit import AuditedReviewThread
from awf.common.github_client import RepoRef
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.monitor_state_keys import (
    _REVIEW_THREAD_AUDIT_UNAVAILABLE_STATE_KEY,
    _REVIEW_THREAD_DISPOSITION_BASELINE_CONVERSATIONS_STATE_KEY,
    _REVIEW_THREAD_DISPOSITION_BASELINE_STATE_KEY,
    _disposition_unverified_key,
    _item_disposition_key,
)
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.constants import _AUDIT_COMMENT_RESOLUTION_EVENT
from awf.runtime.pr_monitor_runner.helpers import _redact_and_truncate_forge_error
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.thread_disposition import (
    LEGACY_BASELINE,
    RESOLVED_BY_OTHERS,
    REVIEW_THREAD_AUDIT_UNAVAILABLE,
    REVIEW_THREAD_DISPOSITION_LEGACY_BASELINE,
    REVIEW_THREAD_DISPOSITION_UNVERIFIED,
    THREAD_DISPOSITION_REPLY_FAILED,
    DispositionClass,
    disposition_count_rows,
    disposition_marker_class,
    read_item_disposition,
    record_item_disposition,
    render_disposition_reply,
    unverified_blocker_message,
    unverified_flag,
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
    baseline_record = state.threads_addressed_ids.get(_REVIEW_THREAD_DISPOSITION_BASELINE_STATE_KEY)
    legacy_baseline = _decode_baseline(baseline_record)
    legacy_conversations = _decode_baseline_conversations(
        state.threads_addressed_ids.get(_REVIEW_THREAD_DISPOSITION_BASELINE_CONVERSATIONS_STATE_KEY)
    )
    retired: list[str] = []  # legacy exemptions whose thread reopened or moved on
    counts: Counter[str] = Counter()
    unverified: list[str] = []
    marked: set[str] = set()  # carry an AWF marker: handled after the rollout
    # The conversation each unverified flag binds to (an acknowledgement's re-audit
    # can move it on).
    digests = {thread.thread_id: thread.conversation_digest for thread in audit.threads}
    for thread in audit.threads:
        legacy_conversation = legacy_conversations.get(thread.thread_id)
        if legacy_conversation is not None and (
            not thread.is_resolved or legacy_conversation != thread.conversation_digest
        ):
            # The baseline exempted the conversation it saw, resolved: a reopen
            # or a new / edited comment is feedback no legacy resolution answered.
            retired.append(thread.thread_id)
            legacy_conversation = None
        if not thread.is_resolved:
            # A reopen ends the flagged resolution: a later guide cannot acknowledge
            # it, and one already recorded but not yet posted no longer applies.
            state.threads_addressed_ids.pop(_disposition_unverified_key(thread.thread_id), None)
            pending = read_item_disposition(state, thread.thread_id)
            if (
                pending is not None
                and pending.disposition_class == "operator_acknowledged"
                and not pending.reply_comment_id
            ):
                _supersede_acknowledgement(state, thread.thread_id, workspace_id=workspace_id)
            continue
        if (thread.resolved_by or "").strip().casefold() != viewer:
            counts[RESOLVED_BY_OTHERS] += 1  # that person's or bot's own disposition
            continue
        if disposition_marker_class(thread.viewer_comment_bodies, thread_id=thread.thread_id):
            marked.add(thread.thread_id)
        # Only a marker after the newest feedback dispositions the current
        # conversation; an older one answered feedback that has since moved on.
        disposition_class: DispositionClass | None = disposition_marker_class(
            _current_disposition_bodies(thread), thread_id=thread.thread_id
        )
        if disposition_class == "operator_acknowledged" and not _acknowledgement_followed(
            state, thread
        ):
            disposition_class = None
        if disposition_class is None:
            disposition_class = await _post_operator_acknowledgement(
                self,
                state=state,
                thread=thread,
                workspace_id=workspace_id,
                repo=repo,
                pr_number=pr_number,
                digests=digests,
            )
        if (
            disposition_class is None
            and legacy_conversation is not None
            and thread.thread_id in legacy_baseline
            and thread.thread_id not in marked
        ):
            counts[LEGACY_BASELINE] += 1  # resolved before AWF replied before resolving
            continue
        if disposition_class is None:
            unverified.append(thread.thread_id)
            continue
        counts[disposition_class] += 1
        state.threads_addressed_ids.pop(_disposition_unverified_key(thread.thread_id), None)
    if retired:
        _retire_legacy_exemptions(
            state,
            legacy_conversations,
            retired,
            workspace_id=workspace_id,
            pr_number=pr_number,
        )
    if baseline_record is None:
        # A thread this monitor recorded a disposition for (or that carries an
        # AWF marker) was handled after the rollout, so its unmarked resolution
        # is no legacy: it still blocks.
        legacy = [
            tid
            for tid in unverified
            if tid not in marked and _item_disposition_key(tid) not in state.threads_addressed_ids
        ]
        await _record_legacy_baseline(
            self,
            workspace_id=workspace_id,
            pr_number=pr_number,
            state=state,
            conversations={tid: digests[tid] for tid in legacy},
            base_branch=base_branch,
            remote_branch=remote_branch,
            monitor_log=monitor_log,
        )
        counts[LEGACY_BASELINE] += len(legacy)
        unverified = [tid for tid in unverified if tid not in legacy]
    if not unverified:
        return DispositionGateResult(count_rows=disposition_count_rows(counts))
    newly_flagged = [
        thread_id
        for thread_id in unverified
        if not state.threads_addressed_ids.get(_disposition_unverified_key(thread_id))
    ]
    for thread_id in unverified:
        state.mark_addressed(
            _disposition_unverified_key(thread_id), unverified_flag(digests[thread_id])
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


def _current_disposition_bodies(thread: AuditedReviewThread) -> tuple[str, ...]:
    """The viewer bodies after the thread's newest feedback, the operator's own included.

    AWF's identity can be the human operator's account, so their follow-up is
    viewer-authored too. AWF only posts marker-carrying replies into a thread, so
    an unmarked viewer comment is that newer feedback: only later bodies count.
    """
    bodies = thread.current_viewer_comment_bodies
    unmarked = [
        index
        for index, body in enumerate(bodies)
        if disposition_marker_class((body,), thread_id=thread.thread_id) is None
    ]
    return bodies[unmarked[-1] + 1 :] if unmarked else bodies


def _decode_baseline(record: str | None) -> frozenset[str]:
    """The legacy-baseline thread ids; anything malformed is an empty baseline."""
    try:
        thread_ids = json.loads(record or "[]")
    except ValueError:
        return frozenset()
    if not isinstance(thread_ids, list):
        return frozenset()
    return frozenset(tid for tid in thread_ids if isinstance(tid, str))


def _decode_baseline_conversations(record: str | None) -> dict[str, str]:
    """Each live legacy exemption's conversation digest; malformed ties none."""
    try:
        conversations = json.loads(record or "{}")
    except ValueError:
        return {}
    if not isinstance(conversations, dict):
        return {}
    return {tid: digest for tid, digest in conversations.items() if isinstance(digest, str)}


def _retire_legacy_exemptions(
    state: MonitorState,
    conversations: dict[str, str],
    thread_ids: list[str],
    *,
    workspace_id: str,
    pr_number: int,
) -> None:
    """Drop the exemptions of legacy threads that reopened or moved on, for good."""
    for thread_id in thread_ids:
        conversations.pop(thread_id, None)
    state.mark_addressed(
        _REVIEW_THREAD_DISPOSITION_BASELINE_CONVERSATIONS_STATE_KEY,
        json.dumps(conversations, sort_keys=True),
    )
    _log.info(
        "monitor.review_thread_disposition_legacy_baseline_retired",
        workspace_id=workspace_id,
        pr_number=pr_number,
        thread_ids=thread_ids,
        reason_code=REVIEW_THREAD_DISPOSITION_LEGACY_BASELINE,
    )


async def _record_legacy_baseline(
    self: Any,
    *,
    workspace_id: str,
    pr_number: int,
    state: MonitorState,
    conversations: dict[str, str],
    base_branch: str,
    remote_branch: str,
    monitor_log: WorkspaceLogSink | None,
) -> None:
    """First gate run of the workspace: baseline its unmarked AWF resolutions.

    Those the monitor holds no disposition record for predate reply-before-
    resolve (the #1018 rollout), so they are recorded — in state and, with their
    ids, in one audit event — and do not block while each stays resolved with
    the ``conversations`` digest it had here. The record also closes the first
    run: every later unmarked resolution blocks.
    """
    thread_ids = sorted(conversations)
    state.mark_addressed(_REVIEW_THREAD_DISPOSITION_BASELINE_STATE_KEY, json.dumps(thread_ids))
    for thread_id in thread_ids:
        state.threads_addressed_ids.pop(_disposition_unverified_key(thread_id), None)
    if not thread_ids:
        return
    state.mark_addressed(
        _REVIEW_THREAD_DISPOSITION_BASELINE_CONVERSATIONS_STATE_KEY,
        json.dumps(conversations, sort_keys=True),
    )
    _log.info(
        "monitor.review_thread_disposition_legacy_baseline",
        workspace_id=workspace_id,
        pr_number=pr_number,
        thread_ids=thread_ids,
        reason_code=REVIEW_THREAD_DISPOSITION_LEGACY_BASELINE,
    )
    await self._record_pr_monitor_audit_event(
        workspace_id=workspace_id,
        event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
        action=_AUDIT_ACTION,
        outcome="legacy_baseline",
        reason_code=REVIEW_THREAD_DISPOSITION_LEGACY_BASELINE,
        pr_number=pr_number,
        status=None,
        base_branch=base_branch,
        remote_branch=remote_branch,
        monitor_log=monitor_log,
        evidence={"thread_ids": thread_ids},
    )


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
    thread: AuditedReviewThread,
    workspace_id: str,
    repo: RepoRef,
    pr_number: int,
    digests: dict[str, str],
) -> DispositionClass | None:
    """Post a guide's acknowledgement into the resolved thread; ``None`` = still unverified.

    The audit can be stale by the time the reply lands, so it counts only once a
    re-audit shows the thread still resolved and the reply following the
    acknowledged conversation. A re-audit that refuses it rebinds the thread's
    ``digests`` entry to the conversation it saw, so the unverified flag — and an
    immediate re-acknowledgement of it — answers the thread as it now reads.
    """
    thread_id = thread.thread_id
    disposition = read_item_disposition(state, thread_id)
    if disposition is None or disposition.disposition_class != "operator_acknowledged":
        return None
    if disposition.reply_comment_id:
        # Posted on an earlier run, yet this audit (fetched after it) finds no
        # marker: the reply was deleted or edited away. The saved id only prevents
        # a duplicate post, it is not evidence; a new guide re-acknowledges.
        return None
    if disposition.conversation_digest != thread.conversation_digest:
        # The guide acknowledged the conversation the gate flagged; a new or edited
        # comment since then is feedback it never saw, so it is dropped and the
        # current resolution needs its own acknowledgement.
        _supersede_acknowledgement(state, thread_id, workspace_id=workspace_id)
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
    try:
        audit = await self._deps.gh.fetch_review_thread_audit(repo=repo, pr_number=pr_number)
    except ForgeClientError as exc:
        _log.warning(
            "monitor.operator_acknowledgement_reaudit_failed",
            workspace_id=workspace_id,
            thread_id=thread_id,
            reason_code=REVIEW_THREAD_AUDIT_UNAVAILABLE,
            forge_reason_code=exc.reason_code,
        )
        return None
    posted = next((t for t in audit.threads if t.thread_id == thread_id), None)
    if posted is not None:
        digests[thread_id] = posted.conversation_digest
    if (
        posted is None
        or not posted.is_resolved  # reopened meanwhile: the acknowledged resolution is gone
        or disposition_marker_class(_current_disposition_bodies(posted), thread_id=thread_id)
        != "operator_acknowledged"
        or not _acknowledgement_followed(state, posted)
    ):
        return None
    return "operator_acknowledged"


def _acknowledgement_followed(state: MonitorState, thread: AuditedReviewThread) -> bool:
    """True when the thread's newest reply follows the conversation the guide acknowledged.

    A feedback edit that lands after the gate's audit but before the reply keeps
    the reply current by time, yet changes the conversation it followed: the
    acknowledgement then answers nothing the operator saw.
    """
    disposition = read_item_disposition(state, thread.thread_id)
    return (
        disposition is not None
        and disposition.disposition_class == "operator_acknowledged"
        and disposition.conversation_digest is not None
        and disposition.conversation_digest == thread.conversation_digest_before_reply
    )


def _supersede_acknowledgement(state: MonitorState, thread_id: str, *, workspace_id: str) -> None:
    """Drop a guide's unposted acknowledgement of a resolution that no longer stands."""
    state.threads_addressed_ids.pop(_item_disposition_key(thread_id), None)
    _log.info(
        "monitor.operator_acknowledgement_superseded",
        workspace_id=workspace_id,
        thread_id=thread_id,
        reason_code=REVIEW_THREAD_DISPOSITION_UNVERIFIED,
    )


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
