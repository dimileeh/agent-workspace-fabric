"""Shared path helpers for executor tests."""

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _test_worktrees_root(factory: async_sessionmaker[AsyncSession]) -> Path:
    bind = factory.kw["bind"]
    database_path = Path(str(bind.url.database))
    return database_path.parent / "work" / "worktrees"


def _test_worktree_path(factory: async_sessionmaker[AsyncSession], workspace_id: str) -> Path:
    return _test_worktrees_root(factory) / workspace_id
