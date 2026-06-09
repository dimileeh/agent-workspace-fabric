"""Regression tests for asyncpg/PostgreSQL connection resilience."""

from __future__ import annotations

import asyncpg
import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, InterfaceError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db import resilience as resilience_mod
from awf.db import session as session_mod
from awf.db.resilience import (
    is_transient_closed_connection_error,
    run_db_operation_with_retry,
)
from awf.db.session import make_engine, make_session_factory
from tests.postgres import postgres_empty_test_url, postgres_test_engine


def _closed_connection_error() -> InterfaceError:
    return InterfaceError(
        "SELECT 1",
        {},
        RuntimeError("connection is closed"),
        connection_invalidated=True,
    )


@pytest.mark.unit
def test_make_engine_configures_asyncpg_liveness_pool_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def _fake_create_async_engine(url: str, **kwargs: object) -> object:
        calls.append((url, kwargs))
        return object()

    monkeypatch.setattr(session_mod, "create_async_engine", _fake_create_async_engine)

    engine = make_engine(
        "postgresql+asyncpg://awf:awf@example.test/awf"
        "?awf_search_path=awf_test&awf_connect_timeout=7",
        connect_args={"application_name": "awf-test"},
    )

    assert engine is not None
    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == "postgresql+asyncpg://awf:awf@example.test/awf"
    assert "awf_search_path" not in url
    assert "awf_connect_timeout" not in url
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_recycle"] == session_mod.DEFAULT_POOL_RECYCLE_SECONDS
    assert kwargs["pool_timeout"] == session_mod.DEFAULT_POOL_TIMEOUT_SECONDS
    assert kwargs["future"] is True
    assert kwargs["echo"] is False
    assert kwargs["connect_args"] == {
        "application_name": "awf-test",
        "timeout": 7.0,
        "server_settings": {"search_path": "awf_test"},
    }


@pytest.mark.unit
def test_make_engine_marks_asyncpg_internal_pre_ping_protocol_error_as_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Dialect:
        name = "postgresql"
        driver = "asyncpg"

        def __init__(self) -> None:
            self.error: BaseException = asyncpg.exceptions.InternalClientError(
                "cannot switch to state 15; another operation (2) is in progress"
            )

        def do_ping(self, _dbapi_connection: object) -> bool:
            raise self.error

    class _SyncEngine:
        def __init__(self) -> None:
            self.dialect = _Dialect()

    class _Engine:
        def __init__(self) -> None:
            self.sync_engine = _SyncEngine()

    engine = _Engine()

    def _fake_create_async_engine(_url: str, **_kwargs: object) -> object:
        return engine

    monkeypatch.setattr(session_mod, "create_async_engine", _fake_create_async_engine)

    returned_engine = make_engine("postgresql+asyncpg://awf:awf@example.test/awf")

    assert returned_engine is engine
    assert engine.sync_engine.dialect.do_ping(object()) is False

    engine.sync_engine.dialect.error = RuntimeError("ordinary ping failure")
    with pytest.raises(RuntimeError, match="ordinary ping failure"):
        engine.sync_engine.dialect.do_ping(object())


@pytest.mark.unit
async def test_closed_pooled_postgres_connection_is_replaced_on_next_checkout() -> None:
    async with postgres_empty_test_url() as database_url:
        pooled_engine = make_engine(database_url)
        parsed_url = make_url(database_url)
        query = dict(parsed_url.query)
        query["awf_null_pool"] = "1"
        terminator_engine = make_engine(
            parsed_url.set(query=query).render_as_string(hide_password=False)
        )
        try:
            async with pooled_engine.connect() as conn:
                backend_pid = await conn.scalar(text("SELECT pg_backend_pid()"))
            assert backend_pid is not None

            async with terminator_engine.connect() as terminator:
                terminated = await terminator.scalar(
                    text("SELECT pg_terminate_backend(:pid)"),
                    {"pid": backend_pid},
                )
                await terminator.commit()
            assert terminated is True

            async with pooled_engine.connect() as conn:
                assert await conn.scalar(text("SELECT 1")) == 1
                replacement_pid = await conn.scalar(text("SELECT pg_backend_pid()"))
            assert replacement_pid is not None
            assert replacement_pid != backend_pid
        finally:
            await terminator_engine.dispose()
            await pooled_engine.dispose()


@pytest.mark.unit
def test_closed_connection_classifier_matches_asyncpg_interface_error_names() -> None:
    class ConnectionDoesNotExistError(Exception):
        pass

    class UnrelatedError(Exception):
        pass

    invalidated = DBAPIError(
        "SELECT 1",
        {},
        RuntimeError("network disconnect"),
        None,
        connection_invalidated=True,
    )
    asyncpg_named = DBAPIError(
        "SELECT 1",
        {},
        ConnectionDoesNotExistError("connection went away"),
        None,
    )
    generic_dbapi_closed = DBAPIError(
        "SELECT 1",
        {},
        RuntimeError("connection is closed"),
        None,
    )
    cursor_interface_error = InterfaceError(
        "SELECT 1",
        {},
        RuntimeError("fetch called when no statement executed"),
    )
    unrelated = DBAPIError("SELECT 1", {}, UnrelatedError("syntax problem"), None)

    assert is_transient_closed_connection_error(_closed_connection_error()) is True
    assert is_transient_closed_connection_error(invalidated) is True
    assert is_transient_closed_connection_error(asyncpg_named) is True
    assert is_transient_closed_connection_error(generic_dbapi_closed) is True
    assert is_transient_closed_connection_error(cursor_interface_error) is False
    assert is_transient_closed_connection_error(unrelated) is False
    assert is_transient_closed_connection_error(ValueError("not a DB failure")) is False


@pytest.mark.unit
def test_closed_connection_classifier_matches_asyncpg_internal_protocol_state_error() -> None:
    protocol_error = asyncpg.exceptions.InternalClientError(
        "cannot switch to state 15; another operation (2) is in progress"
    )
    unrelated_internal_error = asyncpg.exceptions.InternalClientError("unexpected asyncpg bug")

    assert is_transient_closed_connection_error(protocol_error) is True
    assert is_transient_closed_connection_error(unrelated_internal_error) is False


@pytest.mark.unit
def test_closed_connection_classifier_handles_cyclic_exception_chains() -> None:
    exc = RuntimeError("ordinary DB failure")
    exc.__cause__ = exc

    assert is_transient_closed_connection_error(exc) is False


@pytest.mark.unit
def test_closed_connection_classifier_ignores_suppressed_context() -> None:
    try:
        try:
            raise _closed_connection_error()
        except InterfaceError:
            raise RuntimeError("separate application failure") from None
    except RuntimeError as exc:
        wrapped = exc

    assert wrapped.__suppress_context__ is True
    assert wrapped.__context__ is not None
    assert is_transient_closed_connection_error(wrapped) is False


@pytest.mark.unit
def test_closed_connection_classifier_ignores_unsuppressed_context() -> None:
    wrapped = RuntimeError("operation wrapper failed")
    wrapped.__context__ = _closed_connection_error()
    wrapped.__suppress_context__ = False

    assert wrapped.__suppress_context__ is False
    assert wrapped.__context__ is not None
    assert is_transient_closed_connection_error(wrapped) is False


@pytest.mark.unit
def test_closed_connection_classifier_can_walk_unsuppressed_context_when_requested() -> None:
    wrapped = RuntimeError("operation wrapper failed")
    wrapped.__context__ = _closed_connection_error()
    wrapped.__suppress_context__ = False

    assert (
        is_transient_closed_connection_error(
            wrapped,
            include_unsuppressed_context=True,
        )
        is True
    )


@pytest.mark.unit
def test_closed_connection_classifier_walks_explicit_cause() -> None:
    wrapped = RuntimeError("operation wrapper failed")
    wrapped.__cause__ = _closed_connection_error()

    assert is_transient_closed_connection_error(wrapped) is True


@pytest.mark.unit
def test_closed_connection_classifier_walks_exception_group_members() -> None:
    grouped = ExceptionGroup(
        "task group failed",
        [
            RuntimeError("application failure"),
            _closed_connection_error(),
        ],
    )

    assert is_transient_closed_connection_error(grouped) is True


@pytest.mark.unit
def test_closed_connection_classifier_bounds_long_message_fragment_scan() -> None:
    early_fragment = RuntimeError("prefix connection is closed")
    late_fragment = RuntimeError(
        ("x" * (resilience_mod._MAX_CLOSED_CONNECTION_MESSAGE_SCAN_CHARS + 1))
        + " connection is closed"
    )

    assert is_transient_closed_connection_error(early_fragment) is True
    assert is_transient_closed_connection_error(late_fragment) is False


@pytest.mark.unit
async def test_db_retry_rejects_invalid_attempt_count() -> None:
    def _factory() -> object:
        raise AssertionError("session factory should not be called when attempts=0")

    async def _operation(_session: object) -> int:
        return 1

    with pytest.raises(ValueError, match="attempts must be at least 1"):
        await run_db_operation_with_retry(_factory, _operation, attempts=0)


@pytest.mark.unit
async def test_db_retry_reraises_non_transient_errors_without_retrying() -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        calls = 0

        async def _operation(_session: object) -> int:
            nonlocal calls
            calls += 1
            raise RuntimeError("query syntax failed")

        with pytest.raises(RuntimeError, match="query syntax failed"):
            await run_db_operation_with_retry(factory, _operation, attempts=2)

    assert calls == 1


@pytest.mark.unit
async def test_db_retry_does_not_retry_application_error_with_transient_context() -> None:
    sessions: list[_RetrySession] = []

    def _factory() -> _RetrySession:
        session = _RetrySession(fail_commit=False)
        sessions.append(session)
        return session

    async def _operation(_session: _RetrySession) -> None:
        try:
            raise _closed_connection_error()
        except InterfaceError:
            raise RuntimeError("application-level failure")  # noqa: B904

    with pytest.raises(RuntimeError, match="application-level failure"):
        await run_db_operation_with_retry(_factory, _operation, attempts=2)

    assert len(sessions) == 1
    assert sessions[0].events == ["close"]


@pytest.mark.unit
async def test_db_retry_skips_cleanup_for_non_db_operation_errors() -> None:
    events: list[str] = []

    class _Session:
        async def invalidate(self) -> None:
            events.append("invalidate")

        async def rollback(self) -> None:
            events.append("rollback")

        async def close(self) -> None:
            events.append("close")

    def _factory() -> _Session:
        return _Session()

    async def _operation(_session: _Session) -> None:
        raise RuntimeError("application-level failure")

    with pytest.raises(RuntimeError, match="application-level failure"):
        await run_db_operation_with_retry(_factory, _operation)

    assert events == ["close"]


@pytest.mark.unit
async def test_db_retry_propagates_close_failure_after_successful_operation() -> None:
    class CloseError(Exception):
        pass

    class _Session:
        async def close(self) -> None:
            raise CloseError("close failed")

    def _factory() -> _Session:
        return _Session()

    async def _operation(_session: _Session) -> int:
        return 1

    with pytest.raises(CloseError, match="close failed"):
        await run_db_operation_with_retry(_factory, _operation)


@pytest.mark.unit
async def test_db_retry_close_error_does_not_mask_original_operation_error() -> None:
    class CloseError(Exception):
        pass

    class OriginalError(Exception):
        pass

    class _Session:
        async def close(self) -> None:
            raise CloseError("close failed")

    def _factory() -> _Session:
        return _Session()

    async def _operation(_session: _Session) -> None:
        raise OriginalError("operation failed")

    with (
        structlog.testing.capture_logs() as captured,
        pytest.raises(OriginalError, match="operation failed"),
    ):
        await run_db_operation_with_retry(_factory, _operation)

    assert any(
        entry.get("event") == "run_db_operation_with_retry.close_failed_after_operation_error"
        and entry.get("log_level") == "warning"
        and entry.get("error_type") == "CloseError"
        and entry.get("error") == "close failed"
        for entry in captured
    )


@pytest.mark.unit
async def test_db_retry_rolls_back_non_transient_db_operation_errors() -> None:
    events: list[str] = []

    class _Session:
        async def invalidate(self) -> None:
            events.append("invalidate")

        async def rollback(self) -> None:
            events.append("rollback")

        async def close(self) -> None:
            events.append("close")

    def _factory() -> _Session:
        return _Session()

    async def _operation(_session: _Session) -> None:
        raise ProgrammingError("SELECT broken", {}, RuntimeError("syntax error"))

    with pytest.raises(ProgrammingError, match="syntax error"):
        await run_db_operation_with_retry(_factory, _operation)

    assert events == ["rollback", "close"]


@pytest.mark.unit
async def test_db_retry_can_avoid_ambiguous_commit_replays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        calls = 0
        commits = 0

        async def _operation(_session: object) -> str:
            nonlocal calls
            calls += 1
            return "created"

        async def _failing_commit(_session: AsyncSession) -> None:
            nonlocal commits
            commits += 1
            raise _closed_connection_error()

        monkeypatch.setattr(AsyncSession, "commit", _failing_commit)

        with pytest.raises(InterfaceError, match="connection is closed"):
            await run_db_operation_with_retry(
                factory,
                _operation,
                commit=True,
                retry_commit_failures=False,
            )

    assert calls == 1
    assert commits == 1


@pytest.mark.unit
async def test_db_retry_does_not_replay_commit_failures_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        calls = 0
        commits = 0

        async def _operation(_session: object) -> str:
            nonlocal calls
            calls += 1
            return "created"

        async def _failing_commit(_session: AsyncSession) -> None:
            nonlocal commits
            commits += 1
            raise _closed_connection_error()

        monkeypatch.setattr(AsyncSession, "commit", _failing_commit)

        with pytest.raises(InterfaceError, match="connection is closed"):
            await run_db_operation_with_retry(
                factory,
                _operation,
                commit=True,
            )

    assert calls == 1
    assert commits == 1


@pytest.mark.unit
async def test_db_retry_reports_retry_hook_for_replayed_commit_failures() -> None:
    sessions: list[_RetrySession] = []
    retries: list[tuple[BaseException, int]] = []

    def _factory() -> _RetrySession:
        session = _RetrySession(fail_commit=not sessions)
        sessions.append(session)
        return session

    async def _operation(_session: _RetrySession) -> str:
        return "committed"

    async def _on_retry(exc: BaseException, attempt: int) -> None:
        retries.append((exc, attempt))

    result = await run_db_operation_with_retry(
        _factory,
        _operation,
        attempts=2,
        commit=True,
        retry_commit_failures=True,
        on_retry=_on_retry,
    )

    assert result == "committed"
    assert len(sessions) == 2
    assert [session.events for session in sessions] == [
        ["commit", "invalidate", "close"],
        ["commit", "close"],
    ]
    assert [(type(exc), attempt) for exc, attempt in retries] == [(InterfaceError, 1)]


@pytest.mark.unit
async def test_db_retry_replays_commit_failures_when_explicitly_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)
        calls = 0
        commits = 0

        async def _operation(_session: object) -> str:
            nonlocal calls
            calls += 1
            return "refreshed"

        async def _flaky_commit(_session: AsyncSession) -> None:
            nonlocal commits
            commits += 1
            if commits == 1:
                raise _closed_connection_error()

        monkeypatch.setattr(AsyncSession, "commit", _flaky_commit)

        result = await run_db_operation_with_retry(
            factory,
            _operation,
            commit=True,
            retry_commit_failures=True,
        )

    assert result == "refreshed"
    assert calls == 2
    assert commits == 2


class _RetrySession:
    def __init__(self, *, fail_commit: bool) -> None:
        self.fail_commit = fail_commit
        self.events: list[str] = []

    async def commit(self) -> None:
        self.events.append("commit")
        if self.fail_commit:
            raise _closed_connection_error()

    async def invalidate(self) -> None:
        self.events.append("invalidate")

    async def rollback(self) -> None:
        self.events.append("rollback")

    async def close(self) -> None:
        self.events.append("close")
