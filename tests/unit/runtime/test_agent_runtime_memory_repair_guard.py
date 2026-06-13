"""Tests for ``.claude/agent-memory/`` suppression in the repair dirty-tree guard.

The repair guard (``_pre_existing_dirty_repair_worktree_result``) refuses to
start agent repair from a dirty worktree. Untracked AWF-agent-runtime memory
written by reviewer subagents must not count as dirty (the #573 fix), while a
real dirty path — or tracked-modified memory — must still block. See #573.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor_runner.constants import (
    _PRE_EXISTING_DIRTY_WORKTREE_REASON,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def _run_guard(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    status_stdout: str,
):
    """Invoke the repair dirty-tree guard with a canned ``git status`` stdout."""
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=status_stdout)
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    return await runner._pre_existing_dirty_repair_worktree_result(
        workspace_id=workspace_id,
        worktree_path=worktree,
        operation_type="comment_repair",
    )


@pytest.mark.unit
async def test_untracked_agent_memory_only_returns_none(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Untracked memory paths (file or collapsed dir form) are not blocking."""
    result = await _run_guard(
        factory,
        tmp_path,
        status_stdout=(
            "?? .claude/agent-memory/bug-validator/notes.md\n?? .claude/agent-memory/bug-hunter/\n"
        ),
    )

    assert result is None


@pytest.mark.unit
async def test_untracked_agent_memory_collapsed_root_returns_none(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """A collapsed untracked-root entry (``.claude/agent-memory/``) is not blocking.

    Plain ``git status --porcelain`` (the repair guard's status mode) collapses a
    fully-untracked ``.claude/agent-memory`` directory to a single
    ``?? .claude/agent-memory/`` line. That root entry must still be suppressed.
    """
    result = await _run_guard(
        factory,
        tmp_path,
        status_stdout="?? .claude/agent-memory/\n",
    )

    assert result is None


@pytest.mark.unit
async def test_untracked_agent_memory_plus_real_path_blocks_with_real_path(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """A real dirty path still blocks; the memory path is filtered out."""
    result = await _run_guard(
        factory,
        tmp_path,
        status_stdout="?? .claude/agent-memory/x.md\n M src/awf/foo.py\n",
    )

    assert result is not None
    assert result.failed is True
    assert result.reason_code == _PRE_EXISTING_DIRTY_WORKTREE_REASON
    assert result.details["paths"] == ["src/awf/foo.py"]


@pytest.mark.unit
async def test_tracked_modified_agent_memory_blocks(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """Tracked-modified memory (not an ``??`` line) stays visible/blocking."""
    result = await _run_guard(
        factory,
        tmp_path,
        status_stdout=" M .claude/agent-memory/notes.md\n",
    )

    assert result is not None
    assert result.failed is True
    assert result.reason_code == _PRE_EXISTING_DIRTY_WORKTREE_REASON
    assert result.details["paths"] == [".claude/agent-memory/notes.md"]


@pytest.mark.unit
async def test_empty_status_returns_none(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """An empty status is not blocking — unchanged behavior."""
    result = await _run_guard(factory, tmp_path, status_stdout="")

    assert result is None
