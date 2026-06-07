"""BitBucketClient PR lifecycle tests: create, fetch_pr_status, merge.

Covers payload assembly into the neutral ``PRStatus`` (state mapping, check-state
normalization, changed paths), pagination of comments, error paths, and the D5
merge-strategy round-trip including ``fast_forward``-only repos.
"""

from __future__ import annotations

import pytest

from awf.common.bitbucket_client import (
    BITBUCKET_API_ERROR,
    BITBUCKET_MERGE_IN_PROGRESS,
    BITBUCKET_MERGE_METHOD_UNSUPPORTED,
    BitBucketClientError,
)
from awf.runtime.pr_monitor import CheckState, MergeableState, MergeStateStatus

from ._helpers import FakeBitBucket, RecordingSleep, make_client, pr_payload, repo

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


async def test_paginate_propagates_cache_to_subsequent_pages() -> None:
    """``cache=True`` must reach pages 2+, not just the first page (issue:4640573294).

    Regression for the ``_paginate`` cache-propagation gap: subsequent ``next``
    pages were fetched without the ``cache`` keyword, so they neither sent nor
    stored an ETag and silently bypassed the conditional-request optimization the
    caller asked for.
    """
    fake = FakeBitBucket()
    page2_url = f"https://api.bitbucket.org{_PR}/comments?page=2"
    # First pass primes the conditional cache: both pages answer with an ETag.
    fake.page(
        "GET", f"{_PR}/comments", values=[{"id": 1}], next_url=page2_url, headers={"ETag": "p1"}
    )
    fake.page(
        "GET", f"{_PR}/comments", values=[{"id": 2}], headers={"ETag": "p2"}, params={"page": "2"}
    )
    # Second pass re-serves both pages so the primed page-2 ETag is exercised.
    fake.page(
        "GET", f"{_PR}/comments", values=[{"id": 1}], next_url=page2_url, headers={"ETag": "p1"}
    )
    fake.page(
        "GET", f"{_PR}/comments", values=[{"id": 2}], headers={"ETag": "p2"}, params={"page": "2"}
    )
    client = make_client(fake)
    first = await client._paginate(f"{_PR}/comments", operation="bitbucket test", cache=True)
    assert [value["id"] for value in first] == [1, 2]
    await client._paginate(f"{_PR}/comments", operation="bitbucket test", cache=True)
    page2_requests = [r for r in fake.calls("GET") if r.url.params.get("page") == "2"]
    assert page2_requests, "expected a page-2 request on each pass"
    # Only a propagated ``cache=True`` stores the page-2 ETag on pass one and sends
    # it back as ``If-None-Match`` on pass two.
    assert page2_requests[-1].headers.get("If-None-Match") == "p2"


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


async def test_merge_pr_missing_commit_hash_raises() -> None:
    # A terminal merge with no commit hash is an unusable payload: returning a
    # silent "" would be recorded downstream as a successful merge. Raise instead
    # so the miss is diagnosable rather than masking success.
    fake = FakeBitBucket()
    fake.enqueue("POST", f"{_PR}/merge", json={"state": "MERGED"})
    client = make_client(fake)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client.merge_pr(repo=repo(), pr_number=42, method="squash")
    assert "merge_commit.hash" in str(excinfo.value)


async def test_merge_pr_409_maps_to_transient_in_progress_reason() -> None:
    # A 409 on the merge POST means BitBucket already has a merge in flight for
    # this PR (e.g. a prior 202 async merge whose poll was interrupted by a
    # transient fault and re-issued by the monitor). It must map to the transient
    # BITBUCKET_MERGE_IN_PROGRESS reason — not the deterministic BITBUCKET_API_ERROR
    # — so the monitor re-polls fetch_pr_status instead of terminating the
    # workspace on a merge that may still be completing.
    fake = FakeBitBucket()
    fake.enqueue(
        "POST",
        f"{_PR}/merge",
        status=409,
        json={"error": {"message": "merge already in progress"}},
    )
    client = make_client(fake)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client.merge_pr(repo=repo(), pr_number=42, method="squash")
    assert excinfo.value.reason_code == BITBUCKET_MERGE_IN_PROGRESS
    assert excinfo.value.status == 409


async def test_merge_pr_non_409_conflict_keeps_api_error_reason() -> None:
    # Other 4xx faults on the merge POST stay deterministic (BITBUCKET_API_ERROR)
    # so they fail fast rather than being mistaken for an in-flight merge.
    fake = FakeBitBucket()
    fake.enqueue(
        "POST",
        f"{_PR}/merge",
        status=400,
        json={"error": {"message": "bad request"}},
    )
    client = make_client(fake)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client.merge_pr(repo=repo(), pr_number=42, method="squash")
    assert excinfo.value.reason_code == BITBUCKET_API_ERROR


# ── merge_pr async (202 Accepted) task polling ───────────────────────────────


async def test_merge_pr_async_202_polls_location_to_success() -> None:
    # BitBucket runs merges asynchronously: a slow merge answers 202 with a
    # Location header pointing at the task-status endpoint, which must be polled
    # to a terminal status before the merge commit hash is known.
    fake = FakeBitBucket()
    poll_url = f"https://api.bitbucket.org{_PR}/merge/task-status/task-1"
    fake.enqueue("POST", f"{_PR}/merge", status=202, headers={"Location": poll_url})
    fake.enqueue(
        "GET",
        f"{_PR}/merge/task-status/task-1",
        json={
            "task_status": "SUCCESS",
            "merge_result": {"state": "MERGED", "merge_commit": {"hash": "async-sha"}},
        },
    )
    client = make_client(fake)
    sha = await client.merge_pr(repo=repo(), pr_number=42, method="squash")
    assert sha == "async-sha"


async def test_merge_pr_async_202_polls_through_pending() -> None:
    fake = FakeBitBucket()
    poll_url = f"https://api.bitbucket.org{_PR}/merge/task-status/task-2"
    fake.enqueue("POST", f"{_PR}/merge", status=202, headers={"Location": poll_url})
    fake.enqueue("GET", f"{_PR}/merge/task-status/task-2", json={"task_status": "PENDING"})
    fake.enqueue(
        "GET",
        f"{_PR}/merge/task-status/task-2",
        json={"task_status": "SUCCESS", "merge_result": {"merge_commit": {"hash": "later-sha"}}},
    )
    sleep = RecordingSleep()
    client = make_client(fake, sleep=sleep)
    sha = await client.merge_pr(repo=repo(), pr_number=42, method="squash")
    assert sha == "later-sha"
    assert sleep.delays  # waited between polls


async def test_merge_pr_async_202_task_id_fallback_without_location() -> None:
    # Some 202 responses carry the task id only in the body; build the poll URL.
    fake = FakeBitBucket()
    fake.enqueue("POST", f"{_PR}/merge", status=202, json={"task_id": "task-7"})
    fake.enqueue(
        "GET",
        f"{_PR}/merge/task-status/task-7",
        json={"task_status": "SUCCESS", "merge_result": {"merge_commit": {"hash": "fallback-sha"}}},
    )
    client = make_client(fake)
    sha = await client.merge_pr(repo=repo(), pr_number=42, method="squash")
    assert sha == "fallback-sha"


async def test_merge_pr_async_202_non_success_terminal_status_raises() -> None:
    fake = FakeBitBucket()
    poll_url = f"https://api.bitbucket.org{_PR}/merge/task-status/task-3"
    fake.enqueue("POST", f"{_PR}/merge", status=202, headers={"Location": poll_url})
    fake.enqueue("GET", f"{_PR}/merge/task-status/task-3", json={"task_status": "FAILED"})
    client = make_client(fake)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client.merge_pr(repo=repo(), pr_number=42, method="squash")
    assert "FAILED" in str(excinfo.value)
    # The error must not reuse the original 202 POST status: a task that ended in
    # ``FAILED`` is not an HTTP-202 failure, and reporting ``status=202`` would make
    # it indistinguishable from a poll-budget timeout. The HTTP status is omitted.
    assert excinfo.value.status is None
    assert "status=202" not in str(excinfo.value)


async def test_merge_pr_async_202_poll_budget_exhausted_raises() -> None:
    fake = FakeBitBucket()
    poll_url = f"https://api.bitbucket.org{_PR}/merge/task-status/task-4"
    fake.enqueue("POST", f"{_PR}/merge", status=202, headers={"Location": poll_url})
    fake.enqueue("GET", f"{_PR}/merge/task-status/task-4", json={"task_status": "PENDING"})
    client = make_client(fake, max_merge_polls=2, merge_poll_delay_seconds=0)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client.merge_pr(repo=repo(), pr_number=42, method="squash")
    assert "did not complete" in str(excinfo.value)


async def test_merge_pr_async_202_non_dict_poll_body_keeps_polling() -> None:
    # A malformed (non-dict) task-status body is treated as not-yet-terminal so a
    # garbled poll cannot be misread as success; the bounded budget then trips.
    fake = FakeBitBucket()
    poll_url = f"https://api.bitbucket.org{_PR}/merge/task-status/task-5"
    fake.enqueue("POST", f"{_PR}/merge", status=202, headers={"Location": poll_url})
    fake.enqueue("GET", f"{_PR}/merge/task-status/task-5", json=["unexpected"])
    client = make_client(fake, max_merge_polls=1, merge_poll_delay_seconds=0)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client.merge_pr(repo=repo(), pr_number=42, method="squash")
    assert "did not complete" in str(excinfo.value)


async def test_merge_pr_async_202_off_origin_location_rejected() -> None:
    # The poll Location is origin-checked (SSRF guard) before being requested with
    # the Authorization header, exactly like pagination ``next`` links.
    fake = FakeBitBucket()
    fake.enqueue(
        "POST",
        f"{_PR}/merge",
        status=202,
        headers={"Location": "https://evil.example.com/steal"},
    )
    client = make_client(fake)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client.merge_pr(repo=repo(), pr_number=42, method="squash")
    assert "origin" in str(excinfo.value).lower()
    assert fake.calls("GET") == []  # never followed the foreign origin


async def test_merge_pr_async_202_without_poll_location_raises() -> None:
    fake = FakeBitBucket()
    fake.enqueue("POST", f"{_PR}/merge", status=202, json={"unexpected": "shape"})
    client = make_client(fake)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client.merge_pr(repo=repo(), pr_number=42, method="squash")
    assert "poll location" in str(excinfo.value).lower()
