"""External callback registration API contract tests."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import Request

from awf.api import request_admission
from awf.api import schemas as api_schemas
from awf.api.app import configure_database, create_app
from awf.api.routes import callbacks as callbacks_route
from awf.common.config import Settings, get_settings
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
@pytest.mark.parametrize(
    ("policy_error", "error_code"),
    [
        (
            callbacks_route.CallbackTargetPolicyViolationError("https required"),
            "CALLBACK_TARGET_POLICY_VIOLATION",
        ),
        (
            callbacks_route.CallbackTargetPolicyError("invalid target"),
            "CALLBACK_TARGET_INVALID",
        ),
    ],
)
async def test_register_callback_translates_policy_errors(
    monkeypatch: pytest.MonkeyPatch,
    policy_error: Exception,
    error_code: str,
) -> None:
    class _FakeSession:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_exc_info: object) -> None:
            return None

    class _FakeSessionFactory:
        def __call__(self) -> _FakeSession:
            return _FakeSession()

    class _FakeCallbackService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def replay_existing_for_persisted_key_in_session(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> None:
            return None

        async def replay_existing_in_locked_session(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> None:
            return None

        async def register_with_locked_idempotency_key(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> None:
            raise policy_error

    async def _admit_allowed(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(allowed=True, metadata={})

    monkeypatch.setattr(callbacks_route, "CallbackService", _FakeCallbackService)
    monkeypatch.setattr(callbacks_route, "admit_request_async", _admit_allowed)

    with pytest.raises(callbacks_route.HTTPException) as exc_info:
        await callbacks_route.register_callback(
            _callback_payload(name=f"callback-policy-{error_code.lower()}"),
            _direct_callback_request(),
            idempotency_key=f"callback-policy-{error_code.lower()}",
            session_factory=_FakeSessionFactory(),  # type: ignore[arg-type]
            settings=_callback_request_admission_settings(),
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["error_code"] == error_code


@pytest.mark.unit
async def test_register_callback_hot_key_cache_conflict_returns_409() -> None:
    request = _direct_callback_request()
    replay_key_cache = callbacks_route._callback_idempotency_replay_key_cache(  # noqa: SLF001
        request
    )
    replay_key_cache.remember(
        _callback_payload(name="callback-hot-cache-original"),
        idempotency_key="callback-hot-cache-key",
    )

    with pytest.raises(callbacks_route.HTTPException) as exc_info:
        await callbacks_route.register_callback(
            _callback_payload(name="callback-hot-cache-conflict"),
            request,
            idempotency_key="callback-hot-cache-key",
            session_factory=object(),  # type: ignore[arg-type]
            settings=_callback_request_admission_settings(),
        )

    assert exc_info.value.status_code == 409


@pytest.mark.unit
@pytest.mark.parametrize(
    "replay_stage",
    ["persisted-before-admission", "locked-rate-limited", "locked-before-register"],
)
async def test_register_callback_returns_durable_replay_before_fresh_register(
    monkeypatch: pytest.MonkeyPatch,
    replay_stage: str,
) -> None:
    class _FakeSession:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_exc_info: object) -> None:
            return None

    class _FakeSessionFactory:
        def __call__(self) -> _FakeSession:
            return _FakeSession()

    class _FakeCallbackService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.locked_calls = 0

        async def replay_existing_for_persisted_key_in_session(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> api_schemas.CallbackSubscriptionResponse | None:
            if replay_stage == "persisted-before-admission":
                return _callback_response("cb_persisted_route")
            return None

        async def replay_existing_in_locked_session(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> api_schemas.CallbackSubscriptionResponse | None:
            self.locked_calls += 1
            if replay_stage == "locked-rate-limited":
                return _callback_response("cb_rate_limited_replay")
            if replay_stage == "locked-before-register":
                return _callback_response("cb_locked_replay")
            return None

        async def register_with_locked_idempotency_key(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> None:
            raise AssertionError("durable replay should return before fresh register")

    async def _admission(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            allowed=replay_stage != "locked-rate-limited",
            metadata={
                "reason_code": "CALLBACK_REGISTER_RATE_LIMITED",
                "retry_after_seconds": 1,
            },
        )

    monkeypatch.setattr(callbacks_route, "CallbackService", _FakeCallbackService)
    monkeypatch.setattr(callbacks_route, "admit_request_async", _admission)

    response = await callbacks_route.register_callback(
        _callback_payload(name=f"callback-replay-{replay_stage}"),
        _direct_callback_request(),
        idempotency_key=f"callback-replay-{replay_stage}",
        session_factory=_FakeSessionFactory(),  # type: ignore[arg-type]
        settings=_callback_request_admission_settings(),
    )

    assert isinstance(response, api_schemas.CallbackSubscriptionResponse)
    assert response.id in {
        "cb_persisted_route",
        "cb_rate_limited_replay",
        "cb_locked_replay",
    }


@pytest.mark.unit
async def test_register_callback_direct_rate_limit_returns_structured_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeSession:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_exc_info: object) -> None:
            return None

    class _FakeSessionFactory:
        def __call__(self) -> _FakeSession:
            return _FakeSession()

    class _FakeCallbackService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def replay_existing_for_persisted_key_in_session(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> None:
            return None

        async def replay_existing_in_locked_session(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> None:
            return None

        async def register_with_locked_idempotency_key(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> None:
            raise AssertionError("rate-limited callback registration must not register")

    async def _rate_limited(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            allowed=False,
            metadata={
                "reason_code": "CALLBACK_REGISTER_RATE_LIMITED",
                "endpoint_family": "callback_register",
                "identity_type": "bearer_token",
                "identity_digest": "digest",
                "limit": 1,
                "window_seconds": 60,
                "retry_after_seconds": 12,
            },
        )

    monkeypatch.setattr(callbacks_route, "CallbackService", _FakeCallbackService)
    monkeypatch.setattr(callbacks_route, "admit_request_async", _rate_limited)

    response = await callbacks_route.register_callback(
        _callback_payload(name="callback-direct-rate-limited"),
        _direct_callback_request(),
        idempotency_key="callback-direct-rate-limited",
        session_factory=_FakeSessionFactory(),  # type: ignore[arg-type]
        settings=_callback_request_admission_settings(),
    )

    assert response.status_code == 429
    assert json.loads(response.body)["error_code"] == "CALLBACK_REGISTER_RATE_LIMITED"


@pytest.mark.unit
async def test_register_callback_locked_idempotency_conflict_returns_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeSession:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_exc_info: object) -> None:
            return None

    class _FakeSessionFactory:
        def __call__(self) -> _FakeSession:
            return _FakeSession()

    class _FakeCallbackService:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def replay_existing_for_persisted_key_in_session(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> None:
            return None

        async def replay_existing_in_locked_session(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> None:
            return None

        async def register_with_locked_idempotency_key(
            self,
            *_args: object,
            **_kwargs: object,
        ) -> None:
            raise callbacks_route.CallbackIdempotencyConflictError("conflict")

    async def _admit_allowed(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(allowed=True, metadata={})

    monkeypatch.setattr(callbacks_route, "CallbackService", _FakeCallbackService)
    monkeypatch.setattr(callbacks_route, "admit_request_async", _admit_allowed)

    with pytest.raises(callbacks_route.HTTPException) as exc_info:
        await callbacks_route.register_callback(
            _callback_payload(name="callback-locked-conflict"),
            _direct_callback_request(),
            idempotency_key="callback-locked-conflict",
            session_factory=_FakeSessionFactory(),  # type: ignore[arg-type]
            settings=_callback_request_admission_settings(),
        )

    assert exc_info.value.status_code == 409


@pytest.mark.unit
async def test_register_callback_known_replay_key_db_miss_returns_conflict_without_register(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _direct_callback_request()
    payload = _callback_payload(
        name="callback-known-missing",
        target_url="https://operator.example.com/awf/known-missing",
    )
    idempotency_key = "callback-known-missing-key"
    callbacks_route._callback_idempotency_replay_key_cache(request).remember(  # noqa: SLF001
        payload,
        idempotency_key=idempotency_key,
    )
    replay_keys: list[str] = []
    register_keys: list[str] = []

    async def missing_replay(
        _self: callbacks_route.CallbackService,
        _payload: api_schemas.CallbackSubscriptionCreateRequest,
        *,
        idempotency_key: str,
    ) -> None:
        replay_keys.append(idempotency_key)

    async def fail_register(
        _self: callbacks_route.CallbackService,
        _payload: api_schemas.CallbackSubscriptionCreateRequest,
        *,
        idempotency_key: str,
    ) -> None:
        register_keys.append(idempotency_key)
        raise AssertionError("known replay-key durable miss must not register a callback")

    monkeypatch.setattr(callbacks_route.CallbackService, "replay_existing", missing_replay)
    monkeypatch.setattr(callbacks_route.CallbackService, "register", fail_register)

    with pytest.raises(callbacks_route.HTTPException) as exc_info:
        await callbacks_route.register_callback(
            payload,
            request=request,
            idempotency_key=idempotency_key,
            session_factory=object(),  # type: ignore[arg-type]
            settings=_callback_request_admission_settings(limit=10),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["error_code"] == "IDEMPOTENCY_REPLAY_UNAVAILABLE"
    assert replay_keys == [idempotency_key]
    assert register_keys == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "changed_body",
    [
        {**_VALID_BODY, "target_url": "https://operator.example.com/changed"},
        {**_VALID_BODY, "event_types": ["workspace.*", "operation.*"]},
        {**_VALID_BODY, "enabled": False},
    ],
)
async def test_register_callback_same_key_with_changed_body_returns_conflict(
    client: AsyncClient,
    engine: AsyncEngine,
    changed_body: dict[str, object],
) -> None:
    headers = _authorized_headers(idempotency_key="callback-conflict")

    first = await client.post("/v1/callbacks", json=_VALID_BODY, headers=headers)
    before_count = await _subscription_count(engine)
    conflict = await client.post(
        "/v1/callbacks",
        json=changed_body,
        headers=headers,
    )
    after_count = await _subscription_count(engine)

    assert first.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["error_code"] == "IDEMPOTENCY_CONFLICT"
    assert after_count == before_count == 1


@pytest.mark.unit
async def test_list_callbacks_returns_pagination_envelope_and_enabled_filter(
    client: AsyncClient,
) -> None:
    enabled = await client.post(
        "/v1/callbacks",
        json={**_VALID_BODY, "name": "enabled"},
        headers=_authorized_headers(idempotency_key="callback-list-enabled"),
    )
    disabled = await client.post(
        "/v1/callbacks",
        json={
            **_VALID_BODY,
            "name": "disabled",
            "target_url": "https://operator.example.com/disabled",
            "enabled": False,
        },
        headers=_authorized_headers(idempotency_key="callback-list-disabled"),
    )
    assert enabled.status_code == 201
    assert disabled.status_code == 201

    all_response = await client.get("/v1/callbacks", headers=_authorized_headers())
    enabled_response = await client.get(
        "/v1/callbacks",
        params={"enabled": True},
        headers=_authorized_headers(),
    )

    assert all_response.status_code == 200
    all_body = all_response.json()
    assert all_body["next_cursor"] is None
    assert all_body["has_more"] is False
    assert all_body["limit"] == 50
    assert all_body["cursor"] is None
    assert {item["name"] for item in all_body["items"]} == {"enabled", "disabled"}

    assert enabled_response.status_code == 200
    assert [item["name"] for item in enabled_response.json()["items"]] == ["enabled"]


@pytest.mark.unit
@pytest.mark.parametrize("limit", [0, 501])
async def test_list_callbacks_validates_limit_bounds(
    client: AsyncClient,
    limit: int,
) -> None:
    response = await client.get(
        "/v1/callbacks",
        params={"limit": limit},
        headers=_authorized_headers(),
    )

    assert response.status_code == 422


@pytest.mark.unit
async def test_register_callback_requires_authorization_token(
    callback_app_and_client: tuple[FastAPI, AsyncClient],
) -> None:
    _, client = callback_app_and_client
    response = await client.post(
        "/v1/callbacks",
        json=_VALID_BODY,
        headers=_authorized_headers(
            idempotency_key="callback-no-token",
            authorization=None,
        ),
    )

    assert response.status_code == 401
    assert response.json()["detail"]["error_code"] == "UNAUTHORIZED"


@pytest.mark.unit
async def test_register_callback_rejects_invalid_authorization_token(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/callbacks",
        json=_VALID_BODY,
        headers=_authorized_headers(
            idempotency_key="callback-bad-token",
            authorization="Bearer wrong",
        ),
    )

    assert response.status_code == 401
    assert response.json()["detail"]["error_code"] == "UNAUTHORIZED"


@pytest.mark.unit
async def test_list_callbacks_requires_authorization_token(
    callback_app_and_client: tuple[FastAPI, AsyncClient],
) -> None:
    _, client = callback_app_and_client
    response = await client.get("/v1/callbacks")

    assert response.status_code == 401
    assert response.json()["detail"]["error_code"] == "UNAUTHORIZED"


@pytest.mark.unit
async def test_list_callbacks_rejects_invalid_authorization_token(client: AsyncClient) -> None:
    response = await client.get(
        "/v1/callbacks",
        headers={"Authorization": "Bearer wrong"},
    )

    assert response.status_code == 401
    assert response.json()["detail"]["error_code"] == "UNAUTHORIZED"
