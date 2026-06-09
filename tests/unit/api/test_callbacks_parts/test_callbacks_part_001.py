"""External callback registration API contract tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.requests import Request

from awf.api import request_admission
from awf.api import schemas as api_schemas
from awf.api.app import configure_database, create_app
from awf.api.routes import callbacks as callbacks_route
from awf.common import callback_targets
from awf.common.config import Settings, get_settings
from awf.db.repositories import CallbackSubscriptionRepository
from awf.db.session import make_session_factory

_CALLBACK_TOKEN = "callback-secret"
_STABLE_REQUEST_ADMISSION_CLOCK = 1000.0
_VALID_BODY = {
    "name": "operator-console",
    "target_url": "https://operator.example.com/awf/events",
    "event_types": ["workspace.*", "merge.*", "operation.*"],
}


class _NoLegacyIPv4Labels:
    def split(self, separator: str) -> list[str]:
        assert separator == "."
        return []


async def _subscription_count(engine: AsyncEngine) -> int:
    from awf.db.models import CallbackSubscription

    factory = make_session_factory(engine)
    async with factory() as session:
        return int(
            await session.scalar(select(func.count()).select_from(CallbackSubscription)) or 0
        )


def _callback_request_admission_settings(*, limit: int = 1) -> Settings:
    return Settings(
        _env_file=None,
        api_token=_CALLBACK_TOKEN,
        callbacks_enabled=True,
        request_admission_window_seconds=60,
        workspace_create_rate_limit_count=20,
        callback_register_rate_limit_count=limit,
    )


def _direct_callback_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/callbacks",
            "headers": [],
            "client": ("198.51.100.42", 42100),
            "app": FastAPI(),
        }
    )


def _callback_request_without_app_state() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/callbacks",
            "headers": [],
            "client": ("198.51.100.43", 42100),
        }
    )


class _TrackingLock:
    def __init__(self) -> None:
        self.enters = 0
        self.exits = 0

    def __enter__(self) -> None:
        self.enters += 1

    def __exit__(self, *_exc_info: object) -> None:
        self.exits += 1


def _assert_callback_rate_limited(
    response: Response,
    *,
    identity_type: str,
    expected_limit: int = 1,
) -> None:
    assert response.status_code == 429
    body = response.json()
    assert body["error_code"] == "CALLBACK_REGISTER_RATE_LIMITED"
    assert body["message"] == "Callback registration request rate limit exceeded."
    detail = body["detail"]
    assert detail["reason_code"] == "CALLBACK_REGISTER_RATE_LIMITED"
    assert detail["endpoint_family"] == "callback_register"
    assert detail["identity_type"] == identity_type
    assert detail["identity_digest"]
    assert detail["limit"] == expected_limit
    assert detail["window_seconds"] == 60
    assert detail["retry_after_seconds"] > 0
    assert response.headers["Retry-After"] == str(detail["retry_after_seconds"])


def _authorized_headers(
    *,
    idempotency_key: str | None = None,
    authorization: str | None = f"Bearer {_CALLBACK_TOKEN}",
) -> dict[str, str]:
    headers: dict[str, str] = {}
    if authorization is not None:
        headers["Authorization"] = authorization
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _callback_payload(**overrides: object) -> api_schemas.CallbackSubscriptionCreateRequest:
    return api_schemas.CallbackSubscriptionCreateRequest.model_validate(
        {
            **_VALID_BODY,
            **overrides,
        }
    )


def _callback_response(response_id: str) -> api_schemas.CallbackSubscriptionResponse:
    now = datetime.now(UTC)
    return api_schemas.CallbackSubscriptionResponse(
        id=response_id,
        name="operator-console",
        target_url="https://operator.example.com/awf/events",
        event_types=["workspace.*", "merge.*", "operation.*"],
        enabled=True,
        timeout_seconds=10,
        max_attempts=3,
        initial_backoff_seconds=5,
        created_at=now,
        updated_at=now,
        disabled_at=None,
    )


def _install_stable_request_admission_limiter(state: object) -> None:
    setattr(
        state,
        request_admission._LIMITER_STATE_KEY,  # noqa: SLF001
        request_admission.RequestAdmissionLimiter(clock=lambda: _STABLE_REQUEST_ADMISSION_CLOCK),
    )


@pytest.fixture(autouse=True)
def _configure_api_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWF_API_TOKEN", _CALLBACK_TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def callback_app_and_client(
    engine: AsyncEngine,
) -> AsyncIterator[tuple[FastAPI, AsyncClient]]:
    app = create_app(use_lifespan=False)
    configure_database(app, make_session_factory(engine))
    _install_stable_request_admission_limiter(app.state)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield app, c


@pytest.mark.unit
async def test_register_callback_direct_call_uses_default_settings_dependency(
    engine: AsyncEngine,
) -> None:
    response = await callbacks_route.register_callback(
        api_schemas.CallbackSubscriptionCreateRequest.model_validate(_VALID_BODY),
        _direct_callback_request(),
        idempotency_key="callback-direct-default-settings",
        session_factory=make_session_factory(engine),
    )

    assert isinstance(response, api_schemas.CallbackSubscriptionResponse)
    assert response.id.startswith("cb_")
    assert await _subscription_count(engine) == 1


@pytest.mark.unit
async def test_register_callback_requires_idempotency_key(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/callbacks",
        json=_VALID_BODY,
        headers=_authorized_headers(),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "error_code": "INVALID_REQUEST",
        "message": "Idempotency-Key header is required for this endpoint.",
    }


@pytest.mark.unit
async def test_register_callback_rejects_oversized_idempotency_key(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    response = await client.post(
        "/v1/callbacks",
        json=_VALID_BODY,
        headers=_authorized_headers(idempotency_key="k" * 129),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "error_code": "INVALID_REQUEST",
        "message": "Idempotency-Key header must be at most 128 characters.",
    }
    assert await _subscription_count(engine) == 0


@pytest.mark.unit
async def test_register_callback_persists_safe_public_contract(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/v1/callbacks",
        json=_VALID_BODY,
        headers=_authorized_headers(idempotency_key="callback-register-1"),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["id"].startswith("cb_")
    assert body["name"] == "operator-console"
    assert body["target_url"] == "https://operator.example.com/awf/events"
    assert body["event_types"] == ["workspace.*", "merge.*", "operation.*"]
    assert body["enabled"] is True
    assert body["timeout_seconds"] == 10
    assert body["max_attempts"] == 3
    assert body["initial_backoff_seconds"] == 5
    assert body["disabled_at"] is None
    assert body["created_at"] is not None
    assert body["updated_at"] is not None
    assert "secret" not in body
    assert "headers" not in body
    assert "authorization" not in body


@pytest.mark.unit
async def test_register_callback_rejects_burst_after_configured_limit(
    callback_app_and_client: tuple[FastAPI, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = callback_app_and_client
    app.dependency_overrides[get_settings] = lambda: _callback_request_admission_settings(limit=1)

    first = await client.post(
        "/v1/callbacks",
        json={**_VALID_BODY, "name": "callback-rate-first"},
        headers=_authorized_headers(idempotency_key="callback-rate-first"),
    )
    rejected = await client.post(
        "/v1/callbacks",
        json={
            **_VALID_BODY,
            "name": "callback-rate-second",
            "target_url": "https://operator.example.com/awf/events-2",
        },
        headers=_authorized_headers(idempotency_key="callback-rate-second"),
    )

    assert first.status_code == 201
    _assert_callback_rate_limited(rejected, identity_type="bearer_token")
    assert await _subscription_count(engine) == 1


@pytest.mark.unit
async def test_register_callback_rate_limit_rejects_fresh_key_before_db_replay_miss(
    callback_app_and_client: tuple[FastAPI, AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = callback_app_and_client
    app.dependency_overrides[get_settings] = lambda: _callback_request_admission_settings(limit=1)
    persisted_probe_keys: list[str] = []
    locked_replay_keys: list[str] = []
    original_persisted_probe = (
        callbacks_route.CallbackService.replay_existing_for_persisted_key_in_session
    )
    original_locked_replay = callbacks_route.CallbackService.replay_existing_in_locked_session

    async def fail_list_idempotency_replay_keys(
        _self: callbacks_route.CallbackService,
    ) -> list[tuple[str, str]]:
        raise AssertionError("fresh over-limit callbacks must not scan all replay keys")

    async def fail_detached_replay_existing(
        _self: callbacks_route.CallbackService,
        _payload: api_schemas.CallbackSubscriptionCreateRequest,
        *,
        idempotency_key: str,
    ) -> object:
        raise AssertionError(f"fresh callback path must keep {idempotency_key} in-session")

    async def tracked_persisted_probe(
        self: callbacks_route.CallbackService,
        session: AsyncSession,
        payload: api_schemas.CallbackSubscriptionCreateRequest,
        *,
        idempotency_key: str,
    ) -> object:
        persisted_probe_keys.append(idempotency_key)
        return await original_persisted_probe(
            self,
            session,
            payload,
            idempotency_key=idempotency_key,
        )

    async def tracked_locked_replay(
        self: callbacks_route.CallbackService,
        session: AsyncSession,
        payload: api_schemas.CallbackSubscriptionCreateRequest,
        *,
        idempotency_key: str,
    ) -> object:
        locked_replay_keys.append(idempotency_key)
        return await original_locked_replay(
            self,
            session,
            payload,
            idempotency_key=idempotency_key,
        )

    monkeypatch.setattr(
        callbacks_route.CallbackService,
        "replay_existing",
        fail_detached_replay_existing,
    )
    monkeypatch.setattr(
        callbacks_route.CallbackService,
        "replay_existing_for_persisted_key_in_session",
        tracked_persisted_probe,
    )
    monkeypatch.setattr(
        callbacks_route.CallbackService,
        "replay_existing_in_locked_session",
        tracked_locked_replay,
    )
    monkeypatch.setattr(
        callbacks_route.CallbackService,
        "list_idempotency_replay_keys",
        fail_list_idempotency_replay_keys,
    )

    first = await client.post(
        "/v1/callbacks",
        json={**_VALID_BODY, "name": "callback-replay-read-first"},
        headers=_authorized_headers(idempotency_key="callback-replay-read-first"),
    )
    rejected = await client.post(
        "/v1/callbacks",
        json={
            **_VALID_BODY,
            "name": "callback-replay-read-second",
            "target_url": "https://operator.example.com/awf/events-2",
        },
        headers=_authorized_headers(idempotency_key="callback-replay-read-second"),
    )

    assert first.status_code == 201
    _assert_callback_rate_limited(rejected, identity_type="bearer_token")
    assert persisted_probe_keys == [
        "callback-replay-read-first",
        "callback-replay-read-second",
    ]
    assert locked_replay_keys == [
        "callback-replay-read-first",
        "callback-replay-read-second",
    ]


@pytest.mark.unit
async def test_register_callback_fresh_path_acquires_one_idempotency_lock(
    callback_app_and_client: tuple[FastAPI, AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = callback_app_and_client
    app.dependency_overrides[get_settings] = lambda: _callback_request_admission_settings(limit=10)
    idempotency_key = "callback-fresh-single-lock"
    lock_keys: list[str] = []
    original_lock = CallbackSubscriptionRepository.acquire_idempotency_key_lock

    async def tracked_lock(self: CallbackSubscriptionRepository, key: str) -> None:
        lock_keys.append(key)
        await original_lock(self, key)

    monkeypatch.setattr(
        CallbackSubscriptionRepository,
        "acquire_idempotency_key_lock",
        tracked_lock,
    )

    response = await client.post(
        "/v1/callbacks",
        json={
            **_VALID_BODY,
            "name": "callback-fresh-single-lock",
            "target_url": "https://operator.example.com/awf/fresh-single-lock",
        },
        headers=_authorized_headers(idempotency_key=idempotency_key),
    )

    assert response.status_code == 201
    assert lock_keys == [idempotency_key]


@pytest.mark.unit
async def test_register_callback_idempotency_replay_bypasses_limit_but_fresh_keys_are_bounded(
    callback_app_and_client: tuple[FastAPI, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = callback_app_and_client
    app.dependency_overrides[get_settings] = lambda: _callback_request_admission_settings(limit=1)

    first = await client.post(
        "/v1/callbacks",
        json=_VALID_BODY,
        headers=_authorized_headers(idempotency_key="callback-rate-replay"),
    )
    replay = await client.post(
        "/v1/callbacks",
        json=_VALID_BODY,
        headers=_authorized_headers(idempotency_key="callback-rate-replay"),
    )
    fresh = await client.post(
        "/v1/callbacks",
        json={
            **_VALID_BODY,
            "name": "callback-rate-fresh",
            "target_url": "https://operator.example.com/awf/fresh",
        },
        headers=_authorized_headers(idempotency_key="callback-rate-fresh"),
    )

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]
    _assert_callback_rate_limited(fresh, identity_type="bearer_token")
    assert await _subscription_count(engine) == 1


@pytest.mark.unit
async def test_register_callback_db_replay_bypasses_limit_when_replay_cache_is_cold(
    callback_app_and_client: tuple[FastAPI, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = callback_app_and_client
    app.dependency_overrides[get_settings] = lambda: _callback_request_admission_settings(limit=1)
    headers = _authorized_headers(idempotency_key="callback-db-rate-replay")

    first = await client.post("/v1/callbacks", json=_VALID_BODY, headers=headers)
    setattr(
        app.state,
        callbacks_route._CALLBACK_REPLAY_CACHE_STATE_KEY,  # noqa: SLF001
        callbacks_route._CallbackIdempotencyReplayCache(),  # noqa: SLF001
    )
    replay = await client.post("/v1/callbacks", json=_VALID_BODY, headers=headers)
    fresh = await client.post(
        "/v1/callbacks",
        json={
            **_VALID_BODY,
            "name": "callback-db-rate-fresh",
            "target_url": "https://operator.example.com/awf/db-fresh",
        },
        headers=_authorized_headers(idempotency_key="callback-db-rate-fresh"),
    )

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]
    _assert_callback_rate_limited(fresh, identity_type="bearer_token")
    assert await _subscription_count(engine) == 1


@pytest.mark.unit
async def test_register_callback_db_replay_bypasses_limit_when_replay_caches_are_cold(
    callback_app_and_client: tuple[FastAPI, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = callback_app_and_client
    app.dependency_overrides[get_settings] = lambda: _callback_request_admission_settings(limit=1)
    headers = _authorized_headers(idempotency_key="callback-db-cold-rate-replay")

    first = await client.post("/v1/callbacks", json=_VALID_BODY, headers=headers)
    setattr(
        app.state,
        callbacks_route._CALLBACK_REPLAY_CACHE_STATE_KEY,  # noqa: SLF001
        callbacks_route._CallbackIdempotencyReplayCache(),  # noqa: SLF001
    )
    setattr(
        app.state,
        callbacks_route._CALLBACK_REPLAY_KEY_CACHE_STATE_KEY,  # noqa: SLF001
        callbacks_route._CallbackIdempotencyReplayKeyCache(),  # noqa: SLF001
    )
    replay = await client.post("/v1/callbacks", json=_VALID_BODY, headers=headers)
    fresh = await client.post(
        "/v1/callbacks",
        json={
            **_VALID_BODY,
            "name": "callback-db-cold-rate-fresh",
            "target_url": "https://operator.example.com/awf/db-cold-fresh",
        },
        headers=_authorized_headers(idempotency_key="callback-db-cold-rate-fresh"),
    )

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]
    _assert_callback_rate_limited(fresh, identity_type="bearer_token")
    assert await _subscription_count(engine) == 1


@pytest.mark.unit
async def test_register_callback_cold_db_replay_does_not_spend_fresh_quota(
    callback_app_and_client: tuple[FastAPI, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = callback_app_and_client
    app.dependency_overrides[get_settings] = lambda: _callback_request_admission_settings(limit=2)
    headers = _authorized_headers(idempotency_key="callback-cold-replay-quota")

    first = await client.post("/v1/callbacks", json=_VALID_BODY, headers=headers)
    setattr(
        app.state,
        callbacks_route._CALLBACK_REPLAY_CACHE_STATE_KEY,  # noqa: SLF001
        callbacks_route._CallbackIdempotencyReplayCache(),  # noqa: SLF001
    )
    setattr(
        app.state,
        callbacks_route._CALLBACK_REPLAY_KEY_CACHE_STATE_KEY,  # noqa: SLF001
        callbacks_route._CallbackIdempotencyReplayKeyCache(),  # noqa: SLF001
    )
    replay = await client.post("/v1/callbacks", json=_VALID_BODY, headers=headers)
    second_fresh = await client.post(
        "/v1/callbacks",
        json={
            **_VALID_BODY,
            "name": "callback-cold-replay-quota-second",
            "target_url": "https://operator.example.com/awf/cold-replay-second",
        },
        headers=_authorized_headers(idempotency_key="callback-cold-replay-quota-second"),
    )
    third_fresh = await client.post(
        "/v1/callbacks",
        json={
            **_VALID_BODY,
            "name": "callback-cold-replay-quota-third",
            "target_url": "https://operator.example.com/awf/cold-replay-third",
        },
        headers=_authorized_headers(idempotency_key="callback-cold-replay-quota-third"),
    )

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]
    assert second_fresh.status_code == 201
    _assert_callback_rate_limited(
        third_fresh,
        identity_type="bearer_token",
        expected_limit=2,
    )
    assert await _subscription_count(engine) == 2


@pytest.mark.unit
async def test_register_callback_cold_replay_locks_before_durable_lookup(
    callback_app_and_client: tuple[FastAPI, AsyncClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = callback_app_and_client
    app.dependency_overrides[get_settings] = lambda: _callback_request_admission_settings(limit=1)
    headers = _authorized_headers(idempotency_key="callback-inflight-rate-limit-replay")

    first = await client.post("/v1/callbacks", json=_VALID_BODY, headers=headers)
    assert first.status_code == 201
    setattr(
        app.state,
        callbacks_route._CALLBACK_REPLAY_CACHE_STATE_KEY,  # noqa: SLF001
        callbacks_route._CallbackIdempotencyReplayCache(),  # noqa: SLF001
    )
    setattr(
        app.state,
        callbacks_route._CALLBACK_REPLAY_KEY_CACHE_STATE_KEY,  # noqa: SLF001
        callbacks_route._CallbackIdempotencyReplayKeyCache(),  # noqa: SLF001
    )

    calls: list[str] = []
    original_lock = CallbackSubscriptionRepository.acquire_idempotency_key_lock
    original_lookup = CallbackSubscriptionRepository.get_by_idempotency_key

    async def tracked_lock(self: CallbackSubscriptionRepository, key: str) -> None:
        calls.append(f"lock:{key}")
        await original_lock(self, key)

    async def tracked_lookup(
        self: CallbackSubscriptionRepository,
        key: str,
    ) -> object | None:
        calls.append(f"lookup:{key}")
        return await original_lookup(self, key)

    async def fail_hash_lookup(
        _self: CallbackSubscriptionRepository,
        _key: str,
    ) -> str | None:
        raise AssertionError("cold replay must not use a pre-lock hash probe")

    monkeypatch.setattr(
        CallbackSubscriptionRepository,
        "acquire_idempotency_key_lock",
        tracked_lock,
    )
    monkeypatch.setattr(
        CallbackSubscriptionRepository,
        "get_by_idempotency_key",
        tracked_lookup,
    )
    monkeypatch.setattr(
        CallbackSubscriptionRepository,
        "get_idempotency_request_hash",
        fail_hash_lookup,
    )

    replay = await client.post("/v1/callbacks", json=_VALID_BODY, headers=headers)

    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]
    assert calls[:2] == [
        "lock:callback-inflight-rate-limit-replay",
        "lookup:callback-inflight-rate-limit-replay",
    ]


@pytest.mark.unit
async def test_callback_registration_locks_idempotency_key_before_lookup(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    idempotency_key = "callback-create-locks-before-lookup"
    original_lock = CallbackSubscriptionRepository.acquire_idempotency_key_lock
    original_lookup = CallbackSubscriptionRepository.get_by_idempotency_key

    async def tracked_lock(self: CallbackSubscriptionRepository, key: str) -> None:
        calls.append(f"lock:{key}")
        await original_lock(self, key)

    async def tracked_lookup(
        self: CallbackSubscriptionRepository,
        key: str,
    ) -> object | None:
        calls.append(f"lookup:{key}")
        return await original_lookup(self, key)

    monkeypatch.setattr(
        CallbackSubscriptionRepository,
        "acquire_idempotency_key_lock",
        tracked_lock,
    )
    monkeypatch.setattr(
        CallbackSubscriptionRepository,
        "get_by_idempotency_key",
        tracked_lookup,
    )
    service = callbacks_route.CallbackService(
        make_session_factory(engine),
        settings=_callback_request_admission_settings(limit=10),
    )

    subscription = await service.register(
        _callback_payload(
            name="callback-create-lock",
            target_url="https://operator.example.com/awf/create-lock",
        ),
        idempotency_key=idempotency_key,
    )

    assert subscription.id.startswith("cb_")
    assert calls[:2] == [
        f"lock:{idempotency_key}",
        f"lookup:{idempotency_key}",
    ]


@pytest.mark.unit
async def test_register_callback_uses_verified_bearer_identity_for_rate_limit(
    callback_app_and_client: tuple[FastAPI, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = callback_app_and_client
    app.dependency_overrides[get_settings] = lambda: _callback_request_admission_settings(limit=1)

    first = await client.post(
        "/v1/callbacks",
        json={**_VALID_BODY, "name": "verified-identity-first"},
        headers=_authorized_headers(idempotency_key="callback-verified-identity-first"),
    )
    second = await client.post(
        "/v1/callbacks",
        json={
            **_VALID_BODY,
            "name": "verified-identity-second",
            "target_url": "https://operator.example.com/awf/verified-second",
        },
        headers=_authorized_headers(idempotency_key="callback-verified-identity-second"),
    )

    assert first.status_code == 201
    _assert_callback_rate_limited(second, identity_type="bearer_token")
    assert await _subscription_count(engine) == 1


@pytest.mark.unit
async def test_register_callback_rejects_rotated_invalid_bearer_before_rate_limit(
    callback_app_and_client: tuple[FastAPI, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = callback_app_and_client
    app.dependency_overrides[get_settings] = lambda: _callback_request_admission_settings(limit=1)

    first = await client.post(
        "/v1/callbacks",
        json={**_VALID_BODY, "name": "token-a-first"},
        headers=_authorized_headers(idempotency_key="callback-token-a-first"),
    )
    second_token = await client.post(
        "/v1/callbacks",
        json={
            **_VALID_BODY,
            "name": "token-b-first",
            "target_url": "https://operator.example.com/awf/token-b",
        },
        headers={
            "Authorization": "Bearer callback-other-token",
            "Idempotency-Key": "callback-token-b-first",
        },
    )

    assert first.status_code == 201
    assert second_token.status_code == 401
    assert second_token.json()["detail"]["error_code"] == "UNAUTHORIZED"
    assert await _subscription_count(engine) == 1
    payload = json.dumps(second_token.json())
    assert _CALLBACK_TOKEN not in payload
    assert f"Bearer {_CALLBACK_TOKEN}" not in payload
    assert "callback-other-token" not in payload
    assert "Bearer callback-other-token" not in payload


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        {**_VALID_BODY, "target_url": "ftp://operator.example.com/events"},
        {**_VALID_BODY, "target_url": "https://user:pass@operator.example.com/events"},
        {**_VALID_BODY, "target_url": "https://operator.example.com/events#frag"},
        {**_VALID_BODY, "target_url": "https:///missing-host"},
        {**_VALID_BODY, "target_url": "https://operator.example.com:abc/events"},
        {**_VALID_BODY, "target_url": "https://operator.example.com:99999/events"},
        {**_VALID_BODY, "event_types": []},
        {**_VALID_BODY, "event_types": [""]},
        {**_VALID_BODY, "event_types": ["system.secret"]},
        {**_VALID_BODY, "headers": {"Authorization": "Bearer should-not-be-accepted"}},
    ],
)
async def test_register_callback_validates_url_events_and_extra_fields(
    client: AsyncClient,
    payload: dict[str, object],
) -> None:
    response = await client.post(
        "/v1/callbacks",
        json=payload,
        headers=_authorized_headers(idempotency_key="callback-invalid"),
    )

    assert response.status_code == 422


@pytest.mark.unit
async def test_register_callback_validation_errors_keep_fastapi_shape(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/v1/callbacks",
        json={key: value for key, value in _VALID_BODY.items() if key != "name"},
        headers=_authorized_headers(idempotency_key="callback-missing-name"),
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list)
    assert any(item["loc"] == ["body", "name"] and item["type"] == "missing" for item in detail)


@pytest.mark.unit
async def test_callbacks_endpoints_return_unavailable_when_disabled(
    callback_app_and_client: tuple[FastAPI, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = callback_app_and_client
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        callbacks_enabled=False,
        api_token=_CALLBACK_TOKEN,
    )

    register_response = await client.post(
        "/v1/callbacks",
        json=_VALID_BODY,
        headers=_authorized_headers(idempotency_key="callback-disabled"),
    )
    list_response = await client.get("/v1/callbacks", headers=_authorized_headers())

    assert register_response.status_code == 503
    assert register_response.json()["detail"] == {
        "error_code": "CALLBACKS_DISABLED",
        "message": "External callbacks are disabled by configuration.",
    }
    assert list_response.status_code == 503
    assert list_response.json()["detail"] == {
        "error_code": "CALLBACKS_DISABLED",
        "message": "External callbacks are disabled by configuration.",
    }
    assert await _subscription_count(engine) == 0


@pytest.mark.unit
async def test_register_callback_rejects_http_target_when_https_required_without_insert(
    callback_app_and_client: tuple[FastAPI, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = callback_app_and_client
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        api_token=_CALLBACK_TOKEN,
        callbacks_require_https=True,
    )

    response = await client.post(
        "/v1/callbacks",
        json={**_VALID_BODY, "target_url": "http://operator.example.com/awf/events"},
        headers=_authorized_headers(idempotency_key="callback-http-policy"),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "error_code": "CALLBACK_TARGET_POLICY_VIOLATION",
        "message": "target_url must use https",
    }
    assert await _subscription_count(engine) == 0


@pytest.mark.unit
async def test_register_callback_rejects_non_allowlisted_target_without_insert(
    callback_app_and_client: tuple[FastAPI, AsyncClient],
    engine: AsyncEngine,
) -> None:
    app, client = callback_app_and_client
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        api_token=_CALLBACK_TOKEN,
        callbacks_allowed_hosts=("operator.example.com",),
    )

    response = await client.post(
        "/v1/callbacks",
        json={
            **_VALID_BODY,
            "target_url": "https://callback-disallowed.example.com/awf/events",
        },
        headers=_authorized_headers(idempotency_key="callback-allowlist-policy"),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "error_code": "CALLBACK_TARGET_POLICY_VIOLATION",
        "message": "target_url host is not allowlisted",
    }
    assert await _subscription_count(engine) == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    "target_url",
    [
        "http://localhost/events",
        "http://localhost./events",
        "http://awf.localhost/events",
        "http://operator.local/events",
        "http://operator.localdomain/events",
        "http://internal/events",
        "http://127.0.0.1/events",
        "http://127.1/events",
        "http://0177.0.0.1/events",
        "http://0x7f000001/events",
        "http://2130706433/events",
        "http://10.0.0.5/events",
        "http://172.16.0.5/events",
        "http://192.168.0.5/events",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/events",
        "http://[::ffff:127.0.0.1]/events",
        "http://[::ffff:169.254.169.254]/latest/meta-data",
        "http://[::ffff:0:169.254.169.254]/latest/meta-data",
        "http://[2002:c0a8:0101::1]/events",
        "http://[fe80::1]/events",
    ],
)
async def test_register_callback_rejects_internal_target_hosts_without_insert(
    client: AsyncClient,
    engine: AsyncEngine,
    target_url: str,
) -> None:
    response = await client.post(
        "/v1/callbacks",
        json={**_VALID_BODY, "target_url": target_url},
        headers=_authorized_headers(idempotency_key=f"callback-internal-{target_url}"),
    )

    assert response.status_code == 422
    assert await _subscription_count(engine) == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    "target_url",
    [
        "http://[::ffff:127.0.0.1]/events",
        "http://[::ffff:169.254.169.254]/latest/meta-data",
    ],
)
def test_callback_target_rejects_ipv4_mapped_ipv6_when_runtime_marks_global(
    monkeypatch: pytest.MonkeyPatch,
    target_url: str,
) -> None:
    real_ip_address = callback_targets.ipaddress.ip_address

    class LegacyIPv4MappedAddress:
        is_global = True
        is_multicast = False

        def __init__(self, ipv4_mapped: object) -> None:
            self.ipv4_mapped = ipv4_mapped

    def legacy_ip_address(value: str) -> object:
        address = real_ip_address(value)
        ipv4_mapped = getattr(address, "ipv4_mapped", None)
        if ipv4_mapped is None:
            return address
        return LegacyIPv4MappedAddress(ipv4_mapped)

    monkeypatch.setattr(callback_targets.ipaddress, "ip_address", legacy_ip_address)

    with pytest.raises(ValidationError, match="target_url must use a public host"):
        api_schemas.CallbackSubscriptionCreateRequest.model_validate(
            {**_VALID_BODY, "target_url": target_url}
        )


@pytest.mark.unit
def test_legacy_ipv4_literal_detector_rejects_malformed_legacy_hosts() -> None:
    assert (
        callback_targets.looks_like_legacy_ipv4_literal(  # type: ignore[arg-type]
            _NoLegacyIPv4Labels()
        )
        is False
    )
    assert callback_targets.looks_like_legacy_ipv4_literal("192.168..1") is False
    assert callback_targets.looks_like_legacy_ipv4_literal("0xg.168.1.1") is False
    assert callback_targets.looks_like_legacy_ipv4_literal("0x7f.0.0.1") is True


@pytest.mark.unit
def test_workspace_reason_legacy_compatibility_validator_leaves_non_mappings_unchanged() -> None:
    assert (
        api_schemas.WorkspaceReasonWithLegacyStopStackRequest._drop_ignored_legacy_body_fields(
            "not-a-body-mapping"
        )
        == "not-a-body-mapping"
    )


@pytest.mark.unit
def test_workspace_reason_legacy_compatibility_validator_drops_ignored_fields() -> None:
    assert api_schemas.WorkspaceReasonWithLegacyStopStackRequest._drop_ignored_legacy_body_fields(
        {"reason": "operator cleanup", "stop_stack": True}
    ) == {"reason": "operator cleanup"}


@pytest.mark.unit
@pytest.mark.parametrize(
    "hostname",
    ["0x7f.0x0.0x0.0x1", "0x.0.0.1", "0xgg.0.0.1", "127..0.1"],
)
def test_legacy_ipv4_literal_detection_handles_hex_and_malformed_labels(hostname: str) -> None:
    result = callback_targets.looks_like_legacy_ipv4_literal(hostname)

    if hostname == "0x7f.0x0.0x0.0x1":
        assert result is True
    else:
        assert result is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "event_type",
    [
        "workspace.internal_secret",
        "merge.internal_secret",
        "operation.internal_secret",
    ],
)
async def test_register_callback_rejects_internal_namespaced_event_types_without_insert(
    client: AsyncClient,
    engine: AsyncEngine,
    event_type: str,
) -> None:
    response = await client.post(
        "/v1/callbacks",
        json={**_VALID_BODY, "event_types": [event_type]},
        headers=_authorized_headers(idempotency_key=f"callback-invalid-{event_type}"),
    )

    assert response.status_code == 422
    assert await _subscription_count(engine) == 0


@pytest.mark.unit
async def test_register_callback_accepts_exact_public_event_types(
    client: AsyncClient,
) -> None:
    response = await client.post(
        "/v1/callbacks",
        json={
            **_VALID_BODY,
            "event_types": [
                "operation.state_changed",
                "workspace.state_changed",
                "workspace.secondary_failure_recorded",
                "merge.candidate_updated",
                "workspace.state_changed",
            ],
        },
        headers=_authorized_headers(idempotency_key="callback-public-exact"),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["event_types"] == [
        "operation.state_changed",
        "workspace.state_changed",
        "workspace.secondary_failure_recorded",
        "merge.candidate_updated",
    ]
    assert "secret" not in body
    assert "headers" not in body
    assert "authorization" not in body


@pytest.mark.unit
async def test_register_callback_idempotent_replay_returns_original_without_duplicate_rows(
    client: AsyncClient,
    engine: AsyncEngine,
) -> None:
    headers = _authorized_headers(idempotency_key="callback-replay")

    first = await client.post("/v1/callbacks", json=_VALID_BODY, headers=headers)
    before_count = await _subscription_count(engine)
    replay = await client.post("/v1/callbacks", json=_VALID_BODY, headers=headers)
    after_count = await _subscription_count(engine)

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]
    assert after_count == before_count == 1


@pytest.mark.unit
def test_callback_replay_cache_without_app_state_is_request_local() -> None:
    request = SimpleNamespace()

    cache = callbacks_route._callback_idempotency_replay_cache(request)

    assert callbacks_route._callback_idempotency_replay_cache(request) is cache
    assert callbacks_route._callback_idempotency_replay_cache(SimpleNamespace()) is not cache
    assert callbacks_route._callback_idempotency_replay_cache(None) is not (
        callbacks_route._callback_idempotency_replay_cache(None)
    )


@pytest.mark.unit
def test_callback_replay_cache_real_request_without_app_state_fails_loudly() -> None:
    request = _callback_request_without_app_state()

    with pytest.raises(RuntimeError, match=r"request\.app\.state"):
        callbacks_route._callback_idempotency_replay_cache(request)


@pytest.mark.unit
def test_callback_replay_key_cache_without_app_state_is_request_local() -> None:
    request = SimpleNamespace()

    cache = callbacks_route._callback_idempotency_replay_key_cache(request)

    assert callbacks_route._callback_idempotency_replay_key_cache(request) is cache
    assert callbacks_route._callback_idempotency_replay_key_cache(SimpleNamespace()) is not cache
    assert callbacks_route._callback_idempotency_replay_key_cache(None) is not (
        callbacks_route._callback_idempotency_replay_key_cache(None)
    )


@pytest.mark.unit
def test_callback_replay_key_cache_real_request_without_app_state_fails_loudly() -> None:
    request = _callback_request_without_app_state()

    with pytest.raises(RuntimeError, match=r"request\.app\.state"):
        callbacks_route._callback_idempotency_replay_key_cache(request)


@pytest.mark.unit
def test_callback_replay_key_cache_app_state_is_bounded() -> None:
    request = _direct_callback_request()
    cache = callbacks_route._callback_idempotency_replay_key_cache(request)
    payload = _callback_payload(
        name="callback-key-app-state-bound",
        target_url="https://operator.example.com/awf/key-app-state-bound",
    )
    max_entries = callbacks_route._CALLBACK_REPLAY_KEY_CACHE_MAX_ENTRIES  # noqa: SLF001

    for index in range(max_entries + 1):
        cache.remember(payload, idempotency_key=f"callback-app-state-key-{index}")

    newest_key = f"callback-app-state-key-{max_entries}"
    assert cache.matches(payload, idempotency_key="callback-app-state-key-0") is False
    assert cache.matches(payload, idempotency_key=newest_key) is True


@pytest.mark.unit
def test_callback_replay_caches_lock_composite_lru_operations() -> None:
    replay_cache = callbacks_route._CallbackIdempotencyReplayCache(max_entries=2)  # noqa: SLF001
    replay_lock = _TrackingLock()
    replay_cache._lock = replay_lock  # noqa: SLF001
    replay_payload = _callback_payload(
        name="callback-replay-locked",
        target_url="https://operator.example.com/awf/replay-locked",
    )
    replay_cache.remember(
        replay_payload,
        idempotency_key="callback-replay-locked",
        response=_callback_response("cb_replay_locked"),
    )
    assert replay_cache.replay(replay_payload, idempotency_key="callback-replay-locked")
    assert replay_lock.enters == 2
    assert replay_lock.exits == 2

    key_cache = callbacks_route._CallbackIdempotencyReplayKeyCache(max_entries=2)  # noqa: SLF001
    key_lock = _TrackingLock()
    key_cache._lock = key_lock  # noqa: SLF001
    key_payload = _callback_payload(
        name="callback-key-locked",
        target_url="https://operator.example.com/awf/key-locked",
    )
    key_cache.remember(key_payload, idempotency_key="callback-key-locked")
    assert key_cache.matches(key_payload, idempotency_key="callback-key-locked") is True
    assert key_lock.enters == 2
    assert key_lock.exits == 2


@pytest.mark.unit
def test_callback_replay_conflict_does_not_promote_lru_entry() -> None:
    cache = callbacks_route._CallbackIdempotencyReplayCache(max_entries=2)
    first_payload = _callback_payload(
        name="callback-lru-first",
        target_url="https://operator.example.com/awf/lru-first",
    )
    second_payload = _callback_payload(
        name="callback-lru-second",
        target_url="https://operator.example.com/awf/lru-second",
    )
    cache.remember(
        first_payload,
        idempotency_key="callback-lru-first",
        response=_callback_response("cb_lru_first"),
    )
    cache.remember(
        second_payload,
        idempotency_key="callback-lru-second",
        response=_callback_response("cb_lru_second"),
    )

    with pytest.raises(callbacks_route.CallbackIdempotencyConflictError):
        cache.replay(
            _callback_payload(
                name="callback-lru-first",
                target_url="https://operator.example.com/awf/lru-conflict",
            ),
            idempotency_key="callback-lru-first",
        )

    third_payload = _callback_payload(
        name="callback-lru-third",
        target_url="https://operator.example.com/awf/lru-third",
    )
    cache.remember(
        third_payload,
        idempotency_key="callback-lru-third",
        response=_callback_response("cb_lru_third"),
    )

    assert cache.replay(second_payload, idempotency_key="callback-lru-second") is not None
    assert cache.replay(first_payload, idempotency_key="callback-lru-first") is None


@pytest.mark.unit
def test_callback_replay_key_conflict_does_not_promote_lru_entry() -> None:
    cache = callbacks_route._CallbackIdempotencyReplayKeyCache(max_entries=2)
    first_payload = _callback_payload(
        name="callback-key-lru-first",
        target_url="https://operator.example.com/awf/key-lru-first",
    )
    second_payload = _callback_payload(
        name="callback-key-lru-second",
        target_url="https://operator.example.com/awf/key-lru-second",
    )
    cache.remember(first_payload, idempotency_key="callback-key-lru-first")
    cache.remember(second_payload, idempotency_key="callback-key-lru-second")

    with pytest.raises(callbacks_route.CallbackIdempotencyConflictError):
        cache.matches(
            _callback_payload(
                name="callback-key-lru-first",
                target_url="https://operator.example.com/awf/key-lru-conflict",
            ),
            idempotency_key="callback-key-lru-first",
        )

    third_payload = _callback_payload(
        name="callback-key-lru-third",
        target_url="https://operator.example.com/awf/key-lru-third",
    )
    cache.remember(third_payload, idempotency_key="callback-key-lru-third")

    assert cache.matches(second_payload, idempotency_key="callback-key-lru-second") is True
    assert cache.matches(first_payload, idempotency_key="callback-key-lru-first") is False


@pytest.mark.unit
def test_callback_replay_key_cache_default_retains_keys_past_response_cache_limit() -> None:
    cache = callbacks_route._CallbackIdempotencyReplayKeyCache()
    payload = _callback_payload(
        name="callback-key-default-retain",
        target_url="https://operator.example.com/awf/key-default-retain",
    )

    for index in range(callbacks_route._CALLBACK_REPLAY_CACHE_MAX_ENTRIES + 1):  # noqa: SLF001
        cache.remember(payload, idempotency_key=f"callback-default-key-{index}")

    assert cache.matches(payload, idempotency_key="callback-default-key-0") is True


@pytest.mark.unit
def test_callback_replay_key_cache_rejects_invalid_size() -> None:
    with pytest.raises(ValueError, match="max_entries"):
        callbacks_route._CallbackIdempotencyReplayKeyCache(max_entries=0)  # noqa: SLF001


@pytest.mark.unit
def test_direct_callback_replay_caches_tolerate_non_extensible_test_objects() -> None:
    class _Slotless:
        __slots__ = ()

    request = _Slotless()

    replay_cache = callbacks_route._direct_callback_idempotency_replay_cache(  # noqa: SLF001
        request
    )
    replay_key_cache = callbacks_route._direct_callback_idempotency_replay_key_cache(  # noqa: SLF001
        request
    )

    assert isinstance(replay_cache, callbacks_route._CallbackIdempotencyReplayCache)  # noqa: SLF001
    assert isinstance(
        replay_key_cache,
        callbacks_route._CallbackIdempotencyReplayKeyCache,  # noqa: SLF001
    )
    assert (
        callbacks_route._direct_callback_idempotency_replay_cache(request)  # noqa: SLF001
        is not replay_cache
    )
    assert (
        callbacks_route._direct_callback_idempotency_replay_key_cache(request)  # noqa: SLF001
        is not replay_key_cache
    )


@pytest.mark.unit
async def test_callback_durable_replay_helpers_remember_successful_responses() -> None:
    payload = _callback_payload(name="callback-durable-helper")
    replay_cache = callbacks_route._CallbackIdempotencyReplayCache()  # noqa: SLF001
    replay_key_cache = callbacks_route._CallbackIdempotencyReplayKeyCache()  # noqa: SLF001

    class _DurableReplayService:
        async def replay_existing_for_persisted_key(
            self,
            _payload: api_schemas.CallbackSubscriptionCreateRequest,
            *,
            idempotency_key: str,
        ) -> api_schemas.CallbackSubscriptionResponse:
            assert idempotency_key == "persisted-key"
            return _callback_response("cb_persisted_helper")

        async def replay_existing_for_persisted_key_in_session(
            self,
            _session: object,
            _payload: api_schemas.CallbackSubscriptionCreateRequest,
            *,
            idempotency_key: str,
        ) -> api_schemas.CallbackSubscriptionResponse:
            assert idempotency_key == "persisted-session-key"
            return _callback_response("cb_persisted_session_helper")

        async def replay_existing_in_locked_session(
            self,
            _session: object,
            _payload: api_schemas.CallbackSubscriptionCreateRequest,
            *,
            idempotency_key: str,
        ) -> api_schemas.CallbackSubscriptionResponse:
            assert idempotency_key == "locked-key"
            return _callback_response("cb_locked_helper")

    service = _DurableReplayService()

    response = await callbacks_route._callback_durable_replay_response_for_persisted_key(  # noqa: SLF001
        service,  # type: ignore[arg-type]
        replay_cache,
        replay_key_cache,
        payload,
        idempotency_key="persisted-key",
    )
    session_response = await callbacks_route._callback_durable_replay_response_for_persisted_key(  # noqa: SLF001
        service,  # type: ignore[arg-type]
        replay_cache,
        replay_key_cache,
        payload,
        idempotency_key="persisted-session-key",
        session=object(),  # type: ignore[arg-type]
    )
    locked_response = await callbacks_route._callback_durable_replay_response_from_locked_session(  # noqa: SLF001
        service,  # type: ignore[arg-type]
        replay_cache,
        replay_key_cache,
        payload,
        idempotency_key="locked-key",
        session=object(),  # type: ignore[arg-type]
    )

    assert response is not None and response.id == "cb_persisted_helper"
    assert session_response is not None and session_response.id == "cb_persisted_session_helper"
    assert locked_response is not None and locked_response.id == "cb_locked_helper"
    assert replay_cache.replay(payload, idempotency_key="locked-key") is not None
    assert replay_key_cache.matches(payload, idempotency_key="locked-key") is True


@pytest.mark.unit
async def test_callback_durable_replay_helpers_translate_conflicts() -> None:
    payload = _callback_payload(name="callback-durable-conflict")
    replay_cache = callbacks_route._CallbackIdempotencyReplayCache()  # noqa: SLF001
    replay_key_cache = callbacks_route._CallbackIdempotencyReplayKeyCache()  # noqa: SLF001

    class _ConflictingReplayService:
        async def replay_existing_for_persisted_key(
            self,
            _payload: api_schemas.CallbackSubscriptionCreateRequest,
            *,
            idempotency_key: str,
        ) -> None:
            del idempotency_key
            raise callbacks_route.CallbackIdempotencyConflictError("conflict")

        async def replay_existing_in_locked_session(
            self,
            _session: object,
            _payload: api_schemas.CallbackSubscriptionCreateRequest,
            *,
            idempotency_key: str,
        ) -> None:
            del idempotency_key
            raise callbacks_route.CallbackIdempotencyConflictError("conflict")

        async def replay_existing(
            self,
            _payload: api_schemas.CallbackSubscriptionCreateRequest,
            *,
            idempotency_key: str,
        ) -> None:
            del idempotency_key
            raise callbacks_route.CallbackIdempotencyConflictError("conflict")

    service = _ConflictingReplayService()

    with pytest.raises(callbacks_route.HTTPException) as persisted_exc:
        await callbacks_route._callback_durable_replay_response_for_persisted_key(  # noqa: SLF001
            service,  # type: ignore[arg-type]
            replay_cache,
            replay_key_cache,
            payload,
            idempotency_key="persisted-key",
        )
    with pytest.raises(callbacks_route.HTTPException) as locked_exc:
        await callbacks_route._callback_durable_replay_response_from_locked_session(  # noqa: SLF001
            service,  # type: ignore[arg-type]
            replay_cache,
            replay_key_cache,
            payload,
            idempotency_key="locked-key",
            session=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(callbacks_route.HTTPException) as hot_exc:
        await callbacks_route._callback_durable_replay_response(  # noqa: SLF001
            service,  # type: ignore[arg-type]
            replay_cache,
            replay_key_cache,
            payload,
            idempotency_key="hot-key",
        )

    assert persisted_exc.value.status_code == 409
    assert locked_exc.value.status_code == 409
    assert hot_exc.value.status_code == 409
