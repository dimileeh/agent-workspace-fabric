"""FastAPI dependency edge-case tests."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from types import SimpleNamespace

import pytest
import structlog
from fastapi import HTTPException, WebSocketException, status
from fastapi.security import HTTPAuthorizationCredentials
from starlette.datastructures import Headers
from starlette.requests import Request

import awf.api.deps as deps
import awf.api.request_admission as request_admission
from awf.api.auth_context import VERIFIED_BEARER_AUTH_SCOPE_KEY
from awf.api.request_admission import (
    CALLBACK_REGISTER_ENDPOINT_FAMILY,
    WORKSPACE_CREATE_ENDPOINT_FAMILY,
    RequestAdmissionLimiter,
    admit_request,
    extract_request_identity,
)
from awf.common.config import Settings


class _CountingAdmissionBuckets(dict[tuple[str, str, str, int, int], int]):
    def __init__(self, seed: dict[tuple[str, str, str, int, int], int]) -> None:
        super().__init__(seed)
        self.iterated_keys = 0

    def __iter__(self):
        for key in super().__iter__():
            self.iterated_keys += 1
            yield key


class _RaceAmplifyingAdmissionBuckets(dict[tuple[str, str, str, int, int], int]):
    def __init__(self, *, concurrent_readers: int) -> None:
        super().__init__()
        self._read_barrier = threading.Barrier(concurrent_readers)

    def get(
        self,
        key: tuple[str, str, str, int, int],
        default: int | None = None,
    ) -> int | None:
        value = super().get(key, default)
        if value == 0:
            with suppress(threading.BrokenBarrierError):
                self._read_barrier.wait(timeout=0.1)
        return value


def _bearer_credentials(token: str, *, scheme: str = "Bearer") -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme=scheme, credentials=token)


def _request(
    *,
    authorization: str | None = None,
    client_host: str = "198.51.100.10",
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode("latin-1")))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": headers,
            "client": (client_host, 43210),
        }
    )


@pytest.mark.unit
def test_request_admission_unverified_bearer_falls_back_to_client_host() -> None:
    raw_token = "secret-token-value"
    request = _request(authorization=f"Bearer {raw_token}", client_host="203.0.113.20")
    fallback = _request(client_host="203.0.113.20")

    identity = extract_request_identity(
        request,
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
    )
    fallback_identity = extract_request_identity(
        fallback,
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
    )

    assert identity.identity_type == "client_host"
    assert identity.identity_digest == fallback_identity.identity_digest
    assert raw_token not in identity.identity_digest
    assert raw_token not in str(identity.redacted_metadata())


@pytest.mark.unit
def test_request_admission_verified_bearer_identity_is_digest_only() -> None:
    raw_token = "secret-token-value"
    request = _request(authorization=f"Bearer {raw_token}")
    request.scope[VERIFIED_BEARER_AUTH_SCOPE_KEY] = True

    identity = extract_request_identity(
        request,
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
    )

    assert identity.identity_type == "bearer_token"
    assert identity.identity_digest
    assert raw_token not in identity.identity_digest
    assert raw_token not in str(identity.redacted_metadata())


@pytest.mark.unit
def test_request_admission_invalid_bearer_falls_back_to_client_host() -> None:
    identity = extract_request_identity(
        _request(authorization="Bearer    ", client_host="203.0.113.11"),
        endpoint_family=CALLBACK_REGISTER_ENDPOINT_FAMILY,
    )

    assert identity.identity_type == "client_host"
    assert identity.identity_digest


@pytest.mark.unit
def test_request_admission_rejects_invalid_limits_for_admit_and_check() -> None:
    limiter = RequestAdmissionLimiter(clock=lambda: 10.0)
    identity = extract_request_identity(
        _request(),
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
    )

    with pytest.raises(ValueError, match="limit"):
        limiter.admit(
            endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
            identity=identity,
            limit=0,
            window_seconds=60,
            reason_code="LIMIT",
        )
    with pytest.raises(ValueError, match="window"):
        limiter.admit(
            endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
            identity=identity,
            limit=1,
            window_seconds=0,
            reason_code="WINDOW",
        )
    with pytest.raises(ValueError, match="limit"):
        limiter.check(
            endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
            identity=identity,
            limit=0,
            window_seconds=60,
            reason_code="LIMIT",
        )
    with pytest.raises(ValueError, match="window"):
        limiter.check(
            endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
            identity=identity,
            limit=1,
            window_seconds=0,
            reason_code="WINDOW",
        )

    limiter._last_pruned_windows[5] = 99  # noqa: SLF001
    limiter._buckets[(WORKSPACE_CREATE_ENDPOINT_FAMILY, "client_host", "digest", 5, 3)] = 1  # noqa: SLF001
    limiter._prune_locked(window_seconds=60, current_window=1, now=30.0)  # noqa: SLF001

    assert limiter._last_pruned_windows[5] == 99  # noqa: SLF001


@pytest.mark.unit
def test_request_admission_identity_handles_header_and_client_edge_cases() -> None:
    class _NoHeaders:
        client = ("203.0.113.77", 12345)

    class _BadHeaders:
        headers = object()
        client = None
        scope = {"client": ("203.0.113.88", 54321)}

    class _UnknownClient:
        headers = Headers({"authorization": "Basic nope"})
        client = None
        scope = {}

    tuple_identity = extract_request_identity(
        _NoHeaders(),
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
    )
    scope_identity = extract_request_identity(
        _BadHeaders(),
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
    )
    unknown_identity = extract_request_identity(
        _UnknownClient(),
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
    )

    assert tuple_identity.identity_type == "client_host"
    assert request_admission._client_host(_NoHeaders()) == "203.0.113.77"  # noqa: SLF001
    assert request_admission._client_host(_BadHeaders()) == "203.0.113.88"  # noqa: SLF001
    assert request_admission._client_host(_UnknownClient()) == "unknown-client"  # noqa: SLF001
    assert tuple_identity.identity_digest != scope_identity.identity_digest
    assert unknown_identity.identity_digest
    assert (
        extract_request_identity(
            _request(authorization="Bearer"),
            endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
        ).identity_type
        == "client_host"
    )
    assert (
        extract_request_identity(
            _request(authorization="Basic token"),
            endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
        ).identity_type
        == "client_host"
    )
    assert request_admission._bearer_token(_request(authorization="Bearer")) is None  # noqa: SLF001
    assert (
        request_admission._bearer_token(_request(authorization="Bearer   ")) is None  # noqa: SLF001
    )
    assert (
        request_admission._bearer_token(_request(authorization="Basic token")) is None  # noqa: SLF001
    )
    assert request_admission._authorization_header(SimpleNamespace(headers=object())) is None  # noqa: SLF001
    assert request_admission._authorization_header(None) is None  # noqa: SLF001

    assert (
        request_admission._client_host(  # noqa: SLF001
            SimpleNamespace(client=(12345, 80), scope={"client": (None, 443)})
        )
        == "unknown-client"
    )

    class _RaisesForApp:
        @property
        def app(self) -> object:
            raise RuntimeError("app unavailable")

    assert request_admission.request_app_state(_RaisesForApp()) is None
    assert deps._normalized_authorization_header("Bearer") == "Bearer"  # noqa: SLF001


@pytest.mark.unit
def test_request_admission_logs_verified_bearer_header_downgrade() -> None:
    raw_token = "secret-token-value"
    request = SimpleNamespace(
        scope={VERIFIED_BEARER_AUTH_SCOPE_KEY: True},
        headers=SimpleNamespace(get=lambda _name: f"Bearer {raw_token}".encode("latin-1")),
        client=SimpleNamespace(host="203.0.113.25"),
    )

    with structlog.testing.capture_logs() as captured:
        identity = extract_request_identity(
            request,
            endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
        )

    assert identity.identity_type == "client_host"
    assert any(
        entry.get("event") == "request_admission.verified_bearer_identity_downgraded"
        and entry.get("log_level") == "warning"
        and entry.get("endpoint_family") == WORKSPACE_CREATE_ENDPOINT_FAMILY
        and entry.get("fallback_identity_type") == "client_host"
        for entry in captured
    )
    assert raw_token not in str(captured)


@pytest.mark.unit
def test_request_admission_limiter_shares_unverified_bearers_by_client_host() -> None:
    limiter = RequestAdmissionLimiter(clock=lambda: 10.0)
    first = extract_request_identity(
        _request(authorization="Bearer token-a", client_host="203.0.113.30"),
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
    )
    second = extract_request_identity(
        _request(authorization="Bearer token-b", client_host="203.0.113.30"),
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
    )

    assert first.identity_type == "client_host"
    assert first.identity_digest == second.identity_digest
    assert limiter.admit(
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
        identity=first,
        limit=1,
        window_seconds=60,
        reason_code="WORKSPACE_CREATE_RATE_LIMITED",
    ).allowed
    rejected = limiter.admit(
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
        identity=second,
        limit=1,
        window_seconds=60,
        reason_code="WORKSPACE_CREATE_RATE_LIMITED",
    )

    assert rejected.allowed is False
    assert rejected.metadata["identity_type"] == "client_host"
    assert "token-b" not in str(rejected.metadata)


@pytest.mark.unit
def test_request_admission_limiter_separates_verified_bearer_tokens() -> None:
    limiter = RequestAdmissionLimiter(clock=lambda: 10.0)
    first_request = _request(authorization="Bearer token-a")
    second_request = _request(authorization="Bearer token-b")
    first_request.scope[VERIFIED_BEARER_AUTH_SCOPE_KEY] = True
    second_request.scope[VERIFIED_BEARER_AUTH_SCOPE_KEY] = True
    first = extract_request_identity(
        first_request,
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
    )
    second = extract_request_identity(
        second_request,
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
    )

    assert limiter.admit(
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
        identity=first,
        limit=1,
        window_seconds=60,
        reason_code="WORKSPACE_CREATE_RATE_LIMITED",
    ).allowed
    assert limiter.admit(
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
        identity=second,
        limit=1,
        window_seconds=60,
        reason_code="WORKSPACE_CREATE_RATE_LIMITED",
    ).allowed
    rejected = limiter.admit(
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
        identity=first,
        limit=1,
        window_seconds=60,
        reason_code="WORKSPACE_CREATE_RATE_LIMITED",
    )

    assert rejected.allowed is False
    assert rejected.metadata["reason_code"] == "WORKSPACE_CREATE_RATE_LIMITED"
    assert "token-a" not in str(rejected.metadata)


@pytest.mark.unit
def test_request_admission_limiter_separates_fallback_and_verified_bearer_identity() -> None:
    limiter = RequestAdmissionLimiter(clock=lambda: 10.0)
    fallback = extract_request_identity(
        _request(client_host="203.0.113.12"),
        endpoint_family=CALLBACK_REGISTER_ENDPOINT_FAMILY,
    )
    bearer_request = _request(
        authorization="Bearer token-for-same-host", client_host="203.0.113.12"
    )
    bearer_request.scope[VERIFIED_BEARER_AUTH_SCOPE_KEY] = True
    bearer = extract_request_identity(
        bearer_request,
        endpoint_family=CALLBACK_REGISTER_ENDPOINT_FAMILY,
    )

    assert limiter.admit(
        endpoint_family=CALLBACK_REGISTER_ENDPOINT_FAMILY,
        identity=fallback,
        limit=1,
        window_seconds=60,
        reason_code="CALLBACK_REGISTER_RATE_LIMITED",
    ).allowed
    assert limiter.admit(
        endpoint_family=CALLBACK_REGISTER_ENDPOINT_FAMILY,
        identity=bearer,
        limit=1,
        window_seconds=60,
        reason_code="CALLBACK_REGISTER_RATE_LIMITED",
    ).allowed


@pytest.mark.unit
def test_request_admission_limiter_separates_endpoint_families() -> None:
    limiter = RequestAdmissionLimiter(clock=lambda: 10.0)
    workspace_identity = extract_request_identity(
        _request(authorization="Bearer shared-token"),
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
    )
    callback_identity = extract_request_identity(
        _request(authorization="Bearer shared-token"),
        endpoint_family=CALLBACK_REGISTER_ENDPOINT_FAMILY,
    )

    assert limiter.admit(
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
        identity=workspace_identity,
        limit=1,
        window_seconds=60,
        reason_code="WORKSPACE_CREATE_RATE_LIMITED",
    ).allowed
    assert limiter.admit(
        endpoint_family=CALLBACK_REGISTER_ENDPOINT_FAMILY,
        identity=callback_identity,
        limit=1,
        window_seconds=60,
        reason_code="CALLBACK_REGISTER_RATE_LIMITED",
    ).allowed


# Deadlock guard, not a synchronization deadline: a ``Barrier`` releases the
# instant the last party arrives (microseconds here), so a generous timeout never
# weakens the race amplification — it only prevents a spurious ``BrokenBarrierError``
# when a worker thread is CPU-starved under saturated parallelism (e.g. ``-n 20``
# plus coverage tracing). Kept well under pytest's per-test timeout so a genuine
# deadlock still surfaces clearly.
_ADMISSION_START_BARRIER_TIMEOUT_SECONDS = 10.0


@pytest.mark.unit
def test_request_admission_limiter_serializes_concurrent_admissions() -> None:
    concurrent_requests = 8
    limiter = RequestAdmissionLimiter(clock=lambda: 10.0)
    limiter._buckets = _RaceAmplifyingAdmissionBuckets(concurrent_readers=concurrent_requests)
    identity = extract_request_identity(
        _request(client_host="203.0.113.40"),
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
    )
    start_barrier = threading.Barrier(concurrent_requests)

    def admit_concurrently() -> bool:
        start_barrier.wait(timeout=_ADMISSION_START_BARRIER_TIMEOUT_SECONDS)
        return limiter.admit(
            endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
            identity=identity,
            limit=1,
            window_seconds=60,
            reason_code="WORKSPACE_CREATE_RATE_LIMITED",
        ).allowed

    with ThreadPoolExecutor(max_workers=concurrent_requests) as executor:
        results = list(executor.map(lambda _: admit_concurrently(), range(concurrent_requests)))

    assert results.count(True) == 1
    assert results.count(False) == concurrent_requests - 1


@pytest.mark.unit
async def test_admit_request_async_uses_worker_thread_for_limiter_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    async def fake_to_thread(
        func: object,
        /,
        *args: object,
        **kwargs: object,
    ) -> object:
        calls.append((func, args, kwargs))
        assert callable(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(request_admission.asyncio, "to_thread", fake_to_thread)
    request = SimpleNamespace(
        headers=Headers({}),
        client=SimpleNamespace(host="203.0.113.41"),
    )

    decision = await request_admission.admit_request_async(
        request,
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
        limit=1,
        window_seconds=60,
        reason_code="WORKSPACE_CREATE_RATE_LIMITED",
    )

    assert decision.allowed is True
    assert len(calls) == 1
    func, _args, kwargs = calls[0]
    assert getattr(func, "__name__", "") == "admit"
    assert kwargs["endpoint_family"] == WORKSPACE_CREATE_ENDPOINT_FAMILY
    assert kwargs["reason_code"] == "WORKSPACE_CREATE_RATE_LIMITED"


@pytest.mark.unit
async def test_check_request_async_uses_worker_thread_without_consuming_quota(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    async def fake_to_thread(
        func: object,
        /,
        *args: object,
        **kwargs: object,
    ) -> object:
        calls.append((func, args, kwargs))
        assert callable(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(request_admission.asyncio, "to_thread", fake_to_thread)
    request = SimpleNamespace(
        headers=Headers({}),
        client=SimpleNamespace(host="203.0.113.42"),
    )

    decision = await request_admission.check_request_async(
        request,
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
        limit=1,
        window_seconds=60,
        reason_code="WORKSPACE_CREATE_RATE_LIMITED",
    )
    admitted = request_admission.admit_request(
        request,
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
        limit=1,
        window_seconds=60,
        reason_code="WORKSPACE_CREATE_RATE_LIMITED",
    )
    rejected = request_admission.admit_request(
        request,
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
        limit=1,
        window_seconds=60,
        reason_code="WORKSPACE_CREATE_RATE_LIMITED",
    )

    assert decision.allowed is True
    assert admitted.allowed is True
    assert rejected.allowed is False
    assert len(calls) == 1
    func, _args, kwargs = calls[0]
    assert getattr(func, "__name__", "") == "check"
    assert kwargs["endpoint_family"] == WORKSPACE_CREATE_ENDPOINT_FAMILY
    assert kwargs["reason_code"] == "WORKSPACE_CREATE_RATE_LIMITED"


@pytest.mark.unit
def test_request_admission_limiter_prunes_once_per_window() -> None:
    now = 60.0

    def clock() -> float:
        return now

    limiter = RequestAdmissionLimiter(clock=clock)
    limiter._buckets = _CountingAdmissionBuckets(
        {
            (
                WORKSPACE_CREATE_ENDPOINT_FAMILY,
                "client_host",
                f"digest-{index}",
                60,
                1,
            ): 1
            for index in range(25)
        }
    )
    identity = extract_request_identity(
        _request(authorization="Bearer active-token"),
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
    )

    assert limiter.admit(
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
        identity=identity,
        limit=10,
        window_seconds=60,
        reason_code="WORKSPACE_CREATE_RATE_LIMITED",
    ).allowed

    limiter._buckets.iterated_keys = 0

    assert limiter.admit(
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
        identity=identity,
        limit=10,
        window_seconds=60,
        reason_code="WORKSPACE_CREATE_RATE_LIMITED",
    ).allowed
    assert limiter._buckets.iterated_keys == 0

    now = 120.0

    assert limiter.admit(
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
        identity=identity,
        limit=10,
        window_seconds=60,
        reason_code="WORKSPACE_CREATE_RATE_LIMITED",
    ).allowed
    assert all(key[4] == 2 for key in limiter._buckets)


@pytest.mark.unit
def test_request_admission_limiter_prunes_stale_buckets_across_window_sizes() -> None:
    now = 120.0

    def clock() -> float:
        return now

    limiter = RequestAdmissionLimiter(clock=clock)
    stale_thirty_second_bucket = (
        WORKSPACE_CREATE_ENDPOINT_FAMILY,
        "client_host",
        "stale-30-second-window",
        30,
        1,
    )
    stale_sixty_second_bucket = (
        WORKSPACE_CREATE_ENDPOINT_FAMILY,
        "client_host",
        "stale-60-second-window",
        60,
        1,
    )
    limiter._buckets = {
        stale_thirty_second_bucket: 1,
        stale_sixty_second_bucket: 1,
    }
    identity = extract_request_identity(
        _request(authorization="Bearer active-token"),
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
    )

    assert limiter.admit(
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
        identity=identity,
        limit=10,
        window_seconds=60,
        reason_code="WORKSPACE_CREATE_RATE_LIMITED",
    ).allowed

    assert stale_thirty_second_bucket not in limiter._buckets
    assert stale_sixty_second_bucket not in limiter._buckets


@pytest.mark.unit
def test_request_admission_limiter_keeps_live_buckets_for_other_window_sizes() -> None:
    now = 120.0

    def clock() -> float:
        return now

    limiter = RequestAdmissionLimiter(clock=clock)
    live_sixty_second_bucket = (
        WORKSPACE_CREATE_ENDPOINT_FAMILY,
        "client_host",
        "live-60-second-window",
        60,
        2,
    )
    stale_thirty_second_bucket = (
        WORKSPACE_CREATE_ENDPOINT_FAMILY,
        "client_host",
        "stale-30-second-window",
        30,
        3,
    )
    limiter._buckets = {
        live_sixty_second_bucket: 1,
        stale_thirty_second_bucket: 1,
    }
    identity = extract_request_identity(
        _request(authorization="Bearer active-token"),
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
    )

    assert limiter.admit(
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
        identity=identity,
        limit=10,
        window_seconds=30,
        reason_code="WORKSPACE_CREATE_RATE_LIMITED",
    ).allowed

    assert live_sixty_second_bucket in limiter._buckets
    assert stale_thirty_second_bucket not in limiter._buckets


@pytest.mark.unit
def test_request_admission_limiter_marks_all_scanned_window_sizes_pruned() -> None:
    now = 120.0

    def clock() -> float:
        return now

    limiter = RequestAdmissionLimiter(clock=clock)
    live_thirty_second_bucket = (
        WORKSPACE_CREATE_ENDPOINT_FAMILY,
        "client_host",
        "live-30-second-window",
        30,
        4,
    )
    live_sixty_second_bucket = (
        WORKSPACE_CREATE_ENDPOINT_FAMILY,
        "client_host",
        "live-60-second-window",
        60,
        2,
    )
    limiter._buckets = _CountingAdmissionBuckets(
        {
            live_thirty_second_bucket: 1,
            live_sixty_second_bucket: 1,
        }
    )
    identity = extract_request_identity(
        _request(authorization="Bearer active-token"),
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
    )

    assert limiter.admit(
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
        identity=identity,
        limit=10,
        window_seconds=60,
        reason_code="WORKSPACE_CREATE_RATE_LIMITED",
    ).allowed
    assert limiter._last_pruned_windows[30] == 4
    assert limiter._last_pruned_windows[60] == 2

    limiter._buckets.iterated_keys = 0

    assert limiter.admit(
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
        identity=identity,
        limit=10,
        window_seconds=30,
        reason_code="WORKSPACE_CREATE_RATE_LIMITED",
    ).allowed
    assert limiter._buckets.iterated_keys == 0


@pytest.mark.unit
def test_request_admission_reuses_limiter_without_app_state() -> None:
    request = SimpleNamespace(
        headers=Headers({}),
        client=SimpleNamespace(host="203.0.113.250"),
    )

    first = admit_request(
        request,
        endpoint_family="stateless_request_test",
        limit=1,
        window_seconds=60,
        reason_code="STATELESS_REQUEST_RATE_LIMITED",
    )
    rejected = admit_request(
        request,
        endpoint_family="stateless_request_test",
        limit=1,
        window_seconds=60,
        reason_code="STATELESS_REQUEST_RATE_LIMITED",
    )

    assert first.allowed is True
    assert rejected.allowed is False
    assert rejected.metadata["reason_code"] == "STATELESS_REQUEST_RATE_LIMITED"


def test_request_admission_ensures_bearer_marker_from_request_token() -> None:
    request = _request(authorization="Bearer shared-secret")

    request_admission.ensure_bearer_auth_verified_from_header(
        request,
        expected_token="shared-secret",
    )
    identity = request_admission.extract_request_identity(
        request,
        endpoint_family=WORKSPACE_CREATE_ENDPOINT_FAMILY,
    )

    assert request_admission.request_has_verified_bearer_auth(request)
    assert identity.identity_type == "bearer_token"


def test_request_admission_ensures_bearer_marker_ignores_mismatch() -> None:
    request = _request(authorization="Bearer wrong-secret")

    request_admission.ensure_bearer_auth_verified_from_header(
        request,
        expected_token="shared-secret",
    )

    assert not request_admission.request_has_verified_bearer_auth(request)


def test_request_admission_ensures_bearer_marker_skips_missing_context() -> None:
    request = _request(authorization="Bearer shared-secret")

    request_admission.ensure_bearer_auth_verified_from_header(request, expected_token=None)
    request_admission.ensure_bearer_auth_verified_from_header(None, expected_token="shared-secret")

    assert not request_admission.request_has_verified_bearer_auth(request)


@pytest.mark.unit
def test_request_admission_direct_limiter_tolerates_non_extensible_test_objects() -> None:
    class _Slotless:
        __slots__ = ()

    request = _Slotless()

    first = request_admission._direct_request_admission_limiter(request)  # noqa: SLF001
    second = request_admission._direct_request_admission_limiter(request)  # noqa: SLF001

    assert isinstance(first, RequestAdmissionLimiter)
    assert isinstance(second, RequestAdmissionLimiter)
    assert first is not second


@pytest.mark.unit
def test_request_admission_real_request_without_app_state_fails_loudly() -> None:
    request = _request(client_host="203.0.113.251")

    with pytest.raises(RuntimeError, match=r"request\.app\.state"):
        admit_request(
            request,
            endpoint_family="missing_app_state_request_test",
            limit=1,
            window_seconds=60,
            reason_code="MISSING_APP_STATE_RATE_LIMITED",
        )


@pytest.mark.unit
def test_request_admission_none_request_uses_fresh_direct_limiter() -> None:
    first = admit_request(
        None,
        endpoint_family="none_request_test",
        limit=1,
        window_seconds=60,
        reason_code="NONE_REQUEST_RATE_LIMITED",
    )
    second = admit_request(
        None,
        endpoint_family="none_request_test",
        limit=1,
        window_seconds=60,
        reason_code="NONE_REQUEST_RATE_LIMITED",
    )

    assert first.allowed is True
    assert second.allowed is True
    assert second.metadata["reason_code"] == "NONE_REQUEST_RATE_LIMITED"


@pytest.mark.unit
def test_request_admission_none_request_logs_limiter_bypass() -> None:
    with structlog.testing.capture_logs() as captured:
        decision = admit_request(
            None,
            endpoint_family="none_request_warning_test",
            limit=1,
            window_seconds=60,
            reason_code="NONE_REQUEST_RATE_LIMITED",
        )

    assert decision.allowed is True
    assert any(
        entry.get("event") == "request_admission.no_request_bypassing_limiter"
        and entry.get("log_level") == "warning"
        and entry.get("endpoint_family") == "none_request_warning_test"
        and entry.get("reason_code") == "NONE_REQUEST_RATE_LIMITED"
        for entry in captured
    )


@pytest.mark.unit
def test_require_api_token_reports_missing_and_invalid_tokens() -> None:
    missing_settings = Settings(_env_file=None, api_token=None)
    with pytest.raises(HTTPException) as missing:
        deps.require_api_token(None, settings=missing_settings)
    assert missing.value.status_code == 503
    assert missing.value.detail["error_code"] == "API_TOKEN_NOT_CONFIGURED"

    configured_settings = Settings(_env_file=None, api_token="secret")
    with pytest.raises(HTTPException) as unauthorized:
        deps.require_api_token(_bearer_credentials("wrong"), settings=configured_settings)
    assert unauthorized.value.status_code == 401
    assert unauthorized.value.detail["error_code"] == "UNAUTHORIZED"
    assert unauthorized.value.headers == {"WWW-Authenticate": "Bearer"}

    deps.require_api_token(_bearer_credentials("secret"), settings=configured_settings)


@pytest.mark.unit
def test_require_api_token_marks_request_as_verified_on_success() -> None:
    settings = Settings(_env_file=None, api_token="secret")
    request = _request(authorization="Bearer secret")

    deps.require_api_token(_bearer_credentials("secret"), settings=settings, request=request)

    assert request.scope[VERIFIED_BEARER_AUTH_SCOPE_KEY] is True


@pytest.mark.unit
def test_require_api_token_accepts_http_bearer_credentials_case_insensitively() -> None:
    settings = Settings(_env_file=None, api_token="secret")
    credentials = _bearer_credentials("secret", scheme="bearer")

    deps.require_api_token(credentials, settings=settings)


@pytest.mark.unit
async def test_require_websocket_api_token_reads_handshake_authorization_header() -> None:
    settings = Settings(_env_file=None, api_token="secret")
    websocket = SimpleNamespace(
        headers=Headers({"authorization": "bearer secret"}),
        scope={"extensions": {}},
    )

    await deps.require_websocket_api_token(websocket, settings=settings)  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("authorization", "settings", "expected_code", "expected_reason"),
    [
        (
            None,
            Settings(_env_file=None, api_token="secret"),
            status.WS_1008_POLICY_VIOLATION,
            "UNAUTHORIZED",
        ),
        (
            "Bearer wrong",
            Settings(_env_file=None, api_token="secret"),
            status.WS_1008_POLICY_VIOLATION,
            "UNAUTHORIZED",
        ),
        (
            "Bearer secret",
            Settings(_env_file=None, api_token=None),
            status.WS_1011_INTERNAL_ERROR,
            "API_TOKEN_NOT_CONFIGURED",
        ),
    ],
)
async def test_require_websocket_api_token_uses_websocket_exception_without_denial_extension(
    authorization: str | None,
    settings: Settings,
    expected_code: int,
    expected_reason: str,
) -> None:
    headers = Headers({"authorization": authorization}) if authorization is not None else Headers()
    websocket = SimpleNamespace(headers=headers, scope={"extensions": {}})

    with pytest.raises(WebSocketException) as exc_info:
        await deps.require_websocket_api_token(websocket, settings=settings)  # type: ignore[arg-type]

    assert exc_info.value.code == expected_code
    assert exc_info.value.reason == expected_reason


@pytest.mark.unit
async def test_require_websocket_api_token_uses_denial_exception_with_denial_extension() -> None:
    settings = Settings(_env_file=None, api_token="secret")
    websocket = SimpleNamespace(
        headers=Headers(),
        scope={"extensions": {"websocket.http.response": {}}},
    )

    with pytest.raises(deps.WebSocketAuthorizationDenialError) as exc_info:
        await deps.require_websocket_api_token(websocket, settings=settings)  # type: ignore[arg-type]

    assert exc_info.value.failure.status_code == 401
    assert exc_info.value.failure.detail["error_code"] == "UNAUTHORIZED"
    assert exc_info.value.failure.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.unit
def test_require_api_token_rejects_non_ascii_bearer_as_unauthorized() -> None:
    settings = Settings(_env_file=None, api_token="secret")

    with pytest.raises(HTTPException) as unauthorized:
        deps.require_api_token(_bearer_credentials("caf\u00e9"), settings=settings)

    assert unauthorized.value.status_code == 401
    assert unauthorized.value.detail["error_code"] == "UNAUTHORIZED"
    assert unauthorized.value.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.unit
def test_require_api_token_compares_tokens_with_constant_time_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None, api_token="secret-token")
    observed: list[tuple[object, object]] = []

    def _compare(left: object, right: object) -> bool:
        observed.append((left, right))
        return left == right

    monkeypatch.setattr(deps.hmac, "compare_digest", _compare)

    with pytest.raises(HTTPException):
        deps.require_api_token(_bearer_credentials("wrong"), settings=settings)
    with pytest.raises(HTTPException):
        deps.require_api_token(None, settings=settings)
    deps.require_api_token(_bearer_credentials("secret-token"), settings=settings)

    assert (b"Bearer wrong", b"Bearer secret-token") in observed
    assert (b"", b"Bearer secret-token") in observed
    assert (b"Bearer secret-token", b"Bearer secret-token") in observed


@pytest.mark.unit
async def test_get_db_session_commits_and_closes_on_success() -> None:
    session = _RecordingSession()
    request = _request_with_factory(lambda: session)

    generator = deps.get_db_session(request)  # type: ignore[arg-type]
    yielded = await generator.__anext__()
    assert yielded is session

    with pytest.raises(StopAsyncIteration):
        await generator.__anext__()

    assert session.calls == ["commit", "close"]


@pytest.mark.unit
async def test_get_db_session_rolls_back_and_closes_on_error() -> None:
    session = _RecordingSession()
    request = _request_with_factory(lambda: session)

    generator = deps.get_db_session(request)  # type: ignore[arg-type]
    yielded = await generator.__anext__()
    assert yielded is session

    with pytest.raises(ValueError):
        await generator.athrow(ValueError("boom"))

    assert session.calls == ["rollback", "close"]


@pytest.mark.unit
async def test_get_db_session_rolls_back_and_closes_on_base_exception() -> None:
    session = _RecordingSession()
    request = _request_with_factory(lambda: session)

    generator = deps.get_db_session(request)  # type: ignore[arg-type]
    yielded = await generator.__anext__()
    assert yielded is session

    class _RouteAbort(BaseException):
        pass

    with pytest.raises(_RouteAbort):
        await generator.athrow(_RouteAbort("abort"))

    assert session.calls == ["rollback", "close"]


@pytest.mark.unit
async def test_get_db_session_close_error_does_not_mask_route_error() -> None:
    session = _RecordingSession(close_error=RuntimeError("close failed"))
    request = _request_with_factory(lambda: session)

    generator = deps.get_db_session(request)  # type: ignore[arg-type]
    yielded = await generator.__anext__()
    assert yielded is session

    with pytest.raises(ValueError, match="route failed"):
        await generator.athrow(ValueError("route failed"))

    assert session.calls == ["rollback", "close"]


@pytest.mark.unit
async def test_get_db_session_logs_close_error_after_route_error() -> None:
    session = _RecordingSession(close_error=RuntimeError("close failed"))
    request = _request_with_factory(lambda: session)

    generator = deps.get_db_session(request)  # type: ignore[arg-type]
    yielded = await generator.__anext__()
    assert yielded is session

    with (
        structlog.testing.capture_logs() as captured,
        pytest.raises(ValueError, match="route failed"),
    ):
        await generator.athrow(ValueError("route failed"))

    assert session.calls == ["rollback", "close"]
    assert any(
        entry.get("event") == "get_db_session.close_failed_during_exception"
        and entry.get("log_level") == "warning"
        and entry.get("error_type") == "RuntimeError"
        and entry.get("error") == "close failed"
        for entry in captured
    )


@pytest.mark.unit
async def test_get_db_session_close_error_propagates_after_success() -> None:
    session = _RecordingSession(close_error=RuntimeError("close failed"))
    request = _request_with_factory(lambda: session)

    generator = deps.get_db_session(request)  # type: ignore[arg-type]
    yielded = await generator.__anext__()
    assert yielded is session

    with pytest.raises(RuntimeError, match="close failed"):
        await generator.__anext__()

    assert session.calls == ["commit", "close"]


@pytest.mark.unit
async def test_get_db_session_factory_fast_paths_return_existing_factory() -> None:
    factory = object()
    request = _request_with_factory(lambda: factory)

    assert await deps.get_db_session_factory(request) is request.app.state.db_session_factory  # type: ignore[arg-type]


def _request_with_factory(factory: object) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                db_session_factory=factory,
            )
        )
    )


class _RecordingSession:
    def __init__(self, *, close_error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.close_error = close_error

    async def commit(self) -> None:
        self.calls.append("commit")

    async def rollback(self) -> None:
        self.calls.append("rollback")

    async def close(self) -> None:
        self.calls.append("close")
        if self.close_error is not None:
            raise self.close_error
