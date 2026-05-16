"""Async engine + session factory.

Engines are process-global (cached per URL); sessions are per-request and should
not be shared across task boundaries. Tests use PostgreSQL schemas through the
fixtures in tests/conftest.py.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
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

from awf.common.logging import get_logger
from awf.db.dialect import SESSION_DIALECT_NAME_KEY
from awf.db.resilience import (
    invalidate_or_rollback_session,
    is_transient_closed_connection_error,
)
from awf.runtime.events import ensure_workspace_event_broadcasting

DEFAULT_POOL_RECYCLE_SECONDS = 1800
DEFAULT_POOL_TIMEOUT_SECONDS = 30.0

_log = get_logger(__name__)


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
    raw_connect_attempts = query.pop("awf_connect_attempts", None)
    raw_connect_retries = query.pop("awf_connect_retries", None)
    engine_options: dict[str, object] = {}
    use_null_pool = False
    if raw_search_path is not None:
        search_path = _single_query_value(raw_search_path)
        existing_server_settings = resolved_connect_args.get("server_settings")
        server_settings = (
            dict(existing_server_settings) if isinstance(existing_server_settings, dict) else {}
        )
        server_settings.setdefault("search_path", str(search_path))
        resolved_connect_args["server_settings"] = server_settings
    if raw_null_pool is not None:
        null_pool = _single_query_value(raw_null_pool)
        if str(null_pool).lower() in {"1", "true", "yes"}:
            use_null_pool = True
            engine_options["poolclass"] = NullPool
    if not use_null_pool:
        engine_options.setdefault("pool_pre_ping", True)
        engine_options.setdefault("pool_recycle", DEFAULT_POOL_RECYCLE_SECONDS)
        engine_options.setdefault("pool_timeout", DEFAULT_POOL_TIMEOUT_SECONDS)
    if raw_connect_timeout is not None:
        resolved_connect_args.setdefault(
            "timeout",
            _positive_float_query_value(raw_connect_timeout, name="awf_connect_timeout"),
        )
    retry_attempts = _positive_int_query_value(
        raw_connect_attempts,
        name="awf_connect_attempts",
        default=1,
    )
    if raw_connect_retries is not None:
        retry_attempts = _positive_int_query_value(
            raw_connect_retries,
            name="awf_connect_retries",
            default=retry_attempts,
        )
    if (
        raw_search_path is not None
        or raw_null_pool is not None
        or raw_connect_timeout is not None
        or raw_connect_attempts is not None
        or raw_connect_retries is not None
    ):
        parsed_url = parsed_url.set(query=query)
    if retry_attempts > 1:
        engine_options["async_creator"] = _asyncpg_retrying_creator(
            parsed_url.set(drivername="postgresql").render_as_string(hide_password=False),
            connect_args=resolved_connect_args,
            attempts=retry_attempts,
        )

    create_kwargs: dict[str, object] = {
        "echo": echo,
        "future": True,
        **engine_options,
    }
    if "async_creator" not in engine_options:
        create_kwargs["connect_args"] = resolved_connect_args

    engine = create_async_engine(parsed_url.render_as_string(hide_password=False), **create_kwargs)
    _patch_asyncpg_pre_ping_disconnect_detection(engine)
    return engine


def _patch_asyncpg_pre_ping_disconnect_detection(engine: AsyncEngine) -> None:
    """Teach SQLAlchemy's asyncpg pre-ping about raw asyncpg protocol disconnects."""

    sync_engine = getattr(engine, "sync_engine", None)
    dialect = getattr(sync_engine, "dialect", None)
    if dialect is None:
        return
    if (
        getattr(dialect, "name", None) != "postgresql"
        or getattr(dialect, "driver", None) != "asyncpg"
    ):
        return
    do_ping = getattr(dialect, "do_ping", None)
    if not callable(do_ping):
        return

    def _do_ping_with_awf_disconnect_detection(dbapi_connection: object) -> bool:
        try:
            return bool(do_ping(dbapi_connection))
        except Exception as exc:
            if is_transient_closed_connection_error(exc):
                return False
            raise

    dialect.do_ping = _do_ping_with_awf_disconnect_detection


def _single_query_value(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, tuple):
        value = value[0] if value else None
    return None if value is None else str(value)


def _positive_float_query_value(value: object, *, name: str) -> float:
    text = _single_query_value(value)
    try:
        parsed = float(text) if text is not None else 0.0
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number.") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive number.")
    return parsed


def _positive_int_query_value(value: object | None, *, name: str, default: int) -> int:
    text = _single_query_value(value)
    if text is None:
        return default
    try:
        parsed = int(text)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer.") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return parsed


def _asyncpg_retrying_creator(
    dsn: str,
    *,
    connect_args: dict[str, object],
    attempts: int,
) -> Callable[[], Awaitable[Any]]:
    async def _connect() -> Any:
        import asyncpg  # type: ignore[import-untyped]

        transient_errors = (
            OSError,
            TimeoutError,
            asyncpg.PostgresConnectionError,
            asyncpg.TooManyConnectionsError,
        )
        attempt = 1
        while True:
            try:
                return await asyncpg.connect(dsn=dsn, **connect_args)
            except transient_errors:
                if attempt >= attempts:
                    raise
                await asyncio.sleep(min(0.1 * attempt, 1.0))
                attempt += 1

    return _connect


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
                "session_scope.close_failed_during_exception",
                error_type=type(close_exc).__name__,
                error=str(close_exc)[:240],
            )
