"""PostgreSQL-backed test database helpers."""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from awf.db.base import Base
from awf.db.session import make_engine, make_session_factory

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix platforms do not run AWF CI.
    fcntl = None  # type: ignore[assignment]

DEFAULT_TEST_DATABASE_URL = "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
_TEST_CONNECT_ARGS: dict[str, object] = {"timeout": 2}
_TEST_CONNECT_ATTEMPTS = 5


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


@contextmanager
def _postgres_ddl_lock() -> Iterator[None]:
    if fcntl is None:
        yield
        return

    run_uid = os.environ.get("PYTEST_XDIST_TESTRUNUID", "local")
    lock_path = Path(tempfile.gettempdir()) / f"awf-pytest-postgres-ddl-{run_uid}.lock"
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@asynccontextmanager
async def postgres_test_engine() -> AsyncIterator[AsyncEngine]:
    """Yield an isolated PostgreSQL schema with ORM metadata created."""

    database_url = postgres_test_database_url()
    schema = f"awf_test_{uuid.uuid4().hex}"
    quoted_schema = _quote_identifier(schema)
    admin_engine = _make_test_engine(database_url)
    schema_database_url = _schema_url(database_url, schema)
    engine = _make_test_engine(schema_database_url)
    try:
        with _postgres_ddl_lock():
            await _with_connect_retry(
                "create schema",
                schema,
                lambda: _create_schema(admin_engine, quoted_schema),
            )
            await _with_connect_retry("create metadata", schema, lambda: _create_metadata(engine))
        yield engine
    finally:
        await engine.dispose()
        try:
            with _postgres_ddl_lock():
                await _with_connect_retry(
                    "drop schema", schema, lambda: _drop_schema(admin_engine, schema)
                )
        finally:
            await admin_engine.dispose()


def _make_test_engine(database_url: str) -> AsyncEngine:
    return make_engine(database_url, connect_args=_TEST_CONNECT_ARGS)


async def _with_connect_retry[T](
    operation: str,
    schema: str,
    action: Callable[[], Awaitable[T]],
) -> T:
    attempt = 1
    while True:
        try:
            return await action()
        except (OSError, TimeoutError, OperationalError) as exc:
            if not _is_connect_failure(exc):
                raise
            if attempt == _TEST_CONNECT_ATTEMPTS:
                raise RuntimeError(
                    f"PostgreSQL test helper failed to {operation} for schema "
                    f"{schema} after {_TEST_CONNECT_ATTEMPTS} attempts."
                ) from exc
            await asyncio.sleep(0.1 * attempt)
            attempt += 1


def _is_connect_failure(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError | OSError):
        return True
    original = getattr(exc, "orig", None)
    return isinstance(original, TimeoutError | OSError)


async def _create_schema(engine: AsyncEngine, quoted_schema: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA {quoted_schema}"))


async def _create_metadata(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _drop_schema(engine: AsyncEngine, schema: str) -> None:
    quoted_schema = _quote_identifier(schema)
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
    admin_engine = _make_test_engine(database_url)
    engine = _make_test_engine(_schema_url(database_url, schema, null_pool=True))
    try:
        with _postgres_ddl_lock():
            await _with_connect_retry(
                "create schema",
                schema,
                lambda: _create_schema(admin_engine, quoted_schema),
            )
            await _with_connect_retry("create metadata", schema, lambda: _create_metadata(engine))
    finally:
        await admin_engine.dispose()
    return engine


@asynccontextmanager
async def postgres_test_url() -> AsyncIterator[str]:
    """Yield a PostgreSQL URL bound to an isolated schema with metadata."""

    database_url = postgres_test_database_url()
    schema = f"awf_test_{uuid.uuid4().hex}"
    quoted_schema = _quote_identifier(schema)
    admin_engine = _make_test_engine(database_url)
    schema_database_url = _schema_url(database_url, schema, null_pool=True)
    engine = _make_test_engine(schema_database_url)
    try:
        with _postgres_ddl_lock():
            await _with_connect_retry(
                "create schema",
                schema,
                lambda: _create_schema(admin_engine, quoted_schema),
            )
            await _with_connect_retry("create metadata", schema, lambda: _create_metadata(engine))
        yield schema_database_url
    finally:
        await engine.dispose()
        try:
            with _postgres_ddl_lock():
                await _with_connect_retry(
                    "drop schema", schema, lambda: _drop_schema(admin_engine, schema)
                )
        finally:
            await admin_engine.dispose()


@contextmanager
def postgres_test_url_sync() -> Iterator[str]:
    """Synchronous wrapper for tests that exercise sync CLIs."""

    database_url = postgres_test_database_url()
    schema = f"awf_test_{uuid.uuid4().hex}"
    quoted_schema = _quote_identifier(schema)

    async def _setup() -> str:
        admin_engine = _make_test_engine(database_url)
        schema_database_url = _schema_url(database_url, schema, null_pool=True)
        engine = _make_test_engine(schema_database_url)
        try:
            with _postgres_ddl_lock():
                await _with_connect_retry(
                    "create schema",
                    schema,
                    lambda: _create_schema(admin_engine, quoted_schema),
                )
                await _with_connect_retry(
                    "create metadata", schema, lambda: _create_metadata(engine)
                )
        finally:
            await admin_engine.dispose()
            await engine.dispose()
        return schema_database_url

    async def _cleanup() -> None:
        admin_engine = _make_test_engine(database_url)
        try:
            with _postgres_ddl_lock():
                await _with_connect_retry(
                    "drop schema", schema, lambda: _drop_schema(admin_engine, schema)
                )
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
    admin_engine = _make_test_engine(database_url)
    try:
        with _postgres_ddl_lock():
            await _with_connect_retry(
                "create schema",
                schema,
                lambda: _create_schema(admin_engine, quoted_schema),
            )
        yield _schema_url(database_url, schema)
    finally:
        try:
            with _postgres_ddl_lock():
                await _with_connect_retry(
                    "drop schema", schema, lambda: _drop_schema(admin_engine, schema)
                )
        finally:
            await admin_engine.dispose()


@asynccontextmanager
async def postgres_test_session() -> AsyncIterator[AsyncSession]:
    """Yield a session bound to an isolated PostgreSQL schema."""

    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        async with factory() as session:
            yield session
