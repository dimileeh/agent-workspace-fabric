"""PostgreSQL-backed test database helpers."""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from awf.db.base import Base
from awf.db.session import make_engine, make_session_factory

DEFAULT_TEST_DATABASE_URL = "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"


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


def _schema_url(database_url: str, schema: str, *, null_pool: bool = False) -> str:
    parsed_url = make_url(database_url)
    query = dict(parsed_url.query)
    query["awf_search_path"] = schema
    if null_pool:
        query["awf_null_pool"] = "1"
    return parsed_url.set(query=query).render_as_string(hide_password=False)


def _admin_url(database_url: str) -> str:
    return _schema_url(database_url, "public", null_pool=True)


def _test_schema_url(
    database_url: str,
    quoted_schema: str,
    *,
    null_pool: bool = False,
) -> str:
    return _schema_url(database_url, quoted_schema, null_pool=null_pool)


async def _dispose_engine(engine: AsyncEngine | None) -> None:
    if engine is not None:
        await engine.dispose()


@asynccontextmanager
async def postgres_test_engine() -> AsyncIterator[AsyncEngine]:
    """Yield an isolated PostgreSQL schema with ORM metadata created."""

    database_url = postgres_test_database_url()
    schema = f"awf_test_{uuid.uuid4().hex}"
    quoted_schema = _quote_identifier(schema)
    admin_engine = make_engine(_admin_url(database_url))
    schema_created = False
    engine: AsyncEngine | None = None
    try:
        await _create_schema(admin_engine, quoted_schema)
        schema_created = True
        schema_database_url = _test_schema_url(database_url, quoted_schema)
        engine = make_engine(schema_database_url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        if engine is None:
            raise RuntimeError("PostgreSQL test engine was not initialized.")
        yield engine
    finally:
        await _dispose_engine(engine)
        try:
            if schema_created:
                await _drop_schema(admin_engine, schema, quoted_schema)
        finally:
            await admin_engine.dispose()


async def _create_schema(engine: AsyncEngine, quoted_schema: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA {quoted_schema}"))


async def _terminate_schema_lock_holders(engine: AsyncEngine, schema: str) -> None:
    async with engine.begin() as conn:
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


async def _drop_schema(engine: AsyncEngine, schema: str, quoted_schema: str) -> None:
    await _terminate_schema_lock_holders(engine, schema)
    async with engine.begin() as conn:
        await conn.execute(text("SET LOCAL lock_timeout = '5s'"))
        await conn.execute(text(f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE"))


async def create_postgres_test_engine() -> AsyncEngine:
    """Create a test engine bound to an isolated PostgreSQL schema.

    Prefer ``postgres_test_engine`` for new tests so the schema is dropped
    deterministically. This compatibility helper intentionally returns a plain
    ``AsyncEngine`` for older helper shapes that expect to own disposal.
    """

    database_url = postgres_test_database_url()
    schema = f"awf_test_{uuid.uuid4().hex}"
    quoted_schema = _quote_identifier(schema)
    admin_engine = make_engine(_admin_url(database_url))
    schema_created = False
    engine: AsyncEngine | None = None
    try:
        await _create_schema(admin_engine, quoted_schema)
        schema_created = True
        engine = make_engine(_test_schema_url(database_url, quoted_schema, null_pool=True))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception:
        await _dispose_engine(engine)
        if schema_created:
            await _drop_schema(admin_engine, schema, quoted_schema)
        raise
    finally:
        await admin_engine.dispose()

    if engine is None:
        raise RuntimeError("PostgreSQL test engine was not initialized.")
    return engine


@asynccontextmanager
async def postgres_test_url() -> AsyncIterator[str]:
    """Yield a PostgreSQL URL bound to an isolated schema with metadata."""

    database_url = postgres_test_database_url()
    schema = f"awf_test_{uuid.uuid4().hex}"
    quoted_schema = _quote_identifier(schema)
    admin_engine = make_engine(_admin_url(database_url))
    schema_created = False
    engine: AsyncEngine | None = None
    try:
        await _create_schema(admin_engine, quoted_schema)
        schema_created = True
        schema_database_url = _test_schema_url(database_url, quoted_schema)
        engine = make_engine(_test_schema_url(database_url, quoted_schema, null_pool=True))
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await _dispose_engine(engine)
            engine = None
        yield schema_database_url
    finally:
        await _dispose_engine(engine)
        try:
            if schema_created:
                await _drop_schema(admin_engine, schema, quoted_schema)
        finally:
            await admin_engine.dispose()


@contextmanager
def postgres_test_url_sync() -> Iterator[str]:
    """Synchronous wrapper for tests that exercise sync CLIs."""

    database_url = postgres_test_database_url()
    schema = f"awf_test_{uuid.uuid4().hex}"
    quoted_schema = _quote_identifier(schema)

    async def _setup() -> str:
        admin_engine = make_engine(_admin_url(database_url))
        schema_created = False
        engine: AsyncEngine | None = None
        try:
            await _create_schema(admin_engine, quoted_schema)
            schema_created = True
            schema_database_url = _test_schema_url(database_url, quoted_schema)
            engine = make_engine(_test_schema_url(database_url, quoted_schema, null_pool=True))
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
            finally:
                await _dispose_engine(engine)
                engine = None
            return schema_database_url
        except Exception:
            await _dispose_engine(engine)
            if schema_created:
                await _drop_schema(admin_engine, schema, quoted_schema)
            raise
        finally:
            await admin_engine.dispose()

    async def _cleanup() -> None:
        admin_engine = make_engine(_admin_url(database_url))
        try:
            await _drop_schema(admin_engine, schema, quoted_schema)
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
    admin_engine = make_engine(_admin_url(database_url))
    schema_created = False
    try:
        await _create_schema(admin_engine, quoted_schema)
        schema_created = True
        yield _test_schema_url(database_url, quoted_schema)
    finally:
        try:
            if schema_created:
                await _drop_schema(admin_engine, schema, quoted_schema)
        finally:
            await admin_engine.dispose()


@asynccontextmanager
async def postgres_test_session() -> AsyncIterator[AsyncSession]:
    """Yield a session bound to an isolated PostgreSQL schema."""

    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        async with factory() as session:
            yield session
