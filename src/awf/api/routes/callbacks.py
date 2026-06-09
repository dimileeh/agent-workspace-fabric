"""External callback registration endpoints."""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.deps import get_db_session_factory, require_api_token, resolve_settings_dependency
from awf.api.request_admission import (
    CALLBACK_REGISTER_ENDPOINT_FAMILY,
    RequestAdmissionDecision,
    admit_request_async,
    ensure_bearer_auth_verified_from_header,
    request_app_state,
)
from awf.api.responses import RATE_LIMITED_ERROR_RESPONSE
from awf.api.schemas import (
    CallbackSubscriptionCreateRequest,
    CallbackSubscriptionListResponse,
    CallbackSubscriptionResponse,
    ErrorResponse,
    HTTPExceptionErrorResponse,
)
from awf.common.config import Settings, get_settings
from awf.service.callbacks import (
    CallbackIdempotencyConflictError,
    CallbackService,
    CallbackTargetPolicyError,
    CallbackTargetPolicyViolationError,
    callback_request_hash,
)

router = APIRouter(
    prefix="/v1/callbacks",
    tags=["callbacks"],
    responses={
        401: {"model": HTTPExceptionErrorResponse, "description": "Unauthorized"},
        503: {"model": HTTPExceptionErrorResponse, "description": "Service Unavailable"},
    },
)
_IDEMPOTENCY_KEY_MAX_LENGTH = 128
_CALLBACK_REGISTER_RATE_LIMITED = "CALLBACK_REGISTER_RATE_LIMITED"
_IDEMPOTENCY_REPLAY_UNAVAILABLE = "IDEMPOTENCY_REPLAY_UNAVAILABLE"
_CALLBACK_REPLAY_CACHE_STATE_KEY = "callback_register_idempotency_replay_cache"
_CALLBACK_REPLAY_KEY_CACHE_STATE_KEY = "callback_register_idempotency_replay_key_cache"
_CALLBACK_REPLAY_CACHE_MAX_ENTRIES = 4096
_CALLBACK_REPLAY_KEY_CACHE_MAX_ENTRIES = _CALLBACK_REPLAY_CACHE_MAX_ENTRIES


@dataclass(frozen=True, slots=True)
class _CachedCallbackReplay:
    request_hash: str
    response: CallbackSubscriptionResponse


class _CallbackIdempotencyReplayCache:
    def __init__(self, *, max_entries: int = _CALLBACK_REPLAY_CACHE_MAX_ENTRIES) -> None:
        self._max_entries = max_entries
        self._entries: OrderedDict[str, _CachedCallbackReplay] = OrderedDict()
        self._lock = threading.Lock()

    def replay(
        self,
        payload: CallbackSubscriptionCreateRequest,
        *,
        idempotency_key: str,
    ) -> CallbackSubscriptionResponse | None:
        request_hash = callback_request_hash(payload)
        with self._lock:
            cached = self._entries.get(idempotency_key)
            if cached is None:
                return None
            if cached.request_hash != request_hash:
                raise CallbackIdempotencyConflictError(
                    "Idempotency-Key previously used with a different callback request."
                )
            self._entries.move_to_end(idempotency_key)
            return cached.response.model_copy(deep=True)

    def remember(
        self,
        payload: CallbackSubscriptionCreateRequest,
        *,
        idempotency_key: str,
        response: CallbackSubscriptionResponse,
    ) -> None:
        cached = _CachedCallbackReplay(
            request_hash=callback_request_hash(payload),
            response=response.model_copy(deep=True),
        )
        with self._lock:
            self._entries[idempotency_key] = cached
            self._entries.move_to_end(idempotency_key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)


class _CallbackIdempotencyReplayKeyCache:
    def __init__(self, *, max_entries: int | None = None) -> None:
        if max_entries is not None and max_entries < 1:
            raise ValueError("max_entries must be greater than 0")
        self._max_entries = max_entries
        self._entries: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()

    def matches(
        self,
        payload: CallbackSubscriptionCreateRequest,
        *,
        idempotency_key: str,
    ) -> bool:
        payload_hash = callback_request_hash(payload)
        with self._lock:
            request_hash = self._entries.get(idempotency_key)
            if request_hash is None:
                return False
            if request_hash != payload_hash:
                raise CallbackIdempotencyConflictError(
                    "Idempotency-Key previously used with a different callback request."
                )
            self._entries.move_to_end(idempotency_key)
            return True

    def remember(
        self,
        payload: CallbackSubscriptionCreateRequest,
        *,
        idempotency_key: str,
    ) -> None:
        self.remember_hash(
            idempotency_key=idempotency_key,
            request_hash=callback_request_hash(payload),
        )

    def remember_hash(self, *, idempotency_key: str, request_hash: str) -> None:
        with self._lock:
            self._entries[idempotency_key] = request_hash
            self._entries.move_to_end(idempotency_key)
            self._trim()

    def _trim(self) -> None:
        if self._max_entries is None:
            return
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)


def _new_callback_replay_key_cache() -> _CallbackIdempotencyReplayKeyCache:
    return _CallbackIdempotencyReplayKeyCache(max_entries=_CALLBACK_REPLAY_KEY_CACHE_MAX_ENTRIES)


@router.post(
    "",
    response_model=CallbackSubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_token)],
    responses={
        400: {"model": HTTPExceptionErrorResponse, "description": "Bad Request"},
        409: {"model": HTTPExceptionErrorResponse, "description": "Conflict"},
        422: {
            "description": "Validation Error or Callback Target Policy Violation",
            "content": {
                "application/json": {
                    "schema": {
                        "title": "CallbackRegistrationUnprocessableEntityResponse",
                        "oneOf": [
                            {"$ref": "#/components/schemas/HTTPValidationError"},
                            {"$ref": "#/components/schemas/HTTPExceptionErrorResponse"},
                        ],
                    }
                }
            },
        },
        429: RATE_LIMITED_ERROR_RESPONSE,
    },
)
async def register_callback(
    payload: CallbackSubscriptionCreateRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_db_session_factory),
    settings: object = Depends(get_settings),
) -> CallbackSubscriptionResponse | JSONResponse:
    route_settings = resolve_settings_dependency(settings)
    ensure_bearer_auth_verified_from_header(
        request,
        expected_token=route_settings.api_token,
    )
    _ensure_callbacks_enabled(route_settings)
    key = _require_idempotency_key(idempotency_key)
    replay_cache = _callback_idempotency_replay_cache(request)
    replay_key_cache = _callback_idempotency_replay_key_cache(request)
    service = CallbackService(session_factory, settings=route_settings)
    try:
        cached = replay_cache.replay(payload, idempotency_key=key)
    except CallbackIdempotencyConflictError as exc:
        raise _idempotency_conflict() from exc
    if cached is not None:
        return cached

    try:
        known_replay_key = replay_key_cache.matches(payload, idempotency_key=key)
    except CallbackIdempotencyConflictError as exc:
        raise _idempotency_conflict() from exc
    if known_replay_key:
        response = await _callback_durable_replay_response(
            service,
            replay_cache,
            replay_key_cache,
            payload,
            idempotency_key=key,
        )
        if response is not None:
            return response
        raise _idempotency_replay_unavailable()

    async with session_factory() as session:
        # Cold persisted idempotency replays must bypass the rate gate. Fresh
        # requests keep this transaction-level advisory lock through admission
        # and create so the hot path does not lock the same key repeatedly.
        response = await _callback_durable_replay_response_for_persisted_key(
            service,
            replay_cache,
            replay_key_cache,
            payload,
            idempotency_key=key,
            session=session,
        )
        if response is not None:
            return response

        admission = await admit_request_async(
            request,
            endpoint_family=CALLBACK_REGISTER_ENDPOINT_FAMILY,
            limit=route_settings.callback_register_rate_limit_count,
            window_seconds=route_settings.request_admission_window_seconds,
            reason_code=_CALLBACK_REGISTER_RATE_LIMITED,
        )
        if not admission.allowed:
            response = await _callback_durable_replay_response_from_locked_session(
                service,
                replay_cache,
                replay_key_cache,
                payload,
                idempotency_key=key,
                session=session,
            )
            if response is not None:
                return response
            return _callback_register_rate_limited_response(admission)

        response = await _callback_durable_replay_response_from_locked_session(
            service,
            replay_cache,
            replay_key_cache,
            payload,
            idempotency_key=key,
            session=session,
        )
        if response is not None:
            return response

        try:
            subscription = await service.register_with_locked_idempotency_key(
                session,
                payload,
                idempotency_key=key,
            )
        except CallbackTargetPolicyViolationError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error_code": "CALLBACK_TARGET_POLICY_VIOLATION",
                    "message": str(exc),
                },
            ) from exc
        except CallbackTargetPolicyError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error_code": "CALLBACK_TARGET_INVALID",
                    "message": str(exc),
                },
            ) from exc
        except CallbackIdempotencyConflictError as exc:
            raise _idempotency_conflict() from exc
    response = CallbackSubscriptionResponse.model_validate(subscription)
    _remember_callback_replay(
        replay_cache,
        replay_key_cache,
        payload,
        idempotency_key=key,
        response=response,
    )
    return response


@router.get(
    "",
    response_model=CallbackSubscriptionListResponse,
    dependencies=[Depends(require_api_token)],
)
async def list_callbacks(
    enabled: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_db_session_factory),
    settings: object = Depends(get_settings),
) -> CallbackSubscriptionListResponse:
    route_settings = resolve_settings_dependency(settings)
    _ensure_callbacks_enabled(route_settings)
    rows = await CallbackService(session_factory).list(enabled=enabled, limit=limit)
    return CallbackSubscriptionListResponse(
        items=[CallbackSubscriptionResponse.model_validate(row) for row in rows],
        next_cursor=None,
        has_more=False,
        limit=limit,
        cursor=None,
    )


def _ensure_callbacks_enabled(settings: Settings) -> None:
    if not settings.callbacks_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "CALLBACKS_DISABLED",
                "message": "External callbacks are disabled by configuration.",
            },
        )


def _idempotency_conflict() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error_code": "IDEMPOTENCY_CONFLICT",
            "message": (
                "Idempotency-Key previously used with a different payload; "
                "supply a fresh key or replay with the original body."
            ),
        },
    )


def _idempotency_replay_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "error_code": _IDEMPOTENCY_REPLAY_UNAVAILABLE,
            "message": (
                "Idempotency-Key was recognized but the original callback registration "
                "is no longer available; use a fresh key only when creating a new "
                "callback registration is intended."
            ),
        },
    )


def _callback_register_rate_limited_response(
    decision: RequestAdmissionDecision,
) -> JSONResponse:
    retry_after = decision.metadata.get("retry_after_seconds", 1)
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content=ErrorResponse(
            error_code=_CALLBACK_REGISTER_RATE_LIMITED,
            message="Callback registration request rate limit exceeded.",
            detail=dict(decision.metadata),
        ).model_dump(),
        headers={"Retry-After": str(retry_after)},
    )


async def _callback_durable_replay_response_for_persisted_key(
    service: CallbackService,
    replay_cache: _CallbackIdempotencyReplayCache,
    replay_key_cache: _CallbackIdempotencyReplayKeyCache,
    payload: CallbackSubscriptionCreateRequest,
    *,
    idempotency_key: str,
    session: AsyncSession | None = None,
) -> CallbackSubscriptionResponse | None:
    try:
        if session is None:
            durable_replay = await service.replay_existing_for_persisted_key(
                payload,
                idempotency_key=idempotency_key,
            )
        else:
            durable_replay = await service.replay_existing_for_persisted_key_in_session(
                session,
                payload,
                idempotency_key=idempotency_key,
            )
    except CallbackIdempotencyConflictError as exc:
        raise _idempotency_conflict() from exc
    if durable_replay is None:
        return None
    response = CallbackSubscriptionResponse.model_validate(durable_replay)
    _remember_callback_replay(
        replay_cache,
        replay_key_cache,
        payload,
        idempotency_key=idempotency_key,
        response=response,
    )
    return response


async def _callback_durable_replay_response_from_locked_session(
    service: CallbackService,
    replay_cache: _CallbackIdempotencyReplayCache,
    replay_key_cache: _CallbackIdempotencyReplayKeyCache,
    payload: CallbackSubscriptionCreateRequest,
    *,
    idempotency_key: str,
    session: AsyncSession,
) -> CallbackSubscriptionResponse | None:
    try:
        durable_replay = await service.replay_existing_in_locked_session(
            session,
            payload,
            idempotency_key=idempotency_key,
        )
    except CallbackIdempotencyConflictError as exc:
        raise _idempotency_conflict() from exc
    if durable_replay is None:
        return None
    response = CallbackSubscriptionResponse.model_validate(durable_replay)
    _remember_callback_replay(
        replay_cache,
        replay_key_cache,
        payload,
        idempotency_key=idempotency_key,
        response=response,
    )
    return response


async def _callback_durable_replay_response(
    service: CallbackService,
    replay_cache: _CallbackIdempotencyReplayCache,
    replay_key_cache: _CallbackIdempotencyReplayKeyCache,
    payload: CallbackSubscriptionCreateRequest,
    *,
    idempotency_key: str,
) -> CallbackSubscriptionResponse | None:
    try:
        durable_replay = await service.replay_existing(payload, idempotency_key=idempotency_key)
    except CallbackIdempotencyConflictError as exc:
        raise _idempotency_conflict() from exc
    if durable_replay is None:
        return None
    response = CallbackSubscriptionResponse.model_validate(durable_replay)
    _remember_callback_replay(
        replay_cache,
        replay_key_cache,
        payload,
        idempotency_key=idempotency_key,
        response=response,
    )
    return response


def _remember_callback_replay(
    replay_cache: _CallbackIdempotencyReplayCache,
    replay_key_cache: _CallbackIdempotencyReplayKeyCache,
    payload: CallbackSubscriptionCreateRequest,
    *,
    idempotency_key: str,
    response: CallbackSubscriptionResponse,
) -> None:
    replay_cache.remember(payload, idempotency_key=idempotency_key, response=response)
    replay_key_cache.remember(payload, idempotency_key=idempotency_key)


def _callback_idempotency_replay_cache(
    request: Request | object | None,
) -> _CallbackIdempotencyReplayCache:
    state = request_app_state(request)
    if state is None:
        if isinstance(request, Request):
            raise RuntimeError(
                "callback idempotency replay cache requires request.app.state; "
                "direct callers must pass None or a non-Starlette test object."
            )
        return _direct_callback_idempotency_replay_cache(request)

    existing = getattr(state, _CALLBACK_REPLAY_CACHE_STATE_KEY, None)
    if isinstance(existing, _CallbackIdempotencyReplayCache):
        return existing

    cache = _CallbackIdempotencyReplayCache()
    setattr(state, _CALLBACK_REPLAY_CACHE_STATE_KEY, cache)
    return cache


def _callback_idempotency_replay_key_cache(
    request: Request | object | None,
) -> _CallbackIdempotencyReplayKeyCache:
    state = request_app_state(request)
    if state is None:
        if isinstance(request, Request):
            raise RuntimeError(
                "callback idempotency replay key cache requires request.app.state; "
                "direct callers must pass None or a non-Starlette test object."
            )
        return _direct_callback_idempotency_replay_key_cache(request)

    existing = getattr(state, _CALLBACK_REPLAY_KEY_CACHE_STATE_KEY, None)
    if isinstance(existing, _CallbackIdempotencyReplayKeyCache):
        return existing

    cache = _new_callback_replay_key_cache()
    setattr(state, _CALLBACK_REPLAY_KEY_CACHE_STATE_KEY, cache)
    return cache


def _direct_callback_idempotency_replay_cache(
    request: Request | object | None,
) -> _CallbackIdempotencyReplayCache:
    if request is None:
        return _CallbackIdempotencyReplayCache()

    existing = getattr(request, _CALLBACK_REPLAY_CACHE_STATE_KEY, None)
    if isinstance(existing, _CallbackIdempotencyReplayCache):
        return existing

    cache = _CallbackIdempotencyReplayCache()
    try:
        setattr(request, _CALLBACK_REPLAY_CACHE_STATE_KEY, cache)
    except (AttributeError, TypeError):
        return cache
    return cache


def _direct_callback_idempotency_replay_key_cache(
    request: Request | object | None,
) -> _CallbackIdempotencyReplayKeyCache:
    if request is None:
        return _new_callback_replay_key_cache()

    existing = getattr(request, _CALLBACK_REPLAY_KEY_CACHE_STATE_KEY, None)
    if isinstance(existing, _CallbackIdempotencyReplayKeyCache):
        return existing

    cache = _new_callback_replay_key_cache()
    try:
        setattr(request, _CALLBACK_REPLAY_KEY_CACHE_STATE_KEY, cache)
    except (AttributeError, TypeError):
        return cache
    return cache


def _require_idempotency_key(idempotency_key: str | None) -> str:
    if idempotency_key is None or not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "INVALID_REQUEST",
                "message": "Idempotency-Key header is required for this endpoint.",
            },
        )
    key = idempotency_key.strip()
    if len(key) > _IDEMPOTENCY_KEY_MAX_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": "INVALID_REQUEST",
                "message": "Idempotency-Key header must be at most 128 characters.",
            },
        )
    return key
