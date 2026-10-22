"""Durable, attributable review-thread dispositions (#1018).

AWF never resolves a review thread silently. Before every resolve the monitor
posts ONE concise reply inside the thread whose first line is a stable machine
marker::

    <!-- AWF:thread-disposition:v1 class=<class> thread=<thread id> -->

followed by the disposition: the fix commit(s) for ``fixed``, the agent's own
reason for ``false_positive`` / ``deferred`` (plus the tracking issue), the
matched branch commit for ``fixed_branch_evidence`` and the guide operation for
``operator_acknowledged``. The thread id binds the marker to its thread, so a
copy pasted anywhere else is not evidence.

This module is the pure half: the per-item record persisted in
``MonitorState.threads_addressed_ids`` (``_item_disposition_key``), the checks
that decide whether a reply can be posted at all, the reply renderer and the
marker parser the READY_TO_MERGE gate uses. The I/O lives in
``thread_disposition_reply`` (reply-before-resolve) and
``thread_disposition_gate`` (cert / merge gate).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal, cast, get_args

from awf.common.redaction import redact_secrets
from awf.runtime.feedback_policy import (
    recorded_review_thread_body_matches,
    review_thread_body_hash,
)
from awf.runtime.monitor_prompts import (
    LEGACY_BASELINE_LABEL,
    RESOLVED_BY_OTHERS_LABEL,
    _redact_and_escape_markdown_inline,
)
from awf.runtime.monitor_state_keys import (
    _disposition_retired_key,
    _disposition_unverified_key,
    _item_disposition_key,
)
from awf.runtime.pr_monitor import MonitorState, PRStatus, ReviewThread
from awf.runtime.pr_monitor_runner.comment_repair_provenance import (
    ItemCommitProvenance,
    chain_from_state,
)
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    MonitorVerdictResult,
    VerdictResult,
)
from awf.runtime.pr_monitor_runner.logging import _log

# A FALSE POSITIVE / DEFER disposition has no reason to publish, so the thread
# is parked ``needs_human`` instead of being resolved without one.
THREAD_DISPOSITION_REASON_MISSING = "THREAD_DISPOSITION_REASON_MISSING"
# A FIXED disposition has no fix commit (or a DEFER no tracking issue) to cite.
THREAD_DISPOSITION_EVIDENCE_MISSING = "THREAD_DISPOSITION_EVIDENCE_MISSING"
# The forge rejected the in-thread disposition reply; the thread stays open.
THREAD_DISPOSITION_REPLY_FAILED = "THREAD_DISPOSITION_REPLY_FAILED"
# READY_TO_MERGE / merge refused: AWF-resolved threads carry no disposition marker.
REVIEW_THREAD_DISPOSITION_UNVERIFIED = "REVIEW_THREAD_DISPOSITION_UNVERIFIED"
# The forge could not enumerate the PR's review threads for that verification.
REVIEW_THREAD_AUDIT_UNAVAILABLE = "REVIEW_THREAD_AUDIT_UNAVAILABLE"
# Rollout: the first gate run recorded unmarked AWF resolutions as a legacy baseline.
REVIEW_THREAD_DISPOSITION_LEGACY_BASELINE = "REVIEW_THREAD_DISPOSITION_LEGACY_BASELINE"

DispositionClass = Literal[
    "fixed",
    "fixed_branch_evidence",
    "false_positive",
    "deferred",
    "operator_acknowledged",
]
DISPOSITION_CLASSES: tuple[DispositionClass, ...] = get_args(DispositionClass)
RESOLVED_BY_OTHERS = "resolved_by_others"
LEGACY_BASELINE = "legacy_baseline"

_VERDICT_CLASS: dict[str, DispositionClass] = {
    "fix_committed": "fixed",
    "false_positive": "false_positive",
    "defer": "deferred",
}
_MARKER = "<!-- AWF:thread-disposition:v1 class={cls} thread={thread} -->"
_MARKER_RE = re.compile(
    r"<!-- AWF:thread-disposition:v1 class=(?P<cls>[a-z_]+) thread=(?P<thread>\S+) -->"
)
_LABELS: dict[str, str] = {
    "fixed": "fixed",
    "fixed_branch_evidence": "fixed (branch evidence)",
    "false_positive": "false positive",
    "deferred": "deferred",
    "operator_acknowledged": "operator acknowledged",
    RESOLVED_BY_OTHERS: RESOLVED_BY_OTHERS_LABEL,
    LEGACY_BASELINE: LEGACY_BASELINE_LABEL,
}
# An operator acknowledges a flagged thread only with this explicit token; a
# directive that merely mentions the thread id (to re-check it, say) does not.
_ACK_THREAD_RE = re.compile(r"(?<![\w-])ACK-THREAD[ \t]+(?P<thread>PRRT_[A-Za-z0-9_-]+)")
_PAYLOAD_VERSION = 1
_UNSAFE_URL_CHARACTERS = frozenset('<>"`')
_BLOCKER_LISTED_IDS = 10
_BLOCKER_MAX_CHARS = 480


@dataclass(frozen=True)
class ThreadDisposition:
    """What AWF publishes in a review thread before resolving it."""

    item_id: str
    disposition_class: DispositionClass
    verdict: str | None
    reason: str | None = None
    commits: tuple[str, ...] = ()
    commit_base: str | None = None
    issue_url: str | None = None
    source: str = "agent_verdict"
    reply_comment_id: str | None = None
    thread_body_hash: str | None = None
    # The audited conversation (before AWF's reply) this disposition answers: an
    # acknowledgement's flagged one; any other's, once a gate audit saw its reply
    # resolved. It holds only while that conversation stays resolved (#1018).
    conversation_digest: str | None = None
    # AWF's own resolve on this reply landed: unlike a failed resolve's reply, it
    # is never reused once the thread is seen open again (PRRT_kwDOSJAM6s6vZ4nJ).
    resolved: bool = False

    @property
    def is_bound(self) -> bool:
        """Whether this disposition answers a resolution an audit saw.

        An acknowledgement always does (a missing conversation then matches
        none); any other once a gate audit bound it. Seeing the thread open again
        means a bound disposition's resolution was reopened.
        """
        return self.conversation_digest is not None or (
            self.disposition_class == "operator_acknowledged"
        )

    @property
    def answers_resolution(self) -> bool:
        """Whether a resolution followed this disposition: bound, or AWF's resolve landed.

        Seeing the thread open again then means that resolution was reopened, so
        the reply is retired even before any audit bound it. Only a reply whose
        resolve failed answers none yet; its retry reuses it.
        """
        return self.resolved or self.is_bound

    def as_payload(self) -> dict[str, object]:
        """Return the JSON-safe record persisted in state and audit evidence."""
        return {
            "v": _PAYLOAD_VERSION,
            "item_id": self.item_id,
            "class": self.disposition_class,
            "verdict": self.verdict,
            "reason": self.reason,
            "commits": list(self.commits),
            "commit_base": self.commit_base,
            "issue_url": self.issue_url,
            "source": self.source,
            "reply_comment_id": self.reply_comment_id,
            "thread_body_hash": self.thread_body_hash,
            "conversation_digest": self.conversation_digest,
            "resolved": self.resolved,
        }

    @classmethod
    def from_payload(cls, raw: object) -> ThreadDisposition | None:
        """Decode a persisted record; anything malformed reads as "no disposition".

        Failing closed matters: a missing record parks the thread instead of
        resolving it, which is the safe direction.
        """
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                return None
        if not isinstance(raw, Mapping):
            return None
        item_id = _optional_text(raw.get("item_id"))
        disposition_class = raw.get("class")
        if item_id is None or disposition_class not in DISPOSITION_CLASSES:
            return None
        commits = raw.get("commits")
        return cls(
            item_id=item_id,
            disposition_class=cast(DispositionClass, disposition_class),
            verdict=_optional_text(raw.get("verdict")),
            reason=_optional_text(raw.get("reason")),
            commits=tuple(c for c in commits if isinstance(c, str) and c.strip())
            if isinstance(commits, list)
            else (),
            commit_base=_optional_text(raw.get("commit_base")),
            issue_url=_optional_text(raw.get("issue_url")),
            source=_optional_text(raw.get("source")) or "agent_verdict",
            reply_comment_id=_optional_text(raw.get("reply_comment_id")),
            thread_body_hash=_optional_text(raw.get("thread_body_hash")),
            conversation_digest=_optional_text(raw.get("conversation_digest")),
            resolved=raw.get("resolved") is True,
        )


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def read_item_disposition(state: MonitorState, item_id: str) -> ThreadDisposition | None:
    """Return the persisted disposition for ``item_id`` (``None`` when absent)."""
    return ThreadDisposition.from_payload(
        state.threads_addressed_ids.get(_item_disposition_key(item_id))
    )


def record_item_disposition(state: MonitorState, disposition: ThreadDisposition) -> None:
    """Persist ``disposition`` under its item's reserved state key."""
    state.mark_addressed(
        _item_disposition_key(disposition.item_id),
        json.dumps(disposition.as_payload(), sort_keys=True),
    )


def sync_item_disposition(
    state: MonitorState,
    item_id: str,
    result: VerdictResult | MonitorVerdictResult,
    *,
    thread: ReviewThread | None = None,
) -> None:
    """Record (or clear) the disposition an item's latest verdict will publish.

    Called at verdict time, before the result is narrowed to a bare verdict, so
    the agent's FALSE POSITIVE / DEFER reason survives to the reply. FIXED cites
    the item's own #935 provenance commit; a hosted run, which has no local
    chain, cites the head the hosted agent pushed. A FIXED whose commit is not
    known yet stays pending until :func:`attach_item_commits` runs before push.
    Any other verdict clears the record so a stale disposition can never be
    published for a re-triaged item. A record whose reply already landed (a failed
    resolve or push re-queued the item) is kept: it is what the thread says, and
    the retried resolve reuses it instead of posting a second reply — unless the
    re-triaged ``thread``'s reviewer conversation changed since the verdict that
    reply answers (its recorded body hash), which the new verdict answers and so
    is owed its own reply, or a resolution already followed that reply (AWF's
    resolve landed, or an audit bound it): the thread was reopened since, which
    retires it.
    """
    from awf.runtime.pr_monitor_runner.helpers_verdict import _sanitize_verdict_reason

    posted = read_item_disposition(state, item_id)
    if posted is not None and posted.answers_resolution:
        retire_item_disposition(state, posted)
    elif (
        posted is not None
        and posted.reply_comment_id
        and not _feedback_follows_reply(thread, posted)
    ):
        return
    disposition_class = _VERDICT_CLASS.get(result.verdict)
    if disposition_class is None:
        state.threads_addressed_ids.pop(_item_disposition_key(item_id), None)
        return
    commits: tuple[str, ...] = ()
    commit_base: str | None = None
    if disposition_class == "fixed":
        record = _latest_chain_record(chain_from_state(state), item_id)
        if record is not None:
            commits, commit_base = (record.head_sha,), record.item_start_head
        elif state.hosted_terminal_head_advanced and state.last_push_sha:
            commits = (state.last_push_sha,)
    record_item_disposition(
        state,
        ThreadDisposition(
            item_id=item_id,
            disposition_class=disposition_class,
            verdict=result.verdict,
            reason=_sanitize_verdict_reason(result.reason),
            commits=commits,
            commit_base=commit_base,
            thread_body_hash=review_thread_body_hash(thread) if thread is not None else None,
        ),
    )


def retire_item_disposition(
    state: MonitorState, disposition: ThreadDisposition
) -> ThreadDisposition | None:
    """Retire ``disposition``: its thread reopened, or its conversation moved on.

    Its reply, posted or not, answers nothing current any more. A posted bound
    reply is remembered by the conversation it followed, so its marker never
    certifies again, whatever happens to the record. An operator
    acknowledgement is dropped: only a new ``ACK-THREAD`` for the current
    resolution replaces it. Any other record keeps its evidence but loses its
    reply id, binding and landed resolve, so the thread's next AWF resolve posts
    a fresh reply. Returns the record left in state.
    """
    if disposition.reply_comment_id and disposition.conversation_digest is not None:
        state.mark_addressed(
            _disposition_retired_key(disposition.item_id, disposition.conversation_digest),
            "retired",
        )
    if disposition.disposition_class == "operator_acknowledged":
        state.threads_addressed_ids.pop(_item_disposition_key(disposition.item_id), None)
        return None
    retired = replace(disposition, reply_comment_id=None, conversation_digest=None, resolved=False)
    record_item_disposition(state, retired)
    return retired


def retire_reopened_dispositions(
    state: MonitorState, status: PRStatus, *, workspace_id: str
) -> list[str]:
    """Retire every disposition a resolution followed whose thread ``status`` lists open.

    An audit saw a bound disposition's reply resolved, or AWF's own resolve on
    the reply landed, so a later poll listing its thread unresolved is a reopen,
    even if no gate audit runs before the thread is resolved again. Only a failed
    resolve's reply is kept, for its retry to reuse. Returns the retired item ids.
    """
    retired: list[str] = []
    for thread in (*status.unresolved_inline_threads, *status.outdated_unresolved_inline_threads):
        disposition = read_item_disposition(state, thread.thread_id)
        if disposition is not None and disposition.answers_resolution:
            retire_item_disposition(state, disposition)
            retired.append(thread.thread_id)
    if retired:
        _log.info(
            "monitor.thread_disposition_retired",
            workspace_id=workspace_id,
            pr_number=status.number,
            thread_ids=retired,
            reason_code=REVIEW_THREAD_DISPOSITION_UNVERIFIED,
        )
    return retired


def _feedback_follows_reply(thread: ReviewThread | None, posted: ThreadDisposition) -> bool:
    """True when ``thread`` changed since the feedback ``posted`` answers.

    Forges drop AWF's own comments from ``thread.comments``, so the reply itself
    is never visible there; the reviewer conversation's body hash, captured when
    the verdict was recorded, is the evidence instead. A record without that
    snapshot (or no re-triaged thread) proves no newer feedback.
    """
    if thread is None or posted.thread_body_hash is None:
        return False
    return not recorded_review_thread_body_matches(posted.thread_body_hash, thread)


def _latest_chain_record(
    chain: Sequence[ItemCommitProvenance], item_id: str
) -> ItemCommitProvenance | None:
    for record in reversed(chain):
        if record.item_id == item_id:
            return record
    return None


def attach_item_commits(state: MonitorState, chain: Sequence[ItemCommitProvenance]) -> None:
    """Fill every still-pending FIXED disposition from the commit-time chain.

    Runs after push and before the published chain is cleared, so a FIXED item
    whose HEAD probe was settled late still cites its own commit.
    """
    latest = {record.item_id: record for record in chain}  # later records win
    for item_id, record in latest.items():
        disposition = read_item_disposition(state, item_id)
        if disposition is None or disposition.disposition_class != "fixed" or disposition.commits:
            continue
        record_item_disposition(
            state,
            replace(disposition, commits=(record.head_sha,), commit_base=record.item_start_head),
        )


def attach_issue_url(state: MonitorState, item_id: str, issue_url: str) -> None:
    """Record the DEFER tracking issue on the item's disposition.

    A legacy / in-flight defer recorded before #1018 has no disposition yet; it
    is rebuilt from the agent's stashed defer reason so the reply can still cite
    it (a reasonless one then parks rather than resolving). A record whose reply
    already landed is kept as is: :func:`sync_item_disposition` kept it for a
    same-conversation re-triage, and it is what the thread says, so the resolve
    reuses it instead of posting a second reply.
    """
    from awf.runtime.pr_monitor_runner.helpers import _defer_reason_state_key
    from awf.runtime.pr_monitor_runner.helpers_verdict import _sanitize_verdict_reason

    disposition = read_item_disposition(state, item_id)
    if disposition is not None and disposition.reply_comment_id:
        return
    if disposition is None or disposition.disposition_class != "deferred":
        disposition = ThreadDisposition(
            item_id=item_id,
            disposition_class="deferred",
            verdict="defer",
            reason=_sanitize_verdict_reason(
                state.threads_addressed_ids.get(_defer_reason_state_key(item_id))
            ),
            source="legacy_defer",
        )
    record_item_disposition(state, replace(disposition, issue_url=issue_url))


def copy_item_disposition(state: MonitorState, *, from_id: str, thread: ReviewThread) -> None:
    """Copy a comment-keyed disposition onto the thread its verdict is promoted to.

    The comment-keyed record carries no conversation snapshot, so the copy binds
    ``thread``'s: feedback after its reply lands then supersedes it on re-triage.
    """
    source = read_item_disposition(state, from_id)
    if source is None:
        return
    record_item_disposition(
        state,
        replace(
            source,
            item_id=thread.thread_id,
            reply_comment_id=None,
            thread_body_hash=review_thread_body_hash(thread),
        ),
    )


def disposition_problem(disposition: ThreadDisposition | None, verdict: str | None) -> str | None:
    """Return the park reason code when ``disposition`` cannot answer ``verdict``.

    ``None`` means the reply can be posted. Missing means: no record, a record for
    a different verdict, FALSE POSITIVE / DEFER without a reason, FIXED without a
    commit, or DEFER without a tracking issue.
    """
    expected = _VERDICT_CLASS.get(verdict or "")
    missing = (
        THREAD_DISPOSITION_EVIDENCE_MISSING
        if verdict == "fix_committed"
        else THREAD_DISPOSITION_REASON_MISSING
    )
    if disposition is None or expected is None or disposition.verdict != verdict:
        return missing
    if expected == "fixed":
        if disposition.disposition_class not in ("fixed", "fixed_branch_evidence"):
            return missing
        return None if disposition.commits else THREAD_DISPOSITION_EVIDENCE_MISSING
    if disposition.disposition_class != expected:
        return missing
    if not disposition.reason:
        return THREAD_DISPOSITION_REASON_MISSING
    if expected == "deferred" and not disposition.issue_url:
        return THREAD_DISPOSITION_EVIDENCE_MISSING
    return None


def render_disposition_reply(disposition: ThreadDisposition) -> str:
    """Render the two-line, mention-free, marker-carrying disposition reply."""
    marker = _MARKER.format(cls=disposition.disposition_class, thread=disposition.item_id)
    reason = _redact_and_escape_markdown_inline(disposition.reason) if disposition.reason else ""
    commits = ", ".join(_redact_and_escape_markdown_inline(sha) for sha in disposition.commits)
    commit_noun = "commit" if len(disposition.commits) == 1 else "commits"
    label = _LABELS[disposition.disposition_class]
    if disposition.disposition_class == "fixed":
        line = f"**AWF disposition: {label}** in {commit_noun} {commits}." + (
            f" {reason}" if reason else ""
        )
    elif disposition.disposition_class == "fixed_branch_evidence":
        line = (
            f"**AWF disposition: {label}.** The branch already contains matching fix "
            f"{commit_noun} {commits}."
        )
    elif disposition.disposition_class == "deferred":
        line = (
            f"**AWF disposition: {label}.** Reason: {reason} "
            f"Tracked in {_safe_url(disposition.issue_url)}."
        )
    elif disposition.disposition_class == "operator_acknowledged":
        line = f"**AWF disposition: {label}.** {reason}"
    else:
        line = f"**AWF disposition: {label}.** Reason: {reason}"
    return f"{marker}\n{line}"


def _safe_url(url: str | None) -> str:
    """Publish only a plain https link with no mention or markup-breaking text."""
    candidate = redact_secrets(url or "").strip()
    if (
        not candidate.startswith("https://")
        or any(ch.isspace() for ch in candidate)
        or _UNSAFE_URL_CHARACTERS.intersection(candidate)
    ):
        return "(issue link unavailable)"
    return candidate.replace("@", "%40")


def disposition_marker_class(bodies: Sequence[str], *, thread_id: str) -> DispositionClass | None:
    """Return the class of the newest valid AWF marker naming ``thread_id``.

    ``bodies`` are oldest-first; a re-triage can post a second marker into the
    same thread, and the latest one is the disposition the resolution reflects.
    A marker naming another thread, or carrying an unknown class, is no evidence.
    Callers pass only comments AWF's own identity authored.
    """
    for body in reversed(bodies):
        for match in _MARKER_RE.finditer(body):
            if match.group("thread") == thread_id and match.group("cls") in DISPOSITION_CLASSES:
                return cast(DispositionClass, match.group("cls"))
    return None


def disposition_count_rows(counts: Mapping[str, int]) -> tuple[tuple[str, int], ...]:
    """Cert line-1 rows: every disposition class in order, then others' resolutions.

    The rollout's legacy baseline follows as its own row whenever it is non-zero.
    """
    keys = (*DISPOSITION_CLASSES, RESOLVED_BY_OTHERS)
    if counts.get(LEGACY_BASELINE):
        keys = (*keys, LEGACY_BASELINE)
    return tuple((_LABELS[key], int(counts.get(key, 0))) for key in keys)


def acknowledged_thread_ids(directive: str) -> tuple[str, ...]:
    """Thread ids a guide directive acknowledges with ``ACK-THREAD <thread id>``."""
    return tuple(
        dict.fromkeys(match.group("thread") for match in _ACK_THREAD_RE.finditer(directive))
    )


def unverified_flag(conversation_digest: str) -> str:
    """The unverified-flag value: the reason code bound to the audited conversation."""
    return f"{REVIEW_THREAD_DISPOSITION_UNVERIFIED}:{conversation_digest}"


def acknowledge_unverified_threads(
    state: MonitorState,
    thread_ids: Sequence[str],
    *,
    operation_id: str | None,
) -> tuple[str, ...]:
    """Record an operator acknowledgement for every *flagged* thread in ``thread_ids``.

    ``thread_ids`` are the guide's explicit ``ACK-THREAD`` tokens
    (:func:`acknowledged_thread_ids`). Only threads the disposition gate flagged
    can be acknowledged; acknowledging any other thread records nothing. Each
    acknowledgement is bound to the conversation its flag was raised for
    (:func:`unverified_flag`). The gate posts the acknowledgement reply (with its
    marker) into each thread on its next run, while that conversation stands.
    """
    reason = "An AWF operator guide acknowledged this resolution" + (
        f" (operation {operation_id})" if operation_id else ""
    )
    acknowledged: list[str] = []
    for thread_id in dict.fromkeys(thread_ids):
        flag_key = _disposition_unverified_key(thread_id)
        flag = state.threads_addressed_ids.get(flag_key)
        if not flag:
            continue
        record_item_disposition(
            state,
            ThreadDisposition(
                item_id=thread_id,
                disposition_class="operator_acknowledged",
                verdict=None,
                reason=reason,
                source="operator_guide",
                conversation_digest=flag.partition(":")[2],
            ),
        )
        state.threads_addressed_ids.pop(flag_key, None)
        acknowledged.append(thread_id)
    return tuple(acknowledged)


def unverified_blocker_message(thread_ids: Sequence[str]) -> str:
    """Reason-coded NotifyHuman text listing the unverified thread ids (bounded)."""
    head = (
        f"{REVIEW_THREAD_DISPOSITION_UNVERIFIED}: {len(thread_ids)} review thread(s) "
        "resolved by AWF's identity carry no AWF disposition reply: "
    )
    tail = "; acknowledge each with a workspace guide directive ACK-THREAD followed by its id"
    listed: list[str] = []
    for thread_id in thread_ids[:_BLOCKER_LISTED_IDS]:
        candidate = ", ".join([*listed, thread_id])
        if len(head) + len(candidate) + len(tail) + len(" (+NNNN more)") > _BLOCKER_MAX_CHARS:
            break
        listed.append(thread_id)
    remaining = len(thread_ids) - len(listed)
    more = f" (+{remaining} more)" if remaining else ""
    return f"{head}{', '.join(listed)}{more}{tail}"
