"""Tests for awf.db.session.session_scope.

session_scope is the explicit context manager used by workers + CLI (outside
the FastAPI request cycle). Its contract: commit on clean exit, rollback on
exception, always close. We verify all three paths.
"""

from __future__ import annotations

import warnings
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.base import Base
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory, session_scope


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


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
async def test_sqlite_raw_datetime_binds_do_not_use_deprecated_default_adapter() -> None:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            async with engine.begin() as conn:
                await conn.execute(
                    text("SELECT :created_at"),
                    {"created_at": datetime(2026, 4, 26, 12, 0, tzinfo=UTC)},
                )
    finally:
        await engine.dispose()
