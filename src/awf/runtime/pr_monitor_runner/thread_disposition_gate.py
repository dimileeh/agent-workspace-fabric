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
  failure. The operator clears one by reopening it: the next poll re-triages it
  through the normal fix cycle, which posts a fresh disposition before resolving
  it (:func:`~thread_disposition.retire_reopened_dispositions`).
* The gate never writes to the forge: it decides on ONE complete audit.
* Every disposition the monitor records is bound to the conversation it answers
  once an audit sees its reply resolved, and holds only while an audit shows that
  conversation resolved by AWF's identity: a reopen or edit retires it, posted or
  not, so its marker certifies nothing (:func:`_retire_broken_bindings`).
* An unresolved thread is ``decide()``'s to handle, so one the status it acted on
  never listed (opened or reopened since), or listed with other outside comments
  (replied to or edited since), re-polls instead of passing.

``decide()`` stays pure; this runs at the runner level only.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, replace
from typing import Any

from awf.common.forge_errors import ForgeClientError
from awf.common.forge_thread_audit import AuditedReviewThread, ReviewThreadAudit
from awf.common.github_client import RepoRef
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.monitor_state_keys import (
    _REVIEW_THREAD_AUDIT_UNAVAILABLE_STATE_KEY,
    _REVIEW_THREAD_DISPOSITION_BASELINE_CONVERSATIONS_STATE_KEY,
    _REVIEW_THREAD_DISPOSITION_BASELINE_STATE_KEY,
    _disposition_retired_key,
    _disposition_unverified_key,
    _item_disposition_key,
)
from awf.runtime.pr_monitor import MonitorState, PRStatus
from awf.runtime.pr_monitor_runner.constants import _AUDIT_COMMENT_RESOLUTION_EVENT
from awf.runtime.pr_monitor_runner.helpers import _redact_and_truncate_forge_error
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.thread_disposition import (
    LEGACY_BASELINE,
    RESOLVED_BY_OTHERS,
    REVIEW_THREAD_AUDIT_UNAVAILABLE,
    REVIEW_THREAD_DISPOSITION_LEGACY_BASELINE,
    REVIEW_THREAD_DISPOSITION_UNVERIFIED,
    DispositionClass,
    disposition_count_rows,
    disposition_marker_class,
    read_item_disposition,
    record_item_disposition,
    retire_item_disposition,
    unverified_blocker_message,
)

_AUDIT_CONTEXT = "review_thread_audit"
_AUDIT_ACTION = "review_thread_disposition_audit"


@dataclass(frozen=True)
class DispositionGateResult:
    """Outcome of one gate run."""

    retry: bool = False  # transient audit fault, or feedback ``status`` predates: re-poll
    blocker: str | None = None  # NotifyHuman message (reason-coded)
    count_rows: tuple[tuple[str, int], ...] | None = None  # cert line 1; None = unsupported
    audit_unavailable: bool = False  # ``blocker`` is REVIEW_THREAD_AUDIT_UNAVAILABLE
    feedback_changed: bool = False  # caller must wait outside the merge lock


async def review_thread_disposition_gate(
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
    defer_forge_errors: bool = False,
) -> DispositionGateResult:
    """Verify every AWF-resolved thread carries a disposition marker.

    ``status`` is the snapshot ``decide()`` chose this cert or merge on.
    ``defer_forge_errors`` re-raises an audit fetch fault instead of backing off:
    the merge loop re-audits under ``serialized_merge`` and must not sleep while
    holding the lock, so it passes the fault to :func:`review_thread_audit_fault`
    after release and retries on the next poll. It also turns off transport
    retries for the audit (PRRT_kwDOSJAM6s6vbU2K), so no page backs off under the
    lock either.
    """
    try:
        audit = await self._deps.gh.fetch_review_thread_audit(
            repo=repo, pr_number=pr_number, retry=not defer_forge_errors
        )
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
    _retire_broken_bindings(
        state, audit, viewer=viewer, workspace_id=workspace_id, pr_number=pr_number
    )
    return await _verify_audit(
        self,
        audit,
        viewer=viewer,
        workspace_id=workspace_id,
        pr_number=pr_number,
        status=status,
        state=state,
        base_branch=base_branch,
        remote_branch=remote_branch,
        monitor_log=monitor_log,
    )


async def _verify_audit(
    self: Any,
    audit: ReviewThreadAudit,
    *,
    viewer: str,
    workspace_id: str,
    pr_number: int,
    status: PRStatus,
    state: MonitorState,
    base_branch: str,
    remote_branch: str,
    monitor_log: WorkspaceLogSink | None,
) -> DispositionGateResult:
    """Decide on the cycle's one ``audit``: no forge write happens after it."""
    baseline_record = state.threads_addressed_ids.get(_REVIEW_THREAD_DISPOSITION_BASELINE_STATE_KEY)
    legacy_baseline = _decode_baseline(baseline_record)
    legacy_conversations = _decode_baseline_conversations(
        state.threads_addressed_ids.get(_REVIEW_THREAD_DISPOSITION_BASELINE_CONVERSATIONS_STATE_KEY)
    )
    counts: Counter[str] = Counter()
    unverified: list[str] = []
    marked: set[str] = set()  # carry an AWF marker: handled after the rollout
    # The outside comments ``decide()`` saw per listed thread (each listed copy).
    decided: dict[str, set[tuple[str, ...]]] = {}
    for listed in (*status.unresolved_inline_threads, *status.outdated_unresolved_inline_threads):
        decided.setdefault(listed.thread_id, set()).add(tuple(c.body for c in listed.comments))
    undecided: list[str] = []  # unresolved feedback ``decide()`` never saw
    # Each AWF-resolved thread's conversation, as a legacy exemption would bind it.
    digests: dict[str, str] = {}
    for thread in audit.threads:
        if not thread.is_resolved:
            if thread.external_comment_bodies and (
                thread.external_comment_bodies not in decided.get(thread.thread_id, ())
            ):
                # A fix_committed / false_positive thread stays listed unresolved
                # and still merges, so a reply to it (or an edit, which ``decide()``
                # re-triages by body) is unseen feedback too.
                undecided.append(thread.thread_id)
            continue
        if (thread.resolved_by or "").strip().casefold() != viewer:
            counts[RESOLVED_BY_OTHERS] += 1  # that person's or bot's own disposition
            continue
        digests[thread.thread_id] = thread.conversation_digest
        if disposition_marker_class(thread.viewer_comment_bodies, thread_id=thread.thread_id):
            marked.add(thread.thread_id)
        # Only a marker after the newest feedback dispositions the current
        # conversation; an older one answered feedback that has since moved on.
        disposition_class = disposition_marker_class(
            _current_disposition_bodies(thread), thread_id=thread.thread_id
        )
        if disposition_class is None or not _disposition_holds(state, thread, disposition_class):
            if (
                thread.thread_id in legacy_conversations
                and thread.thread_id in legacy_baseline
                and thread.thread_id not in marked
            ):
                counts[LEGACY_BASELINE] += 1  # resolved before AWF replied before resolving
            else:
                unverified.append(thread.thread_id)
            continue
        counts[disposition_class] += 1
        state.threads_addressed_ids.pop(_disposition_unverified_key(thread.thread_id), None)
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
        if undecided:
            # Opened, reopened, replied to, or edited after ``status`` was fetched:
            # re-poll so ``decide()`` handles it rather than certify or merge past it
            # (PRRT_kwDOSJAM6s6vVxR0, PRRT_kwDOSJAM6s6vWLkv).
            _log.info(
                "monitor.review_thread_audit_found_undecided_feedback",
                workspace_id=workspace_id,
                pr_number=pr_number,
                thread_ids=undecided,
                reason_code="REVIEW_THREAD_FEEDBACK_AFTER_DECISION",
            )
            return DispositionGateResult(retry=True, feedback_changed=True)
        return DispositionGateResult(count_rows=disposition_count_rows(counts))
    newly_flagged = [
        thread_id
        for thread_id in unverified
        if state.threads_addressed_ids.get(_disposition_unverified_key(thread_id))
        != REVIEW_THREAD_DISPOSITION_UNVERIFIED
    ]
    for thread_id in unverified:
        # A poll that lists the thread open again re-triages it (the operator's reopen).
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


def _disposition_holds(
    state: MonitorState, thread: AuditedReviewThread, disposition_class: DispositionClass
) -> bool:
    """True when the thread's newest current marker is a live disposition.

    :func:`_retire_broken_bindings` already retired every record this audit
    breaks, so a record left must be the posted reply of that marker's class, one
    its post-reply re-read confirmed. The first audit to see a posted reply
    resolved binds it to the conversation it followed. With no record at all (a re-adopted PR whose record did not cross)
    the marker is the evidence — unless it is a reply this monitor (or a moved-head
    re-adoption, PRRT_kwDOSJAM6s6vdu03) retired, or the
    thread holds a retired reply whose conversation AWF never read, which could be
    any marker in it (PRRT_kwDOSJAM6s6vc8NC).
    """
    disposition = read_item_disposition(state, thread.thread_id)
    if disposition is None:
        retired = {
            _disposition_retired_key(thread.thread_id, thread.conversation_digest_before_reply),
            _disposition_retired_key(thread.thread_id, ""),
        }
        return retired.isdisjoint(state.threads_addressed_ids)
    if disposition.disposition_class != disposition_class or not disposition.reply_comment_id:
        return False  # retired or never posted: the marker is an older reply's
    if disposition.reread_pending:
        return False  # never checked against the conversation it follows
    if disposition.conversation_digest is None:
        record_item_disposition(
            state, replace(disposition, conversation_digest=thread.conversation_digest_before_reply)
        )
    return True


def _retire_broken_bindings(
    state: MonitorState,
    audit: ReviewThreadAudit,
    *,
    viewer: str,
    workspace_id: str,
    pr_number: int,
) -> None:
    """Retire every bound record ``audit`` no longer shows holding (#1018).

    A disposition is bound to the conversation it answers and holds only while
    the thread stays resolved by AWF's identity with that conversation before
    its reply. A reopen, or a new or edited comment, retires it — posted or not.
    One whose AWF resolve landed before any audit bound it holds while that
    resolution stands, so a reopen retires it too (PRRT_kwDOSJAM6s6vZ4nJ).
    Likewise a reopen, change, or another login's resolve of a legacy-baseline
    thread retires its exemption for good (PRRT_kwDOSJAM6s6vcSmq). A reopened
    thread's unverified flag is left for the poll that lists it open, which
    re-triages it.
    """
    legacy_conversations = _decode_baseline_conversations(
        state.threads_addressed_ids.get(_REVIEW_THREAD_DISPOSITION_BASELINE_CONVERSATIONS_STATE_KEY)
    )
    retired_legacy: list[str] = []
    for thread in audit.threads:
        thread_id = thread.thread_id
        resolved_by_viewer = (
            thread.is_resolved and (thread.resolved_by or "").strip().casefold() == viewer
        )
        legacy_conversation = legacy_conversations.get(thread_id)
        if legacy_conversation is not None and (
            not resolved_by_viewer or legacy_conversation != thread.conversation_digest
        ):
            retired_legacy.append(thread_id)
        disposition = read_item_disposition(state, thread_id)
        if disposition is None or not disposition.answers_resolution:
            continue  # a reply whose resolve did not land answers no resolution yet
        if resolved_by_viewer and (
            not disposition.is_bound
            or thread.conversation_digest_before_reply == disposition.conversation_digest
        ):
            continue  # still standing; an unbound landed resolve is bound on verify
        retire_item_disposition(state, disposition)
        _log.info(
            "monitor.thread_disposition_retired",
            workspace_id=workspace_id,
            pr_number=pr_number,
            thread_id=thread_id,
            disposition_class=disposition.disposition_class,
            reason_code=REVIEW_THREAD_DISPOSITION_UNVERIFIED,
        )
    if retired_legacy:
        _retire_legacy_exemptions(
            state,
            legacy_conversations,
            retired_legacy,
            workspace_id=workspace_id,
            pr_number=pr_number,
        )


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
