"""BitbucketClient + parsing edge-coverage tests (issue #345 Part 2).

Closes the remaining branches: account-id caching/error/non-dict, the issue-fallback
PR-page URL, step-log error tolerance, invalid ``Retry-After``, empty-body parsing,
cacheable params, and the pure-parsing guard branches.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from awf.common.bitbucket_client import (
    BITBUCKET_ISSUE_CAPTURE_FAILED,
    BitbucketAuth,
    BitbucketClient,
    BitbucketClientError,
)
from awf.common.bitbucket_client_parsing import (
    _comment_account_id,
    _comment_author,
    _tail,
    build_blocking_reviews,
    build_general_review_comments,
    build_review_threads,
    extract_diffstat_paths,
    html_href,
    latest_external_review_activity,
    parse_bb_datetime,
    parse_check_timings,
)
from awf.common.github_client import RepoRef

from ._helpers import FakeBitbucket, RecordingSleep, make_client, pr_payload, repo

pytestmark = pytest.mark.unit

_HEAD = "s" * 40
_REPO = "/2.0/repositories/workspace/repo"
_PR = f"{_REPO}/pullrequests/42"


def _seed_fetch_status(fake: FakeBitbucket, *, account_body: object = None) -> None:
    fake.enqueue("GET", _PR, json=pr_payload())
    fake.page("GET", f"{_REPO}/commit/{_HEAD}/statuses", values=[])
    fake.page("GET", f"{_PR}/comments", values=[])
    fake.page("GET", f"{_PR}/diffstat", values=[])
    fake.page("GET", f"{_PR}/tasks", values=[])
    fake.enqueue(
        "GET", "/2.0/user", json=account_body if account_body is not None else {"account_id": "v"}
    )


# ── Client edge branches ──────────────────────────────────────────────────────


async def test_fetch_pr_status_changed_paths_via_redirected_diffstat() -> None:
    # BB Cloud's PR diffstat answers 302 → resolved diffstat resource. The client
    # must follow the hop so ``changed_paths`` (used by the monitor's scope-policy
    # check) reflects the real files instead of silently collapsing to empty.
    fake = FakeBitbucket()
    fake.enqueue("GET", _PR, json=pr_payload())
    fake.page("GET", f"{_REPO}/commit/{_HEAD}/statuses", values=[])
    fake.page("GET", f"{_PR}/comments", values=[])
    fake.enqueue(
        "GET",
        f"{_PR}/diffstat",
        status=302,
        headers={"Location": f"https://api.bitbucket.org{_REPO}/diffstat/{_HEAD}..d"},
    )
    fake.page(
        "GET",
        f"{_REPO}/diffstat/{_HEAD}..d",
        values=[{"new": {"path": "src/changed.py"}}],
    )
    fake.page("GET", f"{_PR}/tasks", values=[])
    fake.enqueue("GET", "/2.0/user", json={"account_id": "v"})
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert status.changed_paths == ("src/changed.py",)


async def test_fetch_pr_status_non_dict_body_raises() -> None:
    fake = FakeBitbucket()
    fake.enqueue("GET", _PR, json=[1, 2, 3])  # 200 but not an object
    client = make_client(fake)
    with pytest.raises(BitbucketClientError) as excinfo:
        await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert "not found" in excinfo.value.body


async def test_account_id_is_cached_across_status_fetches() -> None:
    fake = FakeBitbucket()
    _seed_fetch_status(fake)
    # Second status fetch re-enqueues everything except /2.0/user (served from cache).
    fake.enqueue("GET", _PR, json=pr_payload())
    fake.page("GET", f"{_REPO}/commit/{_HEAD}/statuses", values=[])
    fake.page("GET", f"{_PR}/comments", values=[])
    fake.page("GET", f"{_PR}/diffstat", values=[])
    fake.page("GET", f"{_PR}/tasks", values=[])
    client = make_client(fake)
    await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert len(fake.calls("GET")) == 11  # only one /2.0/user across both fetches
    assert sum(1 for r in fake.requests if r.url.path == "/2.0/user") == 1


async def test_fetch_pr_status_populates_review_activity_anchor() -> None:
    # A late external reviewer comment must populate latest_external_review_activity_at
    # and quiet_period_anchor_at so the non-check-reviewer settle gate anchors on the
    # new review activity instead of decaying to the head-only fallback (which would
    # let a fresh Bitbucket comment be merged past immediately). Mirrors GitHub.
    fake = FakeBitbucket()
    fake.enqueue("GET", _PR, json=pr_payload())
    fake.page("GET", f"{_REPO}/commit/{_HEAD}/statuses", values=[])
    fake.page(
        "GET",
        f"{_PR}/comments",
        values=[
            {
                "id": 9,
                "content": {"raw": "please fix"},
                "user": {"account_id": "rev"},
                "created_on": "2024-05-01T00:00:00Z",
                "updated_on": "2024-05-02T00:00:00Z",
            }
        ],
    )
    fake.page("GET", f"{_PR}/diffstat", values=[])
    fake.page("GET", f"{_PR}/tasks", values=[])
    fake.enqueue("GET", "/2.0/user", json={"account_id": "me"})
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert status.latest_external_review_activity_at == datetime(2024, 5, 2, tzinfo=UTC)
    assert status.latest_external_review_activity_source == "issue_comment"
    assert status.quiet_period_anchor_at == datetime(2024, 5, 2, tzinfo=UTC)
    assert status.quiet_period_anchor_source == "issue_comment"


async def test_fetch_pr_status_anchors_on_resolved_reviewer_task() -> None:
    # A task-only PR whose task the agent already resolved: no pending comments/threads,
    # but the resolved reviewer task must still populate the review-activity anchor so the
    # non-check-reviewer settle window is honored instead of merging immediately.
    fake = FakeBitbucket()
    fake.enqueue("GET", _PR, json=pr_payload())
    fake.page("GET", f"{_REPO}/commit/{_HEAD}/statuses", values=[])
    fake.page("GET", f"{_PR}/comments", values=[])
    fake.page("GET", f"{_PR}/diffstat", values=[])
    fake.page(
        "GET",
        f"{_PR}/tasks",
        values=[
            {
                "id": 7,
                "state": "RESOLVED",
                "content": {"raw": "please fix"},
                "creator": {"account_id": "rev"},
                "created_on": "2024-05-01T00:00:00Z",
                "updated_on": "2024-05-03T00:00:00Z",
            }
        ],
    )
    fake.enqueue("GET", "/2.0/user", json={"account_id": "me"})
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    # Resolved task does not block merge but does anchor the quiet window.
    assert status.unresolved_inline_threads == ()
    assert status.latest_external_review_activity_at == datetime(2024, 5, 3, tzinfo=UTC)
    assert status.latest_external_review_activity_source == "review_task"
    assert status.quiet_period_anchor_at == datetime(2024, 5, 3, tzinfo=UTC)
    assert status.quiet_period_anchor_source == "review_task"


async def test_fetch_pr_status_without_activity_leaves_anchor_unset() -> None:
    # No external review activity and no PR timestamps → anchor stays None so the
    # settle gate keeps its head-only fallback (unchanged behavior).
    fake = FakeBitbucket()
    _seed_fetch_status(fake)
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert status.latest_external_review_activity_at is None
    assert status.quiet_period_anchor_at is None


async def test_account_id_fetch_error_propagates() -> None:
    # A failing /2.0/user lookup must surface as a BitbucketClientError rather
    # than be swallowed: silently disabling self-comment filtering would treat
    # AWF's own comments as external feedback. The transient 5xx is preserved so
    # the monitor's transient-retry path can keep polling.
    fake = FakeBitbucket()
    fake.enqueue("GET", _PR, json=pr_payload())
    fake.page("GET", f"{_REPO}/commit/{_HEAD}/statuses", values=[])
    fake.page("GET", f"{_PR}/comments", values=[])
    fake.page("GET", f"{_PR}/diffstat", values=[])
    fake.page("GET", f"{_PR}/tasks", values=[])
    fake.enqueue("GET", "/2.0/user", status=500, json={"error": "x"})
    client = make_client(fake)
    with pytest.raises(BitbucketClientError) as excinfo:
        await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert excinfo.value.status == 500


async def test_account_id_failure_is_not_cached_then_succeeds() -> None:
    # A failed lookup must not be cached as account_id=None: a later poll has to
    # retry /2.0/user so self-comment filtering recovers after a transient blip.
    # All responses are enqueued up front; the FIFO fake pops the first /2.0/user
    # (500) on the first poll and serves the second (200) on the next.
    fake = FakeBitbucket()
    fake.enqueue("GET", _PR, json=pr_payload())
    fake.page("GET", f"{_REPO}/commit/{_HEAD}/statuses", values=[])
    fake.page("GET", f"{_PR}/comments", values=[])  # first poll: empty
    fake.page(
        "GET",
        f"{_PR}/comments",  # second poll: the viewer's own comment
        values=[
            {
                "id": 1,
                "content": {"raw": "hi"},
                "user": {"account_id": "me"},
                "created_on": "2024-01-01T00:00:00Z",
            }
        ],
    )
    fake.page("GET", f"{_PR}/diffstat", values=[])
    fake.page("GET", f"{_PR}/tasks", values=[])
    fake.enqueue("GET", "/2.0/user", status=500, json={"error": "x"})  # first poll fails
    fake.enqueue("GET", "/2.0/user", json={"account_id": "me"})  # second poll succeeds
    client = make_client(fake)
    with pytest.raises(BitbucketClientError):
        await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    # Second poll: /2.0/user is retried and now succeeds, marking the viewer's
    # own comment as authored-by-viewer so it is filtered out.
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert sum(1 for r in fake.requests if r.url.path == "/2.0/user") == 2
    assert len(status.unresolved_review_comments) == 0


async def test_account_id_non_dict_body_leaves_account_none() -> None:
    fake = FakeBitbucket()
    _seed_fetch_status(fake, account_body=[])  # list, not an object
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert status.number == 42


async def test_account_id_malformed_body_is_not_cached_then_succeeds() -> None:
    # A malformed 200 (non-dict, or a dict without account_id/uuid) must not be
    # cached as a terminal result: that would permanently disable viewer-self
    # filtering after a single bad response. A later poll has to retry /2.0/user
    # so self-comment filtering recovers, matching _current_account_id's contract.
    fake = FakeBitbucket()
    fake.enqueue("GET", _PR, json=pr_payload())
    fake.page("GET", f"{_REPO}/commit/{_HEAD}/statuses", values=[])
    fake.page("GET", f"{_PR}/comments", values=[])  # first poll: empty
    fake.page(
        "GET",
        f"{_PR}/comments",  # second poll: the viewer's own comment
        values=[
            {
                "id": 1,
                "content": {"raw": "hi"},
                "user": {"account_id": "me"},
                "created_on": "2024-01-01T00:00:00Z",
            }
        ],
    )
    fake.page("GET", f"{_PR}/diffstat", values=[])
    fake.page("GET", f"{_PR}/tasks", values=[])
    fake.enqueue("GET", "/2.0/user", json={})  # first poll: 200 but no account_id/uuid
    fake.enqueue("GET", "/2.0/user", json={"account_id": "me"})  # second poll succeeds
    client = make_client(fake)
    await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    # Second poll: /2.0/user is retried (not served from the terminal cache) and now
    # resolves the viewer, so their own comment is filtered out.
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert sum(1 for r in fake.requests if r.url.path == "/2.0/user") == 2
    assert len(status.unresolved_review_comments) == 0


async def test_create_issue_fallback_returns_pr_page_url_when_comment_has_no_href() -> None:
    fake = FakeBitbucket()
    _seed_fetch_status(fake)
    fake.enqueue("POST", f"{_REPO}/issues", status=404, json={"type": "error"})
    fake.enqueue("POST", f"{_PR}/comments", json={"id": 7})  # no links.html.href
    client = make_client(fake)
    await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    url = await client.create_issue(repo=repo(), title="t", body="b")
    assert url == "https://bitbucket.org/workspace/repo/pull-requests/42"


async def test_create_issue_fallback_comment_failure_propagates_error() -> None:
    # When the issue tracker is disabled (404) AND the fallback comment POST also
    # fails (e.g. 403 lacking comment permission), nothing durable was captured. The
    # error must PROPAGATE rather than returning a PR-page URL: the deferred-capture
    # call site in fix_cycle.py catches BitbucketClientError and downgrades to
    # needs_human / requeues, so swallowing it here would let the caller record a
    # capture that never happened and resolve the reviewer's thread, dropping the
    # follow-up. This matches create_issue's documented fail-safe contract.
    fake = FakeBitbucket()
    _seed_fetch_status(fake)
    fake.enqueue("POST", f"{_REPO}/issues", status=404, json={"type": "error"})
    fake.enqueue("POST", f"{_PR}/comments", status=403, json={"type": "error"})
    client = make_client(fake)
    await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    with pytest.raises(BitbucketClientError) as excinfo:
        await client.create_issue(repo=repo(), title="t", body="b")
    assert excinfo.value.status == 403


async def test_create_issue_fallback_without_pr_context_raises() -> None:
    # When the issue tracker is disabled (404) AND no PR context has been
    # remembered, nothing durable can be captured: no issue is filed and there
    # is no PR to comment on. Returning the generic repo issues-page URL here
    # would let the deferred-capture call site in fix_cycle.py record a capture
    # that never happened and resolve the reviewer's thread, dropping the
    # follow-up. The error must PROPAGATE so the caller downgrades to
    # needs_human, matching create_issue's documented fail-safe contract.
    #
    # The reason code must be BITBUCKET_ISSUE_CAPTURE_FAILED, NOT
    # BITBUCKET_ISSUE_TRACKER_DISABLED: the latter is catalogued as "note
    # captured on the PR — no action required", which would mislead operators
    # since no comment ran on this path (nothing durable was recorded).
    fake = FakeBitbucket()
    fake.enqueue("POST", f"{_REPO}/issues", status=404, json={"type": "error"})
    client = make_client(fake)  # no fetch_pr_status → no remembered PR context
    with pytest.raises(BitbucketClientError) as excinfo:
        await client.create_issue(repo=repo(), title="t", body="b")
    assert excinfo.value.status == 404
    assert excinfo.value.reason_code == BITBUCKET_ISSUE_CAPTURE_FAILED


async def test_step_log_404_is_tolerated_as_empty() -> None:
    fake = FakeBitbucket()
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


async def test_step_log_follows_offorigin_307_redirect_without_forge_auth() -> None:
    """BB's step-log endpoint answers a documented 307 to off-origin log storage.

    The shared redirect path origin-checks every ``Location`` (SSRF guard) and would
    reject that hop, raising a non-transient ``BitbucketClientError`` that escapes the
    "best-effort, never raises" step-log fetch and loses the CI step evidence. The log
    redirect must instead be followed to the signed storage URL — but with the forge
    ``Authorization`` header stripped, so credentials never reach the storage host.
    """
    fake = FakeBitbucket()
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
    fake.enqueue(
        "GET",
        f"{_REPO}/pipelines/p1/steps/s1/log",
        status=307,
        headers={"Location": "https://bbuseruploads.s3.amazonaws.com/log-storage/s1"},
    )
    fake.enqueue("GET", "/log-storage/s1", text="E   assert 1 == 2\nFAILED tests/test_x.py")
    client = make_client(fake)
    failures = await client.fetch_failing_check_logs(repo=repo(), pr_number=42, head_sha=_HEAD)
    assert "assert 1 == 2" in failures[0].log_excerpt
    # The off-origin storage hop must not carry the forge credential.
    storage_call = next(r for r in fake.calls("GET") if r.url.path == "/log-storage/s1")
    assert "Authorization" not in storage_call.headers


async def test_step_log_second_offorigin_redirect_is_refused_not_followed() -> None:
    """The log-redirect grant is single-use: only ONE off-origin hop is allowed.

    Bitbucket documents exactly one 307 from the step-log endpoint to a signed,
    off-origin storage URL. Once that first hop is followed (with the forge auth
    stripped), the ``allow_log_redirect`` grant is consumed — so a *second*
    off-origin redirect (e.g. a compromised or adversarial storage host bouncing the
    now-unauthenticated client toward an internal/arbitrary target) faces the strict
    SSRF guard and is refused before being issued. The best-effort log fetch tolerates
    the resulting ``BitbucketClientError`` by yielding an empty excerpt rather than
    chaining off-origin requests.
    """
    fake = FakeBitbucket()
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
    fake.enqueue(
        "GET",
        f"{_REPO}/pipelines/p1/steps/s1/log",
        status=307,
        headers={"Location": "https://bbuseruploads.s3.amazonaws.com/log-storage/s1"},
    )
    # The storage host bounces to a SECOND off-origin host — must be refused, not chased.
    fake.enqueue(
        "GET",
        "/log-storage/s1",
        status=307,
        headers={"Location": "https://evil.internal.example/secret"},
    )
    fake.enqueue("GET", "/secret", text="leaked-internal-data")
    client = make_client(fake)
    failures = await client.fetch_failing_check_logs(repo=repo(), pr_number=42, head_sha=_HEAD)
    assert failures[0].log_excerpt == ""
    # The second off-origin target must never be contacted.
    assert all(r.url.host != "evil.internal.example" for r in fake.calls("GET"))


async def test_step_log_transport_failure_yields_empty_log_not_raise() -> None:
    """A transport timeout/reset on the step-log endpoint (or its signed storage
    redirect) must not escape the "best-effort, never raises" log fetch.

    ``_request_text`` already tolerates 4xx/5xx by returning ``""``; a transport-level
    ``BitbucketClientError`` (reason ``BITBUCKET_TRANSPORT_ERROR``) must be tolerated the
    same way. Otherwise it propagates through ``fetch_failing_check_logs`` to
    ``_fetch_status_for_decision``, which treats the whole PR status poll as a transient
    Bitbucket fault and retries forever instead of surfacing the failing CI with an empty
    log — a persistent log-storage outage would block the monitor from acting.
    """
    canned = {
        ("GET", f"{_REPO}/commit/{_HEAD}/statuses"): httpx.Response(
            200, json={"values": [{"state": "FAILED", "name": "Pipeline"}]}
        ),
        ("GET", f"{_REPO}/pipelines/"): httpx.Response(200, json={"values": [{"uuid": "p1"}]}),
        ("GET", f"{_REPO}/pipelines/p1/steps/"): httpx.Response(
            200,
            json={
                "values": [{"uuid": "s1", "name": "Test", "state": {"result": {"name": "FAILED"}}}]
            },
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/steps/s1/log"):
            raise httpx.ConnectError("connection reset by peer", request=request)
        return canned[(request.method.upper(), request.url.path)]

    client = BitbucketClient(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.bitbucket.org"
        ),
        auth=BitbucketAuth(mode="bearer", api_token="bb-token-aaaaaaaaaaaa"),
    )
    failures = await client.fetch_failing_check_logs(repo=repo(), pr_number=42, head_sha=_HEAD)
    # The CI failure is still surfaced, just with no log evidence — not raised.
    assert failures[0].conclusion == "FAILURE"
    assert failures[0].log_excerpt == ""


async def test_invalid_retry_after_falls_back_to_backoff() -> None:
    fake = FakeBitbucket()
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
    fake = FakeBitbucket()
    fake.enqueue("POST", f"{_PR}/comments")  # 200, empty body
    client = make_client(fake)
    await client.post_comment(repo=repo(), pr_number=42, body="hi")  # must not raise


async def test_cacheable_request_with_params_round_trips() -> None:
    fake = FakeBitbucket()
    fake.enqueue("GET", "/x", json={"v": 1}, headers={"ETag": "e1"})
    fake.enqueue("GET", "/x", status=304, headers={"ETag": "e1"})
    client = make_client(fake)
    first = await client._request_json("GET", "/x", operation="op", params={"a": "b"}, cache=True)
    second = await client._request_json("GET", "/x", operation="op", params={"a": "b"}, cache=True)
    assert first == second == {"v": 1}


async def test_304_uses_prefetched_body_when_entry_evicted_mid_flight() -> None:
    """A concurrent task evicting the bounded ETag cache during the ``await`` must
    not turn a 304 into a ``BitbucketClientError`` — the body captured before the
    request is sent is what gets returned."""
    fake = FakeBitbucket()
    fake.enqueue("GET", "/x", json={"v": 1}, headers={"ETag": "e1"})
    client = make_client(fake)
    first = await client._request_json("GET", "/x", operation="op", cache=True)
    assert first == {"v": 1}

    # Swap in a transport whose handler returns 304 *and* clears the cache,
    # modelling a concurrent task evicting our LRU entry while the request is
    # in flight (after If-None-Match is sent, before the 304 is processed).
    def evicting_handler(request: httpx.Request) -> httpx.Response:
        client._etag_cache.clear()
        return httpx.Response(304, headers={"ETag": "e1"})

    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(evicting_handler),
        base_url="https://api.bitbucket.org",
    )
    second = await client._request_json("GET", "/x", operation="op", cache=True)
    assert second == {"v": 1}


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


def test_thread_same_timestamp_replies_ordered_numerically() -> None:
    # Two replies posted in the same second whose ids span a power-of-ten boundary
    # (9 and 10) must order numerically: the secondary sort key is the integer id,
    # not ``str(id)`` (which would sort "10" < "9" and surface the wrong comment as
    # the thread's first external comment).
    repo_ref = RepoRef(owner="ws", name="repo", forge="bitbucket")
    root = {
        "id": 1,
        "content": {"raw": "root"},
        "user": {"account_id": "viewer"},
        "created_on": "2024-01-01T00:00:00Z",
        "inline": {"path": "a.py", "to": 5},
    }
    reply_ten = {
        "id": 10,
        "parent": {"id": 1},
        "content": {"raw": "ten"},
        "user": {"account_id": "ext"},
        "created_on": "2024-01-01T00:00:05Z",
        "inline": {"path": "a.py", "to": 5},
    }
    reply_nine = {
        "id": 9,
        "parent": {"id": 1},
        "content": {"raw": "nine"},
        "user": {"account_id": "ext"},
        "created_on": "2024-01-01T00:00:05Z",
        "inline": {"path": "a.py", "to": 5},
    }
    # Feed them in id-descending order so a stable sort cannot mask the tiebreak.
    threads = build_review_threads(
        [root, reply_ten, reply_nine],
        repo=repo_ref,
        pr_number=1,
        account_id="viewer",
    )
    assert len(threads) == 1
    # The viewer-authored root is dropped; the first external comment is reply 9.
    assert threads[0].body_excerpt == "nine"
    assert tuple(c.body for c in threads[0].comments) == ("nine", "ten")


def test_general_comments_skip_inline_deleted_and_empty() -> None:
    comments = [
        {"id": 1, "inline": {"path": "a.py"}, "content": {"raw": "inline"}},  # inline → skip
        {"id": 2, "deleted": True, "content": {"raw": "gone"}},  # deleted → skip
        {"id": 3, "content": {"raw": "   "}},  # empty body → skip
        {"id": 4, "content": {"raw": "real"}, "user": {"account_id": "me"}},
    ]
    reviews = build_general_review_comments(comments, account_id="me")  # id 4 is viewer
    assert reviews == ()


def test_general_comments_skip_resolved_top_level() -> None:
    comments = [
        # resolved top-level comment → skip (mirrors the inline-thread resolution check)
        {"id": 1, "content": {"raw": "settled"}, "resolution": {"type": "resolved"}},
        # unresolved top-level comment → kept
        {"id": 2, "content": {"raw": "pending"}},
    ]
    reviews = build_general_review_comments(comments, account_id=None)
    assert [c.comment_id for c in reviews] == ["bbcomment:2"]


def test_latest_external_review_activity_picks_newest_external() -> None:
    comments = [
        # viewer-authored → ignored even though newest.
        {
            "id": 1,
            "user": {"account_id": "me"},
            "updated_on": "2024-06-01T00:00:00Z",
            "inline": {"path": "a.py"},
        },
        {"id": 2, "deleted": True, "updated_on": "2024-05-01T00:00:00Z"},  # deleted → ignored
        {"user": {"account_id": "rev"}, "updated_on": "2024-04-01T00:00:00Z"},  # no id → ignored
        # external inline comment (older).
        {
            "id": 4,
            "user": {"account_id": "rev"},
            "created_on": "2024-01-01T00:00:00Z",
            "inline": {"path": "a.py"},
        },
        # external general comment (newer) → wins, source issue_comment.
        {"id": 5, "user": {"account_id": "rev"}, "updated_on": "2024-01-15T00:00:00Z"},
    ]
    at, source = latest_external_review_activity(comments, account_id="me")
    assert at == datetime(2024, 1, 15, tzinfo=UTC)
    assert source == "issue_comment"


def test_latest_external_review_activity_inline_source_and_empty() -> None:
    assert latest_external_review_activity([], account_id=None) == (None, None)
    # A comment with no parseable timestamp is skipped (no activity).
    assert latest_external_review_activity(
        [{"id": 7, "user": {"account_id": "rev"}}], account_id="me"
    ) == (None, None)
    inline = [
        {
            "id": 8,
            "user": {"account_id": "rev"},
            "created_on": "2024-01-01T00:00:00Z",
            "inline": {"path": "a.py"},
        }
    ]
    at, source = latest_external_review_activity(inline, account_id="me")
    assert at == datetime(2024, 1, 1, tzinfo=UTC)
    assert source == "review_thread_comment"


def test_latest_external_review_activity_includes_resolved_task() -> None:
    # A PR whose only feedback is a reviewer task: once the agent resolves it the task
    # no longer blocks merge, but its review activity must still anchor the quiet window
    # (resolved tasks included) so the settle gate does not decay to PR-creation and let
    # the PR merge immediately after task resolution.
    tasks = [
        {
            "id": 1,
            "state": "RESOLVED",
            "creator": {"account_id": "rev"},
            "created_on": "2024-03-01T00:00:00Z",
            "updated_on": "2024-03-05T00:00:00Z",  # newest → wins over the comment
        }
    ]
    comments = [
        {"id": 9, "user": {"account_id": "rev"}, "updated_on": "2024-03-02T00:00:00Z"},
    ]
    at, source = latest_external_review_activity(comments, account_id="me", tasks=tasks)
    assert at == datetime(2024, 3, 5, tzinfo=UTC)
    assert source == "review_task"


def test_latest_external_review_activity_ignores_viewer_and_untimed_tasks() -> None:
    tasks = [
        # viewer-authored → ignored even though newest.
        {"id": 1, "creator": {"account_id": "me"}, "updated_on": "2024-09-01T00:00:00Z"},
        # viewer-authored via uuid-only creator (mirrors comment identity) → ignored.
        {"id": 2, "creator": {"uuid": "me"}, "updated_on": "2024-09-02T00:00:00Z"},
        {"creator": {"account_id": "rev"}, "updated_on": "2024-08-01T00:00:00Z"},  # no id
        {"id": 3, "creator": {"account_id": "rev"}},  # no parseable timestamp → skipped
        # external task falls back to created_on when updated_on is absent.
        {"id": 4, "creator": {"account_id": "rev"}, "created_on": "2024-01-01T00:00:00Z"},
    ]
    at, source = latest_external_review_activity([], account_id="me", tasks=tasks)
    assert at == datetime(2024, 1, 1, tzinfo=UTC)
    assert source == "review_task"


def test_build_blocking_reviews_flags_changes_requested_participant() -> None:
    pr = {
        "participants": [
            {"state": "approved", "user": {"account_id": "a", "display_name": "Approver"}},
            {
                "state": "changes_requested",
                "approved": False,
                "user": {"account_id": "b", "display_name": "Blocker"},
            },
        ]
    }
    blockers = build_blocking_reviews(pr, account_id="me")
    assert len(blockers) == 1
    blocker = blockers[0]
    assert blocker.blocks_merge is True
    assert blocker.state == "CHANGES_REQUESTED"
    assert blocker.author == "Blocker"
    assert blocker.comment_id == "bbreview:b"


def test_build_blocking_reviews_excludes_viewer_and_dedupes() -> None:
    pr = {
        "participants": [
            # Viewer's own requested-changes state must never block AWF.
            {"state": "changes_requested", "user": {"account_id": "me"}},
            # Two payloads for the same reviewer collapse to one blocker.
            {"state": "CHANGES_REQUESTED", "user": {"account_id": "b"}},
            {"state": "changes_requested", "user": {"account_id": "b"}},
        ]
    }
    blockers = build_blocking_reviews(pr, account_id="me")
    assert [b.comment_id for b in blockers] == ["bbreview:b"]


@pytest.mark.parametrize(
    "pr",
    [
        {},  # no participants key
        {"participants": "nope"},  # non-list
        {"participants": ["x", {"state": None}, {"state": "approved"}]},  # no blockers
    ],
)
def test_build_blocking_reviews_returns_empty_without_blockers(pr: dict) -> None:
    assert build_blocking_reviews(pr, account_id=None) == ()


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
    fake = FakeBitbucket()
    fake.enqueue("GET", _PR, json=pr_payload())
    # statuses page mixes a non-dict value (skipped) with a real status.
    fake.enqueue(
        "GET",
        f"{_REPO}/commit/{_HEAD}/statuses",
        json={"values": [{"state": "SUCCESSFUL", "name": "ok"}, "garbage"]},
    )
    fake.page("GET", f"{_PR}/comments", values=[])
    fake.enqueue("GET", f"{_PR}/diffstat")  # empty body → page is None → no pagination
    fake.page("GET", f"{_PR}/tasks", values=[])
    fake.enqueue("GET", "/2.0/user", json={"account_id": "v"})
    client = make_client(fake)
    status = await client.fetch_pr_status(repo=repo(), pr_number=42, base_behind_count=0)
    assert status.changed_paths == ()
    assert len(status.checks) == 1
