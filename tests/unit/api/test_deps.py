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
        start_barrier.wait(timeout=1)
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
