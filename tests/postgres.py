"""PostgreSQL-backed test database helpers."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from awf.db.base import Base
from awf.db.session import make_engine, make_session_factory

DEFAULT_TEST_DATABASE_URL = "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
_SCHEMA_DDL_LOCK_NAMESPACE = 0x415746
_SCHEMA_DDL_LOCK_KEY = 0x54455354
_TEST_CONNECT_TIMEOUT_SECONDS = 10
_SCHEMA_ENGINE_CONNECT_ATTEMPTS = 3


def postgres_test_database_url() -> str:
    url = (
        os.environ.get("AWF_TEST_DATABASE_URL")
        or os.environ.get("AWF_DATABASE_URL")
        or DEFAULT_TEST_DATABASE_URL
    )
    if not url.startswith("postgresql+asyncpg://"):
        raise RuntimeError("Tests require AWF_TEST_DATABASE_URL to be postgresql+asyncpg://...")
    return url


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _schema_url(
    database_url: str,
    schema: str,
    *,
    null_pool: bool = False,
    connect_retries: bool = False,
) -> str:
    parsed_url = make_url(database_url)
    query = dict(parsed_url.query)
    query["awf_search_path"] = schema
    if connect_retries:
        query["awf_connect_timeout"] = str(_TEST_CONNECT_TIMEOUT_SECONDS)
        query["awf_connect_retries"] = str(_SCHEMA_ENGINE_CONNECT_ATTEMPTS)
    if null_pool:
        query["awf_null_pool"] = "1"
    return parsed_url.set(query=query).render_as_string(hide_password=False)


def _admin_url(database_url: str) -> str:
    return _schema_url(database_url, "public")


def _test_schema_url(
    database_url: str,
    quoted_schema: str,
    *,
    null_pool: bool = False,
    connect_retries: bool = False,
) -> str:
    return _schema_url(
        database_url,
        quoted_schema,
        null_pool=null_pool,
        connect_retries=connect_retries,
    )


def _make_test_engine(url: str) -> AsyncEngine:
    return make_engine(url, connect_args={"timeout": _TEST_CONNECT_TIMEOUT_SECONDS})


async def _dispose_engine(engine: AsyncEngine | None) -> None:
    if engine is not None:
        await engine.dispose()


async def _create_metadata_engine(schema_database_url: str) -> AsyncEngine:
    for attempt in range(_SCHEMA_ENGINE_CONNECT_ATTEMPTS):
        engine = _make_test_engine(schema_database_url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        except (OSError, TimeoutError):
            await _dispose_engine(engine)
            if attempt == _SCHEMA_ENGINE_CONNECT_ATTEMPTS - 1:
                raise
            await asyncio.sleep(0.05 * (attempt + 1))
        else:
            return engine
    raise RuntimeError("PostgreSQL test metadata engine was not initialized.")


@asynccontextmanager
async def _connect_with_retries(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    for attempt in range(_SCHEMA_ENGINE_CONNECT_ATTEMPTS):
        conn_context = engine.connect()
        try:
            conn = await conn_context.__aenter__()
        except (OSError, TimeoutError):
            if attempt == _SCHEMA_ENGINE_CONNECT_ATTEMPTS - 1:
                raise
            await asyncio.sleep(0.05 * (attempt + 1))
            continue
        try:
            yield conn
        except BaseException as exc:
            await conn_context.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            await conn_context.__aexit__(None, None, None)
            return
    raise RuntimeError("PostgreSQL test connection was not initialized.")


@asynccontextmanager
async def _postgres_schema_ddl_lock(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """Serialize schema DDL across xdist workers sharing one test database."""

    params = {
        "namespace": _SCHEMA_DDL_LOCK_NAMESPACE,
        "key": _SCHEMA_DDL_LOCK_KEY,
    }
    async with _connect_with_retries(engine) as conn:
        await conn.execute(text("SELECT pg_advisory_lock(:namespace, :key)"), params)
        await conn.commit()
        try:
            yield conn
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:namespace, :key)"), params)
            await conn.commit()


@asynccontextmanager
async def postgres_test_engine() -> AsyncIterator[AsyncEngine]:
    """Yield an isolated PostgreSQL schema with ORM metadata created."""

    database_url = postgres_test_database_url()
    schema = f"awf_test_{uuid.uuid4().hex}"
    quoted_schema = _quote_identifier(schema)
    admin_engine = _make_test_engine(_admin_url(database_url))
    schema_created = False
    engine: AsyncEngine | None = None
    try:
        async with _postgres_schema_ddl_lock(admin_engine) as admin_conn:
            await _create_schema(admin_conn, quoted_schema)
            schema_created = True
        schema_database_url = _test_schema_url(
            database_url,
            quoted_schema,
            connect_retries=True,
        )
        engine = await _create_metadata_engine(schema_database_url)
        yield engine
    finally:
        await _dispose_engine(engine)
        try:
            if schema_created:
                async with _postgres_schema_ddl_lock(admin_engine) as admin_conn:
                    await _drop_schema(admin_conn, schema, quoted_schema)
        finally:
            await admin_engine.dispose()


async def _create_schema(conn: AsyncConnection, quoted_schema: str) -> None:
    await conn.execute(text(f"CREATE SCHEMA {quoted_schema}"))
    await conn.commit()


async def _terminate_schema_lock_holders(conn: AsyncConnection, schema: str) -> None:
    await conn.execute(
        text(
            """
            SELECT pg_terminate_backend(activity.pid)
            FROM pg_stat_activity AS activity
            WHERE activity.datname = current_database()
              AND activity.pid <> pg_backend_pid()
              AND EXISTS (
                SELECT 1
                FROM pg_locks AS locks
                JOIN pg_class AS relation ON relation.oid = locks.relation
                JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                WHERE locks.pid = activity.pid
                  AND namespace.nspname = :schema
              )
            """
        ),
        {"schema": schema},
    )
    await conn.commit()


async def _drop_schema(conn: AsyncConnection, schema: str, quoted_schema: str) -> None:
    await _terminate_schema_lock_holders(conn, schema)
    await conn.execute(text("SET LOCAL lock_timeout = '5s'"))
    await conn.execute(text(f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE"))
    await conn.commit()


async def create_postgres_test_engine() -> AsyncEngine:
    """Create a test engine bound to an isolated PostgreSQL schema.

    Prefer ``postgres_test_engine`` for new tests so the schema is dropped
    deterministically. This compatibility helper intentionally returns a plain
    ``AsyncEngine`` for older helper shapes that expect to own disposal.
    """

    database_url = postgres_test_database_url()
    schema = f"awf_test_{uuid.uuid4().hex}"
    quoted_schema = _quote_identifier(schema)
    admin_engine = _make_test_engine(_admin_url(database_url))
    schema_created = False
    engine: AsyncEngine | None = None
    try:
        async with _postgres_schema_ddl_lock(admin_engine) as admin_conn:
            await _create_schema(admin_conn, quoted_schema)
            schema_created = True
        engine = await _create_metadata_engine(
            _test_schema_url(
                database_url,
                quoted_schema,
                null_pool=True,
                connect_retries=True,
            )
        )
    except Exception:
        await _dispose_engine(engine)
        if schema_created:
            async with _postgres_schema_ddl_lock(admin_engine) as admin_conn:
                await _drop_schema(admin_conn, schema, quoted_schema)
        raise
    finally:
        await admin_engine.dispose()

    return engine


@asynccontextmanager
async def postgres_test_url() -> AsyncIterator[str]:
    """Yield a PostgreSQL URL bound to an isolated schema with metadata."""

    database_url = postgres_test_database_url()
    schema = f"awf_test_{uuid.uuid4().hex}"
    quoted_schema = _quote_identifier(schema)
    admin_engine = _make_test_engine(_admin_url(database_url))
    schema_created = False
    engine: AsyncEngine | None = None
    try:
        async with _postgres_schema_ddl_lock(admin_engine) as admin_conn:
            await _create_schema(admin_conn, quoted_schema)
            schema_created = True
        schema_database_url = _test_schema_url(
            database_url,
            quoted_schema,
            null_pool=True,
            connect_retries=True,
        )
        engine = await _create_metadata_engine(schema_database_url)
        await _dispose_engine(engine)
        engine = None
        yield schema_database_url
    finally:
        await _dispose_engine(engine)
        try:
            if schema_created:
                async with _postgres_schema_ddl_lock(admin_engine) as admin_conn:
                    await _drop_schema(admin_conn, schema, quoted_schema)
        finally:
            await admin_engine.dispose()


@contextmanager
def postgres_test_url_sync() -> Iterator[str]:
    """Synchronous wrapper for tests that exercise sync CLIs."""

    database_url = postgres_test_database_url()
    schema = f"awf_test_{uuid.uuid4().hex}"
    quoted_schema = _quote_identifier(schema)

    async def _setup() -> str:
        admin_engine = _make_test_engine(_admin_url(database_url))
        schema_created = False
        engine: AsyncEngine | None = None
        try:
            async with _postgres_schema_ddl_lock(admin_engine) as admin_conn:
                await _create_schema(admin_conn, quoted_schema)
                schema_created = True
            schema_database_url = _test_schema_url(
                database_url,
                quoted_schema,
                null_pool=True,
                connect_retries=True,
            )
            engine = await _create_metadata_engine(schema_database_url)
            await _dispose_engine(engine)
            engine = None
            return schema_database_url
        except Exception:
            await _dispose_engine(engine)
            if schema_created:
                async with _postgres_schema_ddl_lock(admin_engine) as admin_conn:
                    await _drop_schema(admin_conn, schema, quoted_schema)
            raise
        finally:
            await admin_engine.dispose()

    async def _cleanup() -> None:
        admin_engine = _make_test_engine(_admin_url(database_url))
        try:
            async with _postgres_schema_ddl_lock(admin_engine) as admin_conn:
                await _drop_schema(admin_conn, schema, quoted_schema)
        finally:
            await admin_engine.dispose()

    url = asyncio.run(_setup())
    try:
        yield url
    finally:
        asyncio.run(_cleanup())


@asynccontextmanager
async def postgres_empty_test_url() -> AsyncIterator[str]:
    """Yield a PostgreSQL URL bound to an empty isolated schema."""

    database_url = postgres_test_database_url()
    schema = f"awf_test_{uuid.uuid4().hex}"
    quoted_schema = _quote_identifier(schema)
    admin_engine = _make_test_engine(_admin_url(database_url))
    schema_created = False
    try:
        async with _postgres_schema_ddl_lock(admin_engine) as admin_conn:
            await _create_schema(admin_conn, quoted_schema)
            schema_created = True
        yield _test_schema_url(database_url, quoted_schema, connect_retries=True)
    finally:
        try:
            if schema_created:
                async with _postgres_schema_ddl_lock(admin_engine) as admin_conn:
                    await _drop_schema(admin_conn, schema, quoted_schema)
        finally:
            await admin_engine.dispose()


@asynccontextmanager
async def postgres_test_session() -> AsyncIterator[AsyncSession]:
    """Yield a session bound to an isolated PostgreSQL schema."""

    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        async with factory() as session:
            yield session
