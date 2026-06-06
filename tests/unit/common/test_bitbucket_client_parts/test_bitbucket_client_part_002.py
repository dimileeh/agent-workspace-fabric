"""BitBucketClient PR lifecycle tests: create, fetch_pr_status, merge.

Covers payload assembly into the neutral ``PRStatus`` (state mapping, check-state
normalization, changed paths), pagination of comments, error paths, and the D5
merge-strategy round-trip including ``fast_forward``-only repos.
"""

from __future__ import annotations

import pytest

from awf.common.bitbucket_client import (
    BITBUCKET_MERGE_METHOD_UNSUPPORTED,
    BitBucketClientError,
)
from awf.runtime.pr_monitor import CheckState, MergeableState, MergeStateStatus

from ._helpers import FakeBitBucket, make_client, pr_payload, repo

pytestmark = pytest.mark.unit

_HEAD = "s" * 40
_REPO = "/2.0/repositories/workspace/repo"
_PR = f"{_REPO}/pullrequests/42"


def _seed_fetch_status(
    fake: FakeBitBucket,
    *,
    pr: dict | None = None,
    statuses: list[dict] | None = None,
    comments: list[dict] | None = None,
    diffstat: list[dict] | None = None,
    account_id: str = "viewer-acct",
) -> None:
    fake.enqueue("GET", _PR, json=pr if pr is not None else pr_payload())
    fake.page("GET", f"{_REPO}/commit/{_HEAD}/statuses", values=statuses or [])
    fake.page("GET", f"{_PR}/comments", values=comments or [])
    fake.page("GET", f"{_PR}/diffstat", values=diffstat or [])
    fake.enqueue("GET", "/2.0/user", json={"account_id": account_id})


# ── create_pull_request ──────────────────────────────────────────────────────


async def test_create_pull_request_returns_html_url() -> None:
    fake = FakeBitBucket()
    fake.enqueue(
        "POST",
        f"{_REPO}/pullrequests",
        json={"links": {"html": {"href": "https://bitbucket.org/workspace/repo/pull-requests/9"}}},
    )
    client = make_client(fake)
    url = await client.create_pull_request(
        repo=repo(), base="development", head="feature/x", title="t", body="b"
    )
    assert url == "https://bitbucket.org/workspace/repo/pull-requests/9"
    body = fake.calls("POST")[0]
    import json as _json

    payload = _json.loads(body.content)
    assert payload["source"]["branch"]["name"] == "feature/x"
    assert payload["destination"]["branch"]["name"] == "development"


async def test_create_pull_request_missing_href_raises() -> None:
    fake = FakeBitBucket()
    fake.enqueue("POST", f"{_REPO}/pullrequests", json={"id": 1})
    client = make_client(fake)
    with pytest.raises(BitBucketClientError):
        await client.create_pull_request(
            repo=repo(), base="development", head="h", title="t", body="b"
        )


# ── fetch_pr_status ──────────────────────────────────────────────────────────


async def test_fetch_pr_status_open_clean() -> None:
    fake = FakeBitBucket()
    _seed_fetch_status(
        fake,
        statuses=[{"state": "SUCCESSFUL", "name": "build", "key": "pipe"}],
        diffstat=[{"new": {"path": "src/a.py"}, "status": "modified"}],
    )
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert status.number == 42
    assert status.head_sha == _HEAD
    assert status.check_state == CheckState.SUCCESS
    assert status.mergeable == MergeableState.MERGEABLE
    assert status.merge_state_status == MergeStateStatus.CLEAN
    assert status.closed is False
    assert status.merged is False
    assert status.changed_paths == ("src/a.py",)


async def test_fetch_pr_status_failed_check() -> None:
    fake = FakeBitBucket()
    _seed_fetch_status(fake, statuses=[{"state": "FAILED", "name": "pipeline"}])
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert status.check_state == CheckState.FAILURE
    assert status.ci_failures == ()  # populated separately by fetch_failing_check_logs


async def test_fetch_pr_status_in_progress_check_is_pending() -> None:
    fake = FakeBitBucket()
    _seed_fetch_status(fake, statuses=[{"state": "INPROGRESS", "name": "pipeline"}])
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert status.check_state == CheckState.PENDING


async def test_fetch_pr_status_merged() -> None:
    fake = FakeBitBucket()
    _seed_fetch_status(fake, pr=pr_payload(state="MERGED", merge_commit_hash="m" * 40))
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert status.merged is True
    assert status.merge_commit_sha == "m" * 40


async def test_fetch_pr_status_declined_is_closed() -> None:
    fake = FakeBitBucket()
    _seed_fetch_status(fake, pr=pr_payload(state="DECLINED"))
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert status.closed is True
    assert status.merged is False


async def test_fetch_pr_status_passes_through_base_behind_count() -> None:
    fake = FakeBitBucket()
    _seed_fetch_status(fake)
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=5)
    assert status.base_behind_count == 5


async def test_fetch_pr_status_not_found_raises() -> None:
    fake = FakeBitBucket()
    fake.enqueue("GET", _PR, status=404, json={"type": "error"})
    client = make_client(fake)
    with pytest.raises(BitBucketClientError):
        await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)


async def test_fetch_pr_status_missing_head_sha_raises() -> None:
    fake = FakeBitBucket()
    pr = pr_payload()
    pr["source"]["commit"] = {}
    fake.enqueue("GET", _PR, json=pr)
    client = make_client(fake)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert "no source commit hash" in excinfo.value.body


async def test_fetch_pr_status_paginates_comments() -> None:
    fake = FakeBitBucket()
    fake.enqueue("GET", _PR, json=pr_payload())
    fake.page("GET", f"{_REPO}/commit/{_HEAD}/statuses", values=[])
    fake.page(
        "GET",
        f"{_PR}/comments",
        values=[_general_comment(1, "first")],
        next_url=f"https://api.bitbucket.org{_PR}/comments?page=2",
    )
    fake.page("GET", f"{_PR}/comments", values=[_general_comment(2, "second")])
    fake.page("GET", f"{_PR}/diffstat", values=[])
    fake.enqueue("GET", "/2.0/user", json={"account_id": "viewer"})
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    bodies = {c.body for c in status.unresolved_review_comments}
    assert bodies == {"first", "second"}


def _general_comment(comment_id: int, body: str) -> dict:
    return {
        "id": comment_id,
        "content": {"raw": body},
        "user": {"account_id": "other", "display_name": "Reviewer"},
        "created_on": "2024-01-01T00:00:00+00:00",
        "links": {"html": {"href": f"https://bitbucket.org/c/{comment_id}"}},
    }


# ── merge_pr (D5) ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method, expected_strategy",
    [
        ("merge", "merge_commit"),
        ("squash", "squash"),
        ("fast_forward", "fast_forward"),
    ],
)
async def test_merge_pr_strategy_round_trip(method: str, expected_strategy: str) -> None:
    fake = FakeBitBucket()
    fake.enqueue("POST", f"{_PR}/merge", json={"merge_commit": {"hash": "abc123"}})
    client = make_client(fake)
    sha = await client.merge_pr(repo=repo(), pr_number=42, method=method, delete_branch=True)
    assert sha == "abc123"
    import json as _json

    payload = _json.loads(fake.calls("POST")[0].content)
    assert payload["merge_strategy"] == expected_strategy
    assert payload["close_source_branch"] is True


async def test_merge_pr_unsupported_method_raises_without_request() -> None:
    fake = FakeBitBucket()
    client = make_client(fake)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client.merge_pr(repo=repo(), pr_number=42, method="rebase")
    assert excinfo.value.reason_code == BITBUCKET_MERGE_METHOD_UNSUPPORTED
    assert fake.calls("POST") == []  # never POSTed a wrong strategy


async def test_merge_pr_missing_commit_hash_returns_empty() -> None:
    fake = FakeBitBucket()
    fake.enqueue("POST", f"{_PR}/merge", json={"state": "MERGED"})
    client = make_client(fake)
    sha = await client.merge_pr(repo=repo(), pr_number=42, method="squash")
    assert sha == ""
