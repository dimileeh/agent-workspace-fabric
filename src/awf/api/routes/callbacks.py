"""External callback registration endpoints."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.deps import get_db_session_factory, resolve_settings_dependency
from awf.api.request_admission import (
    CALLBACK_REGISTER_ENDPOINT_FAMILY,
    RequestAdmissionDecision,
    admit_request,
    request_app_state,
)
from awf.api.responses import RATE_LIMITED_ERROR_RESPONSE
from awf.api.schemas import (
    CallbackSubscriptionCreateRequest,
    CallbackSubscriptionListResponse,
    CallbackSubscriptionResponse,
    ErrorResponse,
)
from awf.common.config import Settings, get_settings
from awf.service.callbacks import (
    CallbackIdempotencyConflictError,
    CallbackService,
    callback_request_hash,
)

router = APIRouter(prefix="/v1/callbacks", tags=["callbacks"])
_IDEMPOTENCY_KEY_MAX_LENGTH = 128
_CALLBACK_REGISTER_RATE_LIMITED = "CALLBACK_REGISTER_RATE_LIMITED"
_CALLBACK_REPLAY_CACHE_STATE_KEY = "callback_register_idempotency_replay_cache"
_CALLBACK_REPLAY_CACHE_MAX_ENTRIES = 4096


@dataclass(frozen=True, slots=True)
class _CachedCallbackReplay:
    request_hash: str
    response: CallbackSubscriptionResponse


class _CallbackIdempotencyReplayCache:
    def __init__(self, *, max_entries: int = _CALLBACK_REPLAY_CACHE_MAX_ENTRIES) -> None:
        self._max_entries = max_entries
        self._entries: OrderedDict[str, _CachedCallbackReplay] = OrderedDict()

    def replay(
        self,
        payload: CallbackSubscriptionCreateRequest,
        *,
        idempotency_key: str,
    ) -> CallbackSubscriptionResponse | None:
        cached = self._entries.get(idempotency_key)
        if cached is None:
            return None
        self._entries.move_to_end(idempotency_key)
        if cached.request_hash != callback_request_hash(payload):
            raise CallbackIdempotencyConflictError(
                "Idempotency-Key previously used with a different callback request."
            )
        return cached.response.model_copy(deep=True)

    def remember(
        self,
        payload: CallbackSubscriptionCreateRequest,
        *,
        idempotency_key: str,
        response: CallbackSubscriptionResponse,
    ) -> None:
        self._entries[idempotency_key] = _CachedCallbackReplay(
            request_hash=callback_request_hash(payload),
            response=response.model_copy(deep=True),
        )
        self._entries.move_to_end(idempotency_key)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)


_STATELESS_CALLBACK_REPLAY_CACHE = _CallbackIdempotencyReplayCache()


@router.post(
    "",
    response_model=CallbackSubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
    responses={429: RATE_LIMITED_ERROR_RESPONSE},
)
async def register_callback(
    payload: CallbackSubscriptionCreateRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_db_session_factory),
    settings: object = Depends(get_settings),
) -> CallbackSubscriptionResponse | JSONResponse:
    route_settings = resolve_settings_dependency(settings)
    _ensure_callbacks_enabled(route_settings)
    key = _require_idempotency_key(idempotency_key)
    replay_cache = _callback_idempotency_replay_cache(request)
    try:
        cached = replay_cache.replay(payload, idempotency_key=key)
    except CallbackIdempotencyConflictError as exc:
        raise _idempotency_conflict() from exc
    if cached is not None:
        return cached

    service = CallbackService(session_factory)
    try:
        durable_replay = await service.replay_existing(payload, idempotency_key=key)
    except CallbackIdempotencyConflictError as exc:
        raise _idempotency_conflict() from exc
    if durable_replay is not None:
        response = CallbackSubscriptionResponse.model_validate(durable_replay)
        replay_cache.remember(payload, idempotency_key=key, response=response)
        return response

    admission = admit_request(
        request,
        endpoint_family=CALLBACK_REGISTER_ENDPOINT_FAMILY,
        limit=route_settings.callback_register_rate_limit_count,
        window_seconds=route_settings.request_admission_window_seconds,
        reason_code=_CALLBACK_REGISTER_RATE_LIMITED,
    )
    if not admission.allowed:
        return _callback_register_rate_limited_response(admission)

    try:
        subscription = await service.register(
            payload,
            idempotency_key=key,
        )
    except CallbackIdempotencyConflictError as exc:
        raise _idempotency_conflict() from exc
    response = CallbackSubscriptionResponse.model_validate(subscription)
    replay_cache.remember(payload, idempotency_key=key, response=response)
    return response


@router.get("", response_model=CallbackSubscriptionListResponse)
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


def _callback_idempotency_replay_cache(
    request: Request | object | None,
) -> _CallbackIdempotencyReplayCache:
    state = request_app_state(request)
    if state is None:
        return _STATELESS_CALLBACK_REPLAY_CACHE

    existing = getattr(state, _CALLBACK_REPLAY_CACHE_STATE_KEY, None)
    if isinstance(existing, _CallbackIdempotencyReplayCache):
        return existing

    cache = _CallbackIdempotencyReplayCache()
    setattr(state, _CALLBACK_REPLAY_CACHE_STATE_KEY, cache)
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
