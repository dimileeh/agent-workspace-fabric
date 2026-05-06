"""PostgreSQL-backed test database helpers."""

from __future__ import annotations

import asyncio
import atexit
import hashlib
import os
import re
import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import TextIO

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from awf.db.base import Base
from awf.db.session import make_engine, make_session_factory

DEFAULT_TEST_DATABASE_URL = "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
POSTGRES_TEST_CONNECT_TIMEOUT_SECONDS = 10
POSTGRES_TEST_CONNECT_ATTEMPTS = 3
RETRYABLE_POSTGRES_ERROR_NAMES = {
    "ConnectionDoesNotExistError",
    "InternalClientError",
}
_POSTGRES_TEST_LOCAL_RUN_UID = uuid.uuid4().hex
_POSTGRES_TEST_SCHEMA_RE = re.compile(
    r"^awf_test_(?P<namespace>[0-9a-f]{16})_[0-9a-f]{32}$"
)
_POSTGRES_TEST_ACTIVE_RUN_LOCKS: dict[tuple[str, str], TextIO] = {}
_STALE_SCHEMA_CLEANUP_DONE_KEYS: set[tuple[str, str]] = set()

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows does not run AWF Docker CI.
    fcntl = None  # type: ignore[assignment]


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


def _postgres_database_key(database_url: str) -> str:
    return hashlib.sha256(database_url.encode("utf-8")).hexdigest()[:16]


def _postgres_lock_owner_key() -> str:
    getuid = getattr(os, "getuid", None)
    if callable(getuid):
        return f"uid-{getuid()}"

    username = os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"
    safe_username = re.sub(r"[^0-9A-Za-z_.-]", "_", username).strip("._-")
    return f"user-{safe_username or 'unknown'}"


def _postgres_test_run_uid() -> str:
    return (
        os.environ.get("PYTEST_XDIST_TESTRUNUID")
        or os.environ.get("AWF_EXEC_INVOCATION_ID")
        or os.environ.get("AWF_POSTGRES_TEST_RUN_UID")
        or _POSTGRES_TEST_LOCAL_RUN_UID
    )


def _postgres_test_schema_namespace() -> str:
    return hashlib.sha256(_postgres_test_run_uid().encode("utf-8")).hexdigest()[:16]


def _new_postgres_test_schema() -> str:
    return f"awf_test_{_postgres_test_schema_namespace()}_{uuid.uuid4().hex}"


def _postgres_test_schema_namespace_from_schema(schema: str) -> str | None:
    match = _POSTGRES_TEST_SCHEMA_RE.fullmatch(schema)
    if match is None:
        return None
    return match.group("namespace")


def _schema_url(database_url: str, schema: str, *, null_pool: bool = False) -> str:
    parsed_url = make_url(database_url)
    query = dict(parsed_url.query)
    query["awf_search_path"] = schema
    if null_pool:
        query["awf_null_pool"] = "1"
    return parsed_url.set(query=query).render_as_string(hide_password=False)


def _make_test_engine(url: str) -> AsyncEngine:
    return make_engine(
        url,
        connect_args={"timeout": POSTGRES_TEST_CONNECT_TIMEOUT_SECONDS},
    )


async def _with_postgres_connection_retry[T](operation: Callable[[], Awaitable[T]]) -> T:
    for attempt in range(POSTGRES_TEST_CONNECT_ATTEMPTS):
        try:
            return await operation()
        except Exception as exc:
            if attempt == POSTGRES_TEST_CONNECT_ATTEMPTS - 1 or not _is_retryable_connect_error(
                exc
            ):
                raise
            await asyncio.sleep(0.2 * (attempt + 1))
    raise AssertionError("unreachable postgres connection retry state")


def _is_retryable_connect_error(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError | OSError | ConnectionError):
        return True
    if exc.__class__.__name__ in RETRYABLE_POSTGRES_ERROR_NAMES:
        return True
    if not isinstance(exc, DBAPIError):
        return False
    if isinstance(exc.orig, TimeoutError | OSError | ConnectionError):
        return True
    return exc.orig.__class__.__name__ in RETRYABLE_POSTGRES_ERROR_NAMES


@contextmanager
def _postgres_ddl_lock(database_url: str) -> Iterator[None]:
    if fcntl is None:
        yield
        return

    lock_key = _postgres_database_key(database_url)
    owner_key = _postgres_lock_owner_key()
    lock_path = Path(tempfile.gettempdir()) / (
        f"awf-pytest-postgres-ddl-{owner_key}-{lock_key}.lock"
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _postgres_test_run_lock_path(database_url: str, namespace: str) -> Path:
    database_key = _postgres_database_key(database_url)
    return Path(tempfile.gettempdir()) / (
        f"awf-pytest-postgres-active-{database_key}-{namespace}.lock"
    )


def _ensure_postgres_test_run_active(database_url: str) -> None:
    if fcntl is None:
        return

    namespace = _postgres_test_schema_namespace()
    key = (_postgres_database_key(database_url), namespace)
    if key in _POSTGRES_TEST_ACTIVE_RUN_LOCKS:
        return

    lock_path = _postgres_test_run_lock_path(database_url, namespace)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
    except BaseException:
        lock_file.close()
        raise
    _POSTGRES_TEST_ACTIVE_RUN_LOCKS[key] = lock_file


def _release_postgres_test_run_locks() -> None:
    if fcntl is None:
        return

    for key, lock_file in list(_POSTGRES_TEST_ACTIVE_RUN_LOCKS.items()):
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()
        del _POSTGRES_TEST_ACTIVE_RUN_LOCKS[key]


atexit.register(_release_postgres_test_run_locks)


def _is_postgres_test_schema_namespace_active(database_url: str, namespace: str) -> bool:
    if fcntl is None:
        return True

    lock_path = _postgres_test_run_lock_path(database_url, namespace)
    if not lock_path.exists():
        return False

    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    return False


def cleanup_stale_postgres_test_schemas() -> None:
    """Drop leftover per-test schemas once before DB-backed pytest selections run."""

    database_url = postgres_test_database_url()
    namespace = _postgres_test_schema_namespace()
    cleanup_key = (_postgres_database_key(database_url), namespace)
    if cleanup_key in _STALE_SCHEMA_CLEANUP_DONE_KEYS:
        _ensure_postgres_test_run_active(database_url)
        return

    database_key, namespace = cleanup_key
    lock_path = Path(tempfile.gettempdir()) / (
        f"awf-pytest-postgres-cleanup-{database_key}-{namespace}.lock"
    )
    marker_path = Path(tempfile.gettempdir()) / (
        f"awf-pytest-postgres-cleanup-{database_key}-{namespace}.done"
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("w", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            if marker_path.exists():
                _ensure_postgres_test_run_active(database_url)
                _STALE_SCHEMA_CLEANUP_DONE_KEYS.add(cleanup_key)
                return
            asyncio.run(_drop_stale_postgres_test_schemas(database_url))
            marker_path.touch()
            _ensure_postgres_test_run_active(database_url)
            _STALE_SCHEMA_CLEANUP_DONE_KEYS.add(cleanup_key)
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


async def _drop_stale_postgres_test_schemas(database_url: str | None = None) -> None:
    database_url = database_url or postgres_test_database_url()
    engine = _make_test_engine(database_url)
    try:
        schemas = await _with_postgres_connection_retry(
            lambda: _list_stale_postgres_test_schemas(engine, database_url)
        )
        await _with_postgres_connection_retry(
            lambda: _drop_postgres_test_schemas(engine, schemas)
        )
    finally:
        await engine.dispose()


async def _drop_postgres_test_schemas(engine: AsyncEngine, schemas: list[str]) -> None:
    if not schemas:
        return

    async with engine.begin() as conn:
        for schema in schemas:
            await conn.execute(
                text(f"DROP SCHEMA IF EXISTS {_quote_identifier(schema)} CASCADE")
            )


async def _list_stale_postgres_test_schemas(
    engine: AsyncEngine,
    database_url: str,
) -> list[str]:
    async with engine.begin() as conn:
        result = await conn.execute(
            text(
                """
                SELECT schema_name
                FROM information_schema.schemata
                WHERE schema_name LIKE :pattern ESCAPE '\\'
                """
            ),
            {"pattern": r"awf\_test\_%"},
        )
        schemas = sorted(str(row[0]) for row in result)

    stale_schemas: list[str] = []
    for schema in schemas:
        namespace = _postgres_test_schema_namespace_from_schema(schema)
        if namespace is None:
            continue
        if _is_postgres_test_schema_namespace_active(database_url, namespace):
            continue
        stale_schemas.append(schema)
    return stale_schemas


@asynccontextmanager
async def postgres_test_engine() -> AsyncIterator[AsyncEngine]:
    """Yield an isolated PostgreSQL schema with ORM metadata created."""

    database_url = postgres_test_database_url()
    _ensure_postgres_test_run_active(database_url)
    schema = _new_postgres_test_schema()
    quoted_schema = _quote_identifier(schema)
    schema_database_url = _schema_url(database_url, quoted_schema)
    engine = _make_test_engine(schema_database_url)
    try:
        with _postgres_ddl_lock(database_url):
            await _with_postgres_connection_retry(
                lambda: _create_schema_and_metadata(engine, quoted_schema)
            )
        yield engine
    finally:
        try:
            with _postgres_ddl_lock(database_url):
                await _with_postgres_connection_retry(lambda: _drop_schema(engine, quoted_schema))
        finally:
            await engine.dispose()


async def _create_schema(engine: AsyncEngine, quoted_schema: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA {quoted_schema}"))


async def _create_schema_and_metadata(engine: AsyncEngine, quoted_schema: str) -> None:
    await _create_schema(engine, quoted_schema)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


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
    _ensure_postgres_test_run_active(database_url)
    schema = _new_postgres_test_schema()
    quoted_schema = _quote_identifier(schema)
    engine = _make_test_engine(_schema_url(database_url, quoted_schema, null_pool=True))
    with _postgres_ddl_lock(database_url):
        await _with_postgres_connection_retry(
            lambda: _create_schema_and_metadata(engine, quoted_schema)
        )
    return engine


@asynccontextmanager
async def postgres_test_url() -> AsyncIterator[str]:
    """Yield a PostgreSQL URL bound to an isolated schema with metadata."""

    database_url = postgres_test_database_url()
    _ensure_postgres_test_run_active(database_url)
    schema = _new_postgres_test_schema()
    quoted_schema = _quote_identifier(schema)
    schema_database_url = _schema_url(database_url, quoted_schema)
    engine = _make_test_engine(schema_database_url)
    try:
        with _postgres_ddl_lock(database_url):
            await _with_postgres_connection_retry(
                lambda: _create_schema_and_metadata(engine, quoted_schema)
            )
        yield schema_database_url
    finally:
        try:
            with _postgres_ddl_lock(database_url):
                await _with_postgres_connection_retry(lambda: _drop_schema(engine, quoted_schema))
        finally:
            await engine.dispose()


@contextmanager
def postgres_test_url_sync() -> Iterator[str]:
    """Synchronous wrapper for tests that exercise sync CLIs."""

    database_url = postgres_test_database_url()
    _ensure_postgres_test_run_active(database_url)
    schema = _new_postgres_test_schema()
    quoted_schema = _quote_identifier(schema)

    async def _setup() -> str:
        schema_database_url = _schema_url(database_url, quoted_schema, null_pool=True)
        engine = _make_test_engine(schema_database_url)
        try:
            with _postgres_ddl_lock(database_url):
                await _with_postgres_connection_retry(
                    lambda: _create_schema_and_metadata(engine, quoted_schema)
                )
        finally:
            await engine.dispose()
        return schema_database_url

    async def _cleanup() -> None:
        engine = _make_test_engine(_schema_url(database_url, quoted_schema, null_pool=True))
        try:
            with _postgres_ddl_lock(database_url):
                await _with_postgres_connection_retry(lambda: _drop_schema(engine, quoted_schema))
        finally:
            await engine.dispose()

    url = asyncio.run(_setup())
    try:
        yield url
    finally:
        asyncio.run(_cleanup())


@asynccontextmanager
async def postgres_empty_test_url() -> AsyncIterator[str]:
    """Yield a PostgreSQL URL bound to an empty isolated schema."""

    database_url = postgres_test_database_url()
    _ensure_postgres_test_run_active(database_url)
    schema = _new_postgres_test_schema()
    quoted_schema = _quote_identifier(schema)
    engine = _make_test_engine(_schema_url(database_url, quoted_schema, null_pool=True))
    try:
        with _postgres_ddl_lock(database_url):
            await _with_postgres_connection_retry(lambda: _create_schema(engine, quoted_schema))
        try:
            yield _schema_url(database_url, quoted_schema)
        finally:
            with _postgres_ddl_lock(database_url):
                await _with_postgres_connection_retry(lambda: _drop_schema(engine, quoted_schema))
    finally:
        await engine.dispose()


@asynccontextmanager
async def postgres_test_session() -> AsyncIterator[AsyncSession]:
    """Yield a session bound to an isolated PostgreSQL schema."""

    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        async with factory() as session:
            yield session
