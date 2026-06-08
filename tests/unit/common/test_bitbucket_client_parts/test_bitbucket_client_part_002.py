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
    BITBUCKET_MERGE_TASK_TIMEOUT,
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
    tasks: list[dict] | None = None,
    account_id: str = "viewer-acct",
) -> None:
    fake.enqueue("GET", _PR, json=pr if pr is not None else pr_payload())
    fake.page("GET", f"{_REPO}/commit/{_HEAD}/statuses", values=statuses or [])
    fake.page("GET", f"{_PR}/comments", values=comments or [])
    fake.page("GET", f"{_PR}/diffstat", values=diffstat or [])
    fake.page("GET", f"{_PR}/tasks", values=tasks or [])
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
    # ≥1 commit status was fetched, so the "no checks observed" signal is off.
    assert status.no_checks_observed is False


async def test_fetch_pr_status_no_checks_observed_when_no_statuses() -> None:
    # A repo with Pipelines disabled returns an EMPTY commit-statuses list; the
    # signal is set True from that authoritative fetch (#469).
    fake = FakeBitBucket()
    _seed_fetch_status(fake, statuses=[])
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert status.no_checks_observed is True
    # Empty statuses still parse to PENDING (the gate-6 default), so without the
    # operator opt-out the monitor would keep waiting.
    assert status.check_state == CheckState.PENDING
    # A clean OPEN PR reports a KNOWN mergeable state regardless of CI presence, so
    # with the operator opt-out the decide() path proceeds to Merge() rather than
    # WaitForCI("unknown_mergeable_state"). Assert it explicitly (spec point 7).
    assert status.mergeable == MergeableState.MERGEABLE
    assert status.merge_state_status == MergeStateStatus.CLEAN


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


async def test_fetch_pr_status_statuses_fetch_failure_raises_not_no_checks() -> None:
    # A transport/HTTP failure while fetching commit statuses MUST raise
    # ForgeClientError, never degrade to a mergeable no-checks PRStatus that the
    # require_ci opt-out could then merge blind (#469 safety regression).
    fake = FakeBitBucket()
    fake.enqueue("GET", _PR, json=pr_payload())
    fake.enqueue("GET", f"{_REPO}/commit/{_HEAD}/statuses", status=500, json={"type": "error"})
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
    fake.page("GET", f"{_PR}/tasks", values=[])
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


async def test_merge_pr_409_in_progress_body_maps_to_transient_reason() -> None:
    # A 409 whose body says a merge is already in flight (e.g. a prior 202 async
    # merge whose poll was interrupted by a transient fault and re-issued by the
    # monitor) maps to the transient BITBUCKET_MERGE_IN_PROGRESS reason — not the
    # deterministic BITBUCKET_API_ERROR — so the monitor re-polls fetch_pr_status
    # instead of terminating the workspace on a merge that may still be completing.
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


async def test_merge_pr_409_already_merged_body_maps_to_transient_reason() -> None:
    # BitBucket also answers 409 when the PR "has already been merged" — a re-poll
    # of fetch_pr_status observes the terminal MERGED state, so this is recoverable
    # and must stay transient rather than being misreported as a merge failure.
    fake = FakeBitBucket()
    fake.enqueue(
        "POST",
        f"{_PR}/merge",
        status=409,
        json={"error": {"message": "This pull request has already been merged."}},
    )
    client = make_client(fake)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client.merge_pr(repo=repo(), pr_number=42, method="squash")
    assert excinfo.value.reason_code == BITBUCKET_MERGE_IN_PROGRESS


async def test_merge_pr_409_conflict_body_stays_deterministic() -> None:
    # BitBucket overloads 409 for non-recoverable merge failures too (merge
    # conflicts, unmet merge checks). Those carry no "in progress"/"already
    # merged" signal, so they must stay deterministic (BITBUCKET_API_ERROR) — a
    # blanket transient remap would poll forever instead of surfacing
    # BITBUCKET_MERGE_FAILED and notifying a human.
    fake = FakeBitBucket()
    fake.enqueue(
        "POST",
        f"{_PR}/merge",
        status=409,
        json={"error": {"message": "The pull request has merge conflicts."}},
    )
    client = make_client(fake)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client.merge_pr(repo=repo(), pr_number=42, method="squash")
    assert excinfo.value.reason_code == BITBUCKET_API_ERROR
    assert excinfo.value.status == 409


async def test_merge_pr_409_unrelated_in_progress_body_stays_deterministic() -> None:
    # A bare "in progress" substring is too broad: BitBucket overloads 409 on the
    # merge POST for other concurrent-modification conflicts whose bodies read e.g.
    # "another action is in progress". That is not a merge already in flight, so it
    # must stay deterministic (BITBUCKET_API_ERROR) — classifying it transient would
    # poll forever instead of surfacing the conflict and notifying a human.
    fake = FakeBitBucket()
    fake.enqueue(
        "POST",
        f"{_PR}/merge",
        status=409,
        json={"error": {"message": "Another action is already in progress."}},
    )
    client = make_client(fake)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client.merge_pr(repo=repo(), pr_number=42, method="squash")
    assert excinfo.value.reason_code == BITBUCKET_API_ERROR
    assert excinfo.value.status == 409


async def test_merge_pr_409_merge_in_progress_phrasing_maps_to_transient() -> None:
    # The merge-specific in-progress phrasing (the genuine recoverable case) still
    # maps to the transient reason even though the bare "in progress" substring was
    # removed: the body mentions both "merge" and "in progress".
    fake = FakeBitBucket()
    fake.enqueue(
        "POST",
        f"{_PR}/merge",
        status=409,
        json={"error": {"message": "A merge for this pull request is already in progress."}},
    )
    client = make_client(fake)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client.merge_pr(repo=repo(), pr_number=42, method="squash")
    assert excinfo.value.reason_code == BITBUCKET_MERGE_IN_PROGRESS


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


async def test_merge_pr_async_202_error_envelope_raises_immediately() -> None:
    # A failed async merge (conflict, unmet merge checks) makes the task-status
    # endpoint answer with an error envelope instead of a ``task_status`` value.
    # That payload must be treated as an immediate terminal non-success so the
    # actionable merge-failure message surfaces, rather than leaving ``task_status``
    # empty and polling until the budget trips into a generic timeout.
    fake = FakeBitBucket()
    poll_url = f"https://api.bitbucket.org{_PR}/merge/task-status/task-err"
    fake.enqueue("POST", f"{_PR}/merge", status=202, headers={"Location": poll_url})
    fake.enqueue(
        "GET",
        f"{_PR}/merge/task-status/task-err",
        json={"type": "error", "error": {"message": "merge checks have not passed"}},
    )
    # A high poll budget proves the error short-circuits rather than exhausting it:
    # only one GET is enqueued, so a non-terminal read would fail to dequeue.
    client = make_client(fake, max_merge_polls=10, merge_poll_delay_seconds=0)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client.merge_pr(repo=repo(), pr_number=42, method="squash")
    assert "merge checks have not passed" in str(excinfo.value)
    # Not a timeout: the real reason must not be masked by the generic poll-budget
    # message, and the HTTP status is omitted (the poll GET was 200, not the failure).
    assert "did not complete" not in str(excinfo.value)
    assert excinfo.value.status is None
    assert excinfo.value.reason_code != BITBUCKET_MERGE_TASK_TIMEOUT


async def test_merge_pr_async_202_error_envelope_without_message_raises() -> None:
    # A bare error envelope (no nested message) still terminates immediately with a
    # distinct-from-timeout error rather than polling to budget exhaustion.
    fake = FakeBitBucket()
    poll_url = f"https://api.bitbucket.org{_PR}/merge/task-status/task-err2"
    fake.enqueue("POST", f"{_PR}/merge", status=202, headers={"Location": poll_url})
    fake.enqueue(
        "GET",
        f"{_PR}/merge/task-status/task-err2",
        json={"error": {"detail": "no message field here"}},
    )
    client = make_client(fake, max_merge_polls=10, merge_poll_delay_seconds=0)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client.merge_pr(repo=repo(), pr_number=42, method="squash")
    assert "reported an error" in str(excinfo.value)
    assert "did not complete" not in str(excinfo.value)
    assert excinfo.value.status is None


async def test_merge_pr_async_202_poll_budget_exhausted_raises() -> None:
    fake = FakeBitBucket()
    poll_url = f"https://api.bitbucket.org{_PR}/merge/task-status/task-4"
    fake.enqueue("POST", f"{_PR}/merge", status=202, headers={"Location": poll_url})
    fake.enqueue("GET", f"{_PR}/merge/task-status/task-4", json={"task_status": "PENDING"})
    client = make_client(fake, max_merge_polls=2, merge_poll_delay_seconds=0)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client.merge_pr(repo=repo(), pr_number=42, method="squash")
    assert "did not complete" in str(excinfo.value)
    # The timeout must not reuse the original 202 POST status: ``status=202`` would
    # be indistinguishable from a 202 Accepted and from a non-success terminal task.
    assert excinfo.value.status is None
    assert "status=202" not in str(excinfo.value)
    # A still-PENDING task at budget exhaustion does not mean the merge failed —
    # BitBucket may still complete it. The distinct reason code lets the monitor
    # treat the timeout as an in-flight merge (cancel + re-poll) instead of a hard
    # deterministic failure (which would post a spurious "merge rejected" comment).
    assert excinfo.value.reason_code == BITBUCKET_MERGE_TASK_TIMEOUT


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
