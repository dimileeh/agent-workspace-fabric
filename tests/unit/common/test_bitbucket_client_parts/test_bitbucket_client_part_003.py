"""BitbucketClient comment/thread/issue tests (issue #345 Part 2).

Covers inline review-thread reconstruction (parent grouping, resolved + viewer
filtering, neutral thread-id encoding), ``resolve_thread`` (POST, never DELETE),
``post_comment``, and ``create_issue`` including the issue-tracker-disabled (404)
→ PR-comment fallback emitting ``BITBUCKET_ISSUE_TRACKER_DISABLED``.
"""

from __future__ import annotations

import json

import pytest

from awf.common.bitbucket_client import BITBUCKET_ISSUE_TRACKER_DISABLED, BitbucketClientError

from ._helpers import FakeBitbucket, make_client, pr_payload, repo

pytestmark = pytest.mark.unit

_HEAD = "s" * 40
_REPO = "/2.0/repositories/workspace/repo"
_PR = f"{_REPO}/pullrequests/42"


def _inline_comment(
    comment_id: int,
    body: str,
    *,
    account: str = "other",
    parent: int | None = None,
    resolved: bool = False,
    outdated: bool = False,
) -> dict:
    inline: dict = {"path": "src/a.py", "to": 10}
    if outdated:
        inline["outdated"] = True
    comment: dict = {
        "id": comment_id,
        "content": {"raw": body},
        "user": {"account_id": account, "display_name": "Reviewer"},
        "created_on": f"2024-01-{comment_id:02d}T00:00:00+00:00",
        "inline": inline,
        "links": {"html": {"href": f"https://bitbucket.org/i/{comment_id}"}},
    }
    if parent is not None:
        comment["parent"] = {"id": parent}
    if resolved:
        comment["resolution"] = {"type": "resolved"}
    return comment


def _seed_fetch_status(
    fake: FakeBitbucket, comments: list[dict], *, account_id: str = "viewer"
) -> None:
    fake.enqueue("GET", _PR, json=pr_payload())
    fake.page("GET", f"{_REPO}/commit/{_HEAD}/statuses", values=[])
    fake.page("GET", f"{_PR}/comments", values=comments)
    fake.page("GET", f"{_PR}/diffstat", values=[])
    fake.page("GET", f"{_PR}/tasks", values=[])
    fake.enqueue("GET", "/2.0/user", json={"account_id": account_id})


# ── Inline review threads ─────────────────────────────────────────────────────


async def test_inline_thread_with_reply_grouped_into_one_thread() -> None:
    fake = FakeBitbucket()
    _seed_fetch_status(
        fake,
        [
            _inline_comment(100, "root finding"),
            _inline_comment(101, "a reply", parent=100),
        ],
    )
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert len(status.unresolved_inline_threads) == 1
    thread = status.unresolved_inline_threads[0]
    assert thread.thread_id == "bb:workspace/repo#42:100"
    assert thread.path == "src/a.py"
    assert thread.line == 10
    assert [c.body for c in thread.comments] == ["root finding", "a reply"]


async def test_resolved_thread_is_filtered() -> None:
    fake = FakeBitbucket()
    _seed_fetch_status(fake, [_inline_comment(100, "done", resolved=True)])
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert status.unresolved_inline_threads == ()


async def test_viewer_authored_thread_is_filtered() -> None:
    fake = FakeBitbucket()
    _seed_fetch_status(fake, [_inline_comment(100, "mine", account="viewer")], account_id="viewer")
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert status.unresolved_inline_threads == ()


async def test_outdated_thread_is_filtered() -> None:
    # Mirror the GitHub path: a Bitbucket inline comment marked ``outdated``
    # after a new push no longer describes the current diff, so it must not
    # appear in ``unresolved_inline_threads`` and drive a fix cycle / block merge.
    fake = FakeBitbucket()
    _seed_fetch_status(fake, [_inline_comment(100, "stale finding", outdated=True)])
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert status.unresolved_inline_threads == ()


async def test_fetch_pr_status_surfaces_outdated_unresolved_thread() -> None:
    # #473: a Bitbucket inline thread that went ``outdated`` (addressed elsewhere)
    # but is still unresolved is kept out of the actionable feed yet surfaced in
    # ``outdated_unresolved_inline_threads`` so the monitor can resolve the ones it
    # addressed.
    fake = FakeBitbucket()
    _seed_fetch_status(fake, [_inline_comment(100, "stale finding", outdated=True)])
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert status.unresolved_inline_threads == ()
    assert [t.thread_id for t in status.outdated_unresolved_inline_threads] == [
        "bb:workspace/repo#42:100"
    ]
    thread = status.outdated_unresolved_inline_threads[0]
    assert thread.is_outdated is True
    assert thread.is_resolved is False
    assert thread.body_excerpt == "stale finding"


def test_build_outdated_unresolved_review_threads_only_returns_outdated() -> None:
    from awf.common.bitbucket_client_parsing import (
        build_outdated_unresolved_review_threads,
        build_review_threads,
    )

    comments = [
        _inline_comment(100, "addressed elsewhere", outdated=True),
        _inline_comment(101, "still current"),
        _inline_comment(102, "resolved + outdated", outdated=True, resolved=True),
        _inline_comment(103, "mine but outdated", account="viewer", outdated=True),
    ]
    outdated = build_outdated_unresolved_review_threads(
        comments, repo=repo(), pr_number=42, account_id="viewer"
    )
    assert [t.thread_id for t in outdated] == ["bb:workspace/repo#42:100"]
    assert outdated[0].is_outdated is True
    assert outdated[0].is_resolved is False

    # The actionable feed is unchanged: only the still-current, non-outdated,
    # external thread remains.
    actionable = build_review_threads(comments, repo=repo(), pr_number=42, account_id="viewer")
    assert [t.thread_id for t in actionable] == ["bb:workspace/repo#42:101"]
    assert all(t.is_outdated is False for t in actionable)


def test_partition_inline_review_threads_single_pass_matches_builders() -> None:
    from awf.common.bitbucket_client_parsing import (
        build_outdated_unresolved_review_threads,
        build_review_threads,
        partition_inline_review_threads,
    )

    comments = [
        _inline_comment(100, "addressed elsewhere", outdated=True),
        _inline_comment(101, "still current"),
        _inline_comment(102, "resolved + outdated", outdated=True, resolved=True),
        _inline_comment(103, "mine but outdated", account="viewer", outdated=True),
    ]
    actionable, outdated = partition_inline_review_threads(
        comments, repo=repo(), pr_number=42, account_id="viewer"
    )
    # One pass produces exactly the same two feeds the separate selectors return.
    assert actionable == build_review_threads(
        comments, repo=repo(), pr_number=42, account_id="viewer"
    )
    assert outdated == build_outdated_unresolved_review_threads(
        comments, repo=repo(), pr_number=42, account_id="viewer"
    )
    assert [t.thread_id for t in actionable] == ["bb:workspace/repo#42:101"]
    assert [t.thread_id for t in outdated] == ["bb:workspace/repo#42:100"]


# ── resolve_thread (POST resolves; DELETE would reopen) ───────────────────────


async def test_resolve_thread_uses_post() -> None:
    fake = FakeBitbucket()
    fake.enqueue("POST", f"{_PR}/comments/100/resolve", json={"type": "resolved"})
    client = make_client(fake)
    await client.resolve_thread(thread_id="bb:workspace/repo#42:100")
    call = fake.requests[0]
    assert call.method == "POST"
    assert call.url.path == f"{_PR}/comments/100/resolve"
    assert all(r.method != "DELETE" for r in fake.requests)


async def test_resolve_thread_rejects_unrecognized_id() -> None:
    fake = FakeBitbucket()
    client = make_client(fake)
    with pytest.raises(BitbucketClientError):
        await client.resolve_thread(thread_id="not-a-bb-thread")
    assert fake.requests == []


# ── post_comment ──────────────────────────────────────────────────────────────


async def test_post_comment_sends_raw_content() -> None:
    fake = FakeBitbucket()
    fake.enqueue("POST", f"{_PR}/comments", json={"id": 5})
    client = make_client(fake)
    await client.post_comment(repo=repo(), pr_number=42, body="hello world")
    payload = json.loads(fake.calls("POST")[0].content)
    assert payload == {"content": {"raw": "hello world"}}


# ── create_issue ──────────────────────────────────────────────────────────────


async def test_create_issue_returns_html_url() -> None:
    fake = FakeBitbucket()
    fake.enqueue(
        "POST",
        f"{_REPO}/issues",
        json={"links": {"html": {"href": "https://bitbucket.org/workspace/repo/issues/7"}}},
    )
    client = make_client(fake)
    url = await client.create_issue(repo=repo(), title="t", body="b")
    assert url == "https://bitbucket.org/workspace/repo/issues/7"


async def test_create_issue_builds_url_from_id_when_href_missing() -> None:
    # A successful create that omits ``links.html.href`` must still yield a URL that
    # points at the specific filed issue (derived from the returned ``id``), not the
    # generic issues list — otherwise deferred capture records a page with no
    # guaranteed link to the tracking issue.
    fake = FakeBitbucket()
    fake.enqueue("POST", f"{_REPO}/issues", json={"id": 7})  # no links.html.href
    client = make_client(fake)
    url = await client.create_issue(repo=repo(), title="t", body="b")
    assert url == "https://bitbucket.org/workspace/repo/issues/7"


async def test_create_issue_falls_back_to_issues_page_without_href_or_id() -> None:
    # Only when neither a specific href nor a usable numeric id is present does the
    # URL degrade to the generic issues list (better than nothing).
    fake = FakeBitbucket()
    fake.enqueue("POST", f"{_REPO}/issues", json={"type": "issue"})  # no href, no id
    client = make_client(fake)
    url = await client.create_issue(repo=repo(), title="t", body="b")
    assert url == "https://bitbucket.org/workspace/repo/issues"


async def test_create_issue_falls_back_to_issues_page_when_response_not_dict() -> None:
    # A malformed 200 (non-dict body) still resolves to the generic issues list
    # rather than raising — the issue was created, just unparseable.
    fake = FakeBitbucket()
    fake.enqueue("POST", f"{_REPO}/issues", json=[])  # non-dict success body
    client = make_client(fake)
    url = await client.create_issue(repo=repo(), title="t", body="b")
    assert url == "https://bitbucket.org/workspace/repo/issues"


async def test_create_issue_tracker_disabled_falls_back_to_pr_comment() -> None:
    fake = FakeBitbucket()
    # Prime PR context so the fallback knows which PR to comment on.
    _seed_fetch_status(fake, [])
    fake.enqueue("POST", f"{_REPO}/issues", status=404, json={"type": "error"})
    fake.enqueue(
        "POST",
        f"{_PR}/comments",
        json={
            "links": {"html": {"href": "https://bitbucket.org/workspace/repo/pull-requests/42#c"}}
        },
    )
    client = make_client(fake)
    await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    url = await client.create_issue(repo=repo(), title="Heads up", body="details")
    assert url == "https://bitbucket.org/workspace/repo/pull-requests/42#c"
    comment_payload = json.loads(fake.calls("POST")[-1].content)
    assert "Heads up" in comment_payload["content"]["raw"]
    assert "details" in comment_payload["content"]["raw"]


async def test_create_issue_non_404_error_propagates() -> None:
    fake = FakeBitbucket()
    fake.enqueue("POST", f"{_REPO}/issues", status=500, json={"type": "error"})
    client = make_client(fake)
    with pytest.raises(BitbucketClientError) as excinfo:
        await client.create_issue(repo=repo(), title="t", body="b")
    assert excinfo.value.status == 500


def test_issue_tracker_disabled_reason_code_constant() -> None:
    # The reason code is part of the public contract surfaced in logs/catalog.
    assert BITBUCKET_ISSUE_TRACKER_DISABLED == "BITBUCKET_ISSUE_TRACKER_DISABLED"
