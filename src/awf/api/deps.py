"""FastAPI dependencies.

Dependencies keep request-handling code free of global state. The session factory
is stored on ``app.state`` at startup, so tests can inject a fresh factory per-test
without monkeypatching module-level globals.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import Header, HTTPException, Request, status
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.config import get_settings
from awf.db.base import Base
from awf.db.session import make_engine, make_session_factory


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


async def _ensure_live_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """Return the current DB factory, refreshing a replaced SQLite file.

    Local AWF deployments often run the API and ``scripts/run_awf.py`` against
    the same file-backed SQLite DB. If an operator or old script unlinks and
    recreates that file, SQLite keeps existing connections attached to the
    anonymous old inode. Detect that inode swap before each request and rebuild
    the app's engine so the API follows the visible DB path again.
    """
    factory = _get_session_factory(request)
    database_url: str | None = getattr(request.app.state, "database_url", None)
    if database_url is None:
        return factory
    db_path = _sqlite_file_path(database_url)
    if db_path is None:
        return factory

    current_identity = _sqlite_identity(db_path)
    stored_identity: tuple[int, int] | None = getattr(request.app.state, "db_sqlite_identity", None)
    if stored_identity == current_identity:
        return factory

    lock: asyncio.Lock | None = getattr(request.app.state, "db_reconnect_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        request.app.state.db_reconnect_lock = lock

    async with lock:
        latest_identity = _sqlite_identity(db_path)
        if getattr(request.app.state, "db_sqlite_identity", None) == latest_identity:
            return _get_session_factory(request)
        old_engine = getattr(request.app.state, "db_engine", None)
        new_engine = make_engine(database_url)
        async with new_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        latest_identity = _sqlite_identity(db_path)
        new_factory = make_session_factory(new_engine)
        request.app.state.db_engine = new_engine
        request.app.state.db_session_factory = new_factory
        request.app.state.db_sqlite_identity = latest_identity
        if old_engine is not None:
            await old_engine.dispose()
        return new_factory


async def get_db_session_factory(request: Request) -> async_sessionmaker[AsyncSession]:
    """Return the current per-app async session factory."""
    return await _ensure_live_session_factory(request)


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a per-request async DB session, committing on success and rolling back on error."""
    factory = await _ensure_live_session_factory(request)
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


def require_api_token(authorization: str | None = Header(default=None)) -> None:
    """Require the local AWF bearer token for sensitive operator APIs."""
    settings = get_settings()
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


def _sqlite_file_path(database_url: str) -> Path | None:
    try:
        url = make_url(database_url)
    except Exception:
        return None
    if not url.drivername.startswith("sqlite"):
        return None
    database = url.database
    if not database or database == ":memory:":
        return None
    return Path(database).expanduser().resolve()


def _sqlite_identity(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_dev, stat.st_ino)
