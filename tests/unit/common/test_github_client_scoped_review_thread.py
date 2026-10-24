"""A post-reply audit reads only the requested, repository-bound thread."""

from __future__ import annotations

import json
from typing import Any

import pytest

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClient, GitHubClientError
from tests.unit.common.test_github_client_review_threads import (
    _REPO,
    _audit_page,
    _audit_thread,
    _RecordedSleep,
)

pytestmark = pytest.mark.unit
_TID = "PRRT_target"


def _target(*, cursor: str | None = None) -> dict[str, Any]:
    return {
        **_audit_thread(
            _TID,
            comments=[{"body": "question", "bodyText": "question", "viewerDidAuthor": False}],
            comments_next=cursor,
        ),
        "pullRequest": {"number": 7, "repository": {"nameWithOwner": "acme/widgets"}},
    }


def _page(node: object) -> str:
    return json.dumps({"data": {"viewer": {"login": "awf-operator"}, "node": node}})


async def test_scoped_audit_reuses_full_conversion_without_reading_other_threads() -> None:
    target = _target(cursor="comments-2")
    target["comments"]["nodes"] = [
        {
            "id": f"comment-{number}",
            "body": f"question {number}",
            "bodyText": f"question {number}",
            "viewerDidAuthor": False,
        }
        for number in range(100)
    ]
    last_page = {
        "comments": {
            "nodes": [
                {"id": "own-feedback", "body": "unread", "viewerDidAuthor": True},
                {"databaseId": 991, "body": "reply", "viewerDidAuthor": True},
            ],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }
    }
    full = FakeCommandRunner()
    full.queue_result(stdout=_audit_page([target, _audit_thread("PRRT_other")], cursor=None))
    full.queue_result(stdout=_page(last_page))
    expected = await GitHubClient(full).fetch_review_thread_audit(repo=_REPO, pr_number=7)

    scoped = FakeCommandRunner()
    scoped.queue_result(stdout=_page(target))
    scoped.queue_result(stdout=_page(last_page))
    actual = await GitHubClient(scoped).fetch_review_thread_audit(
        repo=_REPO, pr_number=7, thread_id=_TID
    )

    assert actual.supported and actual.viewer_login == expected.viewer_login
    assert actual.threads == expected.threads[:1]
    assert actual.threads[0].viewer_comment_ids == ("own-feedback", "991")
    assert actual.threads[0].external_comment_bodies == tuple(
        f"question {number}" for number in range(100)
    )
    assert len(scoped.calls) == 2
    for call in scoped.calls:
        assert f"threadId={_TID}" in call.args
        query = next(arg for arg in call.args if arg.startswith("query="))
        assert "reviewThreads(" not in query
    query = next(arg for arg in scoped.calls[0].args if arg.startswith("query="))
    assert "pullRequest" in query and "nameWithOwner" in query and "number" in query
    assert "cursor=comments-2" in scoped.calls[1].args


@pytest.mark.parametrize(
    "cause", ["missing", "wrong-type", "wrong-id", "wrong-pr", "wrong-repo", "no-binding"]
)
async def test_scoped_audit_rejects_missing_or_unbound_target(cause: str) -> None:
    node: dict[str, Any] | None = _target()
    if cause == "missing":
        node = None
    elif cause == "wrong-type":
        node = {"id": _TID}
    elif cause == "wrong-id":
        node["id"] = "PRRT_other"
    elif cause == "wrong-pr":
        node["pullRequest"]["number"] = 8
    elif cause == "wrong-repo":
        node["pullRequest"]["repository"]["nameWithOwner"] = "foreign/widgets"
    else:
        node["pullRequest"] = None
    fake = FakeCommandRunner()
    fake.queue_result(stdout=_page(node))
    with pytest.raises(GitHubClientError):
        await GitHubClient(fake).fetch_review_thread_audit(repo=_REPO, pr_number=7, thread_id=_TID)
    assert len(fake.calls) == 1


@pytest.mark.parametrize("later_page", [False, True], ids=["initial", "continuation"])
@pytest.mark.parametrize("cause", ["missing-node", "missing-comments", "missing-cursor"])
async def test_scoped_audit_fails_closed_on_incomplete_comment_pages(
    later_page: bool, cause: str
) -> None:
    fake = FakeCommandRunner()
    if later_page:
        fake.queue_result(stdout=_page(_target(cursor="comments-2")))
    node: dict[str, Any] | None = _target()
    if cause == "missing-node":
        node = None
    elif cause == "missing-comments":
        node["comments"] = None
    else:
        node["comments"]["pageInfo"] = {"hasNextPage": True, "endCursor": None}
    fake.queue_result(stdout=_page(node))
    with pytest.raises(GitHubClientError):
        await GitHubClient(fake).fetch_review_thread_audit(repo=_REPO, pr_number=7, thread_id=_TID)
    assert len(fake.calls) == (2 if later_page else 1)


@pytest.mark.parametrize("retry", [True, False])
@pytest.mark.parametrize("later_page", [False, True], ids=["initial", "continuation"])
async def test_scoped_audit_preserves_retry_policy_on_every_page(
    retry: bool, later_page: bool
) -> None:
    fake = FakeCommandRunner()
    if later_page:
        fake.queue_result(stdout=_page(_target(cursor="comments-2")))
    fake.queue_result(returncode=1, stderr="HTTP 502 Bad Gateway")
    fake.queue_result(stdout=_page(_target()))
    sleep = _RecordedSleep()
    client = GitHubClient(fake, sleep=sleep)
    if retry:
        audit = await client.fetch_review_thread_audit(
            repo=_REPO, pr_number=7, thread_id=_TID, retry=retry
        )
        assert len(audit.threads) == 1
        assert len(sleep.calls) == 1
    else:
        with pytest.raises(GitHubClientError, match="502"):
            await client.fetch_review_thread_audit(
                repo=_REPO, pr_number=7, thread_id=_TID, retry=retry
            )
        assert sleep.calls == []
    assert len(fake.calls) == 1 + int(later_page) + int(retry)


async def test_scoped_audit_work_scales_with_target_pages_not_pr_threads() -> None:
    fake = FakeCommandRunner()
    client = GitHubClient(fake)
    for number in range(4):
        node = _target()
        node["id"] = f"PRRT_{number}"
        fake.queue_result(stdout=_page(node))
        result = await client.fetch_review_thread_audit(
            repo=_REPO, pr_number=7, thread_id=node["id"]
        )
        assert result.threads[0].thread_id == node["id"]
    assert len(fake.calls) == 4


@pytest.mark.parametrize("thread_id", ["", " ", "PRRT_target "])
async def test_scoped_audit_rejects_noncanonical_target_id(thread_id: str) -> None:
    fake = FakeCommandRunner()
    node = _target()
    node["id"] = thread_id
    fake.queue_result(stdout=_page(node))
    with pytest.raises(GitHubClientError):
        await GitHubClient(fake).fetch_review_thread_audit(
            repo=_REPO, pr_number=7, thread_id=thread_id
        )


async def test_scoped_audit_accepts_github_repository_name_casing() -> None:
    fake = FakeCommandRunner()
    node = _target()
    node["pullRequest"]["repository"]["nameWithOwner"] = "Acme/Widgets"
    fake.queue_result(stdout=_page(node))
    audit = await GitHubClient(fake).fetch_review_thread_audit(
        repo=_REPO, pr_number=7, thread_id=_TID
    )
    assert audit.threads[0].thread_id == _TID
