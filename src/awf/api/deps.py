"""FastAPI dependencies.

Dependencies keep request-handling code free of global state. The session factory
is stored on ``app.state`` at startup, so tests can inject a fresh factory per-test
without monkeypatching module-level globals.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.config import Settings, get_settings
from awf.db.resilience import invalidate_or_rollback_session


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
    try:
        yield session
        await session.commit()
    except Exception as exc:
        await invalidate_or_rollback_session(session, exc)
        raise
    finally:
        await session.close()


def require_api_token(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    """Require the local AWF bearer token for sensitive operator APIs."""
    if not settings.api_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "API_TOKEN_NOT_CONFIGURED",
                "message": "Set AWF_API_TOKEN to enable sensitive operator APIs.",
            },
        )
    expected = f"Bearer {settings.api_token}"
    if authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "UNAUTHORIZED", "message": "Invalid AWF API token."},
            headers={"WWW-Authenticate": "Bearer"},
        )
