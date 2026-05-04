"""Shared path helpers for executor tests."""

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _test_worktrees_root(factory: async_sessionmaker[AsyncSession]) -> Path:
    configured = getattr(factory, "_awf_test_worktrees_root", None)
    if configured is not None:
        return Path(configured)
    bind = factory.kw["bind"]
    database_path = Path(str(bind.url.database))
    return database_path.parent / "work" / "worktrees"


def _test_worktree_path(factory: async_sessionmaker[AsyncSession], workspace_id: str) -> Path:
    return _test_worktrees_root(factory) / workspace_id
