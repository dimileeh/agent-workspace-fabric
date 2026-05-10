"""Tests for awf.db.session.session_scope.

session_scope is the explicit context manager used by workers + CLI (outside
the FastAPI request cycle). Its contract: commit on clean exit, rollback on
exception, always close. We verify all three paths.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory, session_scope
from tests.postgres import postgres_test_engine, postgres_test_url


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class TestSessionScope:
    @pytest.mark.unit
    async def test_commits_on_clean_exit(self, factory: async_sessionmaker[AsyncSession]) -> None:
        async with session_scope(factory) as s:
            await WorkspaceRepository(s).create(
                repo_url="git@x:y.git",
                branch_base="development",
                task_title="t",
                task_prompt="p",
                agent="codex",
                test_commands=[],
            )

        # Re-read from a fresh session to prove the commit landed.
        async with factory() as s:
            rows = (await s.execute(select(Workspace))).scalars().all()
            assert len(rows) == 1

    @pytest.mark.unit
    async def test_rolls_back_on_exception(self, factory: async_sessionmaker[AsyncSession]) -> None:
        class SentinelError(Exception):
            pass

        with pytest.raises(SentinelError):
            async with session_scope(factory) as s:
                await WorkspaceRepository(s).create(
                    repo_url="git@x:y.git",
                    branch_base="development",
                    task_title="t",
                    task_prompt="p",
                    agent="codex",
                    test_commands=[],
                )
                raise SentinelError("rollback, please")

        async with factory() as s:
            rows = (await s.execute(select(Workspace))).scalars().all()
            assert rows == []

    @pytest.mark.unit
    async def test_propagates_original_exception(
        self, factory: async_sessionmaker[AsyncSession]
    ) -> None:
        with pytest.raises(ValueError, match="original"):
            async with session_scope(factory):
                raise ValueError("original")

    @pytest.mark.unit
    async def test_close_error_does_not_mask_original_exception(self) -> None:
        class OriginalError(Exception):
            pass

        class CloseError(Exception):
            pass

        class FailingCloseSession:
            rolled_back = False
            closed = False

            async def commit(self) -> None:
                raise AssertionError("commit should not run after body failure")

            async def rollback(self) -> None:
                self.rolled_back = True

            async def close(self) -> None:
                self.closed = True
                raise CloseError("close failed")

        session = FailingCloseSession()

        with (
            structlog.testing.capture_logs() as captured,
            pytest.raises(OriginalError, match="original"),
        ):
            async with session_scope(lambda: session):  # type: ignore[arg-type]
                raise OriginalError("original")

        assert session.rolled_back is True
        assert session.closed is True
        assert any(
            entry.get("event") == "session_scope.close_failed_during_exception"
            and entry.get("log_level") == "warning"
            and entry.get("error_type") == "CloseError"
            and entry.get("error") == "close failed"
            for entry in captured
        )

    @pytest.mark.unit
    async def test_base_exception_runs_cleanup_before_close(self) -> None:
        class TrackingSession:
            events: list[str]

            def __init__(self) -> None:
                self.events = []

            async def commit(self) -> None:
                raise AssertionError("commit should not run after body cancellation")

            async def rollback(self) -> None:
                self.events.append("rollback")

            async def close(self) -> None:
                self.events.append("close")

        session = TrackingSession()

        with pytest.raises(asyncio.CancelledError, match="cancelled"):
            async with session_scope(lambda: session):  # type: ignore[arg-type]
                raise asyncio.CancelledError("cancelled")

        assert session.events == ["rollback", "close"]

    @pytest.mark.unit
    async def test_close_error_propagates_after_clean_body(self) -> None:
        class CloseError(Exception):
            pass

        class FailingCloseSession:
            committed = False
            closed = False

            async def commit(self) -> None:
                self.committed = True

            async def close(self) -> None:
                self.closed = True
                raise CloseError("close failed")

        session = FailingCloseSession()

        with pytest.raises(CloseError, match="close failed"):
            async with session_scope(lambda: session):  # type: ignore[arg-type]
                pass

        assert session.committed is True
        assert session.closed is True

    @pytest.mark.unit
    async def test_close_error_propagates_after_clean_body_inside_outer_exception(
        self,
    ) -> None:
        class OuterError(Exception):
            pass

        class CloseError(Exception):
            pass

        class FailingCloseSession:
            committed = False
            closed = False

            async def commit(self) -> None:
                self.committed = True

            async def close(self) -> None:
                self.closed = True
                raise CloseError("close failed")

        session = FailingCloseSession()

        try:
            raise OuterError("outer")
        except OuterError:
            with pytest.raises(CloseError, match="close failed"):
                async with session_scope(lambda: session):  # type: ignore[arg-type]
                    pass

        assert session.committed is True
        assert session.closed is True


@pytest.mark.unit
def test_make_engine_rejects_non_postgres_urls() -> None:
    with pytest.raises(ValueError, match="postgresql\\+asyncpg"):
        make_engine("mysql+asyncmy://u:p@example/awf")


@pytest.mark.unit
async def test_make_engine_strips_test_connect_retry_query_params() -> None:
    engine = make_engine(
        "postgresql+asyncpg://u:p@example/awf?awf_connect_timeout=10&awf_connect_retries=2"
    )
    try:
        assert "awf_connect_timeout" not in str(engine.url)
        assert "awf_connect_retries" not in str(engine.url)
    finally:
        await engine.dispose()


@pytest.mark.unit
async def test_make_engine_applies_url_search_path() -> None:
    async with postgres_test_url() as database_url:
        engine = make_engine(database_url)
        try:
            async with engine.connect() as conn:
                current_schema = await conn.scalar(text("select current_schema()"))
        finally:
            await engine.dispose()

    assert isinstance(current_schema, str)
    assert current_schema.startswith("awf_test_")
