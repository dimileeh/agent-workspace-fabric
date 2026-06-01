"""PostgreSQL-only edge behavior that used to be hidden by loose test DBs."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from awf.api.schemas import PullRequestMonitorAdoptionRequest
from awf.common.github_client import (
    PullRequestMetadataError,
    RepoRef,
)
from awf.common.github_client_adoption import _head_repo_slug_from_adoption_payload
from awf.db import session as session_mod
from awf.db.session import make_engine
from awf.runtime.merge_coordinator import InProcessMergeCoordinator
from awf.service.doctor import _database_endpoint
from awf.service.pr_monitor_adoption import (
    PRMonitorAdoptionError,
    _inline_profile_name,
    _raise_if_repo_identity_conflicts,
)
from awf.service.secret_leases import _ensure_utc
from awf.service.status import _utc_datetime
from awf.service.worker import _merge_coordinator_for_database_url
from tests import postgres as postgres_mod


class _FakeSchemaConnection:
    def __init__(self, engine: _FakeSchemaEngine) -> None:
        self._engine = engine

    async def execute(self, _statement: object, _parameters: object = None) -> list[tuple[str]]:
        self._engine.parameters.append(_parameters)
        return [(schema,) for schema in self._engine.schemas]


class _FakeSchemaBegin:
    def __init__(self, engine: _FakeSchemaEngine) -> None:
        self._engine = engine

    async def __aenter__(self) -> _FakeSchemaConnection:
        return _FakeSchemaConnection(self._engine)

    async def __aexit__(self, *_exc_info: object) -> None:
        return None


class _FakeSchemaEngine:
    def __init__(self, schemas: list[str]) -> None:
        self.schemas = schemas
        self.parameters: list[object] = []

    def begin(self) -> _FakeSchemaBegin:
        return _FakeSchemaBegin(self)


class _FakeDisposableEngine:
    def __init__(self, name: str = "engine") -> None:
        self.name = name
        self.disposed = False
        self.dispose_count = 0

    async def dispose(self) -> None:
        self.dispose_count += 1
        self.disposed = True


class _FakeStaleCleanupConnection:
    def __init__(self, engine: _FakeStaleCleanupEngine) -> None:
        self._engine = engine

    async def execute(
        self,
        statement: object,
        _parameters: object = None,
    ) -> list[tuple[str]]:
        statement_text = str(statement)
        self._engine.statements.append(statement_text)
        if "information_schema.schemata" in statement_text:
            return [(schema,) for schema in self._engine.schemas]
        return []

    async def commit(self) -> None:
        self._engine.commit_count += 1


class _FakeStaleCleanupBegin:
    def __init__(self, engine: _FakeStaleCleanupEngine) -> None:
        self._engine = engine

    async def __aenter__(self) -> _FakeStaleCleanupConnection:
        self._engine.begin_count += 1
        return _FakeStaleCleanupConnection(self._engine)

    async def __aexit__(self, *_exc_info: object) -> None:
        return None


class _FakeStaleCleanupEngine:
    def __init__(self, schemas: list[str]) -> None:
        self.schemas = schemas
        self.begin_count = 0
        self.commit_count = 0
        self.dispose_count = 0
        self.lock_count = 0
        self.statements: list[str] = []

    def begin(self) -> _FakeStaleCleanupBegin:
        return _FakeStaleCleanupBegin(self)

    async def dispose(self) -> None:
        self.dispose_count += 1


class _AsyncConnectionContext:
    def __init__(self, connection: _RecordingConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _RecordingConnection:
        return self._connection

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


class _RecordingConnection:
    def __init__(self, statements: list[tuple[str, object | None]]) -> None:
        self._statements = statements

    async def execute(self, statement: object, params: object | None = None) -> None:
        self._statements.append((str(statement), params))

    async def commit(self) -> None:
        return None


@pytest.mark.unit
def test_make_engine_strips_test_url_options_and_enables_null_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _create_async_engine(url: str, **kwargs: Any) -> object:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return SimpleNamespace(url=url)

    monkeypatch.setattr(session_mod, "create_async_engine", _create_async_engine)

    engine = make_engine(
        "postgresql+asyncpg://awf:pw@localhost:5433/awf"
        "?awf_search_path=first&awf_search_path=second"
        "&awf_null_pool=true&awf_null_pool=false"
    )

    assert engine.url == "postgresql+asyncpg://awf:pw@localhost:5433/awf"
    assert captured["url"] == "postgresql+asyncpg://awf:pw@localhost:5433/awf"
    assert captured["kwargs"]["connect_args"]["server_settings"]["search_path"] == "first"
    assert captured["kwargs"]["poolclass"] is NullPool


@pytest.mark.unit
def test_postgres_test_schema_name_is_scoped_to_current_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTEST_XDIST_TESTRUNUID", "ci-shard-a")

    namespace = postgres_mod._postgres_test_schema_namespace()
    schema = postgres_mod._new_postgres_test_schema()

    assert len(namespace) == 16
    assert schema.startswith(f"awf_test_{namespace}_")
    assert len(schema) == len("awf_test_") + 16 + 1 + 32


@pytest.mark.unit
def test_postgres_test_run_uid_ignores_awf_exec_invocation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PYTEST_XDIST_TESTRUNUID", raising=False)
    monkeypatch.delenv("AWF_POSTGRES_TEST_RUN_UID", raising=False)
    monkeypatch.setenv("AWF_EXEC_INVOCATION_ID", "long-lived-agent-invocation")
    monkeypatch.setattr(postgres_mod, "_POSTGRES_TEST_LOCAL_RUN_UID", "local-pytest-run")

    assert postgres_mod._postgres_test_run_uid() == "local-pytest-run"


@pytest.mark.unit
def test_positive_int_env_uses_default_and_rejects_invalid_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_name = "AWF_POSTGRES_TEST_CONNECT_TIMEOUT_SECONDS"
    monkeypatch.delenv(env_name, raising=False)
    assert postgres_mod._positive_int_env(env_name, 10) == 10

    monkeypatch.setenv(env_name, "45")
    assert postgres_mod._positive_int_env(env_name, 10) == 45

    monkeypatch.setenv(env_name, "0")
    with pytest.raises(RuntimeError, match="must be a positive integer"):
        postgres_mod._positive_int_env(env_name, 10)

    monkeypatch.setenv(env_name, "nope")
    with pytest.raises(RuntimeError, match="must be a positive integer"):
        postgres_mod._positive_int_env(env_name, 10)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_postgres_schema_listing_scans_all_inactive_test_namespaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
    monkeypatch.setenv("PYTEST_XDIST_TESTRUNUID", "current-run")
    current_namespace = postgres_mod._postgres_test_schema_namespace()
    inactive_namespace = "1" * 16
    active_namespace = "2" * 16
    current_schema = f"awf_test_{current_namespace}_{'a' * 32}"
    inactive_schema = f"awf_test_{inactive_namespace}_{'b' * 32}"
    active_schema = f"awf_test_{active_namespace}_{'c' * 32}"
    legacy_unowned_schema = f"awf_test_{'c' * 32}"
    seen_namespaces: list[str] = []

    def _is_active(url: str, namespace: str) -> bool:
        assert url == database_url
        seen_namespaces.append(namespace)
        return namespace == active_namespace

    monkeypatch.setattr(postgres_mod, "_is_postgres_test_schema_namespace_active", _is_active)
    engine = _FakeSchemaEngine(
        [inactive_schema, legacy_unowned_schema, active_schema, current_schema]
    )

    schemas = await postgres_mod._list_stale_postgres_test_schemas(
        engine,  # type: ignore[arg-type]
        database_url,
    )

    assert schemas == sorted([current_schema, inactive_schema])
    assert set(seen_namespaces) == {active_namespace, current_namespace, inactive_namespace}
    assert engine.parameters == [
        {"pattern": "awf\\_test\\_%"},
    ]


@pytest.mark.unit
def test_stale_postgres_cleanup_ignores_persistent_done_marker_for_reused_namespace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_url = "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
    monkeypatch.setenv("PYTEST_XDIST_TESTRUNUID", "cleanup-reused-run")
    namespace = postgres_mod._postgres_test_schema_namespace()
    database_key = postgres_mod._postgres_database_key(database_url)
    marker_path = tmp_path / (f"awf-pytest-postgres-cleanup-{database_key}-{namespace}.done")
    marker_path.touch()
    dropped_urls: list[str] = []
    active_urls: list[str] = []

    async def _drop_stale(url: str) -> None:
        dropped_urls.append(url)

    monkeypatch.setattr(postgres_mod, "postgres_test_database_url", lambda: database_url)
    monkeypatch.setattr(postgres_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(postgres_mod, "_drop_stale_postgres_test_schemas", _drop_stale)
    monkeypatch.setattr(
        postgres_mod,
        "_ensure_postgres_test_run_active",
        lambda url: active_urls.append(url),
    )
    postgres_mod._STALE_SCHEMA_CLEANUP_DONE_KEYS.clear()
    try:
        postgres_mod.cleanup_stale_postgres_test_schemas()
        postgres_mod.cleanup_stale_postgres_test_schemas()
    finally:
        postgres_mod._STALE_SCHEMA_CLEANUP_DONE_KEYS.clear()

    assert dropped_urls == [database_url]
    assert active_urls == [database_url, database_url]


@pytest.mark.unit
def test_stale_postgres_cleanup_reuses_done_marker_for_active_namespace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_url = "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
    monkeypatch.setenv("PYTEST_XDIST_TESTRUNUID", "cleanup-active-run")
    namespace = postgres_mod._postgres_test_schema_namespace()
    database_key = postgres_mod._postgres_database_key(database_url)
    marker_path = tmp_path / (f"awf-pytest-postgres-cleanup-{database_key}-{namespace}.done")
    marker_path.touch()
    dropped_urls: list[str] = []
    active_urls: list[str] = []

    async def _drop_stale(url: str) -> None:
        dropped_urls.append(url)

    monkeypatch.setattr(postgres_mod, "postgres_test_database_url", lambda: database_url)
    monkeypatch.setattr(postgres_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(postgres_mod, "_drop_stale_postgres_test_schemas", _drop_stale)
    monkeypatch.setattr(
        postgres_mod,
        "_is_postgres_test_schema_namespace_active",
        lambda url, active_namespace: url == database_url and active_namespace == namespace,
    )
    monkeypatch.setattr(
        postgres_mod,
        "_ensure_postgres_test_run_active",
        lambda url: active_urls.append(url),
    )
    postgres_mod._STALE_SCHEMA_CLEANUP_DONE_KEYS.clear()
    try:
        postgres_mod.cleanup_stale_postgres_test_schemas()
        postgres_mod._STALE_SCHEMA_CLEANUP_DONE_KEYS.clear()
        postgres_mod.cleanup_stale_postgres_test_schemas()
    finally:
        postgres_mod._STALE_SCHEMA_CLEANUP_DONE_KEYS.clear()

    assert dropped_urls == []
    assert active_urls == [database_url, database_url]


@pytest.mark.unit
def test_stale_postgres_cleanup_marks_run_active_after_drop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_url = "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
    monkeypatch.setenv("PYTEST_XDIST_TESTRUNUID", "cleanup-order-run")
    events: list[tuple[str, str]] = []

    async def _drop_stale(url: str) -> None:
        events.append(("drop", url))

    monkeypatch.setattr(postgres_mod, "postgres_test_database_url", lambda: database_url)
    monkeypatch.setattr(postgres_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(postgres_mod, "_drop_stale_postgres_test_schemas", _drop_stale)
    monkeypatch.setattr(
        postgres_mod,
        "_ensure_postgres_test_run_active",
        lambda url: events.append(("active", url)),
    )
    postgres_mod._STALE_SCHEMA_CLEANUP_DONE_KEYS.clear()
    try:
        postgres_mod.cleanup_stale_postgres_test_schemas()
    finally:
        postgres_mod._STALE_SCHEMA_CLEANUP_DONE_KEYS.clear()

    assert events == [("drop", database_url), ("active", database_url)]


@pytest.mark.unit
def test_stale_postgres_cleanup_retryable_connect_failure_is_best_effort(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_url = "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
    monkeypatch.setenv("PYTEST_XDIST_TESTRUNUID", "cleanup-retryable-timeout")
    namespace = postgres_mod._postgres_test_schema_namespace()
    database_key = postgres_mod._postgres_database_key(database_url)
    marker_path = tmp_path / (f"awf-pytest-postgres-cleanup-{database_key}-{namespace}.done")
    events: list[tuple[str, str]] = []

    async def _drop_stale(url: str) -> None:
        events.append(("drop", url))
        raise TimeoutError("transient connect timeout")

    monkeypatch.setattr(postgres_mod, "postgres_test_database_url", lambda: database_url)
    monkeypatch.setattr(postgres_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(postgres_mod, "_drop_stale_postgres_test_schemas", _drop_stale)
    monkeypatch.setattr(
        postgres_mod,
        "_ensure_postgres_test_run_active",
        lambda url: events.append(("active", url)),
    )
    postgres_mod._STALE_SCHEMA_CLEANUP_DONE_KEYS.clear()
    try:
        postgres_mod.cleanup_stale_postgres_test_schemas()
    finally:
        postgres_mod._STALE_SCHEMA_CLEANUP_DONE_KEYS.clear()

    assert events == [("drop", database_url), ("active", database_url)]
    assert marker_path.exists()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_stale_postgres_cleanup_reuses_one_engine_for_all_drops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
    monkeypatch.setenv("PYTEST_XDIST_TESTRUNUID", "cleanup-run")
    current_namespace = postgres_mod._postgres_test_schema_namespace()
    other_namespace = "1" * 16
    first_schema = f"awf_test_{current_namespace}_{'a' * 32}"
    second_schema = f"awf_test_{current_namespace}_{'b' * 32}"
    other_schema = f"awf_test_{other_namespace}_{'c' * 32}"
    engine = _FakeStaleCleanupEngine([second_schema, other_schema, first_schema])
    made_engines: list[_FakeStaleCleanupEngine] = []

    def _make_engine(url: str) -> _FakeStaleCleanupEngine:
        assert url == postgres_mod._admin_url(database_url)
        made_engines.append(engine)
        return engine

    @asynccontextmanager
    async def _ddl_lock(
        engine_arg: _FakeStaleCleanupEngine,
    ) -> AsyncIterator[_FakeStaleCleanupConnection]:
        assert engine_arg is engine
        engine.lock_count += 1
        yield _FakeStaleCleanupConnection(engine)

    monkeypatch.setattr(postgres_mod, "_make_test_engine", _make_engine)
    monkeypatch.setattr(postgres_mod, "_postgres_schema_ddl_lock", _ddl_lock)
    monkeypatch.setattr(
        postgres_mod,
        "_is_postgres_test_schema_namespace_active",
        lambda _url, _namespace: False,
    )

    await postgres_mod._drop_stale_postgres_test_schemas(database_url)

    assert made_engines == [engine]
    assert engine.dispose_count == 1
    assert engine.begin_count == 1
    assert engine.lock_count == 1
    assert engine.commit_count == 6
    assert engine.statements[0].count("information_schema.schemata") == 1
    assert sum("pg_terminate_backend" in statement for statement in engine.statements) == 3
    assert engine.statements.count("SET LOCAL lock_timeout = '5s'") == 3
    assert [
        statement for statement in engine.statements if statement.startswith("DROP SCHEMA")
    ] == sorted(
        [
            f'DROP SCHEMA IF EXISTS "{other_schema}" CASCADE',
            f'DROP SCHEMA IF EXISTS "{first_schema}" CASCADE',
            f'DROP SCHEMA IF EXISTS "{second_schema}" CASCADE',
        ]
    )


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("helper_name", ["postgres_test_engine", "postgres_test_url"])
async def test_postgres_context_helpers_lock_ddl_and_drop_after_metadata_dispose(
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
) -> None:
    database_url = "postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
    schema = f"awf_test_{'1' * 16}_{'a' * 32}"
    quoted_schema = postgres_mod._quote_identifier(schema)
    admin_engine = _FakeDisposableEngine("admin")
    schema_engine = _FakeDisposableEngine("schema")
    events: list[tuple[str, bool, int]] = []
    lock_depth = 0

    @asynccontextmanager
    async def _recording_ddl_lock(
        engine_arg: _FakeDisposableEngine,
    ) -> AsyncIterator[_FakeDisposableEngine]:
        assert engine_arg is admin_engine
        nonlocal lock_depth
        lock_depth += 1
        events.append(("lock", schema_engine.disposed, lock_depth))
        try:
            yield admin_engine
        finally:
            events.append(("unlock", schema_engine.disposed, lock_depth))
            lock_depth -= 1

    monkeypatch.setattr(postgres_mod, "postgres_test_database_url", lambda: database_url)
    monkeypatch.setattr(postgres_mod, "_ensure_postgres_test_run_active", lambda _url: None)
    monkeypatch.setattr(postgres_mod, "_new_postgres_test_schema", lambda: schema)
    monkeypatch.setattr(postgres_mod, "_make_test_engine", lambda _url: admin_engine)
    monkeypatch.setattr(postgres_mod, "_postgres_schema_ddl_lock", _recording_ddl_lock)

    async def _create_metadata_engine(schema_database_url: str) -> _FakeDisposableEngine:
        assert "awf_search_path=" in schema_database_url
        events.append(("metadata", schema_engine.disposed, lock_depth))
        return schema_engine

    async def _create_schema(conn: _FakeDisposableEngine, schema_arg: str) -> None:
        assert conn is admin_engine
        assert schema_arg == quoted_schema
        events.append(("create", schema_engine.disposed, lock_depth))
        assert lock_depth == 1

    async def _drop_schema(
        conn: _FakeDisposableEngine,
        schema_arg: str,
        quoted_schema_arg: str,
    ) -> None:
        assert conn is admin_engine
        assert schema_arg == schema
        assert quoted_schema_arg == quoted_schema
        events.append(("drop", schema_engine.disposed, lock_depth))
        assert schema_engine.disposed is True
        assert lock_depth == 1

    monkeypatch.setattr(postgres_mod, "_create_metadata_engine", _create_metadata_engine)
    monkeypatch.setattr(postgres_mod, "_create_schema", _create_schema)
    monkeypatch.setattr(postgres_mod, "_drop_schema", _drop_schema)

    manager = getattr(postgres_mod, helper_name)()
    async with manager as yielded:
        if helper_name == "postgres_test_engine":
            assert yielded is schema_engine
            assert schema_engine.disposed is False
        else:
            assert isinstance(yielded, str)
            assert schema_engine.disposed is True

    metadata_lock_depth = 0 if helper_name == "postgres_test_engine" else 1
    assert admin_engine.dispose_count == 1
    assert schema_engine.dispose_count == 1
    assert ("metadata", False, metadata_lock_depth) in events
    assert ("create", False, 1) in events
    assert ("drop", True, 1) in events


@pytest.mark.unit
def test_make_engine_strips_test_retry_options_and_installs_async_creator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _create_async_engine(url: str, **kwargs: Any) -> object:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return SimpleNamespace(url=url)

    monkeypatch.setattr(session_mod, "create_async_engine", _create_async_engine)

    make_engine(
        "postgresql+asyncpg://awf:pw@localhost:5433/awf"
        "?awf_search_path=first&awf_connect_timeout=2&awf_connect_attempts=5"
    )

    assert captured["url"] == "postgresql+asyncpg://awf:pw@localhost:5433/awf"
    assert "connect_args" not in captured["kwargs"]
    assert callable(captured["kwargs"]["async_creator"])


@pytest.mark.unit
@pytest.mark.parametrize(
    ("url", "message"),
    [
        (
            "postgresql+asyncpg://awf:pw@localhost:5433/awf?awf_connect_timeout=nope",
            "awf_connect_timeout must be a positive number.",
        ),
        (
            "postgresql+asyncpg://awf:pw@localhost:5433/awf?awf_connect_timeout=0",
            "awf_connect_timeout must be a positive number.",
        ),
        (
            "postgresql+asyncpg://awf:pw@localhost:5433/awf?awf_connect_attempts=nope",
            "awf_connect_attempts must be a positive integer.",
        ),
        (
            "postgresql+asyncpg://awf:pw@localhost:5433/awf?awf_connect_attempts=0",
            "awf_connect_attempts must be a positive integer.",
        ),
    ],
)
def test_make_engine_rejects_invalid_test_retry_options(url: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        make_engine(url)


@pytest.mark.unit
async def test_test_retry_async_creator_retries_transient_connect_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _create_async_engine(url: str, **kwargs: Any) -> object:
        captured["url"] = url
        captured["kwargs"] = kwargs
        return SimpleNamespace(url=url)

    class TransientConnectError(Exception):
        pass

    attempts: list[tuple[str, dict[str, object]]] = []
    sleeps: list[float] = []

    async def _connect(*, dsn: str, **connect_args: object) -> object:
        attempts.append((dsn, connect_args))
        if len(attempts) == 1:
            raise TransientConnectError
        return SimpleNamespace(ok=True)

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(session_mod, "create_async_engine", _create_async_engine)
    monkeypatch.setattr(session_mod.asyncio, "sleep", _sleep)
    monkeypatch.setitem(
        sys.modules,
        "asyncpg",
        SimpleNamespace(
            connect=_connect,
            PostgresConnectionError=TransientConnectError,
            TooManyConnectionsError=RuntimeError,
        ),
    )

    make_engine(
        "postgresql+asyncpg://awf:pw@localhost:5433/awf"
        "?awf_search_path=first&awf_connect_timeout=2&awf_connect_attempts=2"
    )
    creator = captured["kwargs"]["async_creator"]

    assert await creator() == SimpleNamespace(ok=True)
    assert attempts == [
        (
            "postgresql://awf:pw@localhost:5433/awf",
            {"server_settings": {"search_path": "first"}, "timeout": 2.0},
        ),
        (
            "postgresql://awf:pw@localhost:5433/awf",
            {"server_settings": {"search_path": "first"}, "timeout": 2.0},
        ),
    ]
    assert sleeps == [0.1]


@pytest.mark.unit
async def test_test_retry_async_creator_reraises_after_configured_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _create_async_engine(url: str, **kwargs: Any) -> object:
        captured["kwargs"] = kwargs
        return SimpleNamespace(url=url)

    class TransientConnectError(Exception):
        pass

    attempts = 0

    async def _connect(*, dsn: str, **connect_args: object) -> object:
        del dsn, connect_args
        nonlocal attempts
        attempts += 1
        raise TransientConnectError

    async def _sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(session_mod, "create_async_engine", _create_async_engine)
    monkeypatch.setattr(session_mod.asyncio, "sleep", _sleep)
    monkeypatch.setitem(
        sys.modules,
        "asyncpg",
        SimpleNamespace(
            connect=_connect,
            PostgresConnectionError=TransientConnectError,
            TooManyConnectionsError=RuntimeError,
        ),
    )

    make_engine(
        "postgresql+asyncpg://awf:pw@localhost:5433/awf"
        "?awf_connect_timeout=2&awf_connect_attempts=2"
    )

    with pytest.raises(TransientConnectError):
        await captured["kwargs"]["async_creator"]()
    assert attempts == 2


@pytest.mark.unit
async def test_drop_schema_terminates_lock_holders_before_cascade_drop() -> None:
    statements: list[tuple[str, object | None]] = []
    conn = _RecordingConnection(statements)

    await postgres_mod._drop_schema(conn, "awf_test", '"awf_test"')

    assert "pg_terminate_backend" in statements[0][0]
    assert statements[0][1] == {"schema": "awf_test"}
    assert statements[1] == ("SET LOCAL lock_timeout = '5s'", None)
    assert statements[2] == ('DROP SCHEMA IF EXISTS "awf_test" CASCADE', None)


@pytest.mark.unit
async def test_postgres_schema_ddl_lock_uses_database_advisory_lock() -> None:
    statements: list[tuple[str, object | None]] = []
    conn = _RecordingConnection(statements)
    engine = SimpleNamespace(connect=lambda: _AsyncConnectionContext(conn))

    async with postgres_mod._postgres_schema_ddl_lock(engine):
        statements.append(("inside", None))

    assert "pg_advisory_lock" in statements[0][0]
    assert statements[0][1] == {"namespace": 0x415746, "key": 0x54455354}
    assert statements[1] == ("inside", None)
    assert "pg_advisory_unlock" in statements[2][0]
    assert statements[2][1] == {"namespace": 0x415746, "key": 0x54455354}


@pytest.mark.unit
async def test_postgres_test_url_marks_yielded_url_null_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Engine:
        async def dispose(self) -> None:
            return None

    @asynccontextmanager
    async def _fake_lock(engine: _Engine) -> AsyncIterator[_Engine]:
        yield engine

    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _metadata_engine(_url: str) -> _Engine:
        return _Engine()

    monkeypatch.setattr(
        postgres_mod,
        "postgres_test_database_url",
        lambda: "postgresql+asyncpg://awf:pw@localhost:5433/awf",
    )
    monkeypatch.setattr(postgres_mod, "_make_test_engine", lambda _url: _Engine())
    monkeypatch.setattr(postgres_mod, "_postgres_schema_ddl_lock", _fake_lock)
    monkeypatch.setattr(postgres_mod, "_create_schema", _noop)
    monkeypatch.setattr(postgres_mod, "_create_metadata_engine", _metadata_engine)
    monkeypatch.setattr(postgres_mod, "_drop_schema", _noop)

    async with postgres_mod.postgres_test_url() as database_url:
        parsed = make_url(database_url)

    assert parsed.query["awf_search_path"].strip('"').startswith("awf_test_")
    assert parsed.query["awf_null_pool"] == "1"
    assert parsed.query["awf_connect_timeout"] == str(
        postgres_mod.POSTGRES_TEST_CONNECT_TIMEOUT_SECONDS
    )
    assert parsed.query["awf_connect_retries"] == str(postgres_mod.POSTGRES_TEST_CONNECT_ATTEMPTS)


@pytest.mark.unit
def test_postgres_datetime_helpers_return_aware_utc_values() -> None:
    naive = datetime(2026, 5, 4, 12, 0)

    assert _ensure_utc(naive) == naive.replace(tzinfo=UTC)
    assert _utc_datetime(naive) == naive.replace(tzinfo=UTC)


@pytest.mark.unit
def test_database_endpoint_reports_missing_host() -> None:
    assert _database_endpoint("postgresql+asyncpg:///awf") == (
        "AWF_DATABASE_URL must include a host for local service mode."
    )


@pytest.mark.unit
def test_worker_merge_coordinator_falls_back_for_nonstandard_postgres_scheme() -> None:
    coordinator = _merge_coordinator_for_database_url(
        "postgresqlfoo://awf:pw@localhost/awf",
        engine=SimpleNamespace(),
    )

    assert isinstance(coordinator, InProcessMergeCoordinator)


@pytest.mark.unit
def test_adoption_rejects_unparseable_request_repo_identity() -> None:
    request = PullRequestMonitorAdoptionRequest(repo_url="not-a-github-repo", pr_number=1)

    with pytest.raises(PRMonitorAdoptionError) as exc_info:
        _raise_if_repo_identity_conflicts(
            canonical_repo=RepoRef(owner="owner", name="repo"),
            request=request,
        )

    assert exc_info.value.error_code == "INVALID_GITHUB_REPO"
    assert exc_info.value.detail == {
        "repo": "not-a-github-repo",
        "field": "repo_url",
    }


@pytest.mark.unit
def test_adoption_rejects_same_slug_repo_url_on_conflicting_forge() -> None:
    # Repo identity is (forge, owner, name), not slug alone. Forge detection
    # (#345) parses a ``bitbucket.org`` ``repo_url`` as RepoRef(forge="bitbucket")
    # with the SAME ``owner/repo`` slug as a GitHub ``pr_url``. Comparing slug
    # only would accept that inconsistent input; the forge must be compared too
    # so it is rejected up front rather than persisted and failed late at the
    # executor forge gate.
    request = PullRequestMonitorAdoptionRequest(
        pr_url="https://github.com/owner/repo/pull/7",
        repo_url="https://bitbucket.org/owner/repo",
    )

    with pytest.raises(PRMonitorAdoptionError) as exc_info:
        _raise_if_repo_identity_conflicts(
            canonical_repo=RepoRef(owner="owner", name="repo", forge="github"),
            request=request,
        )

    assert exc_info.value.error_code == "PR_ADOPTION_INPUT_REQUIRED"
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == {
        "expected_repo_slug": "owner/repo",
        "actual_repo_slug": "owner/repo",
        "expected_forge": "github",
        "actual_forge": "bitbucket",
        "field": "repo_url",
    }


@pytest.mark.unit
def test_inline_profile_name_requires_mapping_with_string_name() -> None:
    assert _inline_profile_name(None) is None
    assert _inline_profile_name({"name": 42}) is None
    assert _inline_profile_name({"name": "strict-postgres"}) == "strict-postgres"


@pytest.mark.unit
def test_adoption_head_repo_slug_validation_edges() -> None:
    repo = RepoRef(owner="owner", name="repo")

    assert (
        _head_repo_slug_from_adoption_payload(
            {"isCrossRepository": False},
            repo=repo,
            pr_number=7,
        )
        == "owner/repo"
    )

    with pytest.raises(PullRequestMetadataError) as invalid_repo:
        _head_repo_slug_from_adoption_payload(
            {"headRepository": {"nameWithOwner": "not enough parts"}},
            repo=repo,
            pr_number=7,
        )
    assert invalid_repo.value.reason_code == "PR_METADATA_INVALID"

    with pytest.raises(PullRequestMetadataError) as missing_fork_repo:
        _head_repo_slug_from_adoption_payload(
            {"isCrossRepository": True},
            repo=repo,
            pr_number=7,
        )
    assert missing_fork_repo.value.reason_code == "PR_METADATA_INVALID"
