"""Status-feed listing of unmarked identity review-thread comments (#1272).

Parts 002/003 are near the 1500-line maintainability limit; new client cases
for operator-identity threads live here.
"""

from __future__ import annotations

import json

import pytest

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClient, RepoRef
from awf.common.github_graphql import (
    _GQL_PR_REVIEW_THREADS_PAGE,
    _GQL_PR_STATE,
    _GQL_REVIEW_THREAD_COMMENTS_PAGE,
)


def _marker(*, cls: str = "fixed", thread: str = "T_marked") -> str:
    return f"<!-- AWF:thread-disposition:v1 class={cls} thread={thread} -->"


def _sample_pr_payload(*, threads: list[dict]) -> str:
    return json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": 42,
                        "createdAt": "2026-05-06T10:00:00Z",
                        "updatedAt": "2026-05-06T10:00:00Z",
                        "headRefName": "feature/current-head",
                        "headRefOid": "abc123",
                        "mergeable": "MERGEABLE",
                        "mergeStateStatus": "CLEAN",
                        "isDraft": False,
                        "closed": False,
                        "merged": False,
                        "mergeCommit": None,
                        "baseRef": {"name": "development", "target": {"oid": "base0"}},
                        "commits": {
                            "nodes": [
                                {
                                    "commit": {
                                        "statusCheckRollup": {
                                            "state": "SUCCESS",
                                            "contexts": {"nodes": [], "pageInfo": {}},
                                        },
                                        "committedDate": "2026-05-06T10:00:00Z",
                                    }
                                }
                            ]
                        },
                        "reviewThreads": {
                            "nodes": threads,
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        },
                        "reviews": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                        "comments": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                        "files": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                    }
                }
            }
        }
    )


def _thread(
    thread_id: str,
    *,
    comments: list[dict],
    is_outdated: bool = False,
    has_next_page: bool = False,
    end_cursor: str | None = None,
) -> dict:
    return {
        "id": thread_id,
        "isResolved": False,
        "isOutdated": is_outdated,
        "path": "src/x.py",
        "line": 10,
        "comments": {
            "nodes": comments,
            "pageInfo": {"hasNextPage": has_next_page, "endCursor": end_cursor},
        },
    }


@pytest.mark.unit
async def test_lists_unmarked_identity_opener_with_viewer_flag() -> None:
    fake = FakeCommandRunner()
    fake.queue_result(
        returncode=0,
        stdout=_sample_pr_payload(
            threads=[
                _thread(
                    "T_self",
                    comments=[
                        {
                            "databaseId": 201,
                            "body": "Operator opened this thread.",
                            "bodyText": "Operator opened this thread.",
                            "author": {"login": "token-owner"},
                            "viewerDidAuthor": True,
                        }
                    ],
                )
            ]
        ),
    )
    status = await GitHubClient(fake).fetch_pr_status(
        repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
    )

    assert [t.thread_id for t in status.unresolved_inline_threads] == ["T_self"]
    comment = status.unresolved_inline_threads[0].comments[0]
    assert comment.comment_id == "201"
    assert comment.body == "Operator opened this thread."
    assert comment.viewer_did_author is True


@pytest.mark.unit
async def test_drops_marked_disposition_reply_from_status_feed() -> None:
    fake = FakeCommandRunner()
    marker = _marker(thread="T_mixed")
    fake.queue_result(
        returncode=0,
        stdout=_sample_pr_payload(
            threads=[
                _thread(
                    "T_mixed",
                    comments=[
                        {
                            "databaseId": 101,
                            "body": "Please fix this.",
                            "bodyText": "Please fix this.",
                            "author": {"login": "reviewer"},
                            "viewerDidAuthor": False,
                        },
                        {
                            "databaseId": 102,
                            "body": f"{marker}\n**AWF disposition: fixed.**",
                            "bodyText": "**AWF disposition: fixed.**",
                            "author": {"login": "token-owner"},
                            "viewerDidAuthor": True,
                        },
                    ],
                )
            ]
        ),
    )
    status = await GitHubClient(fake).fetch_pr_status(
        repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
    )

    thread = status.unresolved_inline_threads[0]
    assert [c.comment_id for c in thread.comments] == ["101"]
    assert thread.comments[0].viewer_did_author is False


@pytest.mark.unit
async def test_skips_thread_that_only_has_marked_reply() -> None:
    fake = FakeCommandRunner()
    marker = _marker(thread="T_marker_only")
    fake.queue_result(
        returncode=0,
        stdout=_sample_pr_payload(
            threads=[
                _thread(
                    "T_marker_only",
                    comments=[
                        {
                            "databaseId": 301,
                            "body": f"{marker}\n**AWF disposition: fixed.**",
                            "bodyText": "**AWF disposition: fixed.**",
                            "author": {"login": "token-owner"},
                            "viewerDidAuthor": True,
                        }
                    ],
                )
            ]
        ),
    )
    status = await GitHubClient(fake).fetch_pr_status(
        repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
    )

    assert status.unresolved_inline_threads == ()
    assert status.outdated_unresolved_inline_threads == ()


@pytest.mark.unit
async def test_keeps_marker_naming_another_thread() -> None:
    fake = FakeCommandRunner()
    wrong = _marker(thread="T_other")
    fake.queue_result(
        returncode=0,
        stdout=_sample_pr_payload(
            threads=[
                _thread(
                    "T_wrong_marker",
                    comments=[
                        {
                            "databaseId": 401,
                            "body": f"{wrong}\npasted from elsewhere",
                            "bodyText": "pasted from elsewhere",
                            "author": {"login": "token-owner"},
                            "viewerDidAuthor": True,
                        }
                    ],
                )
            ]
        ),
    )
    status = await GitHubClient(fake).fetch_pr_status(
        repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
    )

    thread = status.unresolved_inline_threads[0]
    assert thread.thread_id == "T_wrong_marker"
    assert thread.comments[0].comment_id == "401"
    assert thread.comments[0].viewer_did_author is True
    # Identity listed bodies keep raw markdown so read_own matches audit
    # viewer_comment_bodies (hashes still skip viewer_did_author rows).
    assert thread.comments[0].body == f"{wrong}\npasted from elsewhere"


@pytest.mark.unit
async def test_identity_comment_keeps_raw_markdown_when_body_text_strips() -> None:
    """Unmarked identity bodies must match audit viewer_comment_bodies (#1272).

    GraphQL bodyText strips markdown (e.g. backticks). Persisting bodyText while
    the disposition re-read compares raw body falsely treats unchanged feedback
    as a post-triage edit (PRRT_kwDOSJAM6s644y23).
    """
    raw = "Please guard `None` payloads"
    plain = "Please guard None payloads"
    fake = FakeCommandRunner()
    fake.queue_result(
        returncode=0,
        stdout=_sample_pr_payload(
            threads=[
                _thread(
                    "T_md_identity",
                    comments=[
                        {
                            "databaseId": 801,
                            "body": raw,
                            "bodyText": plain,
                            "author": {"login": "token-owner"},
                            "viewerDidAuthor": True,
                        }
                    ],
                )
            ]
        ),
    )
    status = await GitHubClient(fake).fetch_pr_status(
        repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
    )

    comment = status.unresolved_inline_threads[0].comments[0]
    assert comment.viewer_did_author is True
    assert comment.body == raw
    assert comment.body != plain


@pytest.mark.unit
async def test_page_two_unmarked_identity_comment_is_listed() -> None:
    fake = FakeCommandRunner()
    fake.queue_result(
        returncode=0,
        stdout=_sample_pr_payload(
            threads=[
                _thread(
                    "T_page2",
                    comments=[
                        {
                            "databaseId": 501,
                            "body": "first page",
                            "bodyText": "first page",
                            "author": {"login": "reviewer"},
                            "viewerDidAuthor": False,
                        }
                    ],
                    has_next_page=True,
                    end_cursor="cursor-1",
                )
            ]
        ),
    )
    fake.queue_result(
        returncode=0,
        stdout=json.dumps(
            {
                "data": {
                    "node": {
                        "comments": {
                            "nodes": [
                                {
                                    "databaseId": 502,
                                    "body": "operator follow-up on page two",
                                    "bodyText": "operator follow-up on page two",
                                    "author": {"login": "token-owner"},
                                    "viewerDidAuthor": True,
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        ),
    )
    status = await GitHubClient(fake).fetch_pr_status(
        repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
    )

    comments = status.unresolved_inline_threads[0].comments
    assert [c.comment_id for c in comments] == ["501", "502"]
    assert [c.viewer_did_author for c in comments] == [False, True]


@pytest.mark.unit
async def test_outdated_unmarked_identity_thread_routes_to_outdated_feed() -> None:
    fake = FakeCommandRunner()
    fake.queue_result(
        returncode=0,
        stdout=_sample_pr_payload(
            threads=[
                _thread(
                    "T_outdated_self",
                    is_outdated=True,
                    comments=[
                        {
                            "databaseId": 601,
                            "body": "outdated self note",
                            "bodyText": "outdated self note",
                            "author": {"login": "token-owner"},
                            "viewerDidAuthor": True,
                        }
                    ],
                )
            ]
        ),
    )
    status = await GitHubClient(fake).fetch_pr_status(
        repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
    )

    assert status.unresolved_inline_threads == ()
    assert [t.thread_id for t in status.outdated_unresolved_inline_threads] == ["T_outdated_self"]
    assert status.outdated_unresolved_inline_threads[0].comments[0].viewer_did_author is True


@pytest.mark.unit
async def test_html_comment_only_outside_comment_keeps_empty_body_text() -> None:
    """HTML-only markdown yields empty bodyText; listed body must stay empty.

    Falling back to raw ``body`` would diverge from audit ``external_comment_bodies``
    (which records ``bodyText or ""``), reject an unchanged conversation, and
    rewrite previously saved hashes for those comments.
    """
    fake = FakeCommandRunner()
    fake.queue_result(
        returncode=0,
        stdout=_sample_pr_payload(
            threads=[
                _thread(
                    "T_html_only",
                    comments=[
                        {
                            "databaseId": 701,
                            "body": "<!-- greptile-hidden note -->",
                            "bodyText": "",
                            "author": {"login": "reviewer-bot"},
                            "viewerDidAuthor": False,
                        }
                    ],
                )
            ]
        ),
    )
    status = await GitHubClient(fake).fetch_pr_status(
        repo=RepoRef(owner="o", name="r"), pr_number=1, base_behind_count=0
    )

    comment = status.unresolved_inline_threads[0].comments[0]
    assert comment.comment_id == "701"
    assert comment.body == ""
    assert comment.viewer_did_author is False


@pytest.mark.unit
def test_status_feed_queries_select_raw_body_next_to_body_text() -> None:
    for query in (_GQL_PR_STATE, _GQL_PR_REVIEW_THREADS_PAGE, _GQL_REVIEW_THREAD_COMMENTS_PAGE):
        # Review-thread comment selections must see HTML-comment markers.
        assert "bodyText" in query
        assert "body" in query
        # body must appear as its own field, not only inside bodyText.
        assert (
            "body\n" in query or "body " in query or query.count("body") > query.count("bodyText")
        )
