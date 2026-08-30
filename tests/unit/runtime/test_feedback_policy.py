"""Unit tests for state-independent PR feedback policy helpers."""

from __future__ import annotations

import pytest

from awf.runtime.feedback_policy import (
    CLOSED_OUTDATED_THREAD_VERDICTS,
    canonical_unresolved_inline_threads,
    needs_comment_attention,
    outdated_thread_has_fresh_feedback,
    prefer_duplicate_review_thread,
    preferred_duplicate_review_thread,
    review_thread_body_hash,
    review_thread_body_state_key,
    thread_enters_address_comments,
    thread_needs_attention,
    unresolved_active_count,
    unresolved_canonical_count,
    unresolved_outdated_unique_count,
    unresolved_thread_counts,
)
from awf.runtime.pr_monitor_models import ReviewThread


def _thread(
    tid: str,
    *,
    body: str = "fix this",
    is_outdated: bool = False,
) -> ReviewThread:
    return ReviewThread(
        thread_id=tid,
        path="src/x.py",
        line=10,
        body_excerpt=body,
        author=None,
        is_outdated=is_outdated,
    )


@pytest.mark.unit
def test_needs_comment_attention_taxonomy() -> None:
    assert needs_comment_attention(None) is True
    assert needs_comment_attention("agent_failed") is True
    assert needs_comment_attention("fix_committed") is False
    assert needs_comment_attention("false_positive") is False
    assert needs_comment_attention("defer") is False
    assert needs_comment_attention("needs_human") is False


@pytest.mark.unit
def test_closed_outdated_verdicts() -> None:
    assert frozenset({"false_positive", "fix_committed"}) == CLOSED_OUTDATED_THREAD_VERDICTS


@pytest.mark.unit
def test_body_hash_stable_for_same_conversation() -> None:
    a = _thread("T1", body="nit")
    b = _thread("T1", body="nit")
    assert review_thread_body_hash(a) == review_thread_body_hash(b)
    assert review_thread_body_state_key("T1") == "__review_thread_body_hash__:T1"


@pytest.mark.unit
def test_body_hash_changes_when_conversation_changes() -> None:
    original = _thread("T1", body="nit")
    replied = _thread("T1", body="still broken")
    assert review_thread_body_hash(original) != review_thread_body_hash(replied)


@pytest.mark.unit
def test_body_hash_stable_across_fallback_and_populated_one_comment() -> None:
    """Fallback (no comments) and populated one-comment forms must share a hash.

    Representation-specific ``comment_id`` / ``created_at`` must not flip the
    body hash when the conversation content is the same — otherwise a thread
    closed under the fallback form requeues when the forge later returns the
    same one-comment conversation populated (PRRT_kwDOSJAM6s6dcFNZ).
    """
    from datetime import UTC, datetime

    from awf.runtime.pr_monitor_models import ReviewThreadComment

    fallback = ReviewThread(
        thread_id="T1",
        path="src/x.py",
        line=10,
        body_excerpt="please fix this",
        author="reviewer",
    )
    populated = ReviewThread(
        thread_id="T1",
        path="src/x.py",
        line=10,
        body_excerpt="please fix this",
        author="reviewer",
        comments=(
            ReviewThreadComment(
                comment_id="C1",
                body="please fix this",
                author="reviewer",
                created_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
            ),
        ),
    )
    assert review_thread_body_hash(fallback) == review_thread_body_hash(populated)


@pytest.mark.unit
def test_recorded_body_hash_accepts_pre_normalize_legacy_hash() -> None:
    """Persisted pre-normalize hashes must still match an unchanged conversation.

    Parent monitors stored ``comment_id`` / ``created_at`` in the payload; the
    content-only serializer would otherwise requeue every addressed thread on
    resume (PRRT_kwDOSJAM6s6dfH8h).
    """
    import hashlib
    import json
    from datetime import UTC, datetime

    from awf.runtime.feedback_policy import (
        recorded_review_thread_body_matches,
        thread_enters_address_comments,
    )
    from awf.runtime.pr_monitor_models import ReviewThreadComment

    thread = ReviewThread(
        thread_id="T1",
        path="src/x.py",
        line=10,
        body_excerpt="please fix this",
        author="reviewer",
        comments=(
            ReviewThreadComment(
                comment_id="C1",
                body="please fix this",
                author="reviewer",
                created_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
            ),
        ),
    )
    legacy_payload = [
        {
            "author": "reviewer",
            "body": "please fix this",
            "comment_id": "C1",
            "created_at": "2026-01-15T12:00:00+00:00",
        }
    ]
    legacy_hash = hashlib.sha256(
        json.dumps(legacy_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert legacy_hash != review_thread_body_hash(thread)
    assert recorded_review_thread_body_matches(legacy_hash, thread) is True
    assert recorded_review_thread_body_matches(review_thread_body_hash(thread), thread) is True
    assert recorded_review_thread_body_matches("deadbeef", thread) is False
    assert recorded_review_thread_body_matches(None, thread) is False

    state = {
        "T1": "fix_committed",
        review_thread_body_state_key("T1"): legacy_hash,
    }
    assert thread_needs_attention(state, thread) is False
    assert thread_enters_address_comments(state, thread) is False

    changed = ReviewThread(
        thread_id="T1",
        path="src/x.py",
        line=10,
        body_excerpt="please fix this — updated",
        author="reviewer",
        comments=(
            ReviewThreadComment(
                comment_id="C1",
                body="please fix this",
                author="reviewer",
                created_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
            ),
            ReviewThreadComment(
                comment_id="C2",
                body="still broken",
                author="reviewer",
                created_at=datetime(2026, 1, 16, 12, 0, tzinfo=UTC),
            ),
        ),
    )
    assert recorded_review_thread_body_matches(legacy_hash, changed) is False
    assert thread_enters_address_comments(state, changed) is True


@pytest.mark.unit
def test_thread_needs_attention_missing_and_agent_failed() -> None:
    thread = _thread("T1")
    assert thread_needs_attention({}, thread) is True
    assert thread_needs_attention({"T1": "agent_failed"}, thread) is True
    assert thread_needs_attention({"T1": "fix_committed"}, thread) is True  # missing hash


@pytest.mark.unit
def test_thread_needs_attention_closed_matching_hash() -> None:
    thread = _thread("T1", body="nit")
    state = {
        "T1": "fix_committed",
        review_thread_body_state_key("T1"): review_thread_body_hash(thread),
    }
    assert thread_needs_attention(state, thread) is False


@pytest.mark.unit
def test_thread_needs_attention_closed_changed_body() -> None:
    original = _thread("T1", body="nit")
    replied = _thread("T1", body="new reply")
    state = {
        "T1": "false_positive",
        review_thread_body_state_key("T1"): review_thread_body_hash(original),
    }
    assert thread_needs_attention(state, replied) is True


@pytest.mark.unit
def test_outdated_fresh_feedback_only_for_closed_verdicts() -> None:
    original = _thread("T1", body="nit", is_outdated=True)
    replied = _thread("T1", body="reply", is_outdated=True)
    closed_state = {
        "T1": "fix_committed",
        review_thread_body_state_key("T1"): review_thread_body_hash(original),
    }
    assert outdated_thread_has_fresh_feedback(closed_state, replied) is True
    assert outdated_thread_has_fresh_feedback({"T1": "needs_human"}, replied) is False
    assert outdated_thread_has_fresh_feedback({}, replied) is False


@pytest.mark.unit
def test_thread_enters_address_comments_skips_unchanged_needs_human() -> None:
    """Unchanged needs_human (no snapshot, or matching hash) stays off AddressComments."""
    thread = _thread("T1", body="nit")
    assert thread_enters_address_comments({"T1": "needs_human"}, thread) is False
    matching = {
        "T1": "needs_human",
        review_thread_body_state_key("T1"): review_thread_body_hash(thread),
    }
    assert thread_enters_address_comments(matching, thread) is False
    assert thread_enters_address_comments({}, thread) is True


@pytest.mark.unit
def test_thread_enters_address_comments_unknown_disposition_does_not_requeue() -> None:
    """A recorded disposition outside the body-change requeue set stays off AddressComments.

    Unknown/legacy verdicts are neither ``needs_comment_attention`` nor in the
    closed/defer/needs_human requeue set — body-hash mismatch must not invent a
    repair batch for them.
    """
    thread = _thread("T1", body="nit")
    state = {
        "T1": "acknowledged",
        review_thread_body_state_key("T1"): "stale-hash-that-will-not-match",
    }
    assert needs_comment_attention("acknowledged") is False
    assert thread_enters_address_comments(state, thread) is False
    assert thread_enters_address_comments({"T1": "acknowledged"}, thread) is False


@pytest.mark.unit
def test_thread_enters_address_comments_requeues_needs_human_on_body_change() -> None:
    """Reviewer reply after needs_human must re-enter AddressComments (not strand NotifyHuman)."""
    from awf.runtime.feedback_policy import thread_enters_address_comments

    original = _thread("T1", body="ambiguous ask")
    replied = _thread("T1", body="clarifying: please do X")
    state = {
        "T1": "needs_human",
        review_thread_body_state_key("T1"): review_thread_body_hash(original),
    }
    assert thread_enters_address_comments(state, replied) is True
    assert thread_enters_address_comments(state, original) is False


@pytest.mark.unit
def test_thread_enters_address_comments_closed_changed_body() -> None:
    from awf.runtime.feedback_policy import thread_enters_address_comments

    original = _thread("T1", body="nit")
    replied = _thread("T1", body="new reply")
    state = {
        "T1": "fix_committed",
        review_thread_body_state_key("T1"): review_thread_body_hash(original),
    }
    assert thread_enters_address_comments(state, replied) is True
    assert thread_enters_address_comments(state, original) is False
    # Closed without a body snapshot does not re-queue.
    assert thread_enters_address_comments({"T1": "fix_committed"}, original) is False


@pytest.mark.unit
def test_thread_enters_address_comments_skips_unchanged_defer() -> None:
    """Unchanged defer (no snapshot, or matching hash) stays off AddressComments."""
    from awf.runtime.feedback_policy import thread_enters_address_comments

    thread = _thread("T1", body="deferred")
    assert thread_enters_address_comments({"T1": "defer"}, thread) is False
    matching = {
        "T1": "defer",
        review_thread_body_state_key("T1"): review_thread_body_hash(thread),
    }
    assert thread_enters_address_comments(matching, thread) is False


@pytest.mark.unit
def test_thread_enters_address_comments_requeues_defer_on_body_change() -> None:
    """Reviewer reply after defer must re-enter AddressComments (not strand NotifyHuman)."""
    from awf.runtime.feedback_policy import thread_enters_address_comments

    original = _thread("T1", body="deferred work")
    replied = _thread("T1", body="actually please fix this now")
    state = {
        "T1": "defer",
        review_thread_body_state_key("T1"): review_thread_body_hash(original),
    }
    assert thread_enters_address_comments(state, replied) is True
    assert thread_enters_address_comments(state, original) is False


@pytest.mark.unit
def test_canonical_combine_active_first_active_wins_duplicates() -> None:
    active_a = _thread("A", body="active A")
    active_dup = _thread("D", body="active representation")
    outdated_dup = _thread("D", body="outdated representation", is_outdated=True)
    outdated_b = _thread("B", body="outdated B", is_outdated=True)
    outdated_c = _thread("C", body="outdated C", is_outdated=True)

    combined = canonical_unresolved_inline_threads(
        (active_a, active_dup),
        (outdated_dup, outdated_b, outdated_c),
    )
    assert [t.thread_id for t in combined] == ["A", "D", "B", "C"]
    assert combined[1].body_excerpt == "active representation"
    assert combined[1].is_outdated is False


@pytest.mark.unit
def test_canonical_combine_skips_duplicate_ids_within_active_feed() -> None:
    """Same thread id twice in the active feed coalesces to one representation."""
    first = _thread("A", body="first sighting")
    second = _thread("A", body="duplicate active entry")
    outdated_only = _thread("B", body="outdated only", is_outdated=True)

    combined = canonical_unresolved_inline_threads(
        (first, second),
        (outdated_only,),
    )
    assert [t.thread_id for t in combined] == ["A", "B"]
    # Equal-rank distinct bodies: later feed occurrence wins.
    assert combined[0].body_excerpt == "duplicate active entry"


@pytest.mark.unit
def test_canonical_combine_prefers_outdated_copy_that_enters_address_comments() -> None:
    """Within-outdated duplicates: keep the richer reply conversation.

    Transport may repeat an ID with an older body matching the recorded hash
    and a later node carrying a reviewer reply. Preferring richer comments
    (not state-relative AddressComments toggling) feeds the reply into
    decide() without oscillating after the fresher hash is recorded.
    """
    from datetime import UTC, datetime

    from awf.runtime.feedback_policy import thread_enters_address_comments
    from awf.runtime.pr_monitor_models import ReviewThreadComment

    stale = _thread("T1", body="addressed body", is_outdated=True)
    fresher = ReviewThread(
        thread_id="T1",
        path="src/x.py",
        line=10,
        body_excerpt="new feedback after address",
        author=None,
        is_outdated=True,
        comments=(
            ReviewThreadComment(
                comment_id="1",
                body="addressed body",
                author="bot",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            ReviewThreadComment(
                comment_id="2",
                body="new feedback after address",
                author="reviewer",
                created_at=datetime(2026, 1, 2, tzinfo=UTC),
            ),
        ),
    )
    state = {
        "T1": "fix_committed",
        review_thread_body_state_key("T1"): review_thread_body_hash(stale),
    }
    assert thread_enters_address_comments(state, stale) is False
    assert thread_enters_address_comments(state, fresher) is True

    without_state = canonical_unresolved_inline_threads((), (stale, fresher))
    assert without_state[0] is fresher

    with_state = canonical_unresolved_inline_threads((), (stale, fresher), state)
    assert len(with_state) == 1
    assert with_state[0] is fresher
    assert with_state[0].body_excerpt == "new feedback after address"


@pytest.mark.unit
def test_canonical_combine_does_not_flip_to_stale_after_fresher_hash_recorded() -> None:
    """After repair records the fresher hash, do not re-select the stale ghost.

    State-relative AddressComments toggling would flip onto the older body once
    the fresher hash is recorded, re-batching already-handled text forever while
    hygiene refuses resolve on the mismatched sibling.
    """
    from awf.runtime.feedback_policy import thread_enters_address_comments

    stale = _thread("T1", body="addressed body", is_outdated=True)
    fresher = _thread("T1", body="new feedback after address", is_outdated=True)
    state = {
        "T1": "fix_committed",
        review_thread_body_state_key("T1"): review_thread_body_hash(fresher),
    }
    assert thread_enters_address_comments(state, fresher) is False
    assert thread_enters_address_comments(state, stale) is True

    # Both feed orders must keep the matching fresher representation.
    for outdated in ((stale, fresher), (fresher, stale)):
        combined = canonical_unresolved_inline_threads((), outdated, state)
        assert len(combined) == 1
        assert combined[0].body_excerpt == "new feedback after address"
        assert thread_enters_address_comments(state, combined[0]) is False


@pytest.mark.unit
def test_canonical_combine_prefers_richer_comment_conversation() -> None:
    """Richer comment history outranks an equal-ID body_excerpt ghost."""
    from datetime import UTC, datetime

    from awf.runtime.pr_monitor_models import ReviewThreadComment

    stale = _thread("T1", body="addressed body", is_outdated=True)
    fresher = ReviewThread(
        thread_id="T1",
        path="src/x.py",
        line=10,
        body_excerpt="new feedback after address",
        author=None,
        is_outdated=True,
        comments=(
            ReviewThreadComment(
                comment_id="1",
                body="addressed body",
                author="bot",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            ReviewThreadComment(
                comment_id="2",
                body="new feedback after address",
                author="reviewer",
                created_at=datetime(2026, 1, 2, tzinfo=UTC),
            ),
        ),
    )
    state = {
        "T1": "fix_committed",
        review_thread_body_state_key("T1"): review_thread_body_hash(stale),
    }
    combined = canonical_unresolved_inline_threads((), (stale, fresher), state)
    assert len(combined) == 1
    assert combined[0] is fresher


@pytest.mark.unit
def test_canonical_combine_prefers_edited_comment_over_pre_edit_hash_match() -> None:
    """Same-ID pre/post edit copies: newer updated_at outranks recorded-hash match.

    Transport may emit both the addressed pre-edit body and the reviewer's
    edited body with identical comment counts and created_at. Ranking must
    include updated_at so the edit enters AddressComments instead of the
    anti-oscillation branch retaining the stale hash match.
    """
    from datetime import UTC, datetime

    from awf.runtime.feedback_policy import thread_enters_address_comments
    from awf.runtime.pr_monitor_models import ReviewThreadComment

    created = datetime(2026, 1, 1, tzinfo=UTC)
    pre_edit = ReviewThread(
        thread_id="T1",
        path="src/x.py",
        line=10,
        body_excerpt="original ask",
        author=None,
        is_outdated=True,
        comments=(
            ReviewThreadComment(
                comment_id="c1",
                body="original ask",
                author="reviewer",
                created_at=created,
                updated_at=created,
            ),
        ),
    )
    post_edit = ReviewThread(
        thread_id="T1",
        path="src/x.py",
        line=10,
        body_excerpt="clarified ask after edit",
        author=None,
        is_outdated=True,
        comments=(
            ReviewThreadComment(
                comment_id="c1",
                body="clarified ask after edit",
                author="reviewer",
                created_at=created,
                updated_at=datetime(2026, 1, 2, tzinfo=UTC),
            ),
        ),
    )
    state = {
        "T1": "fix_committed",
        review_thread_body_state_key("T1"): review_thread_body_hash(pre_edit),
    }
    assert thread_enters_address_comments(state, pre_edit) is False
    assert thread_enters_address_comments(state, post_edit) is True

    for outdated in ((pre_edit, post_edit), (post_edit, pre_edit)):
        combined = canonical_unresolved_inline_threads((), outdated, state)
        assert len(combined) == 1
        assert combined[0] is post_edit
        assert thread_enters_address_comments(state, combined[0]) is True


@pytest.mark.unit
def test_prefer_duplicate_review_thread_mixed_naive_and_aware_timestamps() -> None:
    """Mixed naive/aware stamps must not TypeError when ranking duplicates.

    Transport parsers may emit naive or aware datetimes. Ranking normalizes
    every non-None stamp to UTC before max(), so prefer_duplicate_review_thread
    can compare same-ID copies without aborting canonicalization.
    """
    from datetime import UTC, datetime

    from awf.runtime.pr_monitor_models import ReviewThreadComment

    older_mixed = ReviewThread(
        thread_id="T1",
        path="src/x.py",
        line=10,
        body_excerpt="older conversation",
        author=None,
        is_outdated=True,
        comments=(
            ReviewThreadComment(
                comment_id="1",
                body="first",
                author="bot",
                created_at=datetime(2026, 1, 1),  # naive
            ),
            ReviewThreadComment(
                comment_id="2",
                body="older conversation",
                author="reviewer",
                created_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
                updated_at=datetime(2026, 1, 1, 13, 0),  # naive updated_at
            ),
        ),
    )
    newer_aware = ReviewThread(
        thread_id="T1",
        path="src/x.py",
        line=10,
        body_excerpt="newer conversation",
        author=None,
        is_outdated=True,
        comments=(
            ReviewThreadComment(
                comment_id="1",
                body="first",
                author="bot",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
            ReviewThreadComment(
                comment_id="2",
                body="newer conversation",
                author="reviewer",
                created_at=datetime(2026, 1, 2, tzinfo=UTC),
            ),
        ),
    )

    assert prefer_duplicate_review_thread(older_mixed, newer_aware) is newer_aware
    assert prefer_duplicate_review_thread(newer_aware, older_mixed) is newer_aware


@pytest.mark.unit
def test_canonical_combine_state_aware_still_active_wins() -> None:
    """State-aware preference must not override active-wins across feeds."""
    active = _thread("T1", body="active matching")
    stale_outdated = _thread("T1", body="stale outdated", is_outdated=True)
    fresh_outdated = _thread("T1", body="fresh outdated reply", is_outdated=True)
    state = {
        "T1": "fix_committed",
        review_thread_body_state_key("T1"): review_thread_body_hash(active),
    }
    combined = canonical_unresolved_inline_threads(
        (active,),
        (stale_outdated, fresh_outdated),
        state,
    )
    assert len(combined) == 1
    assert combined[0].body_excerpt == "active matching"
    assert combined[0].is_outdated is False


@pytest.mark.unit
def test_unresolved_count_helpers() -> None:
    active = (_thread("A"), _thread("D"))
    outdated = (
        _thread("D", is_outdated=True),
        _thread("B", is_outdated=True),
    )
    assert unresolved_active_count(active) == 2
    assert unresolved_outdated_unique_count(active, outdated) == 1
    assert unresolved_canonical_count(active, outdated) == 3
    assert unresolved_thread_counts(active, outdated) == {
        "unresolved_threads": 3,
        "unresolved_active_threads": 2,
        "unresolved_outdated_threads": 1,
    }


@pytest.mark.unit
def test_unresolved_active_count_dedupes_duplicate_ids() -> None:
    """Duplicate IDs in the active feed must not inflate active vs canonical."""
    active = (
        _thread("A", body="first"),
        _thread("A", body="repeat"),
        _thread("B"),
    )
    outdated = (_thread("C", is_outdated=True),)
    assert unresolved_active_count(active) == 2
    assert unresolved_canonical_count(active, outdated) == 3
    counts = unresolved_thread_counts(active, outdated)
    assert counts["unresolved_active_threads"] == 2
    assert counts["unresolved_threads"] == 3
    assert counts["unresolved_active_threads"] <= counts["unresolved_threads"]


@pytest.mark.unit
def test_unresolved_outdated_count_dedupes_duplicate_ids() -> None:
    """Duplicate IDs in the outdated feed must not inflate outdated vs canonical."""
    active: tuple[ReviewThread, ...] = ()
    outdated = (
        _thread("A", body="first", is_outdated=True),
        _thread("A", body="repeat", is_outdated=True),
    )
    assert unresolved_outdated_unique_count(active, outdated) == 1
    assert unresolved_canonical_count(active, outdated) == 1
    counts = unresolved_thread_counts(active, outdated)
    assert counts["unresolved_outdated_threads"] == 1
    assert counts["unresolved_threads"] == 1
    assert counts["unresolved_outdated_threads"] <= counts["unresolved_threads"]


@pytest.mark.unit
def test_preferred_duplicate_review_thread_requires_at_least_one_copy() -> None:
    """Empty transport groups are a caller bug — refuse rather than invent a thread."""
    with pytest.raises(ValueError, match="at least one copy"):
        preferred_duplicate_review_thread(())
