"""Merge-gate behavior for deferred review feedback in ``pr_monitor.decide``.

Two regressions meet here:

* PR 342: advisory bot *review comments* must not wedge the PR forever. Bots
  (Greptile, CodeRabbit, Gemini, Cursor Bugbot, Codex-connector, etc.) post
  advisory feedback only and cannot resolve threads, so a bot ``defer`` on a
  review-level comment does not block. Human review-comment defers still block.

* #305 (PR #303 incident): an inline review *thread* deferred by a bot was
  allowed to merge while still unresolved on GitHub, shipping the wrong code.
  Under the new contract an unresolved inline thread blocks the merge whenever
  its verdict is ``defer`` or ``needs_human`` — author no longer matters for
  threads. A follow-up ``defer`` is allowed to clear the gate only after the
  runner durably captures it (explanatory comment + filed tracking issue) and
  RESOLVES the thread, at which point it leaves ``unresolved_inline_threads``
  entirely. ``fix_committed`` and ``false_positive`` threads never block.
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


def _thread(tid: str, author: str | None, *, path: str = "src/foo.py") -> ReviewThread:
    return ReviewThread(
        thread_id=tid,
        path=path,
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


class TestInlineThreadDeferGate:
    """#305: an unresolved deferred inline thread blocks regardless of author."""

    @pytest.mark.unit
    def test_bot_defer_thread_blocks_until_captured(self) -> None:
        # Old #342 behavior let this merge; #305 blocks until the runner
        # captures the follow-up and resolves the thread.
        state = MonitorState(threads_addressed_ids={"T1": "defer"})
        status = _status(inline=(_thread("T1", "greptile-apps"),))
        action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, NotifyHuman), f"expected NotifyHuman; got {action!r}"

    @pytest.mark.unit
    def test_human_defer_thread_blocks_merge(self) -> None:
        state = MonitorState(threads_addressed_ids={"T1": "defer"})
        status = _status(inline=(_thread("T1", "alice-human"),))
        action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, NotifyHuman)

    @pytest.mark.unit
    def test_needs_human_thread_blocks_merge(self) -> None:
        state = MonitorState(threads_addressed_ids={"T1": "needs_human"})
        status = _status(inline=(_thread("T1", "greptile-apps"),))
        action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, NotifyHuman)

    @pytest.mark.unit
    def test_protected_workflow_thread_needs_human_blocks_merge(self) -> None:
        # The exact PR #303 incident: a bot thread on a protected workflow file
        # the agent could not edit. It must block, not merge.
        state = MonitorState(threads_addressed_ids={"T1": "needs_human"})
        status = _status(inline=(_thread("T1", "cursor", path=".github/workflows/publish.yml"),))
        action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, NotifyHuman)

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
            "dependabot[bot]",
        ],
    )
    def test_deferred_thread_blocks_for_every_author(self, login: str) -> None:
        # Author classification no longer matters for inline threads under #305.
        state = MonitorState(threads_addressed_ids={"T1": "defer"})
        status = _status(inline=(_thread("T1", login),))
        action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, NotifyHuman), f"{login} defer thread should block"

    @pytest.mark.unit
    def test_false_positive_thread_does_not_block(self) -> None:
        # A durable false-positive verdict is the one way an unresolved thread
        # is allowed to merge: the agent classified it as noise.
        state = MonitorState(threads_addressed_ids={"T1": "false_positive"})
        status = _status(inline=(_thread("T1", "greptile-apps"),))
        action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, Merge)

    @pytest.mark.unit
    def test_fix_committed_thread_does_not_block(self) -> None:
        # A thread marked fix_committed that lingers unresolved on GitHub
        # (maintainer just hasn't clicked Resolve) flows through to merge.
        state = MonitorState(threads_addressed_ids={"T1": "fix_committed"})
        status = _status(inline=(_thread("T1", "alice"),))
        action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, Merge)

    @pytest.mark.unit
    def test_captured_and_resolved_thread_merges(self) -> None:
        # Once the runner captures the follow-up and resolves the thread, it is
        # gone from unresolved_inline_threads, so the gate is clear.
        state = MonitorState(threads_addressed_ids={"T1": "defer"})
        status = _status(inline=())
        action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, Merge)


class TestReviewCommentDeferGate:
    """#342 preserved: bot review-comment defers do not block; human ones do."""

    @pytest.mark.unit
    def test_bot_defer_review_comment_does_not_block_merge(self) -> None:
        state = MonitorState(threads_addressed_ids={"C1": "defer"})
        status = _status(reviews=(_review("C1", "coderabbitai"),))
        action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, Merge)

    @pytest.mark.unit
    def test_bot_suffix_login_review_comment_classified_as_bot(self) -> None:
        state = MonitorState(threads_addressed_ids={"C1": "defer"})
        status = _status(reviews=(_review("C1", "dependabot[bot]"),))
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
    def test_known_bot_review_comment_logins_all_classified(self, login: str) -> None:
        state = MonitorState(threads_addressed_ids={"C1": "defer"})
        status = _status(reviews=(_review("C1", login),))
        action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, Merge), f"{login} should be classified as advisory bot"

    @pytest.mark.unit
    def test_human_defer_review_comment_blocks_merge(self) -> None:
        state = MonitorState(threads_addressed_ids={"C1": "defer"})
        status = _status(reviews=(_review("C1", "alice"),))
        action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, NotifyHuman)

    @pytest.mark.unit
    def test_needs_human_bot_review_comment_blocks_merge(self) -> None:
        # needs_human blocks regardless of author: the diff may be wrong.
        state = MonitorState(threads_addressed_ids={"C1": "needs_human"})
        status = _status(reviews=(_review("C1", "coderabbitai"),))
        action = decide(status=status, state=state, config=MonitorConfig(auto_merge=True))
        assert isinstance(action, NotifyHuman)
