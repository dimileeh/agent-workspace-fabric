"""External callback registration endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.deps import get_db_session_factory, resolve_settings_dependency
from awf.api.request_admission import (
    CALLBACK_REGISTER_ENDPOINT_FAMILY,
    RequestAdmissionDecision,
    admit_request,
)
from awf.api.schemas import (
    CallbackSubscriptionCreateRequest,
    CallbackSubscriptionListResponse,
    CallbackSubscriptionResponse,
    ErrorResponse,
)
from awf.common.config import Settings, get_settings
from awf.service.callbacks import CallbackIdempotencyConflictError, CallbackService

router = APIRouter(prefix="/v1/callbacks", tags=["callbacks"])
_IDEMPOTENCY_KEY_MAX_LENGTH = 128
_CALLBACK_REGISTER_RATE_LIMITED = "CALLBACK_REGISTER_RATE_LIMITED"


@router.post(
    "",
    response_model=CallbackSubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
    responses={429: {"model": ErrorResponse}},
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
    service = CallbackService(session_factory)
    try:
        existing = await service.replay_existing(payload, idempotency_key=key)
    except CallbackIdempotencyConflictError as exc:
        raise _idempotency_conflict() from exc
    if existing is not None:
        return CallbackSubscriptionResponse.model_validate(existing)

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
    return CallbackSubscriptionResponse.model_validate(subscription)


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
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content=ErrorResponse(
            error_code=_CALLBACK_REGISTER_RATE_LIMITED,
            message="Callback registration request rate limit exceeded.",
            detail=dict(decision.metadata),
        ).model_dump(),
    )


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
