"""BitBucketClient cross-cutting transport tests (issue #345 Part 2).

Covers the shared ``_request`` machinery (decision D2) and auth (D6): header
construction for both credential modes, 429/``Retry-After`` backoff, proactive
``X-RateLimit-NearLimit`` slow-down, ETag/``If-None-Match`` conditional requests with
a bounded cache, cursor pagination, error-status mapping, and secret redaction.
"""

from __future__ import annotations

import base64

import httpx
import pytest

from awf.common.bitbucket_client import (
    BITBUCKET_API_ERROR,
    BITBUCKET_AUTH_FAILED,
    BITBUCKET_AUTH_NOT_CONFIGURED,
    BITBUCKET_RATE_LIMITED,
    BITBUCKET_TRANSPORT_ERROR,
    BitBucketAuth,
    BitBucketClient,
    BitBucketClientError,
)

from ._helpers import FakeBitBucket, RecordingSleep, make_client, repo

pytestmark = pytest.mark.unit


def _pr_collection_path() -> str:
    return "/2.0/repositories/workspace/repo/pullrequests"


async def _create_pr(client: BitBucketClient) -> str:
    return await client.create_pull_request(
        repo=repo(), base="development", head="feature/head", title="t", body="b"
    )


# ── Auth (D6) ──────────────────────────────────────────────────────────────


def test_bearer_header_value() -> None:
    auth = BitBucketAuth(mode="bearer", api_token="abc123token")
    assert auth.header_value() == "Bearer abc123token"


def test_basic_header_value_encodes_email_and_token() -> None:
    auth = BitBucketAuth(mode="basic", api_token="tok", email="dev@example.com")
    value = auth.header_value()
    assert value.startswith("Basic ")
    decoded = base64.b64decode(value.removeprefix("Basic ")).decode()
    assert decoded == "dev@example.com:tok"


async def test_request_sends_authorization_header() -> None:
    fake = FakeBitBucket()
    fake.enqueue("POST", _pr_collection_path(), json={"links": {"html": {"href": "u"}}})
    auth = BitBucketAuth(mode="basic", api_token="tok", email="dev@example.com")
    client = make_client(fake, auth=auth)
    await _create_pr(client)
    sent = fake.calls("POST")[0].headers["Authorization"]
    assert sent == auth.header_value()


def test_auth_from_env_bearer() -> None:
    auth = BitBucketAuth.from_env({"BITBUCKET_AUTH_MODE": "bearer", "BITBUCKET_API_TOKEN": "tok"})
    assert auth.mode == "bearer"
    assert auth.email is None


def test_auth_from_env_defaults_to_basic_requires_email() -> None:
    with pytest.raises(BitBucketClientError) as excinfo:
        BitBucketAuth.from_env({"BITBUCKET_API_TOKEN": "tok"})
    assert excinfo.value.reason_code == BITBUCKET_AUTH_NOT_CONFIGURED


@pytest.mark.parametrize(
    "env",
    [
        {"BITBUCKET_AUTH_MODE": "basic", "BITBUCKET_EMAIL": "e@x.com"},  # no token
        {"BITBUCKET_AUTH_MODE": "app_password", "BITBUCKET_API_TOKEN": "t"},  # bad mode
        {"BITBUCKET_API_TOKEN": "t"},  # basic default, no email
    ],
)
def test_auth_from_env_rejects_invalid_config(env: dict[str, str]) -> None:
    with pytest.raises(BitBucketClientError) as excinfo:
        BitBucketAuth.from_env(env)
    assert excinfo.value.reason_code == BITBUCKET_AUTH_NOT_CONFIGURED


def test_secret_values_include_token_and_basic_encoding() -> None:
    auth = BitBucketAuth(mode="basic", api_token="tok", email="e@x.com")
    secrets = auth.secret_values()
    assert "tok" in secrets
    # The base64(email:token) blob (what lands on the wire) is redactable too.
    assert any(base64.b64decode(s).decode() == "e@x.com:tok" for s in secrets if _is_b64(s))


def _is_b64(value: str) -> bool:
    try:
        base64.b64decode(value)
    except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
        return False
    return True


# ── 429 / Retry-After backoff (D2) ───────────────────────────────────────────


async def test_429_then_success_honors_retry_after() -> None:
    fake = FakeBitBucket()
    fake.enqueue("POST", _pr_collection_path(), status=429, headers={"Retry-After": "7"})
    fake.enqueue("POST", _pr_collection_path(), json={"links": {"html": {"href": "u"}}})
    sleep = RecordingSleep()
    client = make_client(fake, sleep=sleep)
    assert await _create_pr(client) == "u"
    assert sleep.delays == [7.0]


async def test_429_without_retry_after_uses_exponential_backoff() -> None:
    fake = FakeBitBucket()
    fake.enqueue("POST", _pr_collection_path(), status=429)
    fake.enqueue("POST", _pr_collection_path(), json={"links": {"html": {"href": "u"}}})
    sleep = RecordingSleep()
    client = make_client(fake, sleep=sleep, backoff_base_seconds=1.0)
    await _create_pr(client)
    assert sleep.delays == [1.0]


async def test_429_exhausts_retries_and_raises() -> None:
    fake = FakeBitBucket()
    fake.enqueue("POST", _pr_collection_path(), status=429)  # single entry repeats
    sleep = RecordingSleep()
    client = make_client(fake, sleep=sleep, max_retries=2)
    with pytest.raises(BitBucketClientError) as excinfo:
        await _create_pr(client)
    assert excinfo.value.status == 429
    assert len(sleep.delays) == 2  # two retries, then give up


async def test_429_exhaustion_maps_to_rate_limited_reason() -> None:
    # A persistent 429 must surface as BITBUCKET_RATE_LIMITED, not the generic
    # BITBUCKET_API_ERROR, so operators/policy can tell quota exhaustion apart
    # from a server fault.
    fake = FakeBitBucket()
    fake.enqueue("POST", _pr_collection_path(), status=429)
    client = make_client(fake, sleep=RecordingSleep(), max_retries=1)
    with pytest.raises(BitBucketClientError) as excinfo:
        await _create_pr(client)
    assert excinfo.value.status == 429
    assert excinfo.value.reason_code == BITBUCKET_RATE_LIMITED


# ── Near-limit proactive slow-down (D2) ───────────────────────────────────────


async def test_near_limit_header_triggers_proactive_sleep() -> None:
    fake = FakeBitBucket()
    fake.enqueue(
        "POST",
        _pr_collection_path(),
        json={"links": {"html": {"href": "u"}}},
        headers={"X-RateLimit-NearLimit": "true"},
    )
    sleep = RecordingSleep()
    client = make_client(fake, sleep=sleep, near_limit_delay_seconds=2.5)
    await _create_pr(client)
    assert 2.5 in sleep.delays


# ── ETag conditional requests + bounded cache (D2) ────────────────────────────


async def test_etag_304_returns_cached_body() -> None:
    fake = FakeBitBucket()
    fake.enqueue("GET", "/x", json={"v": 1}, headers={"ETag": "etag-1"})
    fake.enqueue("GET", "/x", status=304, headers={"ETag": "etag-1"})
    client = make_client(fake)
    first = await client._request_json("GET", "/x", operation="op", cache=True)
    second = await client._request_json("GET", "/x", operation="op", cache=True)
    assert first == {"v": 1}
    assert second == {"v": 1}
    assert fake.requests[1].headers.get("If-None-Match") == "etag-1"


async def test_etag_cache_is_bounded_and_evicts_lru() -> None:
    fake = FakeBitBucket()
    fake.enqueue("GET", "/a", json={"v": "a"}, headers={"ETag": "a1"})
    fake.enqueue("GET", "/b", json={"v": "b"}, headers={"ETag": "b1"})
    fake.enqueue("GET", "/a", json={"v": "a2"}, headers={"ETag": "a2"})
    client = make_client(fake, etag_cache_size=1)
    await client._request_json("GET", "/a", operation="op", cache=True)
    await client._request_json("GET", "/b", operation="op", cache=True)  # evicts /a
    await client._request_json("GET", "/a", operation="op", cache=True)
    # The third /a request found no cache entry, so it sent no conditional header.
    assert "If-None-Match" not in fake.requests[2].headers


async def test_304_without_cached_body_raises() -> None:
    fake = FakeBitBucket()
    fake.enqueue("GET", "/x", status=304)
    client = make_client(fake)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client._request_json("GET", "/x", operation="op", cache=True)
    assert excinfo.value.status == 304


# ── Pagination (D2) ──────────────────────────────────────────────────────────


async def test_paginate_follows_next_links() -> None:
    fake = FakeBitBucket()
    fake.page(
        "GET",
        "/items",
        values=[{"id": 1}],
        next_url="https://api.bitbucket.org/items?page=2",
    )
    fake.page("GET", "/items", values=[{"id": 2}])
    client = make_client(fake)
    values = await client._paginate("/items", operation="op")
    assert [v["id"] for v in values] == [1, 2]


async def test_paginate_aborts_after_max_pages() -> None:
    # A degraded endpoint that always advertises a same-host ``next`` link would
    # loop forever without a cap; the cap turns that into a bounded, diagnosable
    # BITBUCKET_API_ERROR rather than a hung monitor task.
    fake = FakeBitBucket()
    fake.page(
        "GET",
        "/items",
        values=[{"id": 1}],
        next_url="https://api.bitbucket.org/items?cursor=loop",
    )
    client = make_client(fake, max_pages=3)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client._paginate("/items", operation="op")
    assert excinfo.value.reason_code == BITBUCKET_API_ERROR
    assert "page" in str(excinfo.value).lower()
    assert len(fake.calls("GET")) == 3  # capped, did not loop indefinitely


async def test_paginate_rejects_next_url_pointing_at_foreign_host() -> None:
    # BitBucket's cursor ``next`` is an absolute URL; httpx would honor a foreign
    # host as-is and forward the Authorization header (SSRF). The origin check
    # must reject it *before* any request is issued to the unintended host.
    fake = FakeBitBucket()
    fake.page(
        "GET",
        "/items",
        values=[{"id": 1}],
        next_url="https://169.254.169.254/latest/meta-data",
    )
    client = make_client(fake)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client._paginate("/items", operation="op")
    assert excinfo.value.reason_code == BITBUCKET_API_ERROR
    assert "169.254.169.254" in str(excinfo.value)
    assert len(fake.calls("GET")) == 1  # never reached the foreign host


async def test_paginate_allows_relative_next_url() -> None:
    # A relative ``next`` is resolved against the safe base_url by httpx, so the
    # origin guard must let it through.
    fake = FakeBitBucket()
    fake.page("GET", "/items", values=[{"id": 1}], next_url="/items?page=2")
    fake.page("GET", "/items", values=[{"id": 2}])
    client = make_client(fake)
    values = await client._paginate("/items", operation="op")
    assert [v["id"] for v in values] == [1, 2]


# ── Redirect following (BB diffstat/diff 302) ─────────────────────────────────


async def test_request_follows_same_host_302_redirect() -> None:
    # BB Cloud's PR diffstat/diff GETs answer 302 → resolved resource. The shared
    # client does not auto-follow, so ``_request`` must follow same-host hops itself
    # or the redirect body (not JSON) is silently swallowed.
    fake = FakeBitBucket()
    fake.enqueue(
        "GET",
        "/a",
        status=302,
        headers={"Location": "https://api.bitbucket.org/b?spec=x..y"},
    )
    fake.enqueue("GET", "/b", json={"ok": True})
    client = make_client(fake)
    body = await client._request_json("GET", "/a", operation="op")
    assert body == {"ok": True}
    assert [r.url.path for r in fake.calls("GET")] == ["/a", "/b"]


async def test_request_drops_post_body_on_redirect_hop() -> None:
    # RFC 7231 §6.4 drops the body on a 302/303 redirect. ``_request`` must clear
    # ``json_body`` after the first hop so an action payload (e.g. ``merge_pr``'s
    # ``{"merge_strategy": ...}``) is never re-issued verbatim to the redirected
    # URL, which could trigger a duplicate action.
    fake = FakeBitBucket()
    fake.enqueue(
        "POST",
        "/act",
        status=302,
        headers={"Location": "https://api.bitbucket.org/resolved"},
    )
    fake.enqueue("POST", "/resolved", json={"ok": True})
    client = make_client(fake)
    await client._request("POST", "/act", operation="op", json_body={"merge_strategy": "squash"})
    posts = fake.calls("POST")
    assert [r.url.path for r in posts] == ["/act", "/resolved"]
    assert posts[0].content  # first hop carried the body
    assert posts[1].content == b""  # redirected hop sent no body


async def test_paginate_follows_redirect_then_collects_values() -> None:
    # The first diffstat hop redirects; pagination must resume from the resolved
    # resource so ``changed_paths`` is actually populated.
    fake = FakeBitBucket()
    fake.enqueue(
        "GET",
        "/items",
        status=302,
        headers={"Location": "https://api.bitbucket.org/resolved?spec=x..y"},
    )
    fake.page("GET", "/resolved", values=[{"id": 1}, {"id": 2}])
    client = make_client(fake)
    values = await client._paginate("/items", operation="op")
    assert [v["id"] for v in values] == [1, 2]


async def test_request_rejects_redirect_to_foreign_host() -> None:
    # A 302 ``Location`` is honored verbatim with the Authorization header, so a
    # foreign target is the same SSRF risk as a foreign ``next``: refuse it before
    # re-issuing the authenticated request.
    fake = FakeBitBucket()
    fake.enqueue(
        "GET",
        "/a",
        status=302,
        headers={"Location": "https://169.254.169.254/latest/meta-data"},
    )
    client = make_client(fake)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client._request_json("GET", "/a", operation="op")
    assert excinfo.value.reason_code == BITBUCKET_API_ERROR
    assert "169.254.169.254" in str(excinfo.value)
    assert len(fake.calls("GET")) == 1  # never followed to the foreign host


async def test_request_aborts_after_max_redirects() -> None:
    # A degraded endpoint that keeps redirecting to itself would loop forever; the
    # hop cap turns that into a bounded, diagnosable BITBUCKET_API_ERROR.
    fake = FakeBitBucket()
    fake.enqueue(
        "GET",
        "/loop",
        status=302,
        headers={"Location": "https://api.bitbucket.org/loop"},
    )
    client = make_client(fake, max_redirects=3)
    with pytest.raises(BitBucketClientError) as excinfo:
        await client._request_json("GET", "/loop", operation="op")
    assert excinfo.value.reason_code == BITBUCKET_API_ERROR
    assert "redirect" in str(excinfo.value).lower()
    assert len(fake.calls("GET")) == 4  # initial + 3 followed hops, then aborted


# ── Error mapping + redaction ─────────────────────────────────────────────────


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failure_maps_to_auth_failed_reason(status: int) -> None:
    fake = FakeBitBucket()
    fake.enqueue("POST", _pr_collection_path(), status=status, json={"error": "nope"})
    client = make_client(fake)
    with pytest.raises(BitBucketClientError) as excinfo:
        await _create_pr(client)
    assert excinfo.value.status == status
    assert excinfo.value.reason_code == BITBUCKET_AUTH_FAILED


async def test_server_error_maps_to_api_error_reason() -> None:
    fake = FakeBitBucket()
    fake.enqueue("POST", _pr_collection_path(), status=500, json={"error": "boom"})
    client = make_client(fake)
    with pytest.raises(BitBucketClientError) as excinfo:
        await _create_pr(client)
    assert excinfo.value.status == 500
    assert excinfo.value.reason_code == "BITBUCKET_API_ERROR"


async def test_error_body_redacts_token() -> None:
    fake = FakeBitBucket()
    fake.enqueue(
        "POST",
        _pr_collection_path(),
        status=500,
        text="upstream said token supersecrettoken123456 is bad",
    )
    auth = BitBucketAuth(mode="bearer", api_token="supersecrettoken123456")
    client = make_client(fake, auth=auth)
    with pytest.raises(BitBucketClientError) as excinfo:
        await _create_pr(client)
    assert "supersecrettoken123456" not in excinfo.value.body
    assert "supersecrettoken123456" not in str(excinfo.value)


async def test_transport_error_raises_client_error_without_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = BitBucketClient(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.bitbucket.org"
        ),
        auth=BitBucketAuth(mode="bearer", api_token="tok-aaaaaaaa"),
    )
    with pytest.raises(BitBucketClientError) as excinfo:
        await _create_pr(client)
    assert excinfo.value.status is None
    # Transport blips carry a dedicated reason code so the PR monitor can tell
    # them apart from deterministic ``status=None`` aborts (pagination/SSRF).
    assert excinfo.value.reason_code == BITBUCKET_TRANSPORT_ERROR


async def test_invalid_json_body_raises() -> None:
    fake = FakeBitBucket()
    fake.enqueue("POST", _pr_collection_path(), text="not json {{{")
    client = make_client(fake)
    with pytest.raises(BitBucketClientError) as excinfo:
        await _create_pr(client)
    assert "json parse" in excinfo.value.operation
