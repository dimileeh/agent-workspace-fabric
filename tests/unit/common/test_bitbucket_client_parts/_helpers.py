"""Shared ``httpx.MockTransport`` fake for BitBucketClient unit tests.

This is the BitBucket parity of the GitHub ``FakeCommandRunner``: instead of canned
subprocess output it serves canned HTTP responses keyed by ``(method, path)`` and
records every request so tests can assert verb/path/body and ``Authorization``
redaction. Queued responses for the same ``(method, path)`` are served FIFO so a
test can model pagination (page 1 then page 2) or an ETag 200→304 sequence; once a
queue is down to its last entry, that entry is served for any further calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

from awf.common.bitbucket_client import BitBucketAuth, BitBucketClient
from awf.common.github_client import RepoRef

_BASE_URL = "https://api.bitbucket.org"


@dataclass
class _Queued:
    status: int
    json_body: Any
    text: str | None
    headers: dict[str, str]


@dataclass
class RecordingSleep:
    """Async sleep stub that records the delays it was asked to wait."""

    delays: list[float] = field(default_factory=list)

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


class FakeBitBucket:
    """Queue canned BitBucket responses and capture requests for assertions."""

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], list[_Queued]] = {}
        self.requests: list[httpx.Request] = []

    def enqueue(
        self,
        method: str,
        path: str,
        *,
        status: int = 200,
        json: Any = None,
        text: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> FakeBitBucket:
        """Queue one response for the next ``(method, path)`` request."""
        self._routes.setdefault((method.upper(), path), []).append(
            _Queued(status=status, json_body=json, text=text, headers=headers or {})
        )
        return self

    def page(
        self,
        method: str,
        path: str,
        *,
        values: list[dict[str, Any]],
        next_url: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> FakeBitBucket:
        """Queue one BitBucket paginated page (``{values, next}``)."""
        body: dict[str, Any] = {"values": values}
        if next_url is not None:
            body["next"] = next_url
        return self.enqueue(method, path, json=body, headers=headers)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method.upper(), request.url.path)
        queue = self._routes.get(key)
        if not queue:
            raise AssertionError(f"unexpected request: {request.method} {request.url.path}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        headers = dict(item.headers)
        if item.text is not None:
            return httpx.Response(item.status, text=item.text, headers=headers)
        if item.json_body is None:
            return httpx.Response(item.status, content=b"", headers=headers)
        headers.setdefault("Content-Type", "application/json")
        return httpx.Response(item.status, text=json.dumps(item.json_body), headers=headers)

    def client(self) -> httpx.AsyncClient:
        """Build an ``httpx.AsyncClient`` wired to this fake's transport."""
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handler), base_url=_BASE_URL)

    def calls(self, method: str | None = None) -> list[httpx.Request]:
        """Return recorded requests, optionally filtered by HTTP method."""
        if method is None:
            return list(self.requests)
        return [r for r in self.requests if r.method.upper() == method.upper()]


def make_client(
    fake: FakeBitBucket,
    *,
    auth: BitBucketAuth | None = None,
    sleep: RecordingSleep | None = None,
    **kwargs: Any,
) -> BitBucketClient:
    """Construct a ``BitBucketClient`` over the fake transport."""
    return BitBucketClient(
        client=fake.client(),
        auth=auth or BitBucketAuth(mode="bearer", api_token="bb-token-aaaaaaaaaaaa"),
        sleep=sleep or RecordingSleep(),
        **kwargs,
    )


def repo() -> RepoRef:
    """Return a canonical BitBucket ``RepoRef`` for tests."""
    return RepoRef(owner="workspace", name="repo", forge="bitbucket")


def pr_payload(
    *,
    pr_id: int = 42,
    state: str = "OPEN",
    source_branch: str = "feature/head",
    source_sha: str = "s" * 40,
    dest_branch: str = "development",
    dest_sha: str = "d" * 40,
    merge_strategies: list[str] | None = None,
    default_merge_strategy: str | None = None,
    merge_commit_hash: str | None = None,
    html_url: str = "https://bitbucket.org/workspace/repo/pull-requests/42",
) -> dict[str, Any]:
    """Build a BitBucket PR GET payload."""
    branch: dict[str, Any] = {"name": dest_branch}
    if merge_strategies is not None:
        branch["merge_strategies"] = merge_strategies
    if default_merge_strategy is not None:
        branch["default_merge_strategy"] = default_merge_strategy
    payload: dict[str, Any] = {
        "id": pr_id,
        "state": state,
        "source": {"branch": {"name": source_branch}, "commit": {"hash": source_sha}},
        "destination": {"branch": branch, "commit": {"hash": dest_sha}},
        "links": {"html": {"href": html_url}},
    }
    if merge_commit_hash is not None:
        payload["merge_commit"] = {"hash": merge_commit_hash}
    return payload
