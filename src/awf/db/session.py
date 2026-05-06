"""Async engine + session factory.

Engines are process-global (cached per URL); sessions are per-request and should
not be shared across task boundaries. Tests use PostgreSQL schemas through the
fixtures in tests/conftest.py.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from awf.db.dialect import SESSION_DIALECT_NAME_KEY
from awf.runtime.events import ensure_workspace_event_broadcasting


def _first_query_value(value: str | tuple[str, ...]) -> str:
    if isinstance(value, tuple):
        return value[0]
    return value


def make_engine(
    url: str,
    *,
    echo: bool = False,
    connect_args: dict[str, object] | None = None,
) -> AsyncEngine:
    """Create a new async engine for the given URL.

    ``echo`` enables SQL statement logging — handy for debugging tests but noisy
    in prod. Do not default it to True.
    """
    parsed_url = make_url(url)
    if parsed_url.drivername != "postgresql+asyncpg":
        raise ValueError("AWF requires a postgresql+asyncpg:// database URL.")
    resolved_connect_args = dict(connect_args or {})
    query = dict(parsed_url.query)
    raw_search_path = query.pop("awf_search_path", None)
    raw_null_pool = query.pop("awf_null_pool", None)
    raw_connect_timeout = query.pop("awf_connect_timeout", None)
    raw_connect_retries = query.pop("awf_connect_retries", None)
    engine_options: dict[str, object] = {}
    if raw_search_path is not None:
        search_path = _first_query_value(raw_search_path)
        existing_server_settings = resolved_connect_args.get("server_settings")
        server_settings = (
            dict(existing_server_settings) if isinstance(existing_server_settings, dict) else {}
        )
        server_settings.setdefault("search_path", str(search_path))
        resolved_connect_args["server_settings"] = server_settings
    if raw_null_pool is not None:
        null_pool = _first_query_value(raw_null_pool)
        if str(null_pool).lower() in {"1", "true", "yes"}:
            engine_options["poolclass"] = NullPool
    if raw_connect_timeout is not None:
        connect_timeout = _first_query_value(raw_connect_timeout)
        resolved_connect_args.setdefault("timeout", float(str(connect_timeout)))
    retry_count = 1
    if raw_connect_retries is not None:
        connect_retries = _first_query_value(raw_connect_retries)
        retry_count = max(1, int(str(connect_retries)))
    if (
        raw_search_path is not None
        or raw_null_pool is not None
        or raw_connect_timeout is not None
        or raw_connect_retries is not None
    ):
        parsed_url = parsed_url.set(query=query)

    engine_url = parsed_url.render_as_string(hide_password=False)
    engine_connect_args = resolved_connect_args
    if retry_count > 1:
        asyncpg: Any = importlib.import_module("asyncpg")

        asyncpg_url = parsed_url.set(drivername="postgresql").render_as_string(
            hide_password=False
        )

        async def _async_creator() -> object:
            for attempt in range(retry_count):
                try:
                    return await asyncpg.connect(asyncpg_url, **resolved_connect_args)
                except TimeoutError:
                    if attempt == retry_count - 1:
                        raise
                    await asyncio.sleep(0.05 * (attempt + 1))
            raise RuntimeError("AWF database connection was not initialized.")

        engine_options["async_creator"] = _async_creator
        engine_connect_args = {}

    return create_async_engine(
        engine_url,
        echo=echo,
        future=True,
        connect_args=engine_connect_args,
        **engine_options,
    )


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
