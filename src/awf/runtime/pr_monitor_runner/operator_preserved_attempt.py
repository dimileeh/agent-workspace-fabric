"""Operator disposition that retires a preserved correction attempt (issue #1189).

A correction attempt AWF cannot accept keeps its commit and parks the review item
``needs_human`` with a reason that names the preserved tip
(``comment_verdict_correction`` / ``comment_verdict_evidence_tolerance``). The
verdict, its reason and the body snapshot are head-independent, so they also
follow the PR into a re-adopting workspace. For an *outdated* thread nothing
else ever clears that record: outdated hygiene only resolves closed verdicts, and
a guide answering the park either matched nothing (it named the attempt by SHA)
or only re-queued the thread for an agent re-triage that could park again (#938).
The guide was consumed and ``decide()`` returned the identical ``NotifyHuman``.

This module applies the operator's explicit ruling instead: a directive that
**rejects the preserved attempt** and calls the thread **obsolete / resolved**.
The ruling must resolve to exactly one parked preserved-attempt item, either by a
named thread id or by a SHA prefix of its preserved tip. Applying it retires the
record (the ``needs_human`` reason goes, the verdict becomes ``false_positive``
with an operator-sourced disposition, and a tombstone keeps the rejected tip),
writes an audit event, and finalizes the hint without running the agent. The
next poll's outdated hygiene then posts the disposition reply and resolves the
thread through its existing path. "Reject" retires the attempt as evidence for
the item; it never rewrites branch history.

Anything short of that fails closed: the record is left untouched and the hint
parks ``needs_human`` with ``OPERATOR_PRESERVED_ATTEMPT_DISPOSITION_UNAPPLIED``,
so ``decide()`` surfaces that distinct reason rather than the original park. A
directive without the disposition phrase never reaches any of this.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from awf.runtime.feedback_policy import (
    preferred_duplicate_review_thread,
    recorded_review_thread_body_matches,
    review_thread_body_hash,
    review_thread_body_state_key,
)
from awf.runtime.logs import WorkspaceLogSink
from awf.runtime.monitor_state_keys import _preserved_attempt_retired_key
from awf.runtime.operator_hints import mark_operator_hint_needs_human
from awf.runtime.pr_monitor import (
    MonitorState,
    OperatorHint,
    PRStatus,
    ReviewThread,
    _mark_review_thread_addressed,
)
from awf.runtime.pr_monitor_runner.helpers import _agent_failed_reason_state_key
from awf.runtime.pr_monitor_runner.logging import _log
from awf.runtime.pr_monitor_runner.notify_human_details import _needs_human_reason_state_key
from awf.runtime.pr_monitor_runner.operator_hint_parsing import (
    _operator_hint_review_thread_id_candidates,
)
from awf.runtime.pr_monitor_runner.operator_hint_retirement import (
    _finalize_processed_operator_hint,
)
from awf.runtime.pr_monitor_runner.thread_disposition import (
    ThreadDisposition,
    read_item_disposition,
    record_item_disposition,
    retire_item_disposition,
)

OPERATOR_PRESERVED_ATTEMPT_RETIRED = "OPERATOR_PRESERVED_ATTEMPT_RETIRED"
OPERATOR_PRESERVED_ATTEMPT_DISPOSITION_UNAPPLIED = (
    "OPERATOR_PRESERVED_ATTEMPT_DISPOSITION_UNAPPLIED"
)
_AUDIT_EVENT = "workspace.audit.operator_disposition"
_AUDIT_ACTION = "retire_preserved_attempt"
_DISPOSITION_SOURCE = "operator_disposition"

# "reject ... preserved [correction] attempt|commit" inside one clause (no ``.``,
# ``;``, ``:`` or newline in between). A negation right before "reject" captures
# ``neg`` so "do not reject the preserved attempt" is never read as a rejection.
_REJECT_RE = re.compile(
    r"(?P<neg>\b(?:do\s+not|don['’]t|never|not)\s+)?"
    r"\breject(?:s|ed|ing)?\b[^.;:\n]*?\bpreserved\s+(?:correction\s+)?(?:attempt|commit)\b",
    re.IGNORECASE,
)
_RESOLVE_RE = re.compile(r"\b(?:resolv\w*|obsolete)\b", re.IGNORECASE)
# A resolve word only counts as the operator's ruling when it is affirmative: a
# negation right before it ("do not resolve", "not obsolete") or a condition in
# its clause ("resolve if CI passes", "only resolve once ...") voids the ruling.
_RESOLVE_NEGATED_RE = re.compile(
    r"\b(?:not|never|no|without|\w+n['’]t)\s+(?:(?:yet|ever|actually|really)\s+)?$",
    re.IGNORECASE,
)
_CONDITION_RE = re.compile(
    r"\b(?:if|unless|once|when|whenever|until|only|after|before|provided)\b",
    re.IGNORECASE,
)
_CLAUSE_BREAK = ".;:\n"
# A standalone 7-40 hex token with at least one digit and one letter: an all-digit
# token is a comment id far more often than a SHA, and an all-letter one is a word
# ("defaced"). Either would otherwise fail a named thread closed as a mismatch.
_SHA_TOKEN_RE = re.compile(
    r"(?<![\w-])(?=[0-9a-f]*[0-9])(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}(?![\w-])",
    re.IGNORECASE,
)
# The tip as AWF's kept-commit reason templates cite it. Only a reason that also
# says the commit is "preserved" is a preserved-attempt park; an accepted FIXED
# cites its tip too and must never qualify.
_PRESERVED_TIP_RE = re.compile(r"(?:attempt tip|own commit) ([0-9a-f]{7,40})\b")

_UNAPPLIED_EXPLANATION: dict[str, str] = {
    "no_item_named": "it names no review thread id and no preserved attempt SHA.",
    "multiple_threads_named": (
        "it names more than one review thread id, so AWF cannot tell which thread the "
        "ruling covers; rule on one thread per guide."
    ),
    "no_matching_item": "no parked preserved correction attempt matches the named SHA.",
    "ambiguous_item": "the named SHA matches more than one parked preserved correction attempt.",
    "sha_mismatch": "the named thread's preserved attempt is not the named SHA.",
    "not_preserved_attempt": "the named thread is not parked on a preserved correction attempt.",
    "thread_not_outdated": (
        "the thread is still active on the current head; only an outdated thread is "
        "closed this way."
    ),
    "thread_not_open": "the thread is not open on the PR any more.",
    "conversation_changed": (
        "the thread's conversation changed since it was parked, so the ruling may not "
        "cover the newest feedback."
    ),
    "no_body_snapshot": (
        "AWF has no snapshot of the conversation the park answered, so it cannot prove "
        "the ruling covers it."
    ),
}


@dataclass(frozen=True)
class PreservedAttemptDisposition:
    """The items an explicit "reject the preserved attempt; resolve" ruling names."""

    thread_ids: tuple[str, ...]
    shas: tuple[str, ...]


@dataclass(frozen=True)
class PreservedAttemptOutcome:
    """What the hint action records when the disposition took over the cycle."""

    outcome: str
    reason_code: str


@dataclass(frozen=True)
class _Target:
    thread_id: str
    tip: str
    already_retired: bool


def preserved_attempt_disposition(text: str | None) -> PreservedAttemptDisposition | None:
    """Parse an explicit preserved-attempt disposition from an operator directive.

    ``None`` unless the text both rejects a preserved attempt (not negated) and
    affirmatively asks for the thread to be resolved / called obsolete (no
    negated or conditional resolve word anywhere). Thread ids and SHA
    tokens are returned deduplicated in first-occurrence order, SHAs lowercased.
    """
    if not text:
        return None
    rejects = any(match.group("neg") is None for match in _REJECT_RE.finditer(text))
    if not rejects or not _resolves_affirmatively(text):
        return None
    shas = tuple(dict.fromkeys(token.lower() for token in _SHA_TOKEN_RE.findall(text)))
    return PreservedAttemptDisposition(
        thread_ids=_operator_hint_review_thread_id_candidates(text),
        shas=shas,
    )


def _resolves_affirmatively(text: str) -> bool:
    """True when the text asks to resolve and no resolve word is negated or conditional."""
    matches = list(_RESOLVE_RE.finditer(text))
    for match in matches:
        start = max(text.rfind(char, 0, match.start()) for char in _CLAUSE_BREAK) + 1
        ends = [i for i in (text.find(char, match.end()) for char in _CLAUSE_BREAK) if i >= 0]
        clause = text[start : min(ends, default=len(text))]
        if _RESOLVE_NEGATED_RE.search(text[start : match.start()]) or _CONDITION_RE.search(clause):
            return False
    return bool(matches)


def preserved_attempt_tip(reason: str | None) -> str | None:
    """The preserved tip a kept-commit ``needs_human`` reason cites, else ``None``."""
    if not reason or "preserved" not in reason:
        return None
    match = _PRESERVED_TIP_RE.search(reason)
    return match.group(1) if match else None


def _sha_matches(shas: tuple[str, ...], tip: str) -> bool:
    return any(tip.startswith(sha) or sha.startswith(tip) for sha in shas)


def _resolve_targets(
    state: MonitorState, disposition: PreservedAttemptDisposition
) -> tuple[list[_Target], str | None]:
    """Map the ruling onto parked items; ``(targets, None)`` or ``([], detail)``."""
    entries = state.threads_addressed_ids
    reason_prefix = _needs_human_reason_state_key("")
    retired_prefix = _preserved_attempt_retired_key("")
    live: dict[str, str] = {}
    retired: dict[str, str] = {}
    for key, value in entries.items():
        if key.startswith(reason_prefix):
            item_id = key[len(reason_prefix) :]
            tip = preserved_attempt_tip(value)
            if tip is not None and entries.get(item_id) == "needs_human":
                live[item_id] = tip
        elif key.startswith(retired_prefix) and value:
            retired[key[len(retired_prefix) :]] = value
    # A retirement is only done once its disposition reply and resolve landed: a
    # failed reply re-parks the item ``needs_human`` under a reason that no longer
    # cites the tip, and a repeated ruling must retry it rather than no-op.
    done = {tid for tid in retired if entries.get(tid) != "needs_human"}
    shas = disposition.shas
    if len(disposition.thread_ids) > 1:
        # Thread ids are collected from the whole directive, so a second id may be
        # one the operator excluded ("keep PRRT_B parked"); never guess which.
        return [], "multiple_threads_named"
    if disposition.thread_ids:
        thread_id = disposition.thread_ids[0]
        tip = live.get(thread_id)
        already_retired = tip is None and thread_id in done
        tip = tip or retired.get(thread_id)
        if tip is None:
            return [], "not_preserved_attempt"
        if shas and not _sha_matches(shas, tip):
            return [], "sha_mismatch"
        return [_Target(thread_id, tip, already_retired)], None
    if not shas:
        return [], "no_item_named"
    matches = [_Target(tid, tip, False) for tid, tip in live.items() if _sha_matches(shas, tip)]
    if not matches:
        matches = [
            _Target(tid, tip, tid in done)
            for tid, tip in retired.items()
            if tid not in live and _sha_matches(shas, tip)
        ]
    if not matches:
        return [], "no_matching_item"
    if len(matches) > 1:
        return [], "ambiguous_item"
    return matches, None


def _retirement_holds(state: MonitorState, status: PRStatus, thread_id: str) -> bool:
    """True while the tombstone's retirement still governs the live thread.

    The tombstone outlives the verdict it explains: a reopened thread, fresh
    replies or a moved-head re-adopt can drop the ``false_positive`` verdict and
    leave the thread open. Only a thread that is gone from the PR, or still
    carries that verdict, makes a repeated ruling a no-op; anything else goes
    through the live-thread checks again.
    """
    if state.threads_addressed_ids.get(thread_id) == "false_positive":
        return True
    open_threads = status.unresolved_inline_threads + status.outdated_unresolved_inline_threads
    return all(thread.thread_id != thread_id for thread in open_threads)


def _live_thread(
    state: MonitorState, status: PRStatus, thread_id: str
) -> tuple[ReviewThread | None, str | None]:
    """The open outdated thread the ruling closes, or the reason it cannot."""
    if any(thread.thread_id == thread_id for thread in status.unresolved_inline_threads):
        return None, "thread_not_outdated"
    copies = [
        thread
        for thread in status.outdated_unresolved_inline_threads
        if thread.thread_id == thread_id
    ]
    if not copies:
        return None, "thread_not_open"
    thread = preferred_duplicate_review_thread(copies, state.threads_addressed_ids)
    recorded = state.threads_addressed_ids.get(review_thread_body_state_key(thread_id))
    if not recorded:
        return None, "no_body_snapshot"
    if not recorded_review_thread_body_matches(recorded, thread):
        return None, "conversation_changed"
    return thread, None


def _retire(state: MonitorState, target: _Target, thread: ReviewThread) -> None:
    """Retire the preserved-attempt record and stage the operator's disposition reply."""
    thread_id = target.thread_id
    state.threads_addressed_ids.pop(_needs_human_reason_state_key(thread_id), None)
    state.threads_addressed_ids.pop(_agent_failed_reason_state_key(thread_id), None)
    _mark_review_thread_addressed(state, thread, "false_positive")
    state.mark_addressed(_preserved_attempt_retired_key(thread_id), target.tip)
    previous = read_item_disposition(state, thread_id)
    if previous is not None and previous.reply_comment_id:
        # A reply AWF posted for an earlier verdict answers nothing the operator
        # ruled on; retire it so its marker never certifies this resolution.
        retire_item_disposition(state, previous)
    # The reply cites the rejected tip, never the operator's free text: guide
    # notes are private to the operator and the thread is public.
    record_item_disposition(
        state,
        ThreadDisposition(
            item_id=thread_id,
            disposition_class="false_positive",
            verdict="false_positive",
            reason=(
                f"Obsolete per operator ruling; the preserved correction attempt "
                f"{target.tip} is rejected for this thread."
            ),
            source=_DISPOSITION_SOURCE,
            thread_body_hash=review_thread_body_hash(thread),
        ),
    )


async def apply_preserved_attempt_disposition(
    self: Any,
    *,
    workspace_id: str,
    pr_number: int,
    status: PRStatus,
    state: MonitorState,
    hint: OperatorHint,
    base_branch: str,
    remote_branch: str | None,
    operation_id: str | None,
    monitor_log: WorkspaceLogSink | None,
) -> PreservedAttemptOutcome | None:
    """Apply an explicit preserved-attempt disposition before the hint's agent cycle.

    Returns ``None`` (the agent cycle runs as before) when the directive carries
    no disposition, or when a protected-scope block is pending: that resume is
    owned by the grant/directive protocol. Otherwise the hint is finalized
    (applied, or already applied earlier) or parked ``needs_human`` with the
    UNAPPLIED reason code, and the caller persists state and skips the agent.
    """
    disposition = preserved_attempt_disposition(hint.directive)
    if disposition is None or state.has_preserved_protected_block:
        return None
    targets, detail = _resolve_targets(state, disposition)
    staged: list[tuple[_Target, ReviewThread]] = []
    for target in targets:
        if target.already_retired and _retirement_holds(state, status, target.thread_id):
            continue
        thread, detail = _live_thread(state, status, target.thread_id)
        if thread is None:
            break
        staged.append((target, thread))
    audit: dict[str, Any] = {
        "workspace_id": workspace_id,
        "pr_number": pr_number,
        "status": status,
        "base_branch": base_branch,
        "remote_branch": remote_branch,
        "operation_id": operation_id,
        "monitor_log": monitor_log,
    }
    if detail is not None:
        message = (
            f"{OPERATOR_PRESERVED_ATTEMPT_DISPOSITION_UNAPPLIED}: the ruling to reject a "
            f"preserved correction attempt was not applied ({detail}): "
            f"{_UNAPPLIED_EXPLANATION[detail]} Send a guide naming the review thread id "
            "and the preserved attempt SHA."
        )
        await _record(
            self,
            **audit,
            outcome="unapplied",
            reason_code=OPERATOR_PRESERVED_ATTEMPT_DISPOSITION_UNAPPLIED,
            evidence={
                "detail": detail,
                "thread_ids": list(disposition.thread_ids),
                "shas": list(disposition.shas),
                "hint_operation_id": hint.operation_id,
            },
        )
        mark_operator_hint_needs_human(state, message)
        return PreservedAttemptOutcome(
            outcome="operator_hint_preserved_attempt_unapplied",
            reason_code=OPERATOR_PRESERVED_ATTEMPT_DISPOSITION_UNAPPLIED,
        )
    for target, thread in staged:
        _retire(state, target, thread)
        await _record(
            self,
            **audit,
            outcome="retired",
            reason_code=OPERATOR_PRESERVED_ATTEMPT_RETIRED,
            evidence={
                "thread_id": target.thread_id,
                "attempt_tip": target.tip,
                "hint_operation_id": hint.operation_id,
            },
        )
    _finalize_processed_operator_hint(state, hint=hint)
    return PreservedAttemptOutcome(
        outcome="operator_hint_preserved_attempt_retired",
        reason_code=OPERATOR_PRESERVED_ATTEMPT_RETIRED,
    )


async def _record(
    self: Any,
    *,
    workspace_id: str,
    pr_number: int,
    status: PRStatus,
    base_branch: str,
    remote_branch: str | None,
    operation_id: str | None,
    monitor_log: WorkspaceLogSink | None,
    outcome: str,
    reason_code: str,
    evidence: dict[str, object],
) -> None:
    """Log and audit one disposition outcome under its reason code."""
    _log.info(
        "monitor.operator_preserved_attempt_disposition",
        workspace_id=workspace_id,
        pr_number=pr_number,
        outcome=outcome,
        reason_code=reason_code,
        **evidence,
    )
    await self._record_pr_monitor_audit_event(
        workspace_id=workspace_id,
        event_type=_AUDIT_EVENT,
        action=_AUDIT_ACTION,
        outcome=outcome,
        reason_code=reason_code,
        pr_number=pr_number,
        status=status,
        base_branch=base_branch,
        remote_branch=remote_branch,
        operation_id=operation_id,
        monitor_log=monitor_log,
        evidence=evidence,
    )
