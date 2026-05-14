"""External callback registration endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.deps import get_db_session_factory, require_api_token
from awf.api.schemas import (
    CallbackSubscriptionCreateRequest,
    CallbackSubscriptionListResponse,
    CallbackSubscriptionResponse,
)
from awf.common.config import Settings, get_settings
from awf.service.callbacks import CallbackIdempotencyConflictError, CallbackService

router = APIRouter(prefix="/v1/callbacks", tags=["callbacks"])
_IDEMPOTENCY_KEY_MAX_LENGTH = 128


@router.post(
    "",
    response_model=CallbackSubscriptionResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_token)],
)
async def register_callback(
    payload: CallbackSubscriptionCreateRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_db_session_factory),
    settings: Settings = Depends(get_settings),
) -> CallbackSubscriptionResponse:
    _ensure_callbacks_enabled(settings)
    key = _require_idempotency_key(idempotency_key)
    try:
        subscription = await CallbackService(session_factory).register(
            payload,
            idempotency_key=key,
        )
    except CallbackIdempotencyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "IDEMPOTENCY_CONFLICT",
                "message": (
                    "Idempotency-Key previously used with a different payload; "
                    "supply a fresh key or replay with the original body."
                ),
            },
        ) from exc
    return CallbackSubscriptionResponse.model_validate(subscription)


@router.get(
    "",
    response_model=CallbackSubscriptionListResponse,
    dependencies=[Depends(require_api_token)],
)
async def list_callbacks(
    enabled: Annotated[bool | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_db_session_factory),
    settings: Settings = Depends(get_settings),
) -> CallbackSubscriptionListResponse:
    _ensure_callbacks_enabled(settings)
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
