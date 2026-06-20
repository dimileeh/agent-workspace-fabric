"""Additional focused ``pr_monitor_runner`` dirty-worktree commit tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import seed_monitoring_workspace
from tests.unit.runtime.test_pr_monitor_runner_parts.test_pr_monitor_runner_part_005 import (
    _monitor_runner,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class TestDirtyWorktreeCommitSubject:
    @pytest.mark.unit
    async def test_commit_dirty_worktree_truncates_subject_to_72(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        """The monitor dirty-worktree commit subject is capped at 72 chars after tagging."""
        workspace_id = await seed_monitoring_workspace(factory)
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            assert workspace is not None
            workspace.task_tag = "PROJ-123"
            await session.commit()

        fake = FakeCommandRunner()
        for result in (
            {"returncode": 0, "stdout": " M file.py\n"},
            {"returncode": 0, "stdout": " M file.py\n"},
            {"returncode": 0},
            {"returncode": 1},
            {"returncode": 0},
        ):
            fake.queue_result(**result)
        runner = _monitor_runner(tmp_path, fake, session_factory=factory)
        (runner._worktrees_root / workspace_id).mkdir(parents=True, exist_ok=True)

        long_subject = "fix: address PR review comment " + "x" * 80
        committed = await runner._commit_dirty_worktree(
            workspace_id=workspace_id,
            message=long_subject,
        )

        assert committed is True
        commit_calls = [call for call in fake.calls if "commit" in call.args and "-m" in call.args]
        assert commit_calls, "expected a git commit invocation"
        message = commit_calls[-1].args[commit_calls[-1].args.index("-m") + 1]
        assert len(message) == 72
        assert message == ("PROJ-123 " + long_subject)[:72]
