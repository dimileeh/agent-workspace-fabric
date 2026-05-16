"""FastAPI dependencies.

Dependencies keep request-handling code free of global state. The session factory
is stored on ``app.state`` at startup, so tests can inject a fresh factory per-test
without monkeypatching module-level globals.
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Final, cast

from fastapi import (
    Depends,
    HTTPException,
    Request,
    Security,
    WebSocket,
    WebSocketException,
    status,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.responses import JSONResponse

from awf.api.auth_context import mark_bearer_auth_verified
from awf.common.config import Settings, get_settings
from awf.common.logging import get_logger
from awf.db.resilience import invalidate_or_rollback_session

_log = get_logger(__name__)
_api_token_bearer = HTTPBearer(auto_error=False, scheme_name="bearerAuth")
_DIRECT_REQUEST_DEFAULT: Final[Request] = cast(Request, None)


@dataclass(frozen=True)
class _AuthorizationFailure:
    status_code: int
    detail: dict[str, str]
    headers: dict[str, str] | None = None


class WebSocketAuthorizationDenialError(Exception):
    """Signal a structured WebSocket handshake denial response."""

    def __init__(self, failure: _AuthorizationFailure) -> None:
        super().__init__(failure.detail["error_code"])
        self.failure = failure


def _get_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """Pull the per-app session factory off ``app.state``.

    Raises at request-time if the factory wasn't configured — a loud failure is
    better than a silent 500 from a NoneType when a deploy forgets to wire the DB.
    """
    factory: async_sessionmaker[AsyncSession] | None = getattr(
        request.app.state, "db_session_factory", None
    )
    if factory is None:  # pragma: no cover - configuration error, not a request-time bug
        raise RuntimeError(
            "db_session_factory not configured on app.state; call configure_database()."
        )
    return factory


async def get_db_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """Return the current per-app async session factory."""
    return _get_session_factory(request)


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a per-request async DB session, committing on success and rolling back on error."""
    factory = _get_session_factory(request)
    session = factory()
    scope_exc: BaseException | None = None
    try:
        yield session
        await session.commit()
    except Exception as exc:
        scope_exc = exc
        await invalidate_or_rollback_session(session, exc)
        raise
    except BaseException as exc:
        scope_exc = exc
        await invalidate_or_rollback_session(session, exc)
        raise
    finally:
        try:
            await session.close()
        except Exception as close_exc:
            if scope_exc is None:
                raise
            _log.warning(
                "get_db_session.close_failed_during_exception",
                error_type=type(close_exc).__name__,
                error=str(close_exc)[:240],
            )


def resolve_settings_dependency(settings: object) -> Settings:
    """Resolve explicit settings or FastAPI's direct-call dependency sentinel."""
    if isinstance(settings, Settings):
        return settings
    return get_settings()


def require_api_token(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Security(_api_token_bearer),
    ] = None,
    settings: Settings = Depends(get_settings),
    request: Request = _DIRECT_REQUEST_DEFAULT,
) -> None:
    """Require the local AWF bearer token for sensitive operator APIs."""
    _require_authorization_header(
        _authorization_header_value(credentials),
        settings=settings,
    )
    mark_bearer_auth_verified(request)


async def require_websocket_api_token(
    websocket: WebSocket,
    settings: Settings = Depends(get_settings),
) -> None:
    """Require the local AWF bearer token for WebSocket handshakes."""
    failure = _authorization_failure(
        _normalized_authorization_header(websocket.headers.get("authorization")),
        settings=settings,
    )
    if failure is None:
        return
    if _supports_websocket_denial_response(websocket):
        raise WebSocketAuthorizationDenialError(failure)
    raise _websocket_authorization_failure(failure)


def _require_authorization_header(
    authorization: str | None,
    *,
    settings: Settings,
) -> None:
    failure = _authorization_failure(authorization, settings=settings)
    if failure is not None:
        raise _http_authorization_failure(failure)


def _authorization_failure(
    authorization: str | None,
    *,
    settings: Settings,
) -> _AuthorizationFailure | None:
    if not settings.api_token:
        return _AuthorizationFailure(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "API_TOKEN_NOT_CONFIGURED",
                "message": "Set AWF_API_TOKEN to enable sensitive operator APIs.",
            },
        )
    expected = f"Bearer {settings.api_token}".encode()
    supplied = (authorization or "").encode()
    if not hmac.compare_digest(supplied, expected):
        return _AuthorizationFailure(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "UNAUTHORIZED", "message": "Invalid AWF API token."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return None


def _supports_websocket_denial_response(websocket: WebSocket) -> bool:
    return "websocket.http.response" in websocket.scope.get("extensions", {})


def _http_authorization_failure(failure: _AuthorizationFailure) -> HTTPException:
    return HTTPException(
        status_code=failure.status_code,
        detail=failure.detail,
        headers=failure.headers,
    )


def _websocket_authorization_failure(failure: _AuthorizationFailure) -> WebSocketException:
    if failure.status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
        code = status.WS_1011_INTERNAL_ERROR
        reason = "API_TOKEN_NOT_CONFIGURED"
    else:
        code = status.WS_1008_POLICY_VIOLATION
        reason = "UNAUTHORIZED"
    return WebSocketException(code=code, reason=reason)


async def websocket_authorization_denial_handler(
    websocket: WebSocket,
    exc: Exception,
) -> None:
    if not isinstance(exc, WebSocketAuthorizationDenialError):  # pragma: no cover
        raise exc
    failure = exc.failure
    if _supports_websocket_denial_response(websocket):
        response = JSONResponse(
            {"detail": failure.detail},
            status_code=failure.status_code,
            headers=failure.headers,
        )
        await websocket.send_denial_response(response)
    else:  # pragma: no cover - defensive for direct handler callers.
        # require_websocket_api_token raises WebSocketException before this handler
        # when the denial-response extension is unavailable.
        fallback = _websocket_authorization_failure(failure)
        await websocket.close(code=fallback.code, reason=fallback.reason)


def _authorization_header_value(
    credentials: HTTPAuthorizationCredentials | None,
) -> str | None:
    if credentials is None:
        return None
    # HTTPBearer only yields credentials for the bearer scheme, case-insensitively.
    return f"Bearer {credentials.credentials}"


def _normalized_authorization_header(authorization: str | None) -> str | None:
    if authorization is None:
        return None
    scheme, separator, credentials = authorization.partition(" ")
    if not separator:
        return authorization
    normalized_scheme = "Bearer" if scheme.lower() == "bearer" else scheme
    return f"{normalized_scheme} {credentials}"
