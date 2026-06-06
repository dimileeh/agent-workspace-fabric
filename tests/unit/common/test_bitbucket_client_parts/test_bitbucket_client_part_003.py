"""BitBucketClient comment/thread/issue tests (issue #345 Part 2).

Covers inline review-thread reconstruction (parent grouping, resolved + viewer
filtering, neutral thread-id encoding), ``resolve_thread`` (POST, never DELETE),
``post_comment``, and ``create_issue`` including the issue-tracker-disabled (404)
→ PR-comment fallback emitting ``BITBUCKET_ISSUE_TRACKER_DISABLED``.
"""

from __future__ import annotations

import json

import pytest

from awf.common.bitbucket_client import BITBUCKET_ISSUE_TRACKER_DISABLED, BitBucketClientError

from ._helpers import FakeBitBucket, make_client, pr_payload, repo

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
) -> dict:
    comment: dict = {
        "id": comment_id,
        "content": {"raw": body},
        "user": {"account_id": account, "display_name": "Reviewer"},
        "created_on": f"2024-01-{comment_id:02d}T00:00:00+00:00",
        "inline": {"path": "src/a.py", "to": 10},
        "links": {"html": {"href": f"https://bitbucket.org/i/{comment_id}"}},
    }
    if parent is not None:
        comment["parent"] = {"id": parent}
    if resolved:
        comment["resolution"] = {"type": "resolved"}
    return comment


def _seed_fetch_status(
    fake: FakeBitBucket, comments: list[dict], *, account_id: str = "viewer"
) -> None:
    fake.enqueue("GET", _PR, json=pr_payload())
    fake.page("GET", f"{_REPO}/commit/{_HEAD}/statuses", values=[])
    fake.page("GET", f"{_PR}/comments", values=comments)
    fake.page("GET", f"{_PR}/diffstat", values=[])
    fake.enqueue("GET", "/2.0/user", json={"account_id": account_id})


# ── Inline review threads ─────────────────────────────────────────────────────


async def test_inline_thread_with_reply_grouped_into_one_thread() -> None:
    fake = FakeBitBucket()
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
    fake = FakeBitBucket()
    _seed_fetch_status(fake, [_inline_comment(100, "done", resolved=True)])
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert status.unresolved_inline_threads == ()


async def test_viewer_authored_thread_is_filtered() -> None:
    fake = FakeBitBucket()
    _seed_fetch_status(fake, [_inline_comment(100, "mine", account="viewer")], account_id="viewer")
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert status.unresolved_inline_threads == ()


# ── resolve_thread (POST resolves; DELETE would reopen) ───────────────────────


async def test_resolve_thread_uses_post() -> None:
    fake = FakeBitBucket()
    fake.enqueue("POST", f"{_PR}/comments/100/resolve", json={"type": "resolved"})
    client = make_client(fake)
    await client.resolve_thread(thread_id="bb:workspace/repo#42:100")
    call = fake.requests[0]
    assert call.method == "POST"
    assert call.url.path == f"{_PR}/comments/100/resolve"
    assert all(r.method != "DELETE" for r in fake.requests)


async def test_resolve_thread_rejects_unrecognized_id() -> None:
    fake = FakeBitBucket()
    client = make_client(fake)
    with pytest.raises(BitBucketClientError):
        await client.resolve_thread(thread_id="not-a-bb-thread")
    assert fake.requests == []


# ── post_comment ──────────────────────────────────────────────────────────────


async def test_post_comment_sends_raw_content() -> None:
    fake = FakeBitBucket()
    fake.enqueue("POST", f"{_PR}/comments", json={"id": 5})
    client = make_client(fake)
    await client.post_comment(repo=repo(), pr_number=42, body="hello world")
    payload = json.loads(fake.calls("POST")[0].content)
    assert payload == {"content": {"raw": "hello world"}}


# ── create_issue ──────────────────────────────────────────────────────────────


async def test_create_issue_returns_html_url() -> None:
    fake = FakeBitBucket()
    fake.enqueue(
        "POST",
        f"{_REPO}/issues",
        json={"links": {"html": {"href": "https://bitbucket.org/workspace/repo/issues/7"}}},
    )
    client = make_client(fake)
    url = await client.create_issue(repo=repo(), title="t", body="b")
    assert url == "https://bitbucket.org/workspace/repo/issues/7"


async def test_create_issue_tracker_disabled_falls_back_to_pr_comment() -> None:
    fake = FakeBitBucket()
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


async def test_create_issue_tracker_disabled_without_pr_context_returns_usable_url() -> None:
    fake = FakeBitBucket()
    fake.enqueue("POST", f"{_REPO}/issues", status=404, json={"type": "error"})
    client = make_client(fake)
    url = await client.create_issue(repo=repo(), title="t", body="b")
    assert url == "https://bitbucket.org/workspace/repo/issues"


async def test_create_issue_non_404_error_propagates() -> None:
    fake = FakeBitBucket()
    fake.enqueue("POST", f"{_REPO}/issues", status=500, json={"type": "error"})
    client = make_client(fake)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client.create_issue(repo=repo(), title="t", body="b")
    assert excinfo.value.status == 500


def test_issue_tracker_disabled_reason_code_constant() -> None:
    # The reason code is part of the public contract surfaced in logs/catalog.
    assert BITBUCKET_ISSUE_TRACKER_DISABLED == "BITBUCKET_ISSUE_TRACKER_DISABLED"
