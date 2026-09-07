"""Operator guide retirement of a ``needs_human`` review THREAD (issue #938).

Review *comments* have no forge thread to resolve, so a guide that names one
flips it to ``false_positive``. A review *thread* is different: the agent must
re-read it and record the real verdict, so the guide only clears the parked
``needs_human`` and lets the thread re-enter ``AddressComments``. These tests
pin both arms plus the comment path staying byte-for-byte unchanged.
"""

from __future__ import annotations

import pytest

from awf.runtime.feedback_policy import (
    review_thread_body_hash,
    review_thread_body_state_key,
    thread_enters_address_comments,
    thread_needs_attention,
)
from awf.runtime.monitor_prompts import operator_hint_prompt
from awf.runtime.pr_monitor import MonitorState, OperatorHint
from awf.runtime.pr_monitor_models import ReviewThread
from awf.runtime.pr_monitor_runner.operator_hint_parsing import (
    _operator_hint_review_thread_id_candidates,
)
from awf.runtime.pr_monitor_runner.operator_hints import (
    _finalize_processed_operator_hint,
    _mark_referenced_needs_human_feedback_answered,
)

THREAD_ID = "PRRT_kwDOSJAM6s6fsqcA"
THREAD_HASH_KEY = review_thread_body_state_key(THREAD_ID)
THREAD_REASON_KEY = f"__needs_human_reason__:{THREAD_ID}"
REVIEW_COMMENT_ID = "issue:3476977020"
REVIEW_COMMENT_HASH_KEY = f"__review_comment_body_hash__:{REVIEW_COMMENT_ID}"
REVIEW_COMMENT_REASON_KEY = f"__needs_human_reason__:{REVIEW_COMMENT_ID}"


def _guide(directive: str | None, *, reason: str = "operator guide audit note") -> OperatorHint:
    return OperatorHint(
        reason=reason,
        directive=directive,
        operation_id="op_guide_thread_938",
        requested_at="2026-09-06T20:31:00+00:00",
        reason_code="OPERATOR_GUIDE",
    )


def _parked_thread_state(
    *,
    thread_id: str = THREAD_ID,
    verdict: str = "needs_human",
    body_hash: str | None = "thread-body-hash",
) -> MonitorState:
    addressed = {thread_id: verdict, f"__needs_human_reason__:{thread_id}": "needs a human call"}
    if body_hash is not None:
        addressed[review_thread_body_state_key(thread_id)] = body_hash
    return MonitorState(threads_addressed_ids=addressed)


def _review_thread(body: str = "apply the literal ask at the anchor") -> ReviewThread:
    return ReviewThread(
        thread_id=THREAD_ID,
        path="src/awf/runtime/pr_monitor_runner/loop.py",
        line=42,
        body_excerpt=body,
        author="reviewer",
    )


@pytest.mark.unit
def test_operator_hint_directive_requeues_needs_human_review_thread() -> None:
    """A directive naming a parked thread clears the verdict (never asserts one)."""
    state = _parked_thread_state()

    _mark_referenced_needs_human_feedback_answered(
        state, hint=_guide(f"Accept the fix for {THREAD_ID}; apply the literal ask and resolve.")
    )

    assert THREAD_ID not in state.threads_addressed_ids
    assert THREAD_REASON_KEY not in state.threads_addressed_ids
    assert state.threads_addressed_ids[THREAD_HASH_KEY] == "thread-body-hash"


@pytest.mark.unit
def test_requeued_thread_reenters_address_comments() -> None:
    """The cleared verdict is what actually un-wedges the thread for ``decide``."""
    thread = _review_thread()
    state = _parked_thread_state(body_hash=review_thread_body_hash(thread))

    before = dict(state.threads_addressed_ids)
    assert thread_needs_attention(before, thread) is False
    assert thread_enters_address_comments(before, thread) is False

    _mark_referenced_needs_human_feedback_answered(
        state, hint=_guide(f"Operator decision on {THREAD_ID}: accept and apply.")
    )

    after = dict(state.threads_addressed_ids)
    assert thread_needs_attention(after, thread) is True
    assert thread_enters_address_comments(after, thread) is True


@pytest.mark.unit
@pytest.mark.parametrize("body_hash", [None, ""])
def test_thread_retirement_requires_body_hash_sidecar(body_hash: str | None) -> None:
    """Only rows carrying durable body-hash evidence are retired."""
    state = _parked_thread_state(body_hash=body_hash)
    expected = dict(state.threads_addressed_ids)

    _mark_referenced_needs_human_feedback_answered(
        state, hint=_guide(f"Accept the fix for {THREAD_ID}.")
    )

    assert state.threads_addressed_ids == expected


@pytest.mark.unit
def test_thread_retirement_ignores_unknown_thread_id() -> None:
    """A thread id absent from state creates nothing and changes nothing."""
    state = _parked_thread_state()
    expected = dict(state.threads_addressed_ids)

    _mark_referenced_needs_human_feedback_answered(
        state, hint=_guide("Accept the fix for PRRT_kwDOSJAM6s6UNKNOWN.")
    )

    assert state.threads_addressed_ids == expected
    assert "PRRT_kwDOSJAM6s6UNKNOWN" not in state.threads_addressed_ids


@pytest.mark.unit
@pytest.mark.parametrize("verdict", ["defer", "fix_committed", "false_positive", "agent_failed"])
def test_thread_retirement_leaves_non_needs_human_verdicts(verdict: str) -> None:
    """Retirement is scoped to ``needs_human``; other dispositions are untouched."""
    state = _parked_thread_state(verdict=verdict)
    expected = dict(state.threads_addressed_ids)

    _mark_referenced_needs_human_feedback_answered(
        state, hint=_guide(f"Operator decision on {THREAD_ID}: accept and apply.")
    )

    assert state.threads_addressed_ids == expected


@pytest.mark.unit
def test_thread_retirement_accepts_bitbucket_thread_key_shapes() -> None:
    """The forge-neutral Bitbucket thread keys retire; ``bbcomment:`` stays a comment."""
    thread_key = "bb:acme/widgets#12:345"
    task_key = "bbtask:acme/widgets#12:678"
    state = MonitorState(
        threads_addressed_ids={
            thread_key: "needs_human",
            review_thread_body_state_key(thread_key): "bb-hash",
            task_key: "needs_human",
            review_thread_body_state_key(task_key): "bbtask-hash",
            "bbcomment:99": "needs_human",
            "__review_comment_body_hash__:bbcomment:99": "comment-hash",
        }
    )

    _mark_referenced_needs_human_feedback_answered(
        state,
        hint=_guide(f"Accept {thread_key} and {task_key}; bbcomment:99 is a false positive."),
    )

    assert thread_key not in state.threads_addressed_ids
    assert task_key not in state.threads_addressed_ids
    assert state.threads_addressed_ids[review_thread_body_state_key(thread_key)] == "bb-hash"
    assert state.threads_addressed_ids[review_thread_body_state_key(task_key)] == "bbtask-hash"
    assert state.threads_addressed_ids["bbcomment:99"] == "false_positive"


@pytest.mark.unit
def test_thread_retirement_accepts_full_adoption_thread_key_grammar() -> None:
    """Owner/repo segments outside ``[A-Za-z0-9._-]`` still retire (adoption grammar)."""
    thread_key = "bb:acme/widgets%20legacy#12:345"
    task_key = "bbtask:acme+co/widgets~fork#12:678"
    state = MonitorState(
        threads_addressed_ids={
            thread_key: "needs_human",
            review_thread_body_state_key(thread_key): "bb-hash",
            task_key: "needs_human",
            review_thread_body_state_key(task_key): "bbtask-hash",
        }
    )

    directive = f"Accept {thread_key} and {task_key}."

    assert _operator_hint_review_thread_id_candidates(directive) == (thread_key, task_key)

    _mark_referenced_needs_human_feedback_answered(state, hint=_guide(directive))

    assert thread_key not in state.threads_addressed_ids
    assert task_key not in state.threads_addressed_ids


@pytest.mark.unit
def test_operator_hint_thread_key_extraction_does_not_span_whitespace() -> None:
    """The broadened owner/repo classes stay token-local, so prose cannot form a key."""
    assert _operator_hint_review_thread_id_candidates("bb:acme/widgets, see PR #12:345") == ()


@pytest.mark.unit
def test_comment_retirement_unchanged_when_directive_also_names_a_thread() -> None:
    """One directive naming both classes routes each to its own arm."""
    state = MonitorState(
        threads_addressed_ids={
            REVIEW_COMMENT_ID: "needs_human",
            REVIEW_COMMENT_HASH_KEY: "comment-body-hash",
            REVIEW_COMMENT_REASON_KEY: "review still needs a human",
            THREAD_ID: "needs_human",
            THREAD_HASH_KEY: "thread-body-hash",
            THREAD_REASON_KEY: "needs a human call",
        }
    )

    _mark_referenced_needs_human_feedback_answered(
        state,
        hint=_guide(f"{REVIEW_COMMENT_ID} is a false positive; accept the fix for {THREAD_ID}."),
    )

    assert state.threads_addressed_ids[REVIEW_COMMENT_ID] == "false_positive"
    assert state.threads_addressed_ids[REVIEW_COMMENT_HASH_KEY] == "comment-body-hash"
    assert REVIEW_COMMENT_REASON_KEY not in state.threads_addressed_ids
    assert THREAD_ID not in state.threads_addressed_ids
    assert state.threads_addressed_ids[THREAD_HASH_KEY] == "thread-body-hash"
    assert THREAD_REASON_KEY not in state.threads_addressed_ids


@pytest.mark.unit
def test_thread_retirement_uses_acted_reason_text_for_grant_only_resume() -> None:
    """Grant-only resumes retire only when the reason was actually acted on."""
    hint = _guide(None, reason=f"approved protected path; operator accepted {THREAD_ID}")

    acted = _parked_thread_state()
    _mark_referenced_needs_human_feedback_answered(acted, hint=hint, acted_text=hint.reason)
    assert THREAD_ID not in acted.threads_addressed_ids

    unacted = _parked_thread_state()
    expected = dict(unacted.threads_addressed_ids)
    _mark_referenced_needs_human_feedback_answered(unacted, hint=hint)
    assert unacted.threads_addressed_ids == expected


@pytest.mark.unit
def test_operator_hint_review_thread_id_candidates_dedupes_and_preserves_order() -> None:
    """Extraction is order-preserving, deduped, case-sensitive and closed-shape."""
    text = (
        f"{THREAD_ID} then bb:acme/widgets#12:345 then {THREAD_ID} again; "
        "prrt_kwdolower and bb:acme#12:3 are not thread keys."
    )

    assert _operator_hint_review_thread_id_candidates(text) == (
        THREAD_ID,
        "bb:acme/widgets#12:345",
    )


@pytest.mark.unit
def test_finalize_processed_operator_hint_requeues_needs_human_thread() -> None:
    """The wiring the runner actually calls retires the thread and still finalizes."""
    state = _parked_thread_state()
    state.threads_addressed_ids["__awf_protected_block_preserved_head__"] = "deadbeef"
    hint = _guide(f"Accept the fix for {THREAD_ID}; apply the literal ask and resolve.")

    _finalize_processed_operator_hint(state, hint=hint)

    assert THREAD_ID not in state.threads_addressed_ids
    assert THREAD_REASON_KEY not in state.threads_addressed_ids
    assert state.threads_addressed_ids[THREAD_HASH_KEY] == "thread-body-hash"
    assert "__awf_protected_block_preserved_head__" not in state.threads_addressed_ids
    assert state.pending_operator_hint is None
    assert state.threads_addressed_ids["__awf_operator_hint_processed__:op_guide_thread_938"] == (
        "processed"
    )


@pytest.mark.unit
def test_operator_hint_prompt_carries_directive_for_the_hint_cycle() -> None:
    """Issue #938 requirement 2: the hint cycle's prompt carries the directive."""
    directive = f"Accept the fix for {THREAD_ID}; apply the literal ask at the anchor."

    prompt = operator_hint_prompt(
        pr_number=922,
        repo_slug="dimileeh/aira-web",
        reason="operator guide audit note",
        directive=directive,
        operation_id="op_guide_thread_938",
    )

    assert directive in prompt
