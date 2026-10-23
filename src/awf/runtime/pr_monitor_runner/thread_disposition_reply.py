"""Reply-before-resolve: AWF's in-thread disposition precedes every resolve (#1018).

Both resolve sites — the fix-cycle resolve loop and outdated-thread hygiene —
call :func:`reply_before_resolve` immediately before ``resolve_thread``. It posts
the thread's disposition reply (see :mod:`thread_disposition`) and returns the
replied record, or parks the thread ``needs_human`` with a reason code and
returns ``None``; the caller then must NOT resolve. It also returns ``None``
when the thread's feedback moved on before a fresh reply landed: that reply
answers older feedback, so the thread stays open for ``decide()`` to re-triage.

Parking is per thread and never fails the workspace: it records state, a log
line and an audit event, and ``decide()`` turns the parked thread into
``NotifyHuman`` through the ordinary ``needs_human`` merge block. It only ever
runs after the fix cycle's push, so accepted commits stay published.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from awf.common.forge_errors import ForgeClientError
from awf.common.forge_thread_audit import AuditedReviewThread
from awf.common.github_client import RepoRef
from awf.db.repositories import WorkspaceRepository
from awf.runtime.feedback_policy import review_thread_body_state_key
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.monitor_state_keys import (
    _disposition_retired_key,
    _item_disposition_key,
    _outdated_resolve_requeued_key,
)
from awf.runtime.pr_monitor import MonitorState, ReviewThread
from awf.runtime.pr_monitor_runner.comment_repair_provenance import recorded_item_commit
from awf.runtime.pr_monitor_runner.constants import _AUDIT_COMMENT_RESOLUTION_EVENT
from awf.runtime.pr_monitor_runner.fix_cycle_deferred import (
    _deferred_issue_filed_marker,
    _deferred_issue_url,
)
from awf.runtime.pr_monitor_runner.helpers import _redact_and_truncate_forge_error
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.notify_human_details import _needs_human_reason_state_key
from awf.runtime.pr_monitor_runner.thread_disposition import (
    REVIEW_THREAD_AUDIT_UNAVAILABLE,
    THREAD_DISPOSITION_EVIDENCE_MISSING,
    THREAD_DISPOSITION_REASON_MISSING,
    THREAD_DISPOSITION_REPLY_FAILED,
    THREAD_DISPOSITION_UNREAD_FEEDBACK,
    ThreadDisposition,
    attach_issue_url,
    disposition_marker_class,
    disposition_problem,
    read_item_disposition,
    record_item_disposition,
    render_disposition_reply,
    retire_item_disposition,
)

# The conversation a fresh reply follows gained or changed outside feedback since
# the thread was listed: the reply is retired and the thread re-enters triage.
_FEEDBACK_AFTER_DECISION = "REVIEW_THREAD_FEEDBACK_AFTER_DECISION"

_PROBLEM_DETAIL = {
    THREAD_DISPOSITION_REASON_MISSING: (
        "AWF did not resolve this review thread: its FALSE POSITIVE or DEFER "
        "disposition has no reason to post in the thread"
    ),
    THREAD_DISPOSITION_EVIDENCE_MISSING: (
        "AWF did not resolve this review thread: its disposition has no fix commit "
        "(FIXED) or tracking issue (DEFER) to cite in the thread"
    ),
}


async def reply_before_resolve(
    self: Any,
    *,
    workspace_id: str,
    pr_number: int,
    thread_id: str,
    state: MonitorState,
    action: str,
    base_branch: str | None,
    remote_branch: str | None,
    thread: ReviewThread | None = None,
    operation_id: str | None = None,
    operation_type: str | None = None,
    monitor_log: WorkspaceLogSink | None = None,
    source_head_sha: str | None = None,
    repo: RepoRef | None = None,
    retry: bool = True,
) -> ThreadDisposition | None:
    """Post the thread's disposition reply; ``None`` means do NOT resolve.

    With ``repo``, a fresh reply is checked against the live thread before it is
    returned (:func:`_reread_thread_after_reply`); ``thread`` is the thread as the
    monitor listed it, whose outside comments that check compares when given.
    ``retry=False`` (outdated hygiene under
    ``serialized_merge``) makes that re-read fail fast, and a fault requeues the
    thread for the next poll instead of parking it.
    """
    verdict = state.threads_addressed_ids.get(thread_id)
    disposition = _disposition_for_resolve(state, thread_id, verdict, thread)
    if disposition is not None and disposition.answers_resolution:
        # A resolution followed this reply (AWF's resolve landed, or an audit saw
        # it); resolving the thread again means it was reopened since, which
        # retires the reply: answer afresh (PRRT_kwDOSJAM6s6vZ4nJ).
        disposition = retire_item_disposition(state, disposition)
    if (
        disposition is not None
        and disposition.disposition_class == "fixed"
        and not disposition.commits
        and not disposition.reply_comment_id
        and verdict == "fix_committed"
        and operation_id
    ):
        disposition = await _with_item_commit_recorded_this_cycle(
            self,
            workspace_id=workspace_id,
            state=state,
            disposition=disposition,
            operation_id=operation_id,
        )
    problem = disposition_problem(disposition, verdict)

    async def _park(reason_code: str, detail: str, forge_reason_code: str | None = None) -> None:
        await _park_thread(
            self,
            workspace_id=workspace_id,
            pr_number=pr_number,
            thread_id=thread_id,
            state=state,
            action=action,
            reason_code=reason_code,
            detail=detail,
            base_branch=base_branch,
            remote_branch=remote_branch,
            operation_id=operation_id,
            operation_type=operation_type,
            monitor_log=monitor_log,
            source_head_sha=source_head_sha,
            disposition=disposition,
            forge_reason_code=forge_reason_code,
        )

    async def _confirm(posted: ThreadDisposition) -> ThreadDisposition | None:
        """Check the posted reply against the live thread; ``None`` means do NOT resolve."""
        nonlocal disposition
        disposition = posted
        if repo is None:
            return posted
        try:
            moved, followed = await _reread_thread_after_reply(
                self,
                repo=repo,
                pr_number=pr_number,
                thread_id=thread_id,
                thread=thread,
                reply_comment_id=posted.reply_comment_id,
                retry=retry,
            )
        except ForgeClientError as exc:
            if not retry:
                # Under the merge lock the re-read never backs off: its fault releases
                # the lock to retry next poll (PRRT_kwDOSJAM6s6vbU2K).
                await _requeue_unconfirmed_reply(
                    self,
                    exc,
                    workspace_id=workspace_id,
                    pr_number=pr_number,
                    state=state,
                    disposition=posted,
                    action=action,
                    base_branch=base_branch,
                    remote_branch=remote_branch,
                    monitor_log=monitor_log,
                )
                return None
            await _park(
                REVIEW_THREAD_AUDIT_UNAVAILABLE,
                "AWF could not re-read this review thread after posting its disposition "
                f"reply ({_redact_and_truncate_forge_error(str(exc))}) to confirm no "
                "feedback arrived first, so it left the thread open",
                exc.reason_code,
            )
            return None
        if followed is not None or posted.reread_pending:
            # Kept with the reply, so retiring it before any audit bound it still
            # tombstones the conversation its marker follows (PRRT_kwDOSJAM6s6vaq4v).
            posted = replace(
                posted,
                reply_conversation_digest=followed or posted.reply_conversation_digest,
                reread_pending=False,
            )
            disposition = posted
            record_item_disposition(state, posted)
            if moved is None:
                await _persist_posted_disposition(
                    self, workspace_id=workspace_id, state=state, item_id=thread_id
                )
        if moved == THREAD_DISPOSITION_UNREAD_FEEDBACK:
            # The operator, under AWF's own account, left feedback the triage behind
            # this reply never read, yet the gate would take the reply as its answer
            # (PRRT_kwDOSJAM6s6vbU2H). A human reads it: the thread is parked open and
            # the reply retired, so neither a later resolve nor its marker reuses it.
            await _park(
                THREAD_DISPOSITION_UNREAD_FEEDBACK,
                "AWF did not resolve this review thread: a comment by its own forge "
                "identity without an AWF disposition marker precedes its disposition "
                "reply, and AWF never reads that identity's comments as feedback",
            )
            retire_item_disposition(state, posted)
            await _persist_posted_disposition(
                self, workspace_id=workspace_id, state=state, item_id=thread_id
            )
            return None
        if moved is not None:
            # Feedback added or edited after the listed snapshot sits before the
            # reply, where the gate would read the reply as its answer
            # (PRRT_kwDOSJAM6s6vaQrr). The reply is retired, so no later resolve
            # reuses it, and the thread is left open: its changed body re-enters
            # AddressComments, whose re-triage answers that feedback afresh.
            retire_item_disposition(state, posted)
            await _persist_posted_disposition(
                self, workspace_id=workspace_id, state=state, item_id=thread_id
            )
            _log.info(
                "monitor.thread_disposition_feedback_moved",
                workspace_id=workspace_id,
                pr_number=pr_number,
                thread_id=thread_id,
                reply_comment_id=posted.reply_comment_id,
                reason_code=moved,
            )
            return None
        return posted

    if disposition is not None and disposition.reply_comment_id:
        # Replied on an earlier attempt whose resolve did not land: that reply is
        # the thread's disposition, so resolve on it and never double-post. One no
        # re-read confirmed yet is re-read first.
        return await _confirm(disposition) if disposition.reread_pending else disposition
    if disposition is None or problem is not None:
        reason_code = problem or THREAD_DISPOSITION_REASON_MISSING
        await _park(reason_code, _PROBLEM_DETAIL[reason_code])
        return None
    try:
        reply_id = await self._deps.gh.reply_to_thread(
            thread_id=thread_id,
            body=render_disposition_reply(disposition),
        )
    except ForgeClientError as exc:
        # Any forge fault — transient or not — parks: the mutation is never
        # blindly retried (it could double-post) and the thread is never resolved
        # without its reply. An operator guide re-triages it.
        await _park(
            THREAD_DISPOSITION_REPLY_FAILED,
            "AWF could not post the disposition reply in this review thread "
            f"({_redact_and_truncate_forge_error(str(exc))}), so it left the thread open",
            exc.reason_code,
        )
        return None
    # Pending until its re-read succeeds: a re-read fault's park, or a restart
    # before the re-read, leaves nothing the gate certifies (PRRT_kwDOSJAM6s6veO2C).
    disposition = replace(
        disposition,
        reply_comment_id=reply_id,
        reply_conversation_digest=None,
        reread_pending=repo is not None,
    )
    record_item_disposition(state, disposition)
    await _persist_posted_disposition(
        self, workspace_id=workspace_id, state=state, item_id=thread_id
    )
    _log.info(
        "monitor.thread_disposition_replied",
        workspace_id=workspace_id,
        pr_number=pr_number,
        thread_id=thread_id,
        disposition_class=disposition.disposition_class,
        reply_comment_id=reply_id,
    )
    return await _confirm(disposition)


async def _reread_thread_after_reply(
    self: Any,
    *,
    repo: RepoRef,
    pr_number: int,
    thread_id: str,
    thread: ReviewThread | None,
    reply_comment_id: str | None,
    retry: bool,
) -> tuple[str | None, str | None]:
    """Re-read the live thread after a fresh reply: ``(moved, followed)``.

    ``moved`` is why the conversation the reply follows is not the one its triage
    read (:func:`_conversation_moved`), ``None`` when it is; ``followed`` is the
    conversation the reply followed (``None`` when the re-read cannot tell: the
    newest comment is not a marker, so it is not the reply, PRRT_kwDOSJAM6s6vc8NC).
    Runs once a fresh reply has landed, so anything that arrived before the reply
    shows up here; anything later follows the reply, which the gate already
    refuses as its answer. An unsupported audit or a thread missing from it is no
    evidence of new feedback.
    """
    audit = await self._deps.gh.fetch_review_thread_audit(
        repo=repo, pr_number=pr_number, retry=retry
    )
    live = next((t for t in audit.threads if t.thread_id == thread_id), None)
    if not audit.supported or live is None:
        return None, None
    moved = _conversation_moved(thread, live, reply_comment_id)
    newest = live.viewer_comment_bodies[-1:]
    if newest and disposition_marker_class(newest, thread_id=thread_id) is None:
        return moved, None
    return moved, live.conversation_digest_before_reply or None


def _conversation_moved(
    thread: ReviewThread | None, live: AuditedReviewThread, reply_comment_id: str | None
) -> str | None:
    """Why the full conversation before AWF's reply is not the one ``thread`` shows.

    The triage behind the reply read ``thread``'s outside comments, as
    ``fetch_pr_status`` lists them, and none by AWF's own identity: that identity
    can be the operator's own account, and ``fetch_pr_status`` lists none of its
    comments. AWF only posts marker-carrying replies, so an unmarked one is the
    operator's feedback, which the gate would read the reply as answering. Such a
    comment is ``THREAD_DISPOSITION_UNREAD_FEEDBACK`` whatever outside feedback
    followed it (PRRT_kwDOSJAM6s6vbU2H), unless AWF's previous reply in the thread
    sits between them and is still current (no newer outside comment or edit): that
    reply passed this check, or parked the thread for a human, over the same
    conversation. A previous reply that newer feedback superseded proves nothing:
    the re-triage behind this reply may answer only that feedback, as after a park
    that a reviewer reply re-triaged (PRRT_kwDOSJAM6s6vbrwH). Otherwise outside
    comments that differ from the listed ones are
    ``REVIEW_THREAD_FEEDBACK_AFTER_DECISION``; a snapshot without comments, or no
    listed copy at all, proves no such difference. The unread check needs only
    ``live``, so it runs without one too (PRRT_kwDOSJAM6s6vc8M9).
    """
    bodies = live.viewer_comment_bodies
    marked = {
        index
        for index, body in enumerate(bodies)
        if disposition_marker_class((body,), thread_id=live.thread_id) is not None
    }
    # Scan back over what precedes this reply, found by its comment id. A re-read
    # too stale to show it bounds nothing with an older marker: the reply follows
    # every comment it shows (PRRT_kwDOSJAM6s6vc8M9, PRRT_kwDOSJAM6s6veO2F).
    ids = live.viewer_comment_ids
    reply = ids.index(reply_comment_id) if reply_comment_id in ids else len(bodies)
    for index in range(reply - 1, -1, -1):
        if index not in marked:
            return THREAD_DISPOSITION_UNREAD_FEEDBACK
        if index >= live.superseded_viewer_comments:
            break  # AWF's previous reply, still current
    listed = tuple(comment.body for comment in thread.comments) if thread is not None else ()
    if listed and live.external_comment_bodies != listed:
        return _FEEDBACK_AFTER_DECISION
    return None


async def _requeue_unconfirmed_reply(
    self: Any,
    exc: ForgeClientError,
    *,
    workspace_id: str,
    pr_number: int,
    state: MonitorState,
    disposition: ThreadDisposition,
    action: str,
    base_branch: str | None,
    remote_branch: str | None,
    monitor_log: WorkspaceLogSink | None,
) -> None:
    """Leave the thread open for the next poll after a fail-fast re-read fault.

    Nothing confirmed the reply followed the listed conversation, so it is kept
    with ``reread_pending``: the next poll's resolve re-reads that same reply
    before resolving on it, never posting a second one whose check this unchecked
    reply would bound, and the gate certifies nothing on it meanwhile -- on a
    re-adopted PR too, whose seed keeps the record on any head. The requeue
    flag blocks this merge, and the merge loop backs off after releasing the lock.
    The verdict stays, so the retry still fires.
    """
    thread_id = disposition.item_id
    record_item_disposition(state, replace(disposition, reread_pending=True))
    await _persist_posted_disposition(
        self, workspace_id=workspace_id, state=state, item_id=thread_id
    )
    state.mark_addressed(_outdated_resolve_requeued_key(thread_id), "requeued")
    _log.warning(
        "monitor.thread_disposition_reread_requeued",
        workspace_id=workspace_id,
        pr_number=pr_number,
        thread_id=thread_id,
        reason_code=REVIEW_THREAD_AUDIT_UNAVAILABLE,
        forge_reason_code=exc.reason_code,
    )
    await self._record_pr_monitor_audit_event(
        workspace_id=workspace_id,
        event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
        action=action,
        outcome="requeued",
        reason_code=REVIEW_THREAD_AUDIT_UNAVAILABLE,
        pr_number=pr_number,
        status=None,
        base_branch=base_branch or "",
        remote_branch=remote_branch,
        monitor_log=monitor_log,
        evidence={
            "thread_ids": [thread_id],
            "resolved_thread_count": 0,
            "requeued_thread_count": 1,
            "forge_reason_code": exc.reason_code,
            "error_message": _redact_and_truncate_forge_error(str(exc)),
            "disposition": disposition.as_payload(),
        },
    )


async def _with_item_commit_recorded_this_cycle(
    self: Any,
    *,
    workspace_id: str,
    state: MonitorState,
    disposition: ThreadDisposition,
    operation_id: str,
) -> ThreadDisposition:
    """Cite the item commit this cycle durably recorded for a commit-less FIXED.

    The FIXED citation normally comes from the in-state #935 chain; when that
    chain no longer holds the item, the cycle's durable item-commit record is the
    fallback before the thread is parked ``THREAD_DISPOSITION_EVIDENCE_MISSING``.
    """
    record = await recorded_item_commit(
        self,
        workspace_id=workspace_id,
        item_id=disposition.item_id,
        operation_id=operation_id,
    )
    if record is None:
        return disposition
    cited = replace(disposition, commits=(record.head_sha,), commit_base=record.item_start_head)
    record_item_disposition(state, cited)
    _log.info(
        "monitor.thread_disposition_commit_from_cycle_record",
        workspace_id=workspace_id,
        thread_id=disposition.item_id,
        head_sha=record.head_sha[:10],
        operation_id=operation_id,
    )
    return cited


async def record_resolve_landed(
    self: Any,
    *,
    workspace_id: str,
    state: MonitorState,
    disposition: ThreadDisposition,
) -> None:
    """Record that AWF's resolve on ``disposition``'s reply landed, saved at once.

    Only a failed resolve's reply may be reused by its retry. Once a resolve
    landed, seeing the thread open again is a reopen that retires the reply,
    even before a gate audit binds it (PRRT_kwDOSJAM6s6vZ4nJ). Saved narrowly like
    the posted reply, so a reload before the loop's own flush cannot lose it.
    """
    record_item_disposition(state, replace(disposition, resolved=True))
    await _persist_posted_disposition(
        self, workspace_id=workspace_id, state=state, item_id=disposition.item_id
    )


async def _persist_posted_disposition(
    self: Any,
    *,
    workspace_id: str,
    state: MonitorState,
    item_id: str,
) -> None:
    """Save ONLY ``item_id``'s posted disposition record, merged onto the row.

    Runs right after the reply lands, so a resolve that then fails — or a
    restart before the loop's ``_persist_state`` — still finds the reply id and
    never posts a second reply. A DEFER's filed-issue marker travels with it so
    a re-triage after a reload never files a second issue, and the thread's
    retired-reply tombstones so a reload never lets a retired marker certify
    again. It must not flush the
    rest of the in-memory state: inside a fix cycle that still carries
    unconfirmed verdicts whose resolves have not run yet (#305). Best-effort: the
    reply already landed and the in-memory record still reaches the DB with the
    loop's own flush.
    """
    await _persist_item_keys(
        self,
        workspace_id=workspace_id,
        state=state,
        item_id=item_id,
        keys=(
            _item_disposition_key(item_id),
            *_retired_disposition_keys(state, item_id),
            *_filed_issue_marker_keys(state, item_id),
        ),
    )


async def persist_parked_thread(
    self: Any,
    *,
    workspace_id: str,
    state: MonitorState,
    thread_id: str,
) -> None:
    """Save ONLY ``thread_id``'s ``needs_human`` park, merged onto the row.

    A fix-cycle park lives in memory until the loop's ``_persist_state``; a later
    transient ``_execute`` fault reloads state from the DB and would drop it, so
    the resolvable verdict returns and ``reply_to_thread`` re-runs on a reply that
    may already have landed. The body hash travels with the verdict so an older
    row hash cannot re-route the parked thread through AddressComments, and a
    DEFER's filed-issue marker so its re-triage never files a second issue. As
    with the posted reply, nothing else is flushed (#305).
    """
    await _persist_item_keys(
        self,
        workspace_id=workspace_id,
        state=state,
        item_id=thread_id,
        keys=(
            thread_id,
            _needs_human_reason_state_key(thread_id),
            review_thread_body_state_key(thread_id),
            _outdated_resolve_requeued_key(thread_id),
            *_filed_issue_marker_keys(state, thread_id),
        ),
    )


def _retired_disposition_keys(state: MonitorState, thread_id: str) -> tuple[str, ...]:
    """The thread's retired-reply tombstones (any conversation) held in memory."""
    prefix = _disposition_retired_key(thread_id, "")
    return tuple(key for key in state.threads_addressed_ids if key.startswith(prefix))


def _filed_issue_marker_keys(state: MonitorState, thread_id: str) -> tuple[str, ...]:
    """The thread's DEFER filed-issue markers (any body-hash era) held in memory."""
    prefix = _deferred_issue_filed_marker(thread_id, "")
    return tuple(key for key in state.threads_addressed_ids if key.startswith(prefix))


async def _persist_item_keys(
    self: Any,
    *,
    workspace_id: str,
    state: MonitorState,
    item_id: str,
    keys: tuple[str, ...],
) -> None:
    """Mirror ``keys`` from the in-memory map onto the row (absent keys are dropped)."""
    try:
        async with self._deps.session_factory() as session:
            ws = await WorkspaceRepository(session).get_for_update(workspace_id)
            if ws is None:
                return
            threads_addressed = dict(ws.monitor_threads_addressed or {})
            for key in keys:
                value = state.threads_addressed_ids.get(key)
                if value is None:
                    threads_addressed.pop(key, None)
                else:
                    threads_addressed[key] = value
            ws.monitor_threads_addressed = threads_addressed
            await session.commit()
    except (SQLAlchemyError, OSError) as exc:
        _log.warning(
            "monitor.thread_disposition_persist_failed",
            workspace_id=workspace_id,
            thread_id=item_id,
            error=repr(exc)[:400],
        )


def _disposition_for_resolve(
    state: MonitorState,
    thread_id: str,
    verdict: str | None,
    thread: ReviewThread | None,
) -> ThreadDisposition | None:
    """The thread's disposition; a legacy captured DEFER is rebuilt from its records.

    A ``defer`` captured before #1018 has its agent reason and filed-issue marker
    in state but no disposition record. With the thread at hand its issue URL is
    recoverable, so it is replied to and resolved instead of being parked.
    """
    disposition = read_item_disposition(state, thread_id)
    if disposition is not None or verdict != "defer" or thread is None:
        return disposition
    # Outdated hygiene resolves a ``defer`` only once its filed-issue marker exists;
    # should that ever not hold, the empty URL fails closed as EVIDENCE_MISSING.
    attach_issue_url(state, thread_id, _deferred_issue_url(state, thread) or "")
    return read_item_disposition(state, thread_id)


async def _park_thread(
    self: Any,
    *,
    workspace_id: str,
    pr_number: int,
    thread_id: str,
    state: MonitorState,
    action: str,
    reason_code: str,
    detail: str,
    base_branch: str | None,
    remote_branch: str | None,
    operation_id: str | None,
    operation_type: str | None,
    monitor_log: WorkspaceLogSink | None,
    source_head_sha: str | None,
    disposition: ThreadDisposition | None,
    forge_reason_code: str | None = None,
) -> None:
    """Keep the thread open and hand it to a human with a reason code."""
    state.mark_addressed(thread_id, "needs_human")
    state.mark_addressed(_needs_human_reason_state_key(thread_id), f"{reason_code}: {detail}")
    # ``needs_human`` blocks merge on its own; a stale requeue flag would linger.
    state.threads_addressed_ids.pop(_outdated_resolve_requeued_key(thread_id), None)
    _log.warning(
        "monitor.thread_disposition_parked",
        workspace_id=workspace_id,
        pr_number=pr_number,
        thread_id=thread_id,
        reason_code=reason_code,
        forge_reason_code=forge_reason_code,
    )
    await self._record_pr_monitor_audit_event(
        workspace_id=workspace_id,
        event_type=_AUDIT_COMMENT_RESOLUTION_EVENT,
        action=action,
        outcome="needs_human",
        reason_code=reason_code,
        pr_number=pr_number,
        status=None,
        base_branch=base_branch or "",
        remote_branch=remote_branch,
        operation_id=operation_id,
        operation_type=operation_type,
        monitor_log=monitor_log,
        source_head_sha=source_head_sha,
        evidence={
            "thread_ids": [thread_id],
            "resolved_thread_count": 0,
            "needs_human_thread_count": 1,
            "forge_reason_code": forge_reason_code,
            "disposition": disposition.as_payload() if disposition is not None else None,
        },
    )
