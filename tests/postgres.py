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
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from awf.db.base import Base
from awf.db.session import make_engine, make_session_factory
from tests.util import _positive_int_env

DEFAULT_TEST_DATABASE_URL = "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"


POSTGRES_TEST_CONNECT_TIMEOUT_SECONDS = _positive_int_env(
    "AWF_POSTGRES_TEST_CONNECT_TIMEOUT_SECONDS",
    10,
)
POSTGRES_TEST_CONNECT_ATTEMPTS = _positive_int_env(
    "AWF_POSTGRES_TEST_CONNECT_ATTEMPTS",
    3,
)
RETRYABLE_POSTGRES_ERROR_NAMES = {
    "ConnectionDoesNotExistError",
    "InternalClientError",
}
_SCHEMA_DDL_LOCK_NAMESPACE = 0x415746
_SCHEMA_DDL_LOCK_KEY = 0x54455354
_TEST_CONNECT_TIMEOUT_SECONDS = POSTGRES_TEST_CONNECT_TIMEOUT_SECONDS
_SCHEMA_ENGINE_CONNECT_ATTEMPTS = POSTGRES_TEST_CONNECT_ATTEMPTS
_POSTGRES_TEST_LOCAL_RUN_UID = uuid.uuid4().hex
_POSTGRES_TEST_SCHEMA_RE = re.compile(r"^awf_test_(?P<namespace>[0-9a-f]{16})_[0-9a-f]{32}$")
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


def _postgres_server_database_key(database_url: str) -> str:
    parsed_url = make_url(database_url)
    server_url = parsed_url.set(query={})
    return _postgres_database_key(server_url.render_as_string(hide_password=False))


def _postgres_test_run_uid() -> str:
    return (
        os.environ.get("PYTEST_XDIST_TESTRUNUID")
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
    schema: str,
    *,
    null_pool: bool = False,
    connect_retries: bool = False,
) -> str:
    return _schema_url(
        database_url,
        schema,
        null_pool=null_pool,
        connect_retries=connect_retries,
    )


def _make_test_engine(url: str) -> AsyncEngine:
    return make_engine(url, connect_args={"timeout": _TEST_CONNECT_TIMEOUT_SECONDS})


def _query_value(value: object | None) -> str | None:
    if isinstance(value, tuple):
        value = value[0] if value else None
    return None if value is None else str(value)


def _schema_identifier_from_search_path(value: object | None) -> str | None:
    search_path = _query_value(value)
    if search_path is None:
        return None
    if search_path.startswith('"') and search_path.endswith('"'):
        schema = search_path[1:-1].replace('""', '"')
    else:
        schema = search_path
    if schema != "public" and _POSTGRES_TEST_SCHEMA_RE.fullmatch(schema) is None:
        raise RuntimeError("PostgreSQL test search path must target an AWF test schema.")
    return _quote_identifier(schema)


async def _with_postgres_connection_retry[T](operation: Callable[[], Awaitable[T]]) -> T:
    for attempt in range(_SCHEMA_ENGINE_CONNECT_ATTEMPTS):
        try:
            return await operation()
        except Exception as exc:
            if attempt == _SCHEMA_ENGINE_CONNECT_ATTEMPTS - 1 or not _is_retryable_connect_error(
                exc
            ):
                raise
            await asyncio.sleep(0.05 * (attempt + 1))
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


async def _dispose_engine(engine: AsyncEngine | None) -> None:
    if engine is not None:
        await engine.dispose()


def _postgres_test_run_lock_path(database_url: str, namespace: str) -> Path:
    database_key = _postgres_database_key(database_url)
    return Path(tempfile.gettempdir()) / (
        f"awf-pytest-postgres-active-{database_key}-{namespace}.lock"
    )


@contextmanager
def postgres_alembic_subprocess_lock(database_url: str | None = None) -> Iterator[None]:
    """Serialize live Alembic subprocess tests sharing one Postgres server.

    Alembic migrations are isolated by schema in tests, but Postgres concurrent
    index DDL still waits on transactions in the shared database. Running several
    full migration chains at once under xdist can deadlock or trip lock timeouts,
    so tests that shell out to ``alembic`` take this file lock around the
    subprocess. Production migrations are still exercised exactly as issued.
    """

    if fcntl is None:
        yield
        return

    database_url = database_url or postgres_test_database_url()
    lock_path = Path(tempfile.gettempdir()) / (
        f"awf-pytest-alembic-{_postgres_server_database_key(database_url)}.lock"
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


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
    done_path = lock_path.with_suffix(".done")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Serialize concurrent workers while cleanup scans before this process marks
    # its reused namespace active; otherwise leftovers from a crashed run using
    # the same fixed UID would be skipped as live schemas.
    with lock_path.open("w", encoding="utf-8") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            if done_path.exists() and _is_postgres_test_schema_namespace_active(
                database_url,
                namespace,
            ):
                _ensure_postgres_test_run_active(database_url)
                _STALE_SCHEMA_CLEANUP_DONE_KEYS.add(cleanup_key)
                return
            try:
                asyncio.run(_drop_stale_postgres_test_schemas(database_url))
            except Exception as exc:
                if not _is_retryable_connect_error(exc):
                    raise
                _ensure_postgres_test_run_active(database_url)
                done_path.touch()
                _STALE_SCHEMA_CLEANUP_DONE_KEYS.add(cleanup_key)
                return
            _ensure_postgres_test_run_active(database_url)
            done_path.touch()
            _STALE_SCHEMA_CLEANUP_DONE_KEYS.add(cleanup_key)
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


async def _drop_stale_postgres_test_schemas(database_url: str | None = None) -> None:
    database_url = database_url or postgres_test_database_url()
    engine = _make_test_engine(_admin_url(database_url))
    try:
        schemas = await _with_postgres_connection_retry(
            lambda: _list_stale_postgres_test_schemas(engine, database_url)
        )
        await _with_postgres_connection_retry(lambda: _drop_postgres_test_schemas(engine, schemas))
    finally:
        await engine.dispose()


async def _drop_postgres_test_schemas(engine: AsyncEngine, schemas: list[str]) -> None:
    if not schemas:
        return

    async with _postgres_schema_ddl_lock(engine) as conn:
        for schema in schemas:
            await _drop_schema(conn, schema, _quote_identifier(schema))


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
            {"pattern": "awf\\_test\\_%"},
        )
        schemas = sorted(str(row[0]) for row in result)

    stale_schemas: list[str] = []
    active_namespaces: dict[str, bool] = {}
    for schema in schemas:
        namespace = _postgres_test_schema_namespace_from_schema(schema)
        if namespace is None:
            continue
        if namespace not in active_namespaces:
            active_namespaces[namespace] = _is_postgres_test_schema_namespace_active(
                database_url,
                namespace,
            )
        if active_namespaces[namespace]:
            continue
        stale_schemas.append(schema)
    return stale_schemas


async def _create_metadata_engine(schema_database_url: str) -> AsyncEngine:
    for attempt in range(_SCHEMA_ENGINE_CONNECT_ATTEMPTS):
        engine = _make_test_engine(schema_database_url)
        try:
            async with engine.begin() as conn:
                schema_identifier = _schema_identifier_from_search_path(
                    make_url(schema_database_url).query.get("awf_search_path")
                )
                if schema_identifier is not None:
                    await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema_identifier}"))
                    await conn.execute(text(f"SET search_path TO {schema_identifier}"))
                    selected_schema = await conn.scalar(text("SELECT current_schema()"))
                    if selected_schema is None:
                        raise RuntimeError(
                            f"PostgreSQL test metadata search_path did not select "
                            f"{schema_identifier}."
                        )
                await conn.run_sync(Base.metadata.create_all)
        except Exception as exc:
            await _dispose_engine(engine)
            if attempt == _SCHEMA_ENGINE_CONNECT_ATTEMPTS - 1 or not _is_retryable_connect_error(
                exc
            ):
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
        except Exception as exc:
            if attempt == _SCHEMA_ENGINE_CONNECT_ATTEMPTS - 1 or not _is_retryable_connect_error(
                exc
            ):
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
    _ensure_postgres_test_run_active(database_url)
    schema = _new_postgres_test_schema()
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
            schema,
            null_pool=True,
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
    _ensure_postgres_test_run_active(database_url)
    schema = _new_postgres_test_schema()
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
                    schema,
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

    if engine is None:
        raise RuntimeError("PostgreSQL test engine was not initialized.")
    return engine


@asynccontextmanager
async def postgres_test_url() -> AsyncIterator[str]:
    """Yield a PostgreSQL URL bound to an isolated schema with metadata."""

    database_url = postgres_test_database_url()
    _ensure_postgres_test_run_active(database_url)
    schema = _new_postgres_test_schema()
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
                schema,
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
    _ensure_postgres_test_run_active(database_url)
    schema = _new_postgres_test_schema()
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
                    schema,
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
    _ensure_postgres_test_run_active(database_url)
    schema = _new_postgres_test_schema()
    quoted_schema = _quote_identifier(schema)
    admin_engine = _make_test_engine(_admin_url(database_url))
    schema_created = False
    try:
        async with _postgres_schema_ddl_lock(admin_engine) as admin_conn:
            await _create_schema(admin_conn, quoted_schema)
            schema_created = True
        yield _test_schema_url(database_url, schema, connect_retries=True)
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
