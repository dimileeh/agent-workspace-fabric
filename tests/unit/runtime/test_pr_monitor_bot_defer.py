"""Bot-defer-aware merge gate for ``pr_monitor.decide``.

Regression for PR 342: every bot reviewer's advisory nit was blocking
the auto-merge because ``state.threads_addressed_ids[t.thread_id] ==
"defer"`` plus "still unresolved on GitHub" was a hard stop. Bots
(Greptile, CodeRabbit, Gemini, Cursor Bugbot, Codex-connector, etc.)
post advisory feedback only — they cannot themselves resolve threads,
so their unresolved nits linger forever. Humans must still block.
"""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor import (
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
    decide,
)


def _status(
    *,
    inline: tuple[ReviewThread, ...] = (),
    reviews: tuple[ReviewComment, ...] = (),
) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha="abc123",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=inline,
        unresolved_review_comments=reviews,
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )


def _thread(tid: str, author: str) -> ReviewThread:
    return ReviewThread(
        thread_id=tid,
        path="src/foo.py",
        line=42,
        body_excerpt="nit",
        author=author,
    )


def _review(cid: str, author: str) -> ReviewComment:
    return ReviewComment(
        comment_id=cid,
        body_excerpt="nit",
        author=author,
    )


class TestBotDeferUnblocksMerge:
    @pytest.mark.unit
    def test_bot_defer_does_not_block_merge(self) -> None:
        state = MonitorState(threads_addressed_ids={"T1": "defer"})
        status = _status(inline=(_thread("T1", "greptile-apps"),))
        action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, Merge), f"expected Merge; got {action!r}"

    @pytest.mark.unit
    def test_human_defer_blocks_merge(self) -> None:
        state = MonitorState(threads_addressed_ids={"T1": "defer"})
        status = _status(inline=(_thread("T1", "alice-human"),))
        action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, NotifyHuman)

    @pytest.mark.unit
    def test_mixed_defers_block_if_any_human(self) -> None:
        state = MonitorState(threads_addressed_ids={"T_bot": "defer", "T_human": "defer"})
        status = _status(
            inline=(
                _thread("T_bot", "greptile-apps"),
                _thread("T_human", "alice"),
            )
        )
        action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, NotifyHuman)

    @pytest.mark.unit
    def test_bot_suffix_login_classified_as_bot(self) -> None:
        state = MonitorState(threads_addressed_ids={"T1": "defer"})
        status = _status(inline=(_thread("T1", "dependabot[bot]"),))
        action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, Merge)

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "login",
        [
            "greptile-apps",
            "coderabbitai",
            "gemini-code-assist",
            "chatgpt-codex-connector",
            "cursor",
            "codex",
            "github-actions",
        ],
    )
    def test_known_bot_logins_all_classified(self, login: str) -> None:
        state = MonitorState(threads_addressed_ids={"T1": "defer"})
        status = _status(inline=(_thread("T1", login),))
        action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, Merge), f"{login} should be classified as bot"

    @pytest.mark.unit
    def test_bot_defer_review_comment_does_not_block_merge(self) -> None:
        state = MonitorState(threads_addressed_ids={"C1": "defer"})
        status = _status(reviews=(_review("C1", "coderabbitai"),))
        action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, Merge)

    @pytest.mark.unit
    def test_human_defer_review_comment_blocks_merge(self) -> None:
        state = MonitorState(threads_addressed_ids={"C1": "defer"})
        status = _status(reviews=(_review("C1", "alice"),))
        action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, NotifyHuman)

    @pytest.mark.unit
    def test_addressed_without_defer_does_not_block(self) -> None:
        """A thread marked ``fix_committed`` that lingers unresolved on
        GitHub (maintainer just hasn't clicked Resolve yet) flows through
        the normal gates — the defer block only triggers on ``defer``."""
        state = MonitorState(threads_addressed_ids={"T1": "fix_committed"})
        # Thread resolved upstream — the decide() gate sees no unresolved
        # thread at all, so Merge.
        status = _status(inline=())
        action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, Merge)

    @pytest.mark.unit
    def test_unknown_author_treated_as_human(self) -> None:
        """If the author field is unset (GraphQL returned null), we must
        NOT silently assume "bot" and merge — that would let a merge
        slip through on deferred unknown-author feedback. Default to
        human (block the merge)."""
        state = MonitorState(threads_addressed_ids={"T1": "defer"})
        status = _status(inline=(_thread("T1", ""),))
        action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, NotifyHuman)
