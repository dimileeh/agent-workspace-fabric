"""BitBucketClient + parsing edge-coverage tests (issue #345 Part 2).

Closes the remaining branches: account-id caching/error/non-dict, the issue-fallback
PR-page URL, step-log error tolerance, invalid ``Retry-After``, empty-body parsing,
cacheable params, and the pure-parsing guard branches.
"""

from __future__ import annotations

import pytest

from awf.common.bitbucket_client import BitBucketClientError
from awf.common.bitbucket_client_parsing import (
    _comment_account_id,
    _comment_author,
    _tail,
    build_general_review_comments,
    build_review_threads,
    extract_diffstat_paths,
    html_href,
    parse_bb_datetime,
    parse_check_timings,
)
from awf.common.github_client import RepoRef

from ._helpers import FakeBitBucket, RecordingSleep, make_client, pr_payload, repo

pytestmark = pytest.mark.unit

_HEAD = "s" * 40
_REPO = "/2.0/repositories/workspace/repo"
_PR = f"{_REPO}/pullrequests/42"


def _seed_fetch_status(fake: FakeBitBucket, *, account_body: object = None) -> None:
    fake.enqueue("GET", _PR, json=pr_payload())
    fake.page("GET", f"{_REPO}/commit/{_HEAD}/statuses", values=[])
    fake.page("GET", f"{_PR}/comments", values=[])
    fake.page("GET", f"{_PR}/diffstat", values=[])
    fake.enqueue(
        "GET", "/2.0/user", json=account_body if account_body is not None else {"account_id": "v"}
    )


# ── Client edge branches ──────────────────────────────────────────────────────


async def test_fetch_pr_status_non_dict_body_raises() -> None:
    fake = FakeBitBucket()
    fake.enqueue("GET", _PR, json=[1, 2, 3])  # 200 but not an object
    client = make_client(fake)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert "not found" in excinfo.value.body


async def test_account_id_is_cached_across_status_fetches() -> None:
    fake = FakeBitBucket()
    _seed_fetch_status(fake)
    # Second status fetch re-enqueues everything except /2.0/user (served from cache).
    fake.enqueue("GET", _PR, json=pr_payload())
    fake.page("GET", f"{_REPO}/commit/{_HEAD}/statuses", values=[])
    fake.page("GET", f"{_PR}/comments", values=[])
    fake.page("GET", f"{_PR}/diffstat", values=[])
    client = make_client(fake)
    await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert len(fake.calls("GET")) == 9  # only one /2.0/user across both fetches
    assert sum(1 for r in fake.requests if r.url.path == "/2.0/user") == 1


async def test_account_id_fetch_error_is_tolerated() -> None:
    fake = FakeBitBucket()
    fake.enqueue("GET", _PR, json=pr_payload())
    fake.page("GET", f"{_REPO}/commit/{_HEAD}/statuses", values=[])
    fake.page(
        "GET",
        f"{_PR}/comments",
        values=[
            {
                "id": 1,
                "content": {"raw": "hi"},
                "user": {"account_id": "someone"},
                "created_on": "2024-01-01T00:00:00Z",
            }
        ],
    )
    fake.page("GET", f"{_PR}/diffstat", values=[])
    fake.enqueue("GET", "/2.0/user", status=500, json={"error": "x"})
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    # No account id → cannot filter viewer comments, but assembly still succeeds.
    assert len(status.unresolved_review_comments) == 1


async def test_account_id_non_dict_body_leaves_account_none() -> None:
    fake = FakeBitBucket()
    _seed_fetch_status(fake, account_body=[])  # list, not an object
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert status.number == 42


async def test_create_issue_fallback_returns_pr_page_url_when_comment_has_no_href() -> None:
    fake = FakeBitBucket()
    _seed_fetch_status(fake)
    fake.enqueue("POST", f"{_REPO}/issues", status=404, json={"type": "error"})
    fake.enqueue("POST", f"{_PR}/comments", json={"id": 7})  # no links.html.href
    client = make_client(fake)
    await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    url = await client.create_issue(repo=repo(), title="t", body="b")
    assert url == "https://bitbucket.org/workspace/repo/pull-requests/42"


async def test_step_log_404_is_tolerated_as_empty() -> None:
    fake = FakeBitBucket()
    fake.page(
        "GET",
        f"{_REPO}/commit/{_HEAD}/statuses",
        values=[{"state": "FAILED", "name": "Pipeline"}],
    )
    fake.page("GET", f"{_REPO}/pipelines/", values=[{"uuid": "p1"}])
    fake.page(
        "GET",
        f"{_REPO}/pipelines/p1/steps/",
        values=[{"uuid": "s1", "name": "Test", "state": {"result": {"name": "FAILED"}}}],
    )
    fake.enqueue("GET", f"{_REPO}/pipelines/p1/steps/s1/log", status=404, json={"e": 1})
    client = make_client(fake)
    failures = await client.fetch_failing_check_logs(repo=repo(), pr_number=42, head_sha=_HEAD)
    assert failures[0].log_excerpt == ""


async def test_invalid_retry_after_falls_back_to_backoff() -> None:
    fake = FakeBitBucket()
    fake.enqueue(
        "POST",
        f"{_REPO}/pullrequests",
        status=429,
        headers={"Retry-After": "soon"},
    )
    fake.enqueue("POST", f"{_REPO}/pullrequests", json={"links": {"html": {"href": "u"}}})
    sleep = RecordingSleep()
    client = make_client(fake, sleep=sleep, backoff_base_seconds=1.0)
    await client.create_pull_request(repo=repo(), base="d", head="h", title="t", body="b")
    assert sleep.delays == [1.0]


async def test_empty_body_parses_to_none() -> None:
    fake = FakeBitBucket()
    fake.enqueue("POST", f"{_PR}/comments")  # 200, empty body
    client = make_client(fake)
    await client.post_comment(repo=repo(), pr_number=42, body="hi")  # must not raise


async def test_cacheable_request_with_params_round_trips() -> None:
    fake = FakeBitBucket()
    fake.enqueue("GET", "/x", json={"v": 1}, headers={"ETag": "e1"})
    fake.enqueue("GET", "/x", status=304, headers={"ETag": "e1"})
    client = make_client(fake)
    first = await client._request_json("GET", "/x", operation="op", params={"a": "b"}, cache=True)
    second = await client._request_json("GET", "/x", operation="op", params={"a": "b"}, cache=True)
    assert first == second == {"v": 1}


# ── Pure-parsing guard branches ───────────────────────────────────────────────


def test_parse_bb_datetime_naive_gets_utc() -> None:
    parsed = parse_bb_datetime("2024-01-01T00:00:00")
    assert parsed is not None
    assert parsed.tzinfo is not None


@pytest.mark.parametrize("comment", [{}, {"user": "not-a-dict"}, {"user": {}}])
def test_comment_author_and_account_id_guards(comment: dict) -> None:
    assert _comment_author(comment) is None
    assert _comment_account_id(comment) is None


@pytest.mark.parametrize("obj", [None, "x", {"links": "x"}, {"links": {"html": "x"}}, {}])
def test_html_href_guards(obj: object) -> None:
    assert html_href(obj) is None


def test_thread_line_falls_back_to_from() -> None:
    repo_ref = RepoRef(owner="ws", name="repo", forge="bitbucket")
    comments = [
        {
            "id": 1,
            "content": {"raw": "finding"},
            "user": {"account_id": "x"},
            "created_on": "2024-01-01T00:00:00Z",
            "inline": {"path": "a.py", "to": None, "from": 5},
        }
    ]
    threads = build_review_threads(comments, repo=repo_ref, pr_number=1, account_id=None)
    assert threads[0].line == 5


def test_general_comments_skip_inline_deleted_and_empty() -> None:
    comments = [
        {"id": 1, "inline": {"path": "a.py"}, "content": {"raw": "inline"}},  # inline → skip
        {"id": 2, "deleted": True, "content": {"raw": "gone"}},  # deleted → skip
        {"id": 3, "content": {"raw": "   "}},  # empty body → skip
        {"id": 4, "content": {"raw": "real"}, "user": {"account_id": "me"}},
    ]
    reviews = build_general_review_comments(comments, account_id="me")  # id 4 is viewer
    assert reviews == ()


def test_extract_diffstat_paths_ignores_non_dict_sides() -> None:
    assert extract_diffstat_paths([{"new": "x", "old": None}, {}]) == ()


def test_parse_check_timings_skips_nameless_status() -> None:
    timings = parse_check_timings([{"state": "SUCCESSFUL"}, {"state": "FAILED", "name": "build"}])
    assert [t.name for t in timings] == ["build"]


def test_tail_truncates_long_text() -> None:
    out = _tail("abcdefghij", 3)
    assert out.endswith("hij")
    assert "truncated" in out


async def test_paginate_skips_non_dict_value_and_handles_empty_body() -> None:
    fake = FakeBitBucket()
    fake.enqueue("GET", _PR, json=pr_payload())
    # statuses page mixes a non-dict value (skipped) with a real status.
    fake.enqueue(
        "GET",
        f"{_REPO}/commit/{_HEAD}/statuses",
        json={"values": [{"state": "SUCCESSFUL", "name": "ok"}, "garbage"]},
    )
    fake.page("GET", f"{_PR}/comments", values=[])
    fake.enqueue("GET", f"{_PR}/diffstat")  # empty body → page is None → no pagination
    fake.enqueue("GET", "/2.0/user", json={"account_id": "v"})
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert status.changed_paths == ()
    assert len(status.checks) == 1
