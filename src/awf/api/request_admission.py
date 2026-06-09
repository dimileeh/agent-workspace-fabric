"""Request-level admission helpers for expensive public-ish API paths."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from starlette.requests import Request

from awf.api.auth_context import mark_bearer_auth_verified, request_has_verified_bearer_auth
from awf.common.logging import get_logger

WORKSPACE_CREATE_ENDPOINT_FAMILY: Final = "workspace_create"
CALLBACK_REGISTER_ENDPOINT_FAMILY: Final = "callback_register"

BEARER_TOKEN_IDENTITY_TYPE: Final = "bearer_token"
CLIENT_HOST_IDENTITY_TYPE: Final = "client_host"

_UNKNOWN_CLIENT_HOST: Final = "unknown-client"
_LIMITER_STATE_KEY: Final = "request_admission_limiter"
_DIRECT_LIMITER_ATTR: Final = "_awf_request_admission_limiter"
_log = get_logger(__name__)

Clock = Callable[[], float]
AdmissionMetadata = dict[str, str | int]


@dataclass(frozen=True, slots=True)
class RequestAdmissionIdentity:
    """Sanitized request identity used for quota accounting and metadata."""

    identity_type: str
    identity_digest: str

    def redacted_metadata(self) -> dict[str, str]:
        return {
            "identity_type": self.identity_type,
            "identity_digest": self.identity_digest,
        }


@dataclass(frozen=True, slots=True)
class RequestAdmissionDecision:
    """Admission outcome with operator-visible, redacted metadata."""

    allowed: bool
    metadata: AdmissionMetadata


class RequestAdmissionLimiter:
    """Small fixed-window in-memory limiter.

    The limiter is intentionally process-local for this hardening slice. Callers
    pass the configured limit/window per request so tests and local app instances
    can override policy without global state.

    Fixed-window counters reset at window boundaries, so a burst of ``limit``
    requests at the end of one window followed by ``limit`` at the start of the
    next can pass ``2 * limit`` requests in a short interval. This is acceptable
    for the current permissive defaults; consider a sliding-window algorithm if
    limits are tightened.
    """

    def __init__(self, *, clock: Clock | None = None) -> None:
        self._clock = clock or time.monotonic
        self._buckets: dict[tuple[str, str, str, int, int], int] = {}
        self._last_pruned_windows: dict[int, int] = {}
        self._lock = threading.Lock()

    def admit(
        self,
        *,
        endpoint_family: str,
        identity: RequestAdmissionIdentity,
        limit: int,
        window_seconds: int,
        reason_code: str,
    ) -> RequestAdmissionDecision:
        if limit < 1:
            raise ValueError("request admission limit must be at least 1")
        if window_seconds < 1:
            raise ValueError("request admission window must be at least 1 second")

        with self._lock:
            now = self._clock()
            window_index = int(now // window_seconds)
            key = (
                endpoint_family,
                identity.identity_type,
                identity.identity_digest,
                window_seconds,
                window_index,
            )
            self._prune_locked(
                window_seconds=window_seconds,
                current_window=window_index,
                now=now,
            )

            current_count = self._buckets.get(key, 0)
            if current_count >= limit:
                return RequestAdmissionDecision(
                    allowed=False,
                    metadata=_metadata(
                        endpoint_family=endpoint_family,
                        identity=identity,
                        limit=limit,
                        window_seconds=window_seconds,
                        remaining=0,
                        retry_after_seconds=_retry_after_seconds(
                            now=now,
                            window_seconds=window_seconds,
                            window_index=window_index,
                        ),
                        reason_code=reason_code,
                    ),
                )

            next_count = current_count + 1
            self._buckets[key] = next_count
            return RequestAdmissionDecision(
                allowed=True,
                metadata=_metadata(
                    endpoint_family=endpoint_family,
                    identity=identity,
                    limit=limit,
                    window_seconds=window_seconds,
                    remaining=max(limit - next_count, 0),
                    retry_after_seconds=_retry_after_seconds(
                        now=now,
                        window_seconds=window_seconds,
                        window_index=window_index,
                    ),
                    reason_code=reason_code,
                ),
            )

    def check(
        self,
        *,
        endpoint_family: str,
        identity: RequestAdmissionIdentity,
        limit: int,
        window_seconds: int,
        reason_code: str,
    ) -> RequestAdmissionDecision:
        """Return the current admission decision without consuming quota."""
        if limit < 1:
            raise ValueError("request admission limit must be at least 1")
        if window_seconds < 1:
            raise ValueError("request admission window must be at least 1 second")

        with self._lock:
            now = self._clock()
            window_index = int(now // window_seconds)
            key = (
                endpoint_family,
                identity.identity_type,
                identity.identity_digest,
                window_seconds,
                window_index,
            )
            self._prune_locked(
                window_seconds=window_seconds,
                current_window=window_index,
                now=now,
            )

            current_count = self._buckets.get(key, 0)
            allowed = current_count < limit
            return RequestAdmissionDecision(
                allowed=allowed,
                metadata=_metadata(
                    endpoint_family=endpoint_family,
                    identity=identity,
                    limit=limit,
                    window_seconds=window_seconds,
                    remaining=max(limit - current_count, 0) if allowed else 0,
                    retry_after_seconds=_retry_after_seconds(
                        now=now,
                        window_seconds=window_seconds,
                        window_index=window_index,
                    ),
                    reason_code=reason_code,
                ),
            )

    def _prune_locked(self, *, window_seconds: int, current_window: int, now: float) -> None:
        last_pruned_window = self._last_pruned_windows.get(window_seconds)
        if last_pruned_window is not None and current_window <= last_pruned_window:
            return

        pruned_windows = {window_seconds: current_window}
        stale_keys = []
        for key in self._buckets:
            bucket_window_seconds = key[3]
            bucket_current_window = int(now // bucket_window_seconds)
            pruned_windows[bucket_window_seconds] = bucket_current_window
            if key[4] < bucket_current_window:
                stale_keys.append(key)
        for key in stale_keys:
            del self._buckets[key]
        for pruned_window_seconds, pruned_current_window in pruned_windows.items():
            previous_pruned_window = self._last_pruned_windows.get(pruned_window_seconds)
            if previous_pruned_window is None or pruned_current_window > previous_pruned_window:
                self._last_pruned_windows[pruned_window_seconds] = pruned_current_window


def extract_request_identity(
    request: Request | object | None,
    *,
    endpoint_family: str,
) -> RequestAdmissionIdentity:
    """Return a stable, redacted identity for request admission."""

    if request_has_verified_bearer_auth(request):
        token = _bearer_token(request)
        if token is not None:
            return RequestAdmissionIdentity(
                identity_type=BEARER_TOKEN_IDENTITY_TYPE,
                identity_digest=_digest("bearer-token", token),
            )
        _log.warning(
            "request_admission.verified_bearer_identity_downgraded",
            endpoint_family=endpoint_family,
            fallback_identity_type=CLIENT_HOST_IDENTITY_TYPE,
        )

    client_host = _client_host(request)
    return RequestAdmissionIdentity(
        identity_type=CLIENT_HOST_IDENTITY_TYPE,
        identity_digest=_digest("client-host", f"{endpoint_family}\x00{client_host}"),
    )


def ensure_bearer_auth_verified_from_header(
    request: Request | object | None,
    *,
    expected_token: str | None,
) -> None:
    """Best-effort restore bearer verification state from request headers."""

    if expected_token is None or request is None:
        return
    if request_has_verified_bearer_auth(request):
        return

    token = _bearer_token(request)
    if token is None:
        return
    if not hmac.compare_digest(token, expected_token):
        return
    mark_bearer_auth_verified(request)


def admit_request(
    request: Request | object | None,
    *,
    endpoint_family: str,
    limit: int,
    window_seconds: int,
    reason_code: str,
) -> RequestAdmissionDecision:
    _warn_no_request_limiter_bypass(
        request,
        endpoint_family=endpoint_family,
        reason_code=reason_code,
    )
    identity = extract_request_identity(request, endpoint_family=endpoint_family)
    return request_admission_limiter(request).admit(
        endpoint_family=endpoint_family,
        identity=identity,
        limit=limit,
        window_seconds=window_seconds,
        reason_code=reason_code,
    )


async def admit_request_async(
    request: Request | object | None,
    *,
    endpoint_family: str,
    limit: int,
    window_seconds: int,
    reason_code: str,
) -> RequestAdmissionDecision:
    _warn_no_request_limiter_bypass(
        request,
        endpoint_family=endpoint_family,
        reason_code=reason_code,
    )
    identity = extract_request_identity(request, endpoint_family=endpoint_family)
    limiter = request_admission_limiter(request)
    return await asyncio.to_thread(
        limiter.admit,
        endpoint_family=endpoint_family,
        identity=identity,
        limit=limit,
        window_seconds=window_seconds,
        reason_code=reason_code,
    )


async def check_request_async(
    request: Request | object | None,
    *,
    endpoint_family: str,
    limit: int,
    window_seconds: int,
    reason_code: str,
) -> RequestAdmissionDecision:
    _warn_no_request_limiter_bypass(
        request,
        endpoint_family=endpoint_family,
        reason_code=reason_code,
    )
    identity = extract_request_identity(request, endpoint_family=endpoint_family)
    limiter = request_admission_limiter(request)
    return await asyncio.to_thread(
        limiter.check,
        endpoint_family=endpoint_family,
        identity=identity,
        limit=limit,
        window_seconds=window_seconds,
        reason_code=reason_code,
    )


def request_admission_limiter(request: Request | object | None) -> RequestAdmissionLimiter:
    state = request_app_state(request)
    if state is None:
        if isinstance(request, Request):
            raise RuntimeError(
                "request admission limiter requires request.app.state; "
                "direct callers must pass None or a non-Starlette test object."
            )
        return _direct_request_admission_limiter(request)

    existing = getattr(state, _LIMITER_STATE_KEY, None)
    if isinstance(existing, RequestAdmissionLimiter):
        return existing

    limiter = RequestAdmissionLimiter()
    setattr(state, _LIMITER_STATE_KEY, limiter)
    return limiter


def _direct_request_admission_limiter(
    request: Request | object | None,
) -> RequestAdmissionLimiter:
    if request is None:
        return RequestAdmissionLimiter()

    existing = getattr(request, _DIRECT_LIMITER_ATTR, None)
    if isinstance(existing, RequestAdmissionLimiter):
        return existing

    limiter = RequestAdmissionLimiter()
    try:
        setattr(request, _DIRECT_LIMITER_ATTR, limiter)
    except (AttributeError, TypeError):
        return limiter
    return limiter


def _warn_no_request_limiter_bypass(
    request: Request | object | None,
    *,
    endpoint_family: str,
    reason_code: str,
) -> None:
    if request is not None:
        return
    _log.warning(
        "request_admission.no_request_bypassing_limiter",
        endpoint_family=endpoint_family,
        reason_code=reason_code,
    )


def _metadata(
    *,
    endpoint_family: str,
    identity: RequestAdmissionIdentity,
    limit: int,
    window_seconds: int,
    remaining: int,
    retry_after_seconds: int,
    reason_code: str,
) -> AdmissionMetadata:
    return {
        "reason_code": reason_code,
        "endpoint_family": endpoint_family,
        "identity_type": identity.identity_type,
        "identity_digest": identity.identity_digest,
        "limit": limit,
        "window_seconds": window_seconds,
        "remaining": remaining,
        "retry_after_seconds": retry_after_seconds,
    }


def _retry_after_seconds(*, now: float, window_seconds: int, window_index: int) -> int:
    window_ends_at = (window_index + 1) * window_seconds
    return max(1, math.ceil(window_ends_at - now))


def _bearer_token(request: Request | object | None) -> str | None:
    authorization = _authorization_header(request)
    if authorization is None:
        return None
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2:
        return None
    scheme, token = parts
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _authorization_header(request: Request | object | None) -> str | None:
    if request is None:
        return None
    headers = getattr(request, "headers", None)
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    value = getter("authorization")
    return value if isinstance(value, str) else None


def _client_host(request: Request | object | None) -> str:
    if request is None:
        return _UNKNOWN_CLIENT_HOST
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    if isinstance(host, str) and host:
        return host
    if isinstance(client, tuple) and client:
        tuple_host = client[0]
        if isinstance(tuple_host, str) and tuple_host:
            return tuple_host

    scope = getattr(request, "scope", None)
    if isinstance(scope, dict):
        scoped_client = scope.get("client")
        if isinstance(scoped_client, tuple) and scoped_client:
            scoped_host = scoped_client[0]
            if isinstance(scoped_host, str) and scoped_host:
                return scoped_host

    return _UNKNOWN_CLIENT_HOST


def request_app_state(request: Request | object | None) -> object | None:
    if request is None:
        return None
    try:
        app = getattr(request, "app", None)
    except (KeyError, RuntimeError):
        return None
    return getattr(app, "state", None)


def _digest(purpose: str, value: str) -> str:
    return hashlib.sha256(f"awf:request-admission:{purpose}\x00{value}".encode()).hexdigest()


__all__ = [
    "CALLBACK_REGISTER_ENDPOINT_FAMILY",
    "WORKSPACE_CREATE_ENDPOINT_FAMILY",
    "RequestAdmissionDecision",
    "RequestAdmissionIdentity",
    "RequestAdmissionLimiter",
    "admit_request",
    "admit_request_async",
    "check_request_async",
    "extract_request_identity",
    "ensure_bearer_auth_verified_from_header",
    "request_app_state",
    "request_admission_limiter",
]
