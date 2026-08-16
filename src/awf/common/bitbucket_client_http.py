"""HTTP transport core (D2) for the Bitbucket client.

Split out of ``bitbucket_client`` to keep that module under the first-party
line-count guardrail. This is the shared request layer described by decision D2:
a single ``_request`` with 429/``Retry-After`` backoff, bounded cursor pagination
with an SSRF origin guard on every ``next``/``Location``, ETag conditional
requests backed by a bounded LRU, the proactive ``X-RateLimit-NearLimit``
slow-down, and redacted error mapping.

The methods live on a mixin that ``BitbucketClient`` inherits; the attributes
they read are declared (not assigned) here and initialized by
``BitbucketClient.__init__``.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from awf.common.bitbucket_client_errors import (
    BITBUCKET_API_ERROR,
    BITBUCKET_AUTH_FAILED,
    BITBUCKET_RATE_LIMITED,
    BITBUCKET_TRANSPORT_ERROR,
    BitbucketAuth,
    BitbucketClientError,
)
from awf.common.bitbucket_client_parsing import _FrozenParams, freeze_params
from awf.common.logging import get_logger
from awf.common.redaction import redact_secrets

_log = get_logger(__name__)

_ERROR_BODY_LIMIT = 2000


class _BitbucketHttpMixin:
    """Shared Bitbucket Cloud request/pagination/caching layer (decision D2)."""

    _client: httpx.AsyncClient
    _auth: BitbucketAuth
    _sleep: Any
    _secret_values: tuple[str, ...]
    _max_retries: int
    _max_pages: int
    _max_redirects: int
    _backoff_base_seconds: float
    _near_limit_delay_seconds: float
    _etag_cache: OrderedDict[tuple[str, str, _FrozenParams], tuple[str, Any]]
    _etag_cache_size: int

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        json_body: Any = None,
        params: Mapping[str, str] | None = None,
        cache: bool = False,
        retry: bool = True,
    ) -> Any:
        """Send a request and decode JSON, honoring ETag conditional requests."""
        extra_headers: dict[str, str] = {}
        cache_key = (method.upper(), path, freeze_params(params)) if cache else None
        cached: tuple[str, Any] | None = None
        if cache_key is not None:
            cached = self._etag_cache.get(cache_key)
            if cached is not None:
                extra_headers["If-None-Match"] = cached[0]
        response = await self._request(
            method,
            path,
            operation=operation,
            json_body=json_body,
            params=params,
            extra_headers=extra_headers or None,
            retry=retry,
        )
        if response.status_code == 304:
            # Use the entry captured before the await: a concurrent task may have
            # evicted it from the bounded cache while the request was in flight.
            if cached is not None:
                if cache_key is not None:
                    self._etag_cache_set(cache_key, cached[0], cached[1])
                return cached[1]
            raise BitbucketClientError(
                operation=operation,
                status=304,
                body="Bitbucket returned 304 without a cached body",
            )
        parsed = self._parse_json(response, operation)
        if cache_key is not None:
            etag = response.headers.get("ETag")
            if etag:
                self._etag_cache_set(cache_key, etag, parsed)
        return parsed

    async def _paginate(
        self,
        path: str,
        *,
        operation: str,
        params: Mapping[str, str] | None = None,
        cache: bool = False,
        retry: bool = True,
    ) -> list[dict[str, Any]]:
        """Follow Bitbucket ``next`` cursor links, collecting all ``values``.

        The traversal is bounded two ways so a misbehaving or adversarial response
        cannot hang or redirect the monitor: a hard page cap (``max_pages``) and an
        origin check on each absolute ``next`` URL (see :meth:`_validate_next_url`).

        ``retry`` propagates to every page, not just the first: a ``retry=False``
        caller (the pre-merge recheck) must fail fast on page 2+ too, or a
        transient 429 on a later page would run a backoff inside the merge
        critical section (mirrors the GitHub pagination contract).
        """
        values: list[dict[str, Any]] = []
        page = await self._request_json(
            "GET", path, operation=operation, params=params, cache=cache, retry=retry
        )
        pages = 1
        while isinstance(page, dict):
            for value in page.get("values") or []:
                if isinstance(value, dict):
                    values.append(value)
            next_url = page.get("next")
            if not isinstance(next_url, str) or not next_url:
                break
            if pages >= self._max_pages:
                raise BitbucketClientError(
                    operation=operation,
                    status=None,
                    body=(
                        f"Bitbucket pagination exceeded the {self._max_pages}-page cap; "
                        "aborting a likely runaway 'next' chain"
                    ),
                    reason_code=BITBUCKET_API_ERROR,
                )
            self._validate_next_url(next_url, operation)
            pages += 1
            # Propagate the caller's ``cache`` flag to every page, not just the
            # first: otherwise pages 2+ silently bypass the ETag/If-None-Match
            # optimization that ``cache=True`` callers (e.g. fetch_pr_status
            # comments) asked for. ``retry`` propagates the same way so a
            # pre-merge recheck fails fast on later pages instead of backing off.
            page = await self._request_json(
                "GET", next_url, operation=operation, cache=cache, retry=retry
            )
        return values

    def _validate_next_url(self, next_url: str, operation: str) -> None:
        """Reject a cursor ``next`` link that points off the configured forge host.

        Bitbucket's ``next`` is an absolute URL that httpx honors verbatim, bypassing
        ``base_url`` — so a compromised response could steer an authenticated request
        (carrying the ``Authorization`` header) at an internal service (SSRF). A
        relative ``next`` is resolved against the safe ``base_url`` and is allowed.
        """
        self._assert_forge_origin(next_url, operation, what="pagination 'next'")

    def _is_forge_origin(self, url: str) -> bool:
        """Return whether ``url`` is relative or points at the configured forge host.

        A relative ``url`` is resolved against the safe ``base_url`` and is allowed; an
        absolute ``url`` is allowed only when its host, port, and scheme (if present)
        match the forge ``base_url``.
        """
        parsed = urlsplit(url)
        if not parsed.netloc:
            return True
        base = self._client.base_url
        if parsed.hostname != base.host:
            return False
        if parsed.scheme and parsed.scheme != base.scheme:
            return False
        # ``hostname`` strips the port, so a ``next`` link or ``Location`` redirect to
        # ``api.bitbucket.org:8443`` would otherwise pass the host check while steering
        # an authenticated request at a different TCP endpoint (SSRF). Compare the
        # effective ports — falling back to the scheme default when omitted — so the
        # host:port pair, not just the host, must match the forge origin.
        effective_scheme = parsed.scheme or base.scheme
        default_port = {"https": 443, "http": 80}.get(effective_scheme)
        parsed_port = parsed.port if parsed.port is not None else default_port
        base_port = base.port if base.port is not None else default_port
        return parsed_port == base_port

    def _assert_forge_origin(self, url: str, operation: str, *, what: str) -> None:
        """Reject an absolute ``url`` that points off the configured forge host.

        Shared SSRF guard for both pagination ``next`` links and 3xx ``Location``
        targets: either is an absolute URL httpx would honor verbatim (bypassing
        ``base_url``) while still carrying the ``Authorization`` header, so a foreign
        origin must be refused *before* the request is issued. A relative ``url`` is
        resolved against the safe ``base_url`` and is allowed.
        """
        if self._is_forge_origin(url):
            return
        parsed = urlsplit(url)
        raise BitbucketClientError(
            operation=operation,
            status=None,
            body=(
                f"Bitbucket {what} pointed at unexpected origin "
                f"{parsed.scheme}://{parsed.hostname}; expected host {self._client.base_url.host}"
            ),
            reason_code=BITBUCKET_API_ERROR,
        )

    async def _request_text(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        extra_headers: Mapping[str, str] | None = None,
    ) -> str:
        """Fetch a raw text body (logs); returns ``""`` on any error response.

        Bitbucket's pipeline step-log endpoint answers a documented 307 redirect to an
        off-origin signed log-storage URL. The shared redirect path origin-checks every
        ``Location`` (SSRF guard) and would reject that hop, so log fetches enable
        ``allow_log_redirect`` to follow it — but the forge ``Authorization`` header is
        stripped before the off-origin hop, so credentials never reach the storage host.

        Log fetching is best-effort: a transport timeout/reset on the step-log endpoint
        or its signed storage redirect raises ``BitbucketClientError``, which is tolerated
        the same way a 4xx/5xx response is — by returning ``""``. Without this, the error
        escapes to ``_fetch_status_for_decision``, which treats the whole PR status poll
        as a transient Bitbucket fault and retries indefinitely; a persistent log-storage
        outage would then block the monitor from acting on the failing CI.
        """
        try:
            response = await self._request(
                method,
                path,
                operation=operation,
                extra_headers=extra_headers,
                strict=False,
                allow_log_redirect=True,
            )
        except BitbucketClientError:
            return ""
        if response.status_code >= 400:
            return ""
        return response.text

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        json_body: Any = None,
        params: Mapping[str, str] | None = None,
        extra_headers: Mapping[str, str] | None = None,
        strict: bool = True,
        allow_log_redirect: bool = False,
        retry: bool = True,
    ) -> httpx.Response:
        """Single request; ``retry=False`` suppresses the 429/Retry-After backoff."""
        headers = {"Accept": "application/json", "Authorization": self._auth.header_value()}
        if extra_headers:
            headers.update(extra_headers)
        target = path
        target_params = params
        redirects = 0
        # Single-use grant: ``allow_log_redirect`` permits exactly ONE off-origin hop
        # (the documented step-log → signed-storage 307). It is consumed once that hop
        # is followed so every later redirect faces the strict origin guard below.
        log_redirect_allowed = allow_log_redirect
        while True:
            attempt = 0
            while True:
                try:
                    response = await self._client.request(
                        method, target, json=json_body, params=target_params, headers=headers
                    )
                except httpx.RequestError as exc:
                    raise BitbucketClientError(
                        operation=operation,
                        status=None,
                        body=self._redact(str(exc)),
                        reason_code=BITBUCKET_TRANSPORT_ERROR,
                    ) from exc
                if response.status_code == 429 and attempt < (self._max_retries if retry else 0):
                    await self._sleep(self._retry_after_seconds(response, attempt))
                    attempt += 1
                    continue
                break
            # ``retry=False`` (the pre-merge recheck) must fail fast inside the
            # merge critical section. The proactive near-limit slow-down is a
            # sleep just like the 429 backoff, so gate it on ``retry`` too — a
            # ``X-RateLimit-NearLimit`` header must not stall the request while
            # the merge coordinator lock is held.
            if retry:
                await self._maybe_slow_for_near_limit(response)
            location = response.headers.get("Location")
            if not (response.is_redirect and location):
                break
            # BB Cloud's PR diffstat/diff GETs answer 302 → resolved resource. The
            # shared client does not auto-follow, so each hop is origin-checked
            # (SSRF, same guard as pagination ``next``) before being re-issued with
            # the Authorization header. ``Location`` already carries the resolved
            # query, so the original params are dropped on the next hop.
            if redirects >= self._max_redirects:
                raise BitbucketClientError(
                    operation=operation,
                    status=None,
                    body=(
                        f"Bitbucket redirects exceeded the {self._max_redirects}-hop cap; "
                        "aborting a likely runaway redirect chain"
                    ),
                    reason_code=BITBUCKET_API_ERROR,
                )
            if log_redirect_allowed and not self._is_forge_origin(location):
                # BB's step-log endpoint answers a documented 307 to an off-origin
                # signed storage URL. The redirect comes from an already-authenticated
                # api.bitbucket.org response, so follow it — but drop the forge
                # ``Authorization`` header first so the credential never reaches the
                # storage host (the SSRF guard's core concern). The redirected body is
                # only read as a redacted, truncated log excerpt and never re-acted on.
                headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
                # Consume the single-use grant: Bitbucket documents exactly ONE
                # off-origin storage hop, so any *further* off-origin redirect (a
                # compromised or adversarial storage host bouncing the now-unauthenticated
                # client toward an internal/arbitrary target) is anomalous and must hit
                # the strict ``_assert_forge_origin`` guard below instead of being chased.
                # ``_request_text`` tolerates the resulting ``BitbucketClientError`` by
                # returning ``""``, so a genuine multi-hop storage chain degrades to an
                # empty log rather than an unbounded off-origin redirect chain.
                log_redirect_allowed = False
            else:
                # All other 3xx hops stay on the forge: each Location is origin-checked
                # (same SSRF guard as pagination ``next``) before being re-issued.
                self._assert_forge_origin(location, operation, what="redirect Location")
            redirects += 1
            target = location
            target_params = None
            # Drop the request body on every redirect hop. ``Location`` already
            # carries the resolved resource, and RFC 7231 §6.4 semantics drop the
            # body for 302/303 redirects. The only body-carrying requests are
            # POSTs (create PR, merge, post comment); none are expected to
            # redirect, but clearing ``json_body`` removes any chance of an action
            # payload (e.g. ``merge_pr``'s ``{"merge_strategy": ...}``) being
            # re-issued verbatim to a redirected URL.
            json_body = None
        if strict and response.status_code >= 400:
            raise self._error_for(response, operation)
        return response

    def _retry_after_seconds(self, response: httpx.Response, attempt: int) -> float:
        """Return the 429 backoff delay, honoring the ``Retry-After`` header."""
        header = response.headers.get("Retry-After")
        if header:
            try:
                delay: float = float(header)
            except ValueError:
                delay = -1.0
            if delay >= 0:
                return delay
        return float(self._backoff_base_seconds * (2**attempt))

    async def _maybe_slow_for_near_limit(self, response: httpx.Response) -> None:
        """Proactively slow down when Bitbucket signals it is near the rate limit."""
        near_limit = response.headers.get("X-RateLimit-NearLimit")
        if near_limit and near_limit.strip().lower() in {"true", "1", "yes"}:
            _log.info("bitbucket.rate_limit_near", delay_seconds=self._near_limit_delay_seconds)
            await self._sleep(self._near_limit_delay_seconds)

    def _parse_json(self, response: httpx.Response, operation: str) -> Any:
        """Decode a JSON body, or ``None`` for an empty body."""
        text = response.text
        if not text.strip():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise BitbucketClientError(
                operation=f"{operation} (json parse)",
                status=response.status_code,
                body=self._redact(f"{exc}; body was: {text[:400]}"),
            ) from exc

    def _error_for(self, response: httpx.Response, operation: str) -> BitbucketClientError:
        """Build a redacted ``BitbucketClientError`` for a non-2xx response.

        A 429 that survives retry exhaustion is mapped to the dedicated
        ``BITBUCKET_RATE_LIMITED`` reason code so quota exhaustion is diagnosably
        distinct from a generic API fault in logs and policy.
        """
        if response.status_code in {401, 403}:
            reason = BITBUCKET_AUTH_FAILED
        elif response.status_code == 429:
            reason = BITBUCKET_RATE_LIMITED
        else:
            reason = BITBUCKET_API_ERROR
        return BitbucketClientError(
            operation=operation,
            status=response.status_code,
            body=self._redact(response.text[:_ERROR_BODY_LIMIT]),
            reason_code=reason,
        )

    def _redact(self, text: str) -> str:
        """Redact credentials/secrets from text before it lands in logs/errors."""
        return redact_secrets(text, extra_secrets=self._secret_values)

    def _etag_cache_set(self, key: tuple[str, str, _FrozenParams], etag: str, body: Any) -> None:
        """Store an ETag + parsed body in the bounded LRU cache."""
        self._etag_cache[key] = (etag, body)
        self._etag_cache.move_to_end(key)
        while len(self._etag_cache) > self._etag_cache_size:
            self._etag_cache.popitem(last=False)
