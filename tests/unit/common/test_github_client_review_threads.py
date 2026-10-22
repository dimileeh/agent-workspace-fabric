"""GitHubClient review-thread reply + exhaustive disposition audit (#1018)."""

from __future__ import annotations

import json

import pytest

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClient, GitHubClientError, RepoRef

pytestmark = pytest.mark.unit
_REPO = RepoRef(owner="acme", name="widgets")


def _audit_thread(
    tid: str,
    *,
    resolved: bool = True,
    resolved_by: str | None = "awf-operator",
    comments: list[dict[str, object]] | None = None,
    comments_next: str | None = None,
) -> dict[str, object]:
    return {
        "id": tid,
        "isResolved": resolved,
        "isOutdated": False,
        "resolvedBy": None if resolved_by is None else {"login": resolved_by},
        "comments": {
            "nodes": comments or [{"body": "P1: crash on None", "viewerDidAuthor": False}],
            "pageInfo": {"hasNextPage": comments_next is not None, "endCursor": comments_next},
        },
    }


def _audit_page(
    threads: list[dict[str, object]],
    *,
    cursor: str | None,
    viewer: str | None = "awf-operator",
) -> str:
    return json.dumps(
        {
            "data": {
                "viewer": None if viewer is None else {"login": viewer},
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": threads,
                            "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
                        }
                    }
                },
            }
        }
    )


async def test_reply_to_thread_posts_add_review_thread_reply_and_returns_id() -> None:
    fake = FakeCommandRunner()
    fake.queue_result(
        stdout=json.dumps(
            {
                "data": {
                    "addPullRequestReviewThreadReply": {
                        "comment": {"id": "PRRC_1", "databaseId": 991}
                    }
                }
            }
        )
    )
    reply_id = await GitHubClient(fake).reply_to_thread(thread_id="PRRT_test_a", body="hello")
    args = fake.calls[0].args
    assert args[:3] == ["gh", "api", "graphql"]
    assert any(a.startswith("query=") and "addPullRequestReviewThreadReply" in a for a in args)
    assert "threadId=PRRT_test_a" in args and "body=hello" in args
    assert reply_id == "991"
    # A mutation must never be blindly retried: one call, even on success.
    assert len(fake.calls) == 1


async def test_reply_to_thread_falls_back_to_node_id_without_database_id() -> None:
    fake = FakeCommandRunner()
    fake.queue_result(
        stdout=json.dumps(
            {"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "PRRC_node"}}}}
        )
    )
    reply_id = await GitHubClient(fake).reply_to_thread(thread_id="PRRT_test_a", body="x")
    assert reply_id == "PRRC_node"


async def test_reply_to_thread_raises_when_forge_returns_no_comment_id() -> None:
    fake = FakeCommandRunner()
    fake.queue_result(
        stdout=json.dumps({"data": {"addPullRequestReviewThreadReply": {"comment": None}}})
    )
    with pytest.raises(GitHubClientError) as excinfo:
        await GitHubClient(fake).reply_to_thread(thread_id="PRRT_test_a", body="x")
    assert excinfo.value.operation == "reply_to_thread"


async def test_reply_to_thread_raises_on_gh_failure() -> None:
    fake = FakeCommandRunner()
    fake.queue_result(returncode=1, stderr="forbidden")
    with pytest.raises(GitHubClientError):
        await GitHubClient(fake).reply_to_thread(thread_id="PRRT_test_a", body="x")
    assert len(fake.calls) == 1


async def test_audit_paginates_more_than_100_threads() -> None:
    fake = FakeCommandRunner()
    page1 = [_audit_thread(f"PRRT_test_{i:03d}") for i in range(100)]
    page2 = [_audit_thread(f"PRRT_test_{i:03d}") for i in range(100, 105)]
    fake.queue_result(stdout=_audit_page(page1, cursor="c1"))
    fake.queue_result(stdout=_audit_page(page2, cursor=None))
    audit = await GitHubClient(fake).fetch_review_thread_audit(repo=_REPO, pr_number=7)
    assert audit.supported is True
    assert audit.viewer_login == "awf-operator"
    assert len(audit.threads) == 105
    assert audit.threads[104].thread_id == "PRRT_test_104"
    assert audit.threads[0].is_resolved is True
    assert audit.threads[0].resolved_by == "awf-operator"
    assert "cursor=c1" in fake.calls[1].args
    assert not any(a.startswith("cursor=") for a in fake.calls[0].args)
    assert "number=7" in fake.calls[0].args


async def test_audit_fails_closed_on_missing_cursor() -> None:
    fake = FakeCommandRunner()
    page = json.loads(_audit_page([_audit_thread("PRRT_test_a")], cursor=None))
    page["data"]["repository"]["pullRequest"]["reviewThreads"]["pageInfo"] = {
        "hasNextPage": True,
        "endCursor": None,
    }
    fake.queue_result(stdout=json.dumps(page))
    with pytest.raises(GitHubClientError) as excinfo:
        await GitHubClient(fake).fetch_review_thread_audit(repo=_REPO, pr_number=7)
    assert "hasNextPage" in str(excinfo.value)


async def test_audit_fails_closed_on_missing_comment_cursor() -> None:
    fake = FakeCommandRunner()
    thread = _audit_thread("PRRT_test_a")
    thread["comments"] = {
        "nodes": [],
        "pageInfo": {"hasNextPage": True, "endCursor": None},
    }
    fake.queue_result(stdout=_audit_page([thread], cursor=None))
    with pytest.raises(GitHubClientError):
        await GitHubClient(fake).fetch_review_thread_audit(repo=_REPO, pr_number=7)


async def test_audit_raises_when_pull_request_missing() -> None:
    fake = FakeCommandRunner()
    fake.queue_result(
        stdout=json.dumps({"data": {"viewer": {"login": "a"}, "repository": {"pullRequest": None}}})
    )
    with pytest.raises(GitHubClientError) as excinfo:
        await GitHubClient(fake).fetch_review_thread_audit(repo=_REPO, pr_number=7)
    assert "acme/widgets#7" in str(excinfo.value)


async def test_audit_paginates_thread_comments_for_marker() -> None:
    fake = FakeCommandRunner()
    fake.queue_result(
        stdout=_audit_page([_audit_thread("PRRT_test_a", comments_next="k1")], cursor=None)
    )
    fake.queue_result(
        stdout=json.dumps(
            {
                "data": {
                    "node": {
                        "comments": {
                            "nodes": [
                                {"body": "<!-- marker on page 2 -->", "viewerDidAuthor": True}
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        )
    )
    audit = await GitHubClient(fake).fetch_review_thread_audit(repo=_REPO, pr_number=7)
    assert audit.threads[0].viewer_comment_bodies == ("<!-- marker on page 2 -->",)
    assert audit.threads[0].current_viewer_comment_bodies == ("<!-- marker on page 2 -->",)
    assert "threadId=PRRT_test_a" in fake.calls[1].args
    assert "cursor=k1" in fake.calls[1].args


async def test_audit_keeps_only_viewer_authored_bodies_and_null_resolver() -> None:
    fake = FakeCommandRunner()
    thread = _audit_thread(
        "PRRT_test_a",
        resolved_by=None,
        comments=[
            {
                "body": (
                    "reviewer text "
                    "<!-- AWF:thread-disposition:v1 class=fixed thread=PRRT_test_a -->"
                ),
                "viewerDidAuthor": False,
            },
            {"body": "awf reply", "viewerDidAuthor": True},
            {"body": None, "viewerDidAuthor": True},
        ],
    )
    fake.queue_result(stdout=_audit_page([thread, {"id": None}], cursor=None))
    audit = await GitHubClient(fake).fetch_review_thread_audit(repo=_REPO, pr_number=7)
    assert len(audit.threads) == 1
    assert audit.threads[0].resolved_by is None
    assert audit.threads[0].viewer_comment_bodies == ("awf reply",)


async def test_audit_reports_missing_viewer_login_as_none() -> None:
    fake = FakeCommandRunner()
    fake.queue_result(stdout=_audit_page([_audit_thread("PRRT_test_a")], cursor=None, viewer=None))
    audit = await GitHubClient(fake).fetch_review_thread_audit(repo=_REPO, pr_number=7)
    assert audit.supported is True
    assert audit.viewer_login is None


async def test_audit_marks_viewer_comments_older_than_the_newest_feedback() -> None:
    fake = FakeCommandRunner()
    thread = _audit_thread(
        "PRRT_test_a",
        comments=[
            {"body": "P1: crash on None", "viewerDidAuthor": False},
            {"body": "<!-- first disposition -->", "viewerDidAuthor": True},
            {"body": "still crashes", "viewerDidAuthor": False},
            {"body": "<!-- second disposition -->", "viewerDidAuthor": True},
            {"body": "and once more", "viewerDidAuthor": None},
        ],
    )
    fake.queue_result(stdout=_audit_page([thread], cursor=None))

    audited = (await GitHubClient(fake).fetch_review_thread_audit(repo=_REPO, pr_number=7)).threads

    # Comments arrive oldest first; feedback after a reply supersedes it.
    assert audited[0].viewer_comment_bodies == (
        "<!-- first disposition -->",
        "<!-- second disposition -->",
    )
    assert audited[0].superseded_viewer_comments == 2
    assert audited[0].current_viewer_comment_bodies == ()


async def _conversation_digest(comments: list[dict[str, object]]) -> str:
    fake = FakeCommandRunner()
    fake.queue_result(
        stdout=_audit_page([_audit_thread("PRRT_test_a", comments=comments)], cursor=None)
    )
    audit = await GitHubClient(fake).fetch_review_thread_audit(repo=_REPO, pr_number=7)
    return audit.threads[0].conversation_digest


async def test_audit_fingerprints_the_whole_thread_conversation() -> None:
    feedback = {"body": "P1: crash on None", "viewerDidAuthor": False}
    reply = {"body": "fixed", "viewerDidAuthor": True}
    original = await _conversation_digest([feedback, reply])

    # The same conversation keeps its digest; new feedback, a changed author side
    # or an edited comment each change it.
    assert original and original == await _conversation_digest([dict(feedback), dict(reply)])
    changed = {
        await _conversation_digest([feedback, reply, {"body": "still", "viewerDidAuthor": False}]),
        await _conversation_digest([feedback, {"body": "fixed", "viewerDidAuthor": False}]),
        await _conversation_digest([{"body": "P1: edited", "viewerDidAuthor": False}, reply]),
    }
    assert len(changed) == 3 and original not in changed


async def test_audit_supersedes_a_reply_posted_before_feedback_was_edited() -> None:
    fake = FakeCommandRunner()
    thread = _audit_thread(
        "PRRT_test_a",
        comments=[
            {
                "body": "P1: crash on None — and on empty input too",
                "viewerDidAuthor": False,
                "createdAt": "2026-10-22T10:00:00Z",
                "lastEditedAt": "2026-10-22T12:00:00Z",
            },
            {
                "body": "<!-- first disposition -->",
                "viewerDidAuthor": True,
                "createdAt": "2026-10-22T10:30:00Z",
            },
            {
                "body": "nit: rename",
                "viewerDidAuthor": False,
                "createdAt": "2026-10-22T10:45:00Z",
                "lastEditedAt": "2026-10-22T10:50:00Z",
            },
            {
                "body": "<!-- second disposition -->",
                "viewerDidAuthor": True,
                "createdAt": "2026-10-22T11:00:00Z",
            },
        ],
    )
    fake.queue_result(stdout=_audit_page([thread], cursor=None))

    audited = (await GitHubClient(fake).fetch_review_thread_audit(repo=_REPO, pr_number=7)).threads

    # The edit keeps the feedback's place in the list, but every reply answered the old text.
    assert audited[0].superseded_viewer_comments == 2
    assert audited[0].current_viewer_comment_bodies == ()
    query = next(a for a in fake.calls[0].args if a.startswith("query="))
    assert "createdAt" in query and "lastEditedAt" in query


async def test_audit_supersedes_a_reply_posted_before_the_shared_identity_edited_feedback() -> None:
    # AWF can run as the operator's own account and never edits its own replies, so
    # an edited viewer comment is the operator rewording their follow-up: a reply
    # posted before that edit answered the old text, one posted after it stands.
    fake = FakeCommandRunner()
    thread = _audit_thread(
        "PRRT_test_a",
        comments=[
            {
                "body": "P1: crash on None",
                "viewerDidAuthor": False,
                "createdAt": "2026-10-22T10:00:00Z",
            },
            {
                "body": "also handle the empty list — and None keys",
                "viewerDidAuthor": True,
                "createdAt": "2026-10-22T10:30:00Z",
                "lastEditedAt": "2026-10-22T12:00:00Z",
            },
            {
                "body": "<!-- stale disposition -->",
                "viewerDidAuthor": True,
                "createdAt": "2026-10-22T11:00:00Z",
            },
            {
                "body": "<!-- disposition -->",
                "viewerDidAuthor": True,
                "createdAt": "2026-10-22T13:00:00Z",
            },
        ],
    )
    fake.queue_result(stdout=_audit_page([thread], cursor=None))

    audited = (await GitHubClient(fake).fetch_review_thread_audit(repo=_REPO, pr_number=7)).threads

    assert audited[0].superseded_viewer_comments == 2
    assert audited[0].current_viewer_comment_bodies == ("<!-- disposition -->",)


async def test_audit_keeps_a_reply_posted_after_the_feedback_edit() -> None:
    fake = FakeCommandRunner()
    thread = _audit_thread(
        "PRRT_test_a",
        comments=[
            {
                "body": "P1: crash on None",
                "viewerDidAuthor": False,
                "createdAt": "2026-10-22T10:00:00Z",
                "lastEditedAt": "2026-10-22T10:30:00Z",
            },
            {
                "body": "<!-- stale disposition -->",
                "viewerDidAuthor": True,
                "createdAt": "2026-10-22T10:15:00Z",
            },
            {
                "body": "<!-- disposition -->",
                "viewerDidAuthor": True,
                "createdAt": "2026-10-22T11:00:00Z",
            },
        ],
    )
    fake.queue_result(stdout=_audit_page([thread], cursor=None))

    audited = (await GitHubClient(fake).fetch_review_thread_audit(repo=_REPO, pr_number=7)).threads

    assert audited[0].current_viewer_comment_bodies == ("<!-- disposition -->",)


async def test_audit_keeps_every_reply_when_the_feedback_was_edited_before_them() -> None:
    fake = FakeCommandRunner()
    thread = _audit_thread(
        "PRRT_test_a",
        comments=[
            {
                "body": "P1: crash on None",
                "viewerDidAuthor": False,
                "createdAt": "2026-10-22T10:00:00Z",
                "lastEditedAt": "2026-10-22T10:05:00Z",
            },
            {
                "body": "<!-- disposition -->",
                "viewerDidAuthor": True,
                "createdAt": "2026-10-22T11:00:00Z",
            },
        ],
    )
    fake.queue_result(stdout=_audit_page([thread], cursor=None))

    audited = (await GitHubClient(fake).fetch_review_thread_audit(repo=_REPO, pr_number=7)).threads

    assert audited[0].superseded_viewer_comments == 0
    assert audited[0].current_viewer_comment_bodies == ("<!-- disposition -->",)


async def test_audit_fails_closed_on_a_reply_with_no_posting_time_after_an_edit() -> None:
    fake = FakeCommandRunner()
    fake.queue_result(
        stdout=_audit_page([_audit_thread("PRRT_test_a", comments_next="k1")], cursor=None)
    )
    fake.queue_result(
        stdout=json.dumps(
            {
                "data": {
                    "node": {
                        "comments": {
                            "nodes": [
                                {"body": "<!-- undated -->", "viewerDidAuthor": True},
                                {
                                    "body": "P2: also the docs",
                                    "viewerDidAuthor": False,
                                    "createdAt": "2026-10-22T09:00:00Z",
                                    "lastEditedAt": "2026-10-22T09:30:00Z",
                                },
                                {
                                    "body": "<!-- disposition -->",
                                    "viewerDidAuthor": True,
                                    "createdAt": "2026-10-22T10:00:00Z",
                                },
                                {"body": "<!-- also undated -->", "viewerDidAuthor": True},
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        )
    )

    audited = (await GitHubClient(fake).fetch_review_thread_audit(repo=_REPO, pr_number=7)).threads

    # A reply AWF cannot place after the newest edit is not proof it answered it.
    assert audited[0].superseded_viewer_comments == 3
    assert audited[0].current_viewer_comment_bodies == ()
    query = next(a for a in fake.calls[1].args if a.startswith("query="))
    assert "createdAt" in query and "lastEditedAt" in query
