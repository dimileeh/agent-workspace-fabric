"""GitHub audit: the thread's opener and the comments after AWF's newest reply (#1138).

The disposition gate's bot-acknowledgement check needs to know who opened a
thread, which comments followed the newest comment by AWF's identity (with
author type and comment identity), and how the thread reads without them.
"""

from __future__ import annotations

import pytest

from awf.common.commands import FakeCommandRunner
from awf.common.forge_thread_audit import AuditedReviewThread, AuditedThreadComment
from awf.common.github_client import GitHubClient
from awf.common.github_graphql import (
    _GQL_REVIEW_THREAD_AUDIT,
    _GQL_REVIEW_THREAD_AUDIT_COMMENTS_PAGE,
    _GQL_REVIEW_THREAD_AUDIT_ONE,
)
from tests.unit.common.test_github_client_review_threads import (
    _REPO,
    _audit_page,
    _audit_thread,
)

pytestmark = pytest.mark.unit

_BOT = {"__typename": "Bot", "login": "review-bot"}
_HUMAN = {"__typename": "User", "login": "octocat"}
_FEEDBACK = {
    "id": "PRRC_1",
    "databaseId": 11,
    "body": "P1: crash on None",
    "bodyText": "P1: crash on None",
    "viewerDidAuthor": False,
    "author": _BOT,
    "createdAt": "2026-10-22T10:00:00Z",
}
_REPLY = {
    "id": "PRRC_2",
    "databaseId": 12,
    "body": "<!-- marker -->",
    "viewerDidAuthor": True,
    "author": {"__typename": "User", "login": "awf-operator"},
    "createdAt": "2026-10-22T10:30:00Z",
}
_ACK = {
    "id": "PRRC_3",
    "databaseId": 13,
    "body": "Agreed.",
    "bodyText": "Agreed.",
    "viewerDidAuthor": False,
    "author": _BOT,
    "createdAt": "2026-10-22T11:00:00Z",
}


async def _audited(comments: list[dict[str, object]]) -> AuditedReviewThread:
    fake = FakeCommandRunner()
    fake.queue_result(
        stdout=_audit_page([_audit_thread("PRRT_test_a", comments=comments)], cursor=None)
    )
    audit = await GitHubClient(fake).fetch_review_thread_audit(repo=_REPO, pr_number=7)
    return audit.threads[0]


@pytest.mark.parametrize(
    "query",
    [
        _GQL_REVIEW_THREAD_AUDIT,
        _GQL_REVIEW_THREAD_AUDIT_ONE,
        _GQL_REVIEW_THREAD_AUDIT_COMMENTS_PAGE,
    ],
)
def test_audit_queries_select_each_comment_author_type_and_login(query: str) -> None:
    assert "author { __typename login }" in query


async def test_audit_reports_the_opener_and_the_comments_after_awfs_newest_reply() -> None:
    thread = await _audited([_FEEDBACK, _REPLY, _ACK])

    assert thread.opener == AuditedThreadComment(
        comment_id="11", author_login="review-bot", author_is_bot=True, body="P1: crash on None"
    )
    assert thread.trailing_comments == (
        AuditedThreadComment(
            comment_id="13", author_login="review-bot", author_is_bot=True, body="Agreed."
        ),
    )


async def test_the_view_without_trailing_comments_reads_as_the_thread_before_them() -> None:
    full = await _audited([_FEEDBACK, _REPLY, _ACK])
    before = await _audited([_FEEDBACK, _REPLY])

    view = full.without_trailing
    assert view is not None
    assert view.conversation_digest == before.conversation_digest
    assert view.conversation_digest_before_reply == before.conversation_digest_before_reply
    assert view.superseded_viewer_comments == before.superseded_viewer_comments == 0
    assert view.external_comment_bodies == before.external_comment_bodies
    assert view.viewer_comment_ids == ("12",)
    assert (view.is_resolved, view.resolved_by) == (full.is_resolved, full.resolved_by)
    assert view.trailing_comments == () and view.without_trailing is None
    # The full thread still reads the reply as superseded, as before.
    assert full.superseded_viewer_comments == 1
    assert full.conversation_digest_before_reply == ""
    assert before.without_trailing is None and before.trailing_comments == ()


async def test_an_edit_to_a_trailing_comment_is_ignored_in_the_view_only() -> None:
    edited_ack = {**_ACK, "lastEditedAt": "2026-10-22T12:00:00Z"}
    full = await _audited([_FEEDBACK, _REPLY, edited_ack])

    assert full.without_trailing is not None
    assert full.without_trailing.superseded_viewer_comments == 0
    assert full.superseded_viewer_comments == 1


async def test_an_edit_to_earlier_feedback_still_supersedes_the_reply_in_the_view() -> None:
    edited_feedback = {**_FEEDBACK, "lastEditedAt": "2026-10-22T10:45:00Z"}
    full = await _audited([edited_feedback, _REPLY, _ACK])

    assert full.without_trailing is not None
    assert full.without_trailing.superseded_viewer_comments == 1


async def test_a_deleted_or_human_author_is_not_a_bot() -> None:
    ghost = {**_ACK, "author": None}
    human = {**_ACK, "id": "PRRC_4", "databaseId": 14, "author": _HUMAN}

    thread = await _audited([{**_FEEDBACK, "author": None}, _REPLY, ghost, human])

    assert thread.opener is not None
    assert (thread.opener.author_login, thread.opener.author_is_bot) == (None, False)
    assert [(c.author_login, c.author_is_bot) for c in thread.trailing_comments] == [
        (None, False),
        ("octocat", False),
    ]


async def test_a_thread_without_awfs_reply_has_no_trailing_comments() -> None:
    thread = await _audited([_FEEDBACK, _ACK])

    assert thread.opener is not None and thread.opener.comment_id == "11"
    assert thread.trailing_comments == ()
    assert thread.without_trailing is None


async def test_the_full_thread_digests_are_unchanged_by_the_author_field() -> None:
    with_author = await _audited([_FEEDBACK, _REPLY])
    without_author = await _audited(
        [{k: v for k, v in comment.items() if k != "author"} for comment in (_FEEDBACK, _REPLY)]
    )

    assert with_author.conversation_digest == without_author.conversation_digest
    assert (
        with_author.conversation_digest_before_reply
        == without_author.conversation_digest_before_reply
    )
