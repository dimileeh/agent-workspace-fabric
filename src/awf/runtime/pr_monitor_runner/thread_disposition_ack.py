"""One-time agent check of a review bot's reply after AWF's disposition (#1138, D1).

Review bots often answer AWF's disposition reply ("Agreed — this is a false
positive"). The #1018 gate counts any comment after the marker as newer
feedback, so the marker goes stale and the #1138 fallback reopens the thread for
a full re-triage. This module is the pure policy in front of that fallback.

A thread is eligible only when it is resolved by AWF's identity, its opener is a
forge Bot account (the forge's own account type; no bot names or text patterns),
every comment after AWF's newest reply is by that same bot, and that newest
reply is a current marker once those comments are set aside. For each such
comment the agent's judgment is recorded under its identity
(:func:`comment_identity`; an edit is a new identity), so the check runs at most
once per identity, across re-adoption too:

* every comment ``ACKNOWLEDGES``: the gate reads the thread without them, so the
  existing marker stays the disposition. No reply is posted and nothing reopens.
* any comment not judged yet: the gate reads the thread without them too, so its
  binding is not retired meanwhile, but certifies nothing and re-polls; the
  runner step (:mod:`~.thread_disposition_ack_check`) judges it outside the merge lock.
  After :data:`MAX_ACK_REFETCH_FAILURES` failed re-reads of the thread, its
  unjudged comments are recorded ``UNJUDGED`` instead, so it cannot re-poll forever.
* any comment ``NEW_POINT`` or ``UNJUDGED`` (agent failure, unparseable answer):
  the full thread is kept, which is today's path to the fallback reopen.

Human comments, and comments by anyone but the thread's opening bot, keep
today's semantics: they supersede the marker.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from typing import Final, Literal, cast

from awf.common.forge_thread_audit import (
    AuditedReviewThread,
    AuditedThreadComment,
    ReviewThreadAudit,
)
from awf.common.prompt_evidence import UntrustedEvidence, render_untrusted_evidence
from awf.common.redaction import redact_secrets
from awf.runtime.monitor_state_keys import _disposition_ack_key, _disposition_ack_pending_key
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.thread_disposition import disposition_marker_class

ACKNOWLEDGES: Final = "ACKNOWLEDGES"
NEW_POINT: Final = "NEW_POINT"
UNJUDGED: Final = "UNJUDGED"  # the check failed: the agent errored, or its answer was unusable
AckJudgment = Literal["ACKNOWLEDGES", "NEW_POINT", "UNJUDGED"]
ACK_JUDGMENTS: frozenset[str] = frozenset({ACKNOWLEDGES, NEW_POINT, UNJUDGED})
# Failed re-reads of one flagged thread before its check counts as failed.
MAX_ACK_REFETCH_FAILURES = 3
_ANSWER_PREFIX = "AWF-DISPOSITION-ACK:"
_ANSWER_RE = re.compile(r"AWF-DISPOSITION-ACK: (ACKNOWLEDGES|NEW_POINT)")
_EVIDENCE_MAX_CHARS = 4000


def comment_identity(comment: AuditedThreadComment) -> str:
    """SHA-256 hex of the comment's id, author and body: an edit is a new identity."""
    identity = [comment.comment_id, comment.author_login, comment.body]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def acknowledgement_view(thread: AuditedReviewThread, *, viewer: str) -> AuditedReviewThread | None:
    """The thread without its trailing bot comments when it is eligible, else ``None``.

    ``viewer`` is AWF's casefolded login.
    """
    view = thread.without_trailing
    opener = thread.opener
    if view is None or not thread.trailing_comments or opener is None or not opener.author_is_bot:
        return None
    if not thread.is_resolved or (thread.resolved_by or "").strip().casefold() != viewer:
        return None
    bot = (opener.author_login or "").casefold()
    if not bot or any(
        not comment.author_is_bot or (comment.author_login or "").casefold() != bot
        for comment in thread.trailing_comments
    ):
        return None
    # AWF's marker must be the newest viewer comment, current without them: an
    # unmarked follow-up by AWF's identity (the operator's account) is feedback.
    bodies = view.current_viewer_comment_bodies
    if not bodies or disposition_marker_class(bodies[-1:], thread_id=thread.thread_id) is None:
        return None
    return view


def read_ack_judgment(
    state: MonitorState, thread_id: str, comment: AuditedThreadComment
) -> AckJudgment | None:
    """The recorded judgment of ``comment``; an unknown value reads as ``UNJUDGED``."""
    value = state.threads_addressed_ids.get(
        _disposition_ack_key(thread_id, comment_identity(comment))
    )
    if value is None:
        return None
    return cast(AckJudgment, value) if value in ACK_JUDGMENTS else UNJUDGED


def ack_refetch_failures(state: MonitorState, thread_id: str) -> int:
    """Failed re-reads so far of a flagged thread (the pending key's decimal value)."""
    raw = state.threads_addressed_ids.get(_disposition_ack_pending_key(thread_id), "")
    return int(raw) if raw.isdecimal() else 0


def record_ack_judgment(
    state: MonitorState, thread_id: str, comment: AuditedThreadComment, judgment: AckJudgment
) -> None:
    """Persist the agent's judgment of ``comment`` under its identity."""
    state.mark_addressed(_disposition_ack_key(thread_id, comment_identity(comment)), judgment)


def acknowledgement_views(
    state: MonitorState, audit: ReviewThreadAudit, *, viewer: str
) -> tuple[ReviewThreadAudit, frozenset[str]]:
    """Apply the recorded judgments to ``audit``; return it and the threads still to judge.

    Sets the pending key of each thread still to judge (keeping its failed re-read
    count) and drops every other one, so the runner step only ever judges what the
    gate's latest audit asked for.
    """
    threads: list[AuditedReviewThread] = []
    pending: dict[str, int] = {}
    for thread in audit.threads:
        view = acknowledgement_view(thread, viewer=viewer)
        judgments = (
            {read_ack_judgment(state, thread.thread_id, c) for c in thread.trailing_comments}
            if view is not None
            else set()
        )
        if view is None or NEW_POINT in judgments or UNJUDGED in judgments:
            threads.append(thread)  # today's semantics: the fallback re-triages it
            continue
        if None in judgments:
            failures = ack_refetch_failures(state, thread.thread_id)
            if failures >= MAX_ACK_REFETCH_FAILURES:
                for comment in thread.trailing_comments:
                    if read_ack_judgment(state, thread.thread_id, comment) is None:
                        record_ack_judgment(state, thread.thread_id, comment, UNJUDGED)
                threads.append(thread)  # the check failed: the fallback re-triages it
                continue
            pending[thread.thread_id] = failures
        threads.append(view)
    prefix = _disposition_ack_pending_key("")
    for key in [k for k in state.threads_addressed_ids if k.startswith(prefix)]:
        if key[len(prefix) :] not in pending:
            del state.threads_addressed_ids[key]
    for thread_id, failures in pending.items():
        state.mark_addressed(_disposition_ack_pending_key(thread_id), str(failures))
    return replace(audit, threads=tuple(threads)), frozenset(pending)


def ack_check_prompt(opener_body: str, disposition_reply: str, newer_reply: str) -> str:
    """The judgment-only prompt: the reviewer's comment, AWF's reply and the newer reply."""
    evidence = "\n\n".join(
        f"{heading}:\n{_evidence(name, text)}"
        for heading, name, text in (
            ("The reviewer's original comment", "review thread opener", opener_body),
            ("AWF's disposition reply", "AWF disposition reply", disposition_reply),
            ("The reviewer's newer reply", "review thread newer reply", newer_reply),
        )
    )
    return (
        "AWF posted a disposition reply in a pull-request review thread and resolved the "
        "thread. The review bot that opened the thread then replied again. This is a "
        "judgment-only task: do not modify files, run commands, or commit.\n\n"
        "Decide whether the bot's newer reply only ACKNOWLEDGES AWF's disposition (it "
        "agrees, thanks, or closes the point and asks for nothing more) or raises a "
        "NEW_POINT (it disagrees, asks for a change, or adds feedback the disposition does "
        "not answer). When unsure, answer NEW_POINT.\n\n"
        f"{evidence}\n\n"
        "End your answer with exactly one final line, either\n"
        "AWF-DISPOSITION-ACK: ACKNOWLEDGES\n"
        "or\n"
        "AWF-DISPOSITION-ACK: NEW_POINT\n"
        "and print no other line containing AWF-DISPOSITION-ACK."
    )


def _evidence(name: str, text: str) -> str:
    bounded = redact_secrets(text)
    if len(bounded) > _EVIDENCE_MAX_CHARS:
        bounded = f"{bounded[:_EVIDENCE_MAX_CHARS]}\n[truncated]"
    return render_untrusted_evidence(
        UntrustedEvidence(source_kind="review_thread_comment", source_name=name, text=bounded)
    )


def parse_ack_answer(stdout: str) -> Literal["ACKNOWLEDGES", "NEW_POINT"] | None:
    """The agent's answer: exactly one record line, and it is the final non-empty line."""
    from awf.runtime.pr_monitor_runner.helpers_verdict import _final_non_empty_line

    final = _final_non_empty_line(stdout)
    match = _ANSWER_RE.fullmatch(final.strip()) if final is not None else None
    if match is None or sum(_ANSWER_PREFIX in line for line in stdout.splitlines()) != 1:
        return None
    return ACKNOWLEDGES if match.group(1) == ACKNOWLEDGES else NEW_POINT
