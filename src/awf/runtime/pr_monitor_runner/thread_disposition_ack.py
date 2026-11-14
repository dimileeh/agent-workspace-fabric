"""One-time check of a review bot's reply after AWF's disposition (#1138, D1; #1238).

Review bots often answer AWF's disposition reply ("Agreed — this is a false
positive"). The #1018 gate counts any comment after the marker as newer
feedback, so the marker goes stale and the #1138 fallback reopens the thread for
a full re-triage. This module is the pure policy in front of that fallback.

A thread is eligible only when it is resolved by AWF's identity, its opener is a
forge Bot account (the forge's own account type; no bot names or text patterns),
every comment after AWF's newest reply is by that same bot, and that newest
reply is a current marker once those comments are set aside. Each such comment
is judged by a deterministic concurrence check (:func:`is_bot_concurrence`),
else by the agent, and the judgment is recorded under its identity
(:func:`comment_identity`; an edit is a new identity), so the check runs at most
once per identity, across re-adoption too. A bot that concurs again after a
re-triage posts a new identity, which is judged afresh (#1238):

* every comment ``ACKNOWLEDGES``: the gate reads the thread without them, so the
  existing marker stays the disposition. No reply is posted and nothing reopens.
* any comment not judged yet: the gate reads the thread without them too, so its
  binding is not retired meanwhile, but certifies nothing and re-polls; the
  runner step (:mod:`~.thread_disposition_ack_check`) judges it outside the merge lock.
  After :data:`MAX_ACK_REFETCH_FAILURES` failed re-reads of the thread, its
  unjudged comments are recorded ``UNJUDGED`` instead, so it cannot re-poll forever.
* any comment ``NEW_POINT`` or ``UNJUDGED`` (agent failure, unparseable answer,
  or a reply too long to quote whole):
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
_ANSWER_RE = re.compile(r"AWF-DISPOSITION-ACK: (ACKNOWLEDGES|NEW_POINT)")
_EVIDENCE_MAX_CHARS = 4000
# #1238: the deterministic concurrence check. Generic wording only, no reviewer names.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
# An innermost collapsed block. It is set aside only when its summary labels it as
# supporting material; any other fold could hold a request, so the agent judges it.
_DETAILS_RE = re.compile(r"<details\b[^>]*>((?:(?!<details\b).)*?)</details\s*>", re.DOTALL | re.I)
_SUPPORTING_FOLD_RE = re.compile(
    r"\s*<summary\b[^>]*>[^<]*\b(?:analysis|learnings?|scripts?|outputs?|logs?)\b[^<]*</summary",
    re.I,
)
_PROPOSED_CHANGE_RE = re.compile(r"^\s*(?:```|~~~)\s*(?:suggestion|diff)\b", re.M | re.I)
_CODE_REFERENCE_RE = re.compile(r"```|~~~|\b[\w./-]+\.\w+:\d+|\blines?\s+\d+", re.I)
_DISSENT_RE = re.compile(
    r"\?|\b(?:however|but|still|should|please|consider|suggest|recommend|missing|"
    r"disagree|incorrect|wrong|also|additionally|another|not|never|"
    r"would|might|needs|need to|requires|lacks|worth|nit)\b|n't\b|"
    r"\b(?:could|can) you\b|"
    r"\b(?:issue|problem|bug|concern)s?\s+(?:remains?|persists?)\b(?!\s+withdrawn)",
    re.I,
)
# A clause concurs only as a whole (it is matched in full): "Agreed that the handler
# crashes" or "has a guard that crashes" may report a defect, so the agent judges it.
_CONCURRENCE_RE = re.compile(
    r"(?:@\S+\s+)?(?:"
    # An agreement word concurs only alone.
    r"agreed|i agree|you're (?:right|correct)|that's (?:right|correct)|"
    r"lgtm|looks good(?: to me)?|withdrawn|retracted|"
    # A predicate concurs only as the end of its clause, whatever its subject.
    r".*?\b(?:withdrawn|retracted|withdraw(?:ing)? (?:the|this|my) (?:finding|comment|point)|"
    # A verification concurs only when it is of the fix: a bare one may confirm the finding.
    r"(?:confirmed|verified) (?:the )?fix(?: (?:on|in|at) the (?:current|latest) "
    r"(?:head|revision|commit))?|fix (?:is |was |has been )?(?:confirmed|verified)|"
    r"is (?:now )?(?:fixed|addressed|resolved)|is a fix(?: rather than a false positive)?|"
    r"covers this case|"
    # The fix is present: the code has it, or its commit is in the revision.
    r"(?:has|have|contains|includes) the (?:guard|fix|check|test)|"
    r"commit [0-9a-f]{7,40} is (?:now )?in(?: the (?:current|latest) (?:head|revision))?|"
    r"leav(?:e|ing) (?:the )?resolution to (?:the )?(?:awf(?: monitor)?|monitor|you|@\S+)|"
    r"no further (?:action|changes?) (?:is |are )?(?:needed|required)))",
    re.I,
)
# Every clause must concur or only thank; anything else (an imperative such as "Add a
# test", or a statement such as "The handler crashes") could be a new point, so the
# agent judges it. A spaced hyphen, a dash, a bracket or "and" also joins clauses.
_CLAUSE_SPLIT_RE = re.compile(r"(?:[.!?;:,\n()\[\]–—]|\s-+\s|\band\b)+", re.I)
_THANKS_CLAUSE_RE = re.compile(r"(?:@\S+\s+)?(?:thanks|thank you)(?: for (?:the|your) \w+)?", re.I)


def comment_identity(comment: AuditedThreadComment) -> str:
    """SHA-256 hex of the comment's id, author and body: an edit is a new identity."""
    identity = [comment.comment_id, comment.author_login, comment.body]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def is_bot_concurrence(body: str) -> bool:
    """Whether ``body`` only concurs with AWF's disposition, decided without the agent.

    ``True`` means certainly a concurrence: it withdraws the finding, confirms the
    fix, or leaves resolution to AWF, in the bot's own words outside quotes, with
    no dissent or new request, no code fence and no code reference, and every
    clause of it either concurs or only thanks. HTML comments,
    and ``<details>`` blocks whose summary labels them as supporting material
    (analysis, learnings, scripts, output, logs), are set aside.
    ``False`` means not sure, never ``NEW_POINT``: the agent judges it. Anything
    ambiguous (a proposed change anywhere, unbalanced markup, any other fold) is
    ``False``.
    """
    if _PROPOSED_CHANGE_RE.search(body):
        return False
    prose = _HTML_COMMENT_RE.sub(" ", body)
    while fold := _DETAILS_RE.search(prose):
        if not _SUPPORTING_FOLD_RE.match(fold.group(1)):
            return False  # a fold not labelled as supporting material could hold a request
        prose = f"{prose[: fold.start()]} {prose[fold.end() :]}"
    prose = prose.replace("\u2019", "'")
    if re.search(r"<!--|</?details\b", prose, re.I):
        return False  # unbalanced or unterminated: it could hide a request
    if _CODE_REFERENCE_RE.search(prose) or _DISSENT_RE.search(prose):
        return False
    own = "\n".join(line for line in prose.splitlines() if not line.lstrip().startswith(">"))
    clauses = [c.strip() for c in _CLAUSE_SPLIT_RE.split(own) if c.strip()]
    if not all(_CONCURRENCE_RE.fullmatch(c) or _THANKS_CLAUSE_RE.fullmatch(c) for c in clauses):
        return False
    return any(_CONCURRENCE_RE.fullmatch(c) for c in clauses)


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


def ack_reply_fits(newer_reply: str) -> bool:
    """Whether :func:`ack_check_prompt` quotes ``newer_reply`` whole.

    A truncated reply is never judged: a new point past the cap would go unseen.
    """
    return len(redact_secrets(newer_reply)) <= _EVIDENCE_MAX_CHARS


def _evidence(name: str, text: str) -> str:
    bounded = redact_secrets(text)
    if len(bounded) > _EVIDENCE_MAX_CHARS:
        bounded = f"{bounded[:_EVIDENCE_MAX_CHARS]}\n[truncated]"
    return render_untrusted_evidence(
        UntrustedEvidence(source_kind="review_thread_comment", source_name=name, text=bounded)
    )


def parse_ack_answer(stdout: str) -> Literal["ACKNOWLEDGES", "NEW_POINT"] | None:
    """The agent's answer: the last exact record line; output after it is ignored.

    Lines that are not exact records are skipped. No exact record is no answer.
    """
    for line in reversed(stdout.splitlines()):
        match = _ANSWER_RE.fullmatch(line.strip())
        if match is not None:
            return ACKNOWLEDGES if match.group(1) == ACKNOWLEDGES else NEW_POINT
    return None
