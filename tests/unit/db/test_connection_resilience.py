"""Regression tests for asyncpg/PostgreSQL connection resilience."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, InterfaceError
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
    return InterfaceError("SELECT 1", {}, RuntimeError("connection is closed"))


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
    async with postgres_test_engine() as engine:
        factory = make_session_factory(engine)

        async def _operation(_session: object) -> int:
            return 1

        with pytest.raises(ValueError, match="attempts must be at least 1"):
            await run_db_operation_with_retry(factory, _operation, attempts=0)


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
