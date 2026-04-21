"""FastAPI dependencies.

Dependencies keep request-handling code free of global state. The session factory
is stored on ``app.state`` at startup, so tests can inject a fresh factory per-test
without monkeypatching module-level globals.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


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


async def get_db_session(
    factory: async_sessionmaker[AsyncSession] = Depends(_get_session_factory),
) -> AsyncIterator[AsyncSession]:
    """Yield a per-request async DB session, committing on success and rolling back on error."""
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
