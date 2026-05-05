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


@asynccontextmanager
async def postgres_test_engine() -> AsyncIterator[AsyncEngine]:
    """Yield an isolated PostgreSQL schema with ORM metadata created."""

    database_url = postgres_test_database_url()
    schema = f"awf_test_{uuid.uuid4().hex}"
    quoted_schema = _quote_identifier(schema)
    admin_engine = make_engine(database_url)
    await _create_schema(admin_engine, quoted_schema)
    schema_database_url = _schema_url(database_url, quoted_schema)
    engine = make_engine(schema_database_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield engine
    finally:
        await engine.dispose()
        try:
            await _drop_schema(admin_engine, quoted_schema)
        finally:
            await admin_engine.dispose()


async def _create_schema(engine: AsyncEngine, quoted_schema: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA {quoted_schema}"))


async def _drop_schema(engine: AsyncEngine, quoted_schema: str) -> None:
    async with engine.begin() as conn:
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
    admin_engine = make_engine(database_url)
    await _create_schema(admin_engine, quoted_schema)
    await admin_engine.dispose()
    engine = make_engine(_schema_url(database_url, quoted_schema, null_pool=True))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


@asynccontextmanager
async def postgres_test_url() -> AsyncIterator[str]:
    """Yield a PostgreSQL URL bound to an isolated schema with metadata."""

    database_url = postgres_test_database_url()
    schema = f"awf_test_{uuid.uuid4().hex}"
    quoted_schema = _quote_identifier(schema)
    admin_engine = make_engine(database_url)
    await _create_schema(admin_engine, quoted_schema)
    schema_database_url = _schema_url(database_url, quoted_schema)
    engine = make_engine(schema_database_url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield schema_database_url
    finally:
        await engine.dispose()
        try:
            await _drop_schema(admin_engine, quoted_schema)
        finally:
            await admin_engine.dispose()


@contextmanager
def postgres_test_url_sync() -> Iterator[str]:
    """Synchronous wrapper for tests that exercise sync CLIs."""

    database_url = postgres_test_database_url()
    schema = f"awf_test_{uuid.uuid4().hex}"
    quoted_schema = _quote_identifier(schema)

    async def _setup() -> str:
        admin_engine = make_engine(database_url)
        await _create_schema(admin_engine, quoted_schema)
        await admin_engine.dispose()

        schema_database_url = _schema_url(database_url, quoted_schema, null_pool=True)
        engine = make_engine(schema_database_url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await engine.dispose()
        return schema_database_url

    async def _cleanup() -> None:
        admin_engine = make_engine(database_url)
        try:
            await _drop_schema(admin_engine, quoted_schema)
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
    admin_engine = make_engine(database_url)
    await _create_schema(admin_engine, quoted_schema)
    try:
        yield _schema_url(database_url, quoted_schema)
    finally:
        try:
            await _drop_schema(admin_engine, quoted_schema)
        finally:
            await admin_engine.dispose()


@asynccontextmanager
async def postgres_test_session() -> AsyncIterator[AsyncSession]:
    """Yield a session bound to an isolated PostgreSQL schema."""

    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        async with factory() as session:
            yield session
