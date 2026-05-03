"""Async engine + session factory.

Engines are process-global (cached per URL); sessions are per-request and should
not be shared across task boundaries. Tests get a fresh engine + in-memory SQLite
via the fixtures in tests/conftest.py.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from awf.db.dialect import SESSION_DIALECT_NAME_KEY
from awf.runtime.events import ensure_workspace_event_broadcasting

_SQLITE_DATETIME_ADAPTERS_REGISTERED = False


def make_engine(url: str, *, echo: bool = False) -> AsyncEngine:
    """Create a new async engine for the given URL.

    ``echo`` enables SQL statement logging — handy for debugging tests but noisy
    in prod. Do not default it to True.
    """
    if url.startswith("sqlite"):
        _ensure_sqlite_datetime_adapters()
    return create_async_engine(url, echo=echo, future=True)


def _ensure_sqlite_datetime_adapters() -> None:
    """Install explicit datetime adapters for raw SQLite binds on Python 3.12+."""
    global _SQLITE_DATETIME_ADAPTERS_REGISTERED
    if _SQLITE_DATETIME_ADAPTERS_REGISTERED:
        return
    sqlite3.register_adapter(date, lambda value: value.isoformat())
    sqlite3.register_adapter(datetime, lambda value: value.isoformat(" "))
    _SQLITE_DATETIME_ADAPTERS_REGISTERED = True


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory bound to the given engine.

    ``expire_on_commit=False`` so attributes remain readable after commit —
    important for API handlers that return ORM objects after a write.
    """
    ensure_workspace_event_broadcasting()
    return async_sessionmaker(
        engine,
        expire_on_commit=False,
        class_=AsyncSession,
        info={SESSION_DIALECT_NAME_KEY: engine.dialect.name},
    )


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Explicit session context manager for workers / CLI.

    FastAPI routes should use the ``get_db_session`` dependency in
    ``awf.api.deps`` instead; this is for code outside the request cycle.
    """
    session = session_factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
