"""Table-driven tests for ``pr_monitor.decide`` — pure decision core."""

from __future__ import annotations

import pytest

from awf.runtime.monitor_state_keys import _merge_method_blocked_key
from awf.runtime.pr_monitor import (
    CheckFailure,
    CheckState,
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    PRStatus,
    ReviewComment,
    ReviewThread,
    ReviewThreadComment,
    _mark_review_thread_addressed,
    _review_thread_needs_attention,
    decide,
)


def _thread(
    tid: str = "T1",
    body: str = "fix this",
    is_resolved: bool = False,
    author: str | None = None,
) -> ReviewThread:
    return ReviewThread(
        thread_id=tid,
        path="src/x.py",
        line=10,
        body_excerpt=body,
        author=author,
        is_resolved=is_resolved,
    )


def _review(
    cid: str = "C1",
    body: str = "see below",
    is_resolved: bool = False,
    blocks_merge: bool = False,
    author: str | None = None,
    source_kind: str = "review",
    state: str | None = None,
) -> ReviewComment:
    return ReviewComment(
        comment_id=cid,
        body_excerpt=body,
        author=author,
        is_resolved=is_resolved,
        blocks_merge=blocks_merge,
        source_kind=source_kind,
        state=state,
    )


def _status(
    *,
    head_sha: str = "abc123",
    mergeable: MergeableState = MergeableState.MERGEABLE,
    check_state: CheckState = CheckState.SUCCESS,
    inline: tuple[ReviewThread, ...] = (),
    reviews: tuple[ReviewComment, ...] = (),
    blocking_reviews: tuple[ReviewComment, ...] | None = None,
    base_behind: int = 0,
    merge_state_status: MergeStateStatus = MergeStateStatus.CLEAN,
    ci_failures: tuple[CheckFailure, ...] = (),
    closed: bool = False,
    merged: bool = False,
) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=head_sha,
        mergeable=mergeable,
        check_state=check_state,
        unresolved_inline_threads=inline,
        unresolved_review_comments=reviews,
        blocking_reviews=(
            tuple(c for c in reviews if c.blocks_merge)
            if blocking_reviews is None
            else blocking_reviews
        ),
        base_behind_count=base_behind,
        merge_state_status=merge_state_status,
        ci_failures=ci_failures,
        closed=closed,
        merged=merged,
    )


class TestDeferredFeedbackGate:
    """Deferred unresolved feedback must never be treated as merge-ready."""

    @pytest.mark.unit
    def test_deferred_thread_still_open_blocks_merge(self) -> None:
        """The exact scenario: CI green, nothing to merge against,
        only thing left is a thread the agent deferred. Must NOT merge."""
        state = MonitorState(threads_addressed_ids={"T1": "defer"})
        status = _status(inline=(_thread(tid="T1"),))
        action = decide(state=state, status=status, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, NotifyHuman)

    @pytest.mark.unit
    def test_deferred_review_comment_still_open_blocks_merge(self) -> None:
        """Same contract for top-level review comments."""
        state = MonitorState(threads_addressed_ids={"C1": "defer"})
        status = _status(reviews=(_review(cid="C1"),))
        action = decide(state=state, status=status, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, NotifyHuman)

    @pytest.mark.unit
    def test_resolved_deferred_thread_unblocks_merge(self) -> None:
        """Happy path: agent deferred T1; the maintainer then resolved
        T1 on GitHub. Next poll GitHub reports T1 no longer in
        ``unresolved_inline_threads`` → the defer gate finds no
        deferred-still-open thread → Merge proceeds."""
        state = MonitorState(threads_addressed_ids={"T1": "defer"})
        status = _status(inline=())  # maintainer resolved it
        action = decide(state=state, status=status, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, Merge)

    @pytest.mark.unit
    def test_fix_committed_thread_does_not_trigger_defer_gate(self) -> None:
        """Sanity: only ``defer`` should route through NotifyHuman. A
        thread marked ``fix_committed`` would already have been
        resolved on GitHub, but even if it somehow lingers
        unresolved, it must not be treated as deferred."""
        state = MonitorState(threads_addressed_ids={"T1": "fix_committed"})
        # If it ever appeared unresolved — which would be a different
        # bug — it must either re-trigger AddressComments OR merge,
        # NEVER be confused with a defer.
        status_gone = _status(inline=())
        assert isinstance(
            decide(status=status_gone, state=state, config=MonitorConfig(auto_merge=True)),
            Merge,
        )

    @pytest.mark.unit
    def test_release_variant_still_ends_in_notify_human(self) -> None:
        """Release-PR variant (``auto_merge=False``) already returns
        NotifyHuman unconditionally — the defer gate must not corrupt
        that path (verifying no regression on the release-PR variant)."""
        state = MonitorState(threads_addressed_ids={"T1": "defer"})
        status = _status(inline=(_thread(tid="T1"),))
        action = decide(state=state, status=status, config=MonitorConfig(auto_merge=False))
        assert isinstance(action, NotifyHuman)

    @pytest.mark.unit
    def test_deferred_bot_thread_with_human_reply_blocks_merge(self) -> None:
        """A human reply in an otherwise bot-authored thread is human input."""
        thread = ReviewThread(
            thread_id="T1",
            path="src/x.py",
            line=10,
            body_excerpt="bot nit",
            author="chatgpt-codex-connector",
            comments=(
                ReviewThreadComment(
                    comment_id="101",
                    body="bot nit",
                    author="chatgpt-codex-connector",
                ),
                ReviewThreadComment(
                    comment_id="102",
                    body="maintainer says this still matters",
                    author="dimileeh",
                ),
            ),
        )
        state = MonitorState()
        _mark_review_thread_addressed(state, thread, "defer")

        action = decide(
            state=state,
            status=_status(inline=(thread,)),
            config=MonitorConfig(auto_merge=True),
        )

        assert isinstance(action, NotifyHuman)

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "reply_body",
        (
            "The latest patch still misses the error path.",
            "The fallback remains reachable in this branch.",
        ),
    )
    def test_reviewer_reply_with_new_feedback_requeues_thread(
        self,
        reply_body: str,
    ) -> None:
        original = ReviewThread(
            thread_id="T1",
            path="src/x.py",
            line=10,
            body_excerpt="bot nit",
            author="chatgpt-codex-connector",
            comments=(
                ReviewThreadComment(
                    comment_id="101",
                    body="bot nit",
                    author="chatgpt-codex-connector",
                ),
            ),
        )
        state = MonitorState()
        _mark_review_thread_addressed(state, original, "false_positive")
        changed = ReviewThread(
            thread_id="T1",
            path="src/x.py",
            line=10,
            body_excerpt="bot nit",
            author="chatgpt-codex-connector",
            comments=(
                *original.comments,
                ReviewThreadComment(
                    comment_id="102",
                    body=reply_body,
                    author="chatgpt-codex-connector",
                ),
            ),
        )

        assert _review_thread_needs_attention(state, changed) is True


class TestStateImmutability:
    @pytest.mark.unit
    def test_decide_does_not_bump_iter_count(self) -> None:
        """Iteration accounting is the runner's job — ``decide`` is pure."""
        state = MonitorState(iter_count=3)
        decide(_status(inline=(_thread(),)), state, MonitorConfig())
        assert state.iter_count == 3

    @pytest.mark.unit
    def test_decide_does_not_mutate_addressed_ids(self) -> None:
        state = MonitorState(threads_addressed_ids={"X": "fix_committed"})
        decide(_status(inline=(_thread("Y"),)), state, MonitorConfig())
        assert state.threads_addressed_ids == {"X": "fix_committed"}


class TestMonitorState:
    @pytest.mark.unit
    def test_mark_addressed_records_verdict(self) -> None:
        state = MonitorState()
        state.mark_addressed("T1", "fix_committed")
        state.mark_addressed("T2", "false_positive")
        assert state.threads_addressed_ids == {"T1": "fix_committed", "T2": "false_positive"}

    @pytest.mark.unit
    def test_mark_addressed_overwrites_previous_verdict(self) -> None:
        """Useful if an earlier ``defer`` gets superseded by a real fix on
        a later iteration."""
        state = MonitorState()
        state.mark_addressed("T1", "defer")
        state.mark_addressed("T1", "fix_committed")
        assert state.threads_addressed_ids == {"T1": "fix_committed"}

    @pytest.mark.unit
    def test_review_thread_needs_attention_when_never_addressed(self) -> None:
        """A thread with no prior verdict (verdict=None) needs attention."""
        state = MonitorState()
        thread = _thread(tid="T1")
        assert _review_thread_needs_attention(state, thread) is True

    @pytest.mark.unit
    def test_review_thread_needs_attention_when_agent_failed(self) -> None:
        """A thread with verdict ``agent_failed`` still needs attention.

        ``agent_failed`` means AWF owes the thread another attempt; it must
        not be treated as addressed (see ``_needs_comment_attention`` doc)."""
        state = MonitorState(threads_addressed_ids={"T1": "agent_failed"})
        thread = _thread(tid="T1")
        assert _review_thread_needs_attention(state, thread) is True


class TestMergeMethodBlocker:
    """decide() must surface a merge-method blocker stored in state."""

    @pytest.mark.unit
    def test_merge_method_blocker_returns_notify_human_with_message(self) -> None:
        """When a merge-method blocker is recorded in state, decide returns
        NotifyHuman carrying the blocker message so the human-attention
        comment includes a reason (e.g. 'squash merge is not allowed by
        this repository ruleset')."""
        key = _merge_method_blocked_key(pr_number=42, head_sha="abc123")
        blocker_msg = "squash merge is not allowed by this repository's ruleset"
        state = MonitorState(threads_addressed_ids={key: blocker_msg})
        action = decide(
            status=_status(head_sha="abc123"),
            state=state,
            config=MonitorConfig(auto_merge=True),
        )
        assert isinstance(action, NotifyHuman)
        assert action.message == blocker_msg
