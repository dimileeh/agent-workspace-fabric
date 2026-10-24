"""Pure review-thread disposition model (#1018).

Every resolve AWF issues is preceded by one in-thread reply carrying a stable
machine marker; these tests pin the per-item record, the reply rendering (no
mention, no forgeable marker, one line, redacted), the marker parser and the
cert count rows.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from awf.runtime.feedback_policy import review_thread_body_hash, review_thread_body_state_key
from awf.runtime.monitor_state_keys import (
    _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY,
    _item_disposition_key,
)
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReviewThread,
    ReviewThreadComment,
)
from awf.runtime.pr_monitor_runner.comment_repair_provenance import (
    ItemCommitProvenance,
    encode_item_commit_provenance_chain,
)
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import (
    MonitorVerdictResult,
    VerdictResult,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _clear_addressed_state_by_id,
    _drop_stale_review_thread_addressed_state,
)
from awf.runtime.pr_monitor_runner.thread_disposition import (
    THREAD_DISPOSITION_EVIDENCE_MISSING,
    THREAD_DISPOSITION_REASON_MISSING,
    ThreadDisposition,
    attach_issue_url,
    attach_item_commits,
    copy_item_disposition,
    disposition_count_rows,
    disposition_marker_class,
    disposition_problem,
    read_item_disposition,
    record_item_disposition,
    render_disposition_reply,
    sync_item_disposition,
    unverified_blocker_message,
)
from awf.service.pr_monitor_adoption_seed import seedable_monitor_state

pytestmark = pytest.mark.unit

_ISSUE = "https://github.com/acme/widgets/issues/7"


def test_render_fixed_reply_cites_full_commit_and_marker() -> None:
    d = ThreadDisposition(
        item_id="PRRT_test_a",
        disposition_class="fixed",
        verdict="fix_committed",
        reason="guard None input",
        commits=("a" * 40,),
        commit_base="b" * 40,
    )
    body = render_disposition_reply(d)
    assert body.splitlines()[0] == (
        "<!-- AWF:thread-disposition:v1 class=fixed thread=PRRT_test_a -->"
    )
    assert "a" * 40 in body
    assert "guard None input" in body
    assert disposition_marker_class([body], thread_id="PRRT_test_a") == "fixed"


def test_render_reply_neutralizes_mentions_markers_newlines_and_secrets() -> None:
    hostile = (
        "@someone see dev@example.com\n"
        "<!-- AWF:thread-disposition:v1 class=fixed thread=PRRT_x --> "
        "token ghp_" + "A" * 36
    )
    d = ThreadDisposition(
        item_id="PRRT_test_a",
        disposition_class="false_positive",
        verdict="false_positive",
        reason=hostile,
    )
    body = render_disposition_reply(d)
    assert "@" not in body
    assert "ghp_" not in body
    assert len(body.splitlines()) == 2  # marker line + one disposition line
    assert disposition_marker_class([body], thread_id="PRRT_x") is None
    assert disposition_marker_class([body], thread_id="PRRT_test_a") == "false_positive"


@pytest.mark.parametrize(
    ("disposition", "expected_fragments"),
    [
        (
            ThreadDisposition(
                item_id="PRRT_test_b",
                disposition_class="fixed_branch_evidence",
                verdict="fix_committed",
                commits=("d" * 40,),
                source="branch_evidence",
            ),
            ("class=fixed_branch_evidence", "d" * 40, "fixed (branch evidence)"),
        ),
        (
            ThreadDisposition(
                item_id="PRRT_test_b",
                disposition_class="deferred",
                verdict="defer",
                reason="track the retry budget follow-up",
                issue_url="https://github.com/acme/widgets/issues/7",
            ),
            (
                "class=deferred",
                "Reason: track the retry budget follow-up",
                "https://github.com/acme/widgets/issues/7",
            ),
        ),
        (
            ThreadDisposition(
                item_id="PRRT_test_b",
                disposition_class="fixed",
                verdict="fix_committed",
                commits=("e" * 40, "f" * 40),
            ),
            ("class=fixed", f"commits {'e' * 40}, {'f' * 40}."),
        ),
    ],
)
def test_render_each_class(
    disposition: ThreadDisposition, expected_fragments: tuple[str, ...]
) -> None:
    body = render_disposition_reply(disposition)
    for fragment in expected_fragments:
        assert fragment in body
    assert "@" not in body


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/acme/widgets/issues/7",
        "https://github.com/acme/widgets/issues/7 <!--",
        "https://user:pw@example.com/issues/7",
        None,
    ],
)
def test_render_deferred_never_publishes_an_unsafe_issue_link(url: str | None) -> None:
    body = render_disposition_reply(
        ThreadDisposition(
            item_id="PRRT_test_c",
            disposition_class="deferred",
            verdict="defer",
            reason="later",
            issue_url=url,
        )
    )
    assert "(issue link unavailable)" in body or "%40" in body
    assert "@" not in body
    assert "pw" not in body


def test_marker_class_requires_matching_thread_id() -> None:
    other = "<!-- AWF:thread-disposition:v1 class=fixed thread=PRRT_other -->"
    assert disposition_marker_class([other], thread_id="PRRT_test_a") is None
    assert (
        disposition_marker_class(
            ["<!-- AWF:thread-disposition:v1 class=bogus thread=PRRT_test_a -->"],
            thread_id="PRRT_test_a",
        )
        is None
    )
    assert disposition_marker_class([], thread_id="PRRT_test_a") is None
    assert (
        disposition_marker_class(
            [other, "<!-- AWF:thread-disposition:v1 class=deferred thread=PRRT_test_a -->"],
            thread_id="PRRT_test_a",
        )
        == "deferred"
    )


def test_marker_class_prefers_the_newest_marker_in_the_thread() -> None:
    # Bodies arrive oldest-first; a re-triage after fresh feedback posts a second
    # marker, and the resolution the gate certifies is the latest disposition.
    stale = "<!-- AWF:thread-disposition:v1 class=false_positive thread=PRRT_test_a -->"
    final = "<!-- AWF:thread-disposition:v1 class=fixed thread=PRRT_test_a -->"
    other = "<!-- AWF:thread-disposition:v1 class=deferred thread=PRRT_other -->"
    assert disposition_marker_class([stale, final, other], thread_id="PRRT_test_a") == "fixed"


def _fp(reason: str | None = "reason") -> ThreadDisposition:
    return ThreadDisposition(
        item_id="t", disposition_class="false_positive", verdict="false_positive", reason=reason
    )


def _fixed(commits: tuple[str, ...] = ("c" * 40,)) -> ThreadDisposition:
    return ThreadDisposition(
        item_id="t", disposition_class="fixed", verdict="fix_committed", commits=commits
    )


def _deferred(reason: str | None = "r", issue_url: str | None = _ISSUE) -> ThreadDisposition:
    return ThreadDisposition(
        item_id="t",
        disposition_class="deferred",
        verdict="defer",
        reason=reason,
        issue_url=issue_url,
    )


@pytest.mark.parametrize(
    ("disposition", "verdict", "expected"),
    [
        (None, "false_positive", THREAD_DISPOSITION_REASON_MISSING),
        (None, "defer", THREAD_DISPOSITION_REASON_MISSING),
        (None, "fix_committed", THREAD_DISPOSITION_EVIDENCE_MISSING),
        (None, None, THREAD_DISPOSITION_REASON_MISSING),
        (_fixed(commits=()), "fix_committed", THREAD_DISPOSITION_EVIDENCE_MISSING),
        (_deferred(issue_url=None), "defer", THREAD_DISPOSITION_EVIDENCE_MISSING),
        (_deferred(reason=None), "defer", THREAD_DISPOSITION_REASON_MISSING),
        (_fp(reason=None), "false_positive", THREAD_DISPOSITION_REASON_MISSING),
        (_fp(), "fix_committed", THREAD_DISPOSITION_EVIDENCE_MISSING),
        (_fixed(), "false_positive", THREAD_DISPOSITION_REASON_MISSING),
        (_fp(), "needs_human", THREAD_DISPOSITION_REASON_MISSING),
        (
            ThreadDisposition(
                item_id="t",
                disposition_class="false_positive",
                verdict="fix_committed",
                commits=("c" * 40,),
            ),
            "fix_committed",
            THREAD_DISPOSITION_EVIDENCE_MISSING,
        ),
        (
            ThreadDisposition(
                item_id="t", disposition_class="deferred", verdict="false_positive", reason="r"
            ),
            "false_positive",
            THREAD_DISPOSITION_REASON_MISSING,
        ),
        (_fixed(), "fix_committed", None),
        (_fp(), "false_positive", None),
        (_deferred(), "defer", None),
        (
            ThreadDisposition(
                item_id="t",
                disposition_class="fixed_branch_evidence",
                verdict="fix_committed",
                commits=("d" * 40,),
            ),
            "fix_committed",
            None,
        ),
    ],
)
def test_disposition_problem_matrix(
    disposition: ThreadDisposition | None, verdict: str | None, expected: str | None
) -> None:
    assert disposition_problem(disposition, verdict) == expected


def _seed_chain(state: MonitorState, *records: ItemCommitProvenance) -> None:
    state.mark_addressed(
        _COMMENT_REPAIR_ITEM_PROVENANCE_STATE_KEY, encode_item_commit_provenance_chain(records)
    )


def test_sync_item_disposition_uses_latest_chain_record_and_hosted_fallback() -> None:
    state = MonitorState()
    _seed_chain(
        state,
        ItemCommitProvenance(item_id="PRRT_test_a", item_start_head="0" * 40, head_sha="1" * 40),
        ItemCommitProvenance(
            item_id="PRRT_test_other", item_start_head="1" * 40, head_sha="2" * 40
        ),
        ItemCommitProvenance(item_id="PRRT_test_a", item_start_head="2" * 40, head_sha="3" * 40),
    )
    sync_item_disposition(
        state, "PRRT_test_a", VerdictResult(verdict="fix_committed", reason="guarded")
    )
    fixed = read_item_disposition(state, "PRRT_test_a")
    assert fixed is not None
    assert fixed.disposition_class == "fixed"
    assert fixed.commits == ("3" * 40,)
    assert fixed.commit_base == "2" * 40
    assert fixed.reason == "guarded"

    hosted = MonitorState()
    hosted.last_push_sha = "9" * 40
    hosted.hosted_terminal_head_advanced = True
    sync_item_disposition(hosted, "PRRT_test_h", VerdictResult(verdict="fix_committed", reason="x"))
    hosted_record = read_item_disposition(hosted, "PRRT_test_h")
    assert hosted_record is not None
    assert hosted_record.commits == ("9" * 40,)

    pending = MonitorState()
    pending.last_push_sha = "9" * 40  # not a hosted advance: no evidence yet
    sync_item_disposition(
        pending, "PRRT_test_p", VerdictResult(verdict="fix_committed", reason="x")
    )
    pending_record = read_item_disposition(pending, "PRRT_test_p")
    assert pending_record is not None
    assert pending_record.commits == ()

    sync_item_disposition(
        state, "PRRT_test_other", VerdictResult(verdict="fix_committed", reason="other")
    )
    other = read_item_disposition(state, "PRRT_test_other")
    assert other is not None
    assert other.commits == ("2" * 40,)  # not the chain tip: older record found

    sync_item_disposition(
        state,
        "PRRT_test_fp",
        VerdictResult(verdict="false_positive", reason="the guard already rejects None"),
    )
    fp = read_item_disposition(state, "PRRT_test_fp")
    assert fp is not None
    assert fp.disposition_class == "false_positive"
    assert fp.reason == "the guard already rejects None"
    assert fp.commits == ()

    sync_item_disposition(state, "PRRT_test_fp", MonitorVerdictResult(verdict="agent_failed"))
    assert _item_disposition_key("PRRT_test_fp") not in state.threads_addressed_ids
    sync_item_disposition(state, "PRRT_test_a", VerdictResult(verdict="needs_human", reason="?"))
    assert read_item_disposition(state, "PRRT_test_a") is None


def test_attach_item_commits_fills_only_pending_fixed() -> None:
    state = MonitorState()
    record_item_disposition(state, _fixed(commits=()))
    record_item_disposition(
        state,
        ThreadDisposition(
            item_id="PRRT_test_done",
            disposition_class="fixed",
            verdict="fix_committed",
            commits=("a" * 40,),
        ),
    )
    record_item_disposition(
        state,
        ThreadDisposition(
            item_id="PRRT_test_fp", disposition_class="false_positive", verdict="false_positive"
        ),
    )
    chain = (
        ItemCommitProvenance(item_id="t", item_start_head="0" * 40, head_sha="1" * 40),
        ItemCommitProvenance(item_id="PRRT_test_done", item_start_head="1" * 40, head_sha="2" * 40),
        ItemCommitProvenance(item_id="PRRT_test_fp", item_start_head="2" * 40, head_sha="3" * 40),
        ItemCommitProvenance(item_id="PRRT_test_none", item_start_head="3" * 40, head_sha="4" * 40),
    )
    attach_item_commits(state, chain)
    filled = read_item_disposition(state, "t")
    assert filled is not None and filled.commits == ("1" * 40,)
    assert filled.commit_base == "0" * 40
    done = read_item_disposition(state, "PRRT_test_done")
    assert done is not None and done.commits == ("a" * 40,)
    fp = read_item_disposition(state, "PRRT_test_fp")
    assert fp is not None and fp.commits == ()
    assert read_item_disposition(state, "PRRT_test_none") is None


def test_attach_issue_url_creates_deferred_record_from_defer_reason_stash() -> None:
    state = MonitorState()
    state.mark_addressed("__defer_reason__:PRRT_test_d", "follow up on retries")
    attach_issue_url(state, "PRRT_test_d", _ISSUE)
    created = read_item_disposition(state, "PRRT_test_d")
    assert created is not None
    assert created.disposition_class == "deferred"
    assert created.verdict == "defer"
    assert created.reason == "follow up on retries"
    assert created.issue_url == _ISSUE

    record_item_disposition(state, _deferred(issue_url=None))
    attach_issue_url(state, "t", _ISSUE)
    existing = read_item_disposition(state, "t")
    assert existing is not None
    assert existing.reason == "r"
    assert existing.issue_url == _ISSUE


def test_copy_item_disposition_rekeys_item_id() -> None:
    state = MonitorState()
    record_item_disposition(
        state,
        ThreadDisposition(
            item_id="4688598838",
            disposition_class="false_positive",
            verdict="false_positive",
            reason="already guarded",
            reply_comment_id="stale",
        ),
    )
    thread = replace(_thread_with("4688598838"), thread_id="PRRT_test_t")
    copy_item_disposition(state, from_id="4688598838", thread=thread)
    copied = read_item_disposition(state, "PRRT_test_t")
    assert copied is not None
    assert copied.item_id == "PRRT_test_t"
    assert copied.reason == "already guarded"
    assert copied.reply_comment_id is None
    # The comment-keyed source has no conversation snapshot; the copy binds the
    # thread's, so feedback after its reply is posted supersedes it.
    assert copied.thread_body_hash == review_thread_body_hash(thread)
    copy_item_disposition(state, from_id="missing", thread=replace(thread, thread_id="PRRT_test_u"))
    assert read_item_disposition(state, "PRRT_test_u") is None


def test_payload_roundtrip_and_malformed_payload_fails_closed() -> None:
    d = ThreadDisposition(
        item_id="PRRT_test_a",
        disposition_class="deferred",
        verdict="defer",
        reason="r",
        commits=("a" * 40,),
        commit_base="b" * 40,
        issue_url=_ISSUE,
        source="agent_verdict",
        reply_comment_id="991",
        thread_body_hash="f" * 64,
        conversation_digest="e" * 64,
        resolved=True,
    )
    payload = d.as_payload()
    assert payload["v"] == 1
    assert payload["class"] == "deferred"
    assert ThreadDisposition.from_payload(payload) == d
    assert ThreadDisposition.from_payload(json.dumps(payload)) == d
    state = MonitorState()
    record_item_disposition(state, d)
    assert json.loads(state.threads_addressed_ids[_item_disposition_key("PRRT_test_a")]) == payload
    assert read_item_disposition(state, "PRRT_test_a") == d

    assert ThreadDisposition.from_payload(None) is None
    assert ThreadDisposition.from_payload("not json") is None
    assert ThreadDisposition.from_payload("[1, 2]") is None
    assert ThreadDisposition.from_payload({**payload, "class": "bogus"}) is None
    assert ThreadDisposition.from_payload({**payload, "item_id": " "}) is None
    lenient = ThreadDisposition.from_payload(
        {**payload, "commits": "not-a-list", "reason": 5, "source": None}
    )
    assert lenient is not None
    assert lenient.commits == ()
    assert lenient.reason is None
    assert lenient.source == "agent_verdict"
    # Only an explicit ``true`` records a landed resolve; anything else fails closed
    # to "not resolved", whose reply a resolve retry may reuse.
    for raw in ("yes", 1, None):
        decoded = ThreadDisposition.from_payload({**payload, "resolved": raw})
        assert decoded is not None and decoded.resolved is False


def test_disposition_count_rows_orders_all_classes_and_others() -> None:
    rows = disposition_count_rows({"fixed": 2, "resolved_by_others": 3, "deferred": 1})
    assert rows == (
        ("fixed", 2),
        ("fixed (branch evidence)", 0),
        ("false positive", 0),
        ("deferred", 1),
        ("resolved by others", 3),
    )


def test_unverified_blocker_message_lists_ten_ids_and_counts_rest() -> None:
    ids = [f"PRRT_test_long_{i:010d}" for i in range(12)]
    message = unverified_blocker_message(ids)
    assert message.startswith("REVIEW_THREAD_DISPOSITION_UNVERIFIED:")
    # Clearing one is a reopen, which AWF re-triages before resolving it afresh.
    assert message.endswith(
        "; to clear one, reopen it: AWF re-triages it through the normal fix cycle and "
        "posts a fresh disposition before resolving it"
    )
    assert "ACK-THREAD" not in message
    assert len(message) <= 560
    for tid in ids[:10]:
        assert tid in message
    assert ids[10] not in message
    assert "+2 more" in message
    single = unverified_blocker_message(["PRRT_test_one"])
    assert "PRRT_test_one" in single
    assert "more" not in single
    # Forge ids are opaque: very long ones stop listing early but keep the bound.
    long_ids = [f"PRRT_{'x' * 60}{i}" for i in range(6)]
    bounded = unverified_blocker_message(long_ids)
    assert len(bounded) <= 560
    assert long_ids[0] in bounded
    assert long_ids[5] not in bounded
    assert "more)" in bounded


def test_clear_addressed_state_by_id_drops_item_disposition() -> None:
    state = MonitorState()
    state.mark_addressed("PRRT_test_a", "false_positive")
    record_item_disposition(state, replace(_fp(), item_id="PRRT_test_a"))
    _clear_addressed_state_by_id(state, "PRRT_test_a")
    assert _item_disposition_key("PRRT_test_a") not in state.threads_addressed_ids
    assert "PRRT_test_a" not in state.threads_addressed_ids


def test_clear_addressed_state_by_id_keeps_posted_reply_unless_superseded() -> None:
    state = MonitorState()
    posted = replace(_fp(), item_id="PRRT_test_a", reply_comment_id="reply-1")
    record_item_disposition(state, posted)
    record_item_disposition(state, replace(_fp(), item_id="PRRT_test_b"))
    # A rollback (failed resolve, failed push, re-queue) keeps the reply id: the
    # reply is already in the thread, so a retry must reuse it, never re-post.
    _clear_addressed_state_by_id(state, "PRRT_test_a")
    _clear_addressed_state_by_id(state, "PRRT_test_b")
    assert read_item_disposition(state, "PRRT_test_a") == posted
    # Nothing was posted for b, so there is no reply to keep.
    assert _item_disposition_key("PRRT_test_b") not in state.threads_addressed_ids
    # Fresh reviewer feedback supersedes it: the new feedback gets its own reply.
    _clear_addressed_state_by_id(state, "PRRT_test_a", keep_posted_reply=False)
    assert _item_disposition_key("PRRT_test_a") not in state.threads_addressed_ids


def _status_with(thread: ReviewThread) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha="a" * 40,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(thread,),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )


def test_stale_thread_body_drops_the_posted_reply_for_fresh_feedback() -> None:
    state = MonitorState()
    thread = ReviewThread(
        thread_id="PRRT_test_a",
        path="widgets/core.py",
        line=12,
        body_excerpt="please guard None payloads -- and the empty list too",
        author="review-bot",
    )
    state.mark_addressed("PRRT_test_a", "false_positive")
    state.mark_addressed(review_thread_body_state_key("PRRT_test_a"), "0" * 64)
    record_item_disposition(state, replace(_fp(), item_id="PRRT_test_a", reply_comment_id="r-1"))

    assert _drop_stale_review_thread_addressed_state(_status_with(thread), state) is True

    assert read_item_disposition(state, "PRRT_test_a") is None


@pytest.mark.parametrize(
    "result",
    [
        VerdictResult(verdict="false_positive", reason="re-run wording"),
        # The #935 chain was cleared by the push that published the cited commit.
        VerdictResult(verdict="fix_committed", reason="guarded"),
        VerdictResult(verdict="defer", reason="later"),
        VerdictResult(verdict="needs_human", reason="ask a human"),
    ],
    ids=["same-class", "fixed-without-chain", "class-change", "non-resolvable"],
)
def test_sync_item_disposition_never_replaces_a_posted_reply(
    result: VerdictResult,
) -> None:
    state = MonitorState()
    posted = replace(
        _fixed(("1" * 40,)) if result.verdict == "fix_committed" else _fp(),
        item_id="PRRT_test_a",
        reply_comment_id="reply-1",
    )
    record_item_disposition(state, posted)

    sync_item_disposition(state, "PRRT_test_a", result)

    # The published record stays the disposition, so a re-queued resolve reuses the
    # reply already in the thread and never posts a second one.
    assert read_item_disposition(state, "PRRT_test_a") == posted


def _thread_with(*comment_ids: str) -> ReviewThread:
    return ReviewThread(
        thread_id="PRRT_test_a",
        path="widgets/core.py",
        line=12,
        body_excerpt="please guard None payloads",
        author="review-bot",
        comments=tuple(
            ReviewThreadComment(comment_id=comment_id, body=f"comment {comment_id}")
            for comment_id in comment_ids
        ),
    )


def _posted_answering(thread: ReviewThread | None) -> ThreadDisposition:
    return replace(
        _fp(),
        item_id="PRRT_test_a",
        reply_comment_id="reply-1",
        thread_body_hash=review_thread_body_hash(thread) if thread is not None else None,
    )


@pytest.mark.parametrize(
    ("answered", "retriaged"),
    [
        # Forges drop AWF's own reply from ``thread.comments``, so an unchanged
        # reviewer conversation is all the re-triage sees.
        (_thread_with("c-1"), _thread_with("c-1")),
        # A record posted before the body snapshot existed proves no newer feedback.
        (None, _thread_with("c-1", "c-3")),
    ],
    ids=["conversation-unchanged", "no-body-snapshot"],
)
def test_sync_item_disposition_keeps_a_posted_reply_no_feedback_follows(
    answered: ReviewThread | None, retriaged: ReviewThread
) -> None:
    state = MonitorState()
    posted = _posted_answering(answered)
    record_item_disposition(state, posted)

    sync_item_disposition(
        state, "PRRT_test_a", VerdictResult(verdict="defer", reason="later"), thread=retriaged
    )

    assert read_item_disposition(state, "PRRT_test_a") == posted


@pytest.mark.parametrize(
    "result",
    [
        VerdictResult(verdict="false_positive", reason="not reachable"),
        VerdictResult(verdict="defer", reason="later"),
    ],
    ids=["same-class", "class-change"],
)
def test_sync_item_disposition_answers_afresh_after_adoption_onto_a_moved_head(
    result: VerdictResult,
) -> None:
    # PRRT_kwDOSJAM6s6vc3Z0: a predecessor's unconfirmed reply answers the code at a
    # head the adopted one may have reverted. Its verdict drops for re-triage, and
    # the unchanged thread's new verdict is owed its own reply, never that one.
    thread = _thread_with("c-1")
    predecessor = MonitorState()
    predecessor.mark_addressed("PRRT_test_a", "false_positive")
    record_item_disposition(predecessor, replace(_posted_answering(thread), reread_pending=True))
    adopted = MonitorState()
    for key, value in seedable_monitor_state(
        predecessor.threads_addressed_ids, head_continuity=False
    ).items():
        adopted.mark_addressed(key, value)

    sync_item_disposition(adopted, "PRRT_test_a", result, thread=thread)

    fresh = read_item_disposition(adopted, "PRRT_test_a")
    assert fresh is not None
    assert (fresh.verdict, fresh.reason, fresh.reply_comment_id, fresh.reread_pending) == (
        result.verdict,
        result.reason,
        None,
        False,
    )


@pytest.mark.parametrize(
    "bound",
    [
        replace(_posted_answering(_thread_with("c-1")), conversation_digest="seen-resolved"),
        replace(_posted_answering(_thread_with("c-1")), resolved=True),
    ],
    ids=["agent-disposition", "resolve-landed"],
)
def test_sync_item_disposition_retires_a_reply_a_gate_audit_saw_resolved(
    bound: ThreadDisposition,
) -> None:
    # A resolution followed the record's reply: an audit saw the thread resolved
    # after it, or AWF's own resolve on it landed (PRRT_kwDOSJAM6s6vZ4nJ). A
    # re-triage means the thread was reopened since, which retires that reply,
    # so the new verdict is owed its own instead of a silent re-resolve.
    state = MonitorState()
    record_item_disposition(state, bound)

    sync_item_disposition(
        state,
        "PRRT_test_a",
        VerdictResult(verdict="false_positive", reason="not reachable"),
        thread=_thread_with("c-1"),
    )

    disposition = read_item_disposition(state, "PRRT_test_a")
    assert disposition is not None
    assert (
        disposition.disposition_class,
        disposition.reason,
        disposition.reply_comment_id,
        disposition.conversation_digest,
        disposition.resolved,
    ) == ("false_positive", "not reachable", None, None, False)


def test_sync_item_disposition_replaces_a_posted_reply_superseded_by_new_feedback() -> None:
    # A failed resolve cleared the verdict (so the stale-body drop skipped the
    # thread), then a reviewer replied after AWF's FALSE POSITIVE reply. The
    # re-triaged verdict answers that reply and is owed its own disposition. AWF's
    # reply is absent from the fetched thread, exactly as the forges return it.
    state = MonitorState()
    _seed_chain(
        state,
        ItemCommitProvenance(item_id="PRRT_test_a", item_start_head="0" * 40, head_sha="2" * 40),
    )
    record_item_disposition(state, _posted_answering(_thread_with("c-1")))
    thread = _thread_with("c-1", "c-3")

    sync_item_disposition(
        state,
        "PRRT_test_a",
        VerdictResult(verdict="fix_committed", reason="guarded"),
        thread=thread,
    )

    fixed = read_item_disposition(state, "PRRT_test_a")
    assert fixed is not None
    assert (
        fixed.disposition_class,
        fixed.commits,
        fixed.reply_comment_id,
        fixed.thread_body_hash,
    ) == ("fixed", ("2" * 40,), None, review_thread_body_hash(thread))
    # A re-triage that leaves the thread open drops the superseded record too.
    record_item_disposition(state, _posted_answering(_thread_with("c-1")))
    sync_item_disposition(
        state,
        "PRRT_test_a",
        VerdictResult(verdict="needs_human", reason="ask a human"),
        thread=thread,
    )
    assert _item_disposition_key("PRRT_test_a") not in state.threads_addressed_ids


def test_sync_item_disposition_replaces_an_unposted_record() -> None:
    state = MonitorState()
    _seed_chain(
        state,
        ItemCommitProvenance(item_id="PRRT_test_f", item_start_head="0" * 40, head_sha="2" * 40),
    )
    record_item_disposition(state, replace(_fixed(("1" * 40,)), item_id="PRRT_test_f"))
    sync_item_disposition(
        state, "PRRT_test_f", VerdictResult(verdict="fix_committed", reason="guarded")
    )
    fixed = read_item_disposition(state, "PRRT_test_f")
    assert fixed is not None
    assert fixed.commits == ("2" * 40,) and fixed.reply_comment_id is None
