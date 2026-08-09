"""NotifyHuman item-detail and deduplication regression coverage."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    NotifyHuman,
    PRStatus,
    ReviewComment,
    ReviewThread,
)
from awf.runtime.pr_monitor_runner import comments as pr_monitor_runner_comments
from awf.runtime.pr_monitor_runner import notify_human_loop
from awf.runtime.pr_monitor_runner.helpers import (
    _collect_defer_items,
    _notification_key,
    _notify_human_blocker_items,
)
from awf.runtime.pr_monitor_runner.notify_human_details import _notification_items_digest
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import FakeAdapter, RecordedSleep, make_runner


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _status(
    *,
    threads: tuple[ReviewThread, ...] = (),
    reviews: tuple[ReviewComment, ...] = (),
    blocking_reviews: tuple[ReviewComment, ...] = (),
) -> PRStatus:
    return PRStatus(
        number=46,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=threads,
        unresolved_review_comments=reviews,
        blocking_reviews=blocking_reviews,
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )


class _RecordingGh:
    def __init__(self) -> None:
        self.posts: list[dict[str, object]] = []

    async def post_comment(self, *, repo: object, pr_number: int, body: str) -> None:
        self.posts.append({"repo": repo, "pr_number": pr_number, "body": body})


@pytest.mark.unit
def test_notify_human_blocker_items_does_not_duplicate_deferred_blocking_review() -> None:
    review = ReviewComment(
        comment_id="R-deferred",
        body_excerpt="Please revise the error handling.",
        author="human-reviewer",
        blocks_merge=True,
    )
    status = _status(reviews=(review,), blocking_reviews=(review,))
    state = MonitorState(threads_addressed_ids={"R-deferred": "defer"})

    bot_items, human_items = _notify_human_blocker_items(status, state)

    assert bot_items == []
    assert [item["id"] for item in human_items] == ["R-deferred"]
    assert human_items[0]["verdict"] == "defer"


@pytest.mark.unit
def test_notify_human_blocker_items_classifies_bot_blocking_review() -> None:
    review = ReviewComment(
        comment_id="R-bot",
        body_excerpt="",
        author="review-bot[bot]",
        blocks_merge=True,
    )

    bot_items, human_items = _notify_human_blocker_items(
        _status(blocking_reviews=(review,)), MonitorState()
    )

    assert [item["id"] for item in bot_items] == ["R-bot"]
    assert bot_items[0]["verdict"] == "changes_requested"
    assert human_items == []


@pytest.mark.unit
async def test_human_notification_includes_effective_blocking_reviews_in_details_and_digest(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _RecordingGh()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    triaged_review = ReviewComment(
        comment_id="R-triaged",
        body_excerpt="Please update the release notes.",
        author="human-reviewer",
        blocks_merge=True,
        state="CHANGES_REQUESTED",
        url="https://github.example/reviews/R-triaged",
    )
    empty_review = ReviewComment(
        comment_id="R-empty",
        body_excerpt="",
        author="second-reviewer",
        blocks_merge=True,
        state="CHANGES_REQUESTED",
        url="https://github.example/reviews/R-empty",
    )
    status = _status(
        reviews=(triaged_review,),
        blocking_reviews=(triaged_review, empty_review),
    )
    state = MonitorState(threads_addressed_ids={"R-triaged": "fix_committed"})

    await runner._post_human_notification_once(
        repo=RepoRef(owner="example", name="repo"),
        pr_number=46,
        status=status,
        state=state,
    )

    bot_items, human_items = _notify_human_blocker_items(status, state)
    items = bot_items + human_items
    items_digest = _notification_items_digest(items)
    assert [item["id"] for item in human_items] == ["R-triaged", "R-empty"]
    assert all(item["verdict"] == "changes_requested" for item in human_items)
    assert len(gh.posts) == 1
    assert "https://github.example/reviews/R-triaged" in str(gh.posts[0]["body"])
    assert "https://github.example/reviews/R-empty" in str(gh.posts[0]["body"])
    assert (
        state.threads_addressed_ids[
            _notification_key(
                head_sha=status.head_sha,
                blocker_reason="a merge-blocking changes-requested review remains unresolved",
                items_digest=items_digest,
            )
        ]
        == "notified"
    )


@pytest.mark.unit
async def test_human_notification_dedup_includes_order_independent_item_ids(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _RecordingGh()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    first_thread = ReviewThread(
        thread_id="T-first",
        path="src/first.py",
        line=1,
        body_excerpt="first blocker",
        author="review-bot[bot]",
        url="https://github.example/reviews/T-first",
    )
    second_thread = ReviewThread(
        thread_id="T-second",
        path="src/second.py",
        line=2,
        body_excerpt="second blocker",
        author="review-bot[bot]",
        url="https://github.example/reviews/T-second",
    )
    state = MonitorState(
        threads_addressed_ids={"T-first": "needs_human", "T-second": "needs_human"}
    )
    first_status = _status(threads=(first_thread,))
    second_status = _status(threads=(first_thread, second_thread))

    await runner._post_human_notification_once(
        repo=RepoRef(owner="example", name="repo"),
        pr_number=46,
        status=first_status,
        state=state,
    )
    await runner._post_human_notification_once(
        repo=RepoRef(owner="example", name="repo"),
        pr_number=46,
        status=first_status,
        state=state,
    )
    await runner._post_human_notification_once(
        repo=RepoRef(owner="example", name="repo"),
        pr_number=46,
        status=second_status,
        state=state,
    )

    first_items, _ = _collect_defer_items(first_status, state)
    second_items, _ = _collect_defer_items(second_status, state)
    first_digest = _notification_items_digest(first_items)
    second_digest = _notification_items_digest(second_items)
    assert len(gh.posts) == 2
    assert first_digest != second_digest
    assert _notification_items_digest(tuple(reversed(second_items))) == second_digest
    assert (
        state.threads_addressed_ids[
            _notification_key(
                head_sha=first_status.head_sha,
                blocker_reason="review feedback needs human input and remains unresolved on GitHub",
                items_digest=first_digest,
            )
        ]
        == "notified"
    )
    assert _notification_key(head_sha=first_status.head_sha, blocker_reason="manual") == (
        f"__awf_notify__:{first_status.head_sha}:manual"
    )


@pytest.mark.unit
async def test_human_notification_dedup_includes_same_id_blocker_detail_changes(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    gh = _RecordingGh()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    state = MonitorState(threads_addressed_ids={"T-updated": "defer"})
    first_status = _status(
        threads=(
            ReviewThread(
                thread_id="T-updated",
                path="src/monitor.py",
                line=91,
                body_excerpt="Initial blocker detail.",
                author="human-reviewer",
                url="https://github.example/reviews/T-updated",
            ),
        )
    )
    second_status = _status(
        threads=(
            ReviewThread(
                thread_id="T-updated",
                path="src/monitor.py",
                line=91,
                body_excerpt="Updated blocker detail.",
                author="human-reviewer",
                url="https://github.example/reviews/T-updated",
            ),
        )
    )

    await runner._post_human_notification_once(
        repo=RepoRef(owner="example", name="repo"),
        pr_number=46,
        status=first_status,
        state=state,
    )
    await runner._post_human_notification_once(
        repo=RepoRef(owner="example", name="repo"),
        pr_number=46,
        status=second_status,
        state=state,
    )

    assert len(gh.posts) == 2
    assert "Initial blocker detail." in str(gh.posts[0]["body"])
    assert "Updated blocker detail." in str(gh.posts[1]["body"])


@pytest.mark.unit
def test_deferred_item_collection_keeps_existing_forge_urls() -> None:
    thread = ReviewThread(
        thread_id="T-url",
        path="src/thread.py",
        line=10,
        body_excerpt="thread",
        author="review-bot[bot]",
        url="https://github.example/reviews/T-url",
    )
    review = ReviewComment(
        comment_id="R-url",
        body_excerpt="review",
        author="octocat",
        url="https://github.example/reviews/R-url",
    )
    state = MonitorState(threads_addressed_ids={"T-url": "defer", "R-url": "defer"})

    bot_items, human_items = _collect_defer_items(
        _status(threads=(thread,), reviews=(review,)), state
    )

    assert bot_items[0]["url"] == thread.url
    assert human_items[0]["url"] == review.url


@pytest.mark.unit
@pytest.mark.parametrize(
    ("blocker_reason", "derived_reason"),
    (
        ("explicit human decision", "unused"),
        ('<what you need> and exit."', "re-derived human decision"),
        ('<what you need> and exit."', None),
    ),
)
async def test_human_notification_uses_the_same_items_and_digest_for_every_reason_fallback(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    blocker_reason: str,
    derived_reason: str | None,
) -> None:
    gh = _RecordingGh()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    bot_items = [
        {
            "kind": "thread",
            "id": "T-detail",
            "author": "review-bot[bot]",
            "path": "src/detail.py",
            "line": 4,
            "url": "https://github.example/reviews/T-detail",
            "body": "private blocker detail",
            "verdict": "needs_human",
            "agent_verdict_reason": None,
        }
    ]
    human_items = [
        {
            "kind": "review",
            "id": "R-detail",
            "author": "octocat",
            "path": None,
            "line": None,
            "url": "https://github.example/reviews/R-detail",
            "body": "human deferred detail",
            "verdict": "defer",
            "agent_verdict_reason": None,
        }
    ]
    rendered_items: list[tuple[dict[str, object], ...]] = []
    digests: list[str | None] = []

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.helpers._notify_human_blocker_items",
        lambda _status, _state: (bot_items, human_items),
    )
    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.helpers._notify_human_reason",
        lambda _status, _state, **_kwargs: derived_reason,
    )
    monkeypatch.setattr(
        pr_monitor_runner_comments,
        "ready_to_merge_comment",
        lambda **kwargs: rendered_items.append(tuple(kwargs["blocker_items"])) or "body",
    )
    original_key = _notification_key

    def _capture_key(**kwargs: object) -> str:
        digests.append(
            kwargs.get("items_digest") if isinstance(kwargs.get("items_digest"), str) else None
        )
        return original_key(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "awf.runtime.pr_monitor_runner.helpers._notification_key",
        _capture_key,
    )

    await runner._post_human_notification_once(
        repo=RepoRef(owner="example", name="repo"),
        pr_number=46,
        status=_status(),
        state=MonitorState(),
        blocker_reason=blocker_reason,
    )

    assert rendered_items == [tuple(bot_items + human_items)]
    assert digests == [_notification_items_digest(bot_items + human_items)]


class _NotifyHumanLoopRunner:
    def __init__(self) -> None:
        self._config = SimpleNamespace(auto_merge=True, poll_interval_seconds=0.0)
        self._deps = SimpleNamespace(sleep=self._sleep)
        self.attention_reasons: list[str] = []

    async def _sleep(self, _seconds: float) -> None:
        return None

    async def _begin_monitor_operation(self, **_kwargs: object) -> object:
        return object()

    async def _set_workspace_attention(self, _workspace_id: str, *, reason: str) -> None:
        self.attention_reasons.append(reason)

    async def _post_human_notification_once(self, **_kwargs: object) -> None:
        return None

    async def _clear_forge_transient_retry_state_on_success(self, **_kwargs: object) -> None:
        return None

    async def _finish_monitor_operation(self, _operation: object, **_kwargs: object) -> None:
        return None


@pytest.mark.unit
async def test_notify_human_workspace_attention_remains_a_compact_sentence() -> None:
    runner = _NotifyHumanLoopRunner()
    status = _status(
        threads=(
            ReviewThread(
                thread_id="T-detail",
                path="src/detail.py",
                line=4,
                body_excerpt="private blocker detail",
                author="review-bot[bot]",
            ),
        ),
    )
    state = MonitorState(threads_addressed_ids={"T-detail": "needs_human"})

    completed = await notify_human_loop.handle_notify_human_action(
        runner,
        action=NotifyHuman(message="review feedback needs human input"),
        workspace_id="ws_notify",
        repo_url="https://github.example/example/repo",
        repo=RepoRef(owner="example", name="repo"),
        pr_number=46,
        status=status,
        state=state,
        base_branch="main",
        remote_branch="feature",
        compose_project="awf-test",
        compose_file=Path("compose.yml"),
        monitor_log=None,
    )

    assert not completed
    assert runner.attention_reasons == ["review feedback needs human input"]
    attention = runner.attention_reasons[0]
    assert "Agent escalated" not in attention
    assert "[src/detail.py:4]" not in attention
    assert "private blocker detail" not in attention
