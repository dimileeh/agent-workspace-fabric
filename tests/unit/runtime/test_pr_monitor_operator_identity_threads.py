"""Runner-level scenarios for shared operator/monitor forge identity (#1272).

(A) A thread opened by the identity is addressed and resolved with a marked reply.
(B) A thread the identity resolved by hand is re-opened, re-triaged with the
    identity's reply read, and resolved with a marker.
(C) An identity reply inside a bot thread, present at triage, does not park.

Uses only existing harnesses: ``make_runner``, ``pr_payload``,
``DefaultMergeMethodGitHubClient``, ``ReopenForge``, ``audit_of``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.forge_thread_audit import AuditedReviewThread
from awf.db.session import make_session_factory
from awf.runtime.monitor_state_keys import (
    _disposition_auto_reopen_key,
    _disposition_unverified_key,
)
from awf.runtime.pr_monitor import (
    AddressComments,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    ReviewThread,
    ReviewThreadComment,
    decide,
)
from awf.runtime.pr_monitor_runner.comment_verdict_protocol_types import VerdictResult
from awf.runtime.pr_monitor_runner.thread_disposition import (
    read_item_disposition,
    record_item_disposition,
    retire_reopened_dispositions,
    sync_item_disposition,
)
from awf.runtime.pr_monitor_runner.thread_disposition_reply import reply_before_resolve
from tests.postgres import postgres_test_engine
from tests.shared.monitor_runner import DefaultMergeMethodGitHubClient
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
)
from tests.unit.runtime._thread_disposition_fixtures import (
    OrderedForge,
    seed_resolvable_disposition,
    settled_baseline_state,
)
from tests.unit.runtime.test_pr_monitor_thread_disposition_fix_cycle import (
    _MARKER,
    _REPO,
    _run_cycle,
)
from tests.unit.runtime.test_pr_monitor_thread_disposition_gate import (
    _VIEWER,
    _execute,
    _gate,
    _green_status,
    _marker,
    _release_runner,
)
from tests.unit.runtime.test_pr_monitor_thread_disposition_reopen import ReopenForge, audit_of

pytestmark = pytest.mark.unit

_ASK = "please guard None payloads"
_OPERATOR = "operator note inside the thread"
_REASON = "the caller already rejects None payloads"
_TID = "PRRT_test_operator_identity"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _identity_opener_thread(tid: str = _TID) -> ReviewThread:
    return ReviewThread(
        thread_id=tid,
        path="widgets/core.py",
        line=12,
        body_excerpt=_ASK,
        author=_VIEWER,
        comments=(
            ReviewThreadComment(
                comment_id="own-0",
                body=_ASK,
                author=_VIEWER,
                viewer_did_author=True,
            ),
        ),
    )


async def test_scenario_a_identity_opened_thread_is_addressed_and_resolved_with_marker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(A) Identity opener is listed, read at triage, and resolved with a marker."""
    gh = OrderedForge(FakeCommandRunner())
    thread = _identity_opener_thread()
    marker = _MARKER.format(cls="false_positive", tid=_TID)

    async def _audit(*, repo, pr_number, retry=True, thread_id=None):  # noqa: ANN001
        del repo, pr_number, retry
        assert thread_id == _TID
        posted = [body for replied_to, body in gh.replies() if replied_to == _TID]
        assert posted
        return audit_of(
            AuditedReviewThread(
                thread_id=_TID,
                is_resolved=False,
                is_outdated=False,
                resolved_by=None,
                viewer_comment_bodies=(_ASK, posted[0]),
                viewer_comment_ids=("own-0", "reply-1"),
                external_comment_bodies=(),
                conversation_digest_before_reply="d" * 64,
            )
        )

    monkeypatch.setattr(gh, "fetch_review_thread_audit", _audit)

    def _seed(state: MonitorState, addressed: ReviewThread) -> None:
        disposition = seed_resolvable_disposition(state, addressed.thread_id, "false_positive")
        record_item_disposition(
            state,
            replace(disposition, read_own_comment_ids=("own-0",)),
        )

    _, state, _ = await _run_cycle(
        factory=factory,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        gh=gh,
        verdict="false_positive",
        on_address=_seed,
        thread=thread,
        settle_threads=(thread,),
    )

    assert [event[0] for event in gh.events] == ["reply", "resolve"]
    assert state.threads_addressed_ids[_TID] == "false_positive"
    assert (
        marker.split("class=")[0] in gh.events[0][2] or "thread-disposition:v1" in gh.events[0][2]
    )
    recorded = read_item_disposition(state, _TID)
    assert recorded is not None
    assert recorded.read_own_comment_ids == ("own-0",)
    assert recorded.reply_comment_id == "reply-1"


async def test_scenario_b_hand_resolve_reopens_retriages_and_resolves_with_marker(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """(B) Hand resolve by the identity still auto-reopens; re-triage reads and marks."""
    # Hand resolve: viewer resolved without a disposition marker (#1018 unchanged).
    hand_resolved = AuditedReviewThread(
        thread_id=_TID,
        is_resolved=True,
        is_outdated=False,
        resolved_by=_VIEWER,
        viewer_comment_bodies=(_OPERATOR,),
        viewer_comment_ids=("own-1",),
        external_comment_bodies=(_ASK,),
        conversation_digest="c" * 64,
    )
    gh = ReopenForge(FakeCommandRunner(), audit=audit_of(hand_resolved))
    workspace_id, runner = await _release_runner(factory, tmp_path, gh)
    state = settled_baseline_state()

    terminal = await _execute(runner, workspace_id, NotifyHuman(), state)

    assert terminal is False
    assert gh.unresolved == [_TID]
    assert state.threads_addressed_ids[_disposition_auto_reopen_key(_TID)] == "1"
    assert state.threads_addressed_ids[_disposition_unverified_key(_TID)] == (
        "REVIEW_THREAD_DISPOSITION_UNVERIFIED"
    )

    # Next poll lists the reopened thread with the identity reply present.
    listed = ReviewThread(
        thread_id=_TID,
        path="widgets/core.py",
        line=12,
        body_excerpt=_ASK,
        author="review-bot",
        comments=(
            ReviewThreadComment(
                comment_id="c1", body=_ASK, author="review-bot", viewer_did_author=False
            ),
            ReviewThreadComment(
                comment_id="own-1", body=_OPERATOR, author=_VIEWER, viewer_did_author=True
            ),
        ),
    )
    reopened = replace(_green_status(), unresolved_inline_threads=(listed,))
    retire_reopened_dispositions(state, reopened, workspace_id=workspace_id)
    action = decide(reopened, state, MonitorConfig(auto_merge=False))
    assert isinstance(action, AddressComments)
    assert [t.thread_id for t in action.threads] == [_TID]

    state.mark_addressed(_TID, "false_positive")
    sync_item_disposition(
        state,
        _TID,
        VerdictResult(verdict="false_positive", reason=_REASON),
        thread=listed,
    )
    recorded = read_item_disposition(state, _TID)
    assert recorded is not None
    assert recorded.read_own_comment_ids == ("own-1",)

    # Re-read after the fresh marker sees the already-read identity reply.
    async def _audit_after_reply(*, repo, pr_number, retry=True, thread_id=None):  # noqa: ANN001
        del repo, pr_number, retry, thread_id
        posted = gh.thread_replies[-1][1] if gh.thread_replies else _marker("false_positive", _TID)
        return audit_of(
            AuditedReviewThread(
                thread_id=_TID,
                is_resolved=False,
                is_outdated=False,
                resolved_by=None,
                viewer_comment_bodies=(_OPERATOR, posted),
                viewer_comment_ids=("own-1", "reply-1"),
                external_comment_bodies=(_ASK,),
                conversation_digest_before_reply="d" * 64,
            )
        )

    gh.fetch_review_thread_audit = _audit_after_reply  # type: ignore[method-assign]
    replied = await reply_before_resolve(
        SimpleNamespace(_monitor_owner_id=None, _deps=runner._deps),
        workspace_id=workspace_id,
        pr_number=42,
        thread_id=_TID,
        state=state,
        action="resolve_thread",
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        thread=listed,
        repo=_REPO,
    )
    assert replied is not None
    assert replied.reply_comment_id == "reply-1"
    assert "thread-disposition:v1" in gh.thread_replies[0][1]

    # Restore the canned audit for the cert gate (resolved + marker).
    del gh.fetch_review_thread_audit
    gh.audit = audit_of(
        AuditedReviewThread(
            thread_id=_TID,
            is_resolved=True,
            is_outdated=False,
            resolved_by=_VIEWER,
            viewer_comment_bodies=(_OPERATOR, gh.thread_replies[0][1]),
            viewer_comment_ids=("own-1", "reply-1"),
            external_comment_bodies=(_ASK,),
            conversation_digest="e" * 64,
            conversation_digest_before_reply="d" * 64,
        )
    )
    certified = await _gate(runner, workspace_id, state, status=reopened)
    assert certified.blocker is None and certified.count_rows is not None
    assert dict(certified.count_rows)["false positive"] >= 1


async def test_scenario_c_identity_reply_in_bot_thread_at_triage_does_not_park(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """(C) Identity reply present at triage is skipped; disposition resolve lands."""
    cmd = FakeCommandRunner()
    # Status feed lists the bot opener plus the identity reply (and selects body).
    cmd.queue_result(
        returncode=0,
        stdout=pr_payload(
            threads=[
                {
                    "id": _TID,
                    "isResolved": False,
                    "isOutdated": False,
                    "path": "widgets/core.py",
                    "line": 12,
                    "comments": {
                        "nodes": [
                            {
                                "databaseId": 101,
                                "body": _ASK,
                                "bodyText": _ASK,
                                "author": {"login": "review-bot"},
                                "viewerDidAuthor": False,
                            },
                            {
                                "databaseId": 102,
                                "body": _OPERATOR,
                                "bodyText": _OPERATOR,
                                "author": {"login": _VIEWER},
                                "viewerDidAuthor": True,
                            },
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            ]
        ),
    )
    gh = DefaultMergeMethodGitHubClient(cmd)
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )
    status = await gh.fetch_pr_status(repo=_REPO, pr_number=42, base_behind_count=0)
    listed = status.unresolved_inline_threads[0]
    assert [c.viewer_did_author for c in listed.comments] == [False, True]
    assert [c.comment_id for c in listed.comments] == ["101", "102"]

    state = MonitorState()
    state.mark_addressed(_TID, "false_positive")
    sync_item_disposition(
        state,
        _TID,
        VerdictResult(verdict="false_positive", reason=_REASON),
        thread=listed,
    )
    recorded = read_item_disposition(state, _TID)
    assert recorded is not None
    assert recorded.read_own_comment_ids == ("102",)
    assert recorded.reason == _REASON

    replied = await reply_before_resolve(
        runner,
        workspace_id=workspace_id,
        pr_number=42,
        thread_id=_TID,
        state=state,
        action="resolve_thread",
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        thread=listed,
        repo=_REPO,
    )
    assert replied is not None
    assert replied.reply_comment_id is not None
    assert state.threads_addressed_ids.get(_TID) == "false_positive"
    assert not any(
        str(v).startswith("THREAD_DISPOSITION_UNREAD_FEEDBACK")
        for v in state.threads_addressed_ids.values()
    )


async def test_scenario_c_uses_pr_payload_body_field_in_status_queries() -> None:
    """pr_payload fixtures used above exercise the body-aware status path."""
    payload = pr_payload(
        threads=[
            {
                "id": "T",
                "isResolved": False,
                "isOutdated": False,
                "path": "a.py",
                "line": 1,
                "comments": {
                    "nodes": [
                        {
                            "databaseId": 1,
                            "body": "raw",
                            "bodyText": "raw",
                            "viewerDidAuthor": False,
                        }
                    ]
                },
            }
        ]
    )
    assert '"body": "raw"' in payload
    assert '"bodyText": "raw"' in payload
