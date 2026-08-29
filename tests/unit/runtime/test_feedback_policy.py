"""Unit tests for state-independent PR feedback policy helpers."""

from __future__ import annotations

import pytest

from awf.runtime.feedback_policy import (
    CLOSED_OUTDATED_THREAD_VERDICTS,
    canonical_unresolved_inline_threads,
    needs_comment_attention,
    outdated_thread_has_fresh_feedback,
    review_thread_body_hash,
    review_thread_body_state_key,
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
    from awf.runtime.feedback_policy import thread_enters_address_comments

    thread = _thread("T1", body="nit")
    assert thread_enters_address_comments({"T1": "needs_human"}, thread) is False
    matching = {
        "T1": "needs_human",
        review_thread_body_state_key("T1"): review_thread_body_hash(thread),
    }
    assert thread_enters_address_comments(matching, thread) is False
    assert thread_enters_address_comments({}, thread) is True


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
    """Same thread id twice in the active feed keeps the first representation."""
    first = _thread("A", body="first sighting")
    second = _thread("A", body="duplicate active entry")
    outdated_only = _thread("B", body="outdated only", is_outdated=True)

    combined = canonical_unresolved_inline_threads(
        (first, second),
        (outdated_only,),
    )
    assert [t.thread_id for t in combined] == ["A", "B"]
    assert combined[0].body_excerpt == "first sighting"


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
