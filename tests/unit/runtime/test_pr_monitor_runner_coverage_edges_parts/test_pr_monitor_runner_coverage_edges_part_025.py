"""Protected-scope revert coverage for untracked file restore checks."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.control.quality_gates import QualityGateViolation
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
        from awf.db.session import make_session_factory

        yield make_session_factory(engine)


def _git_worktree_command(worktree_path: Path, *args: str) -> list[str]:
    return ["git", "-c", f"safe.directory={worktree_path}", "-C", str(worktree_path), *args]


@pytest.mark.unit
async def test_protected_scope_revert_verifies_untracked_restore_against_fetch_head(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="remote-blob\n")
    cmd.queue_result(returncode=0, stdout="remote-blob\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    remaining = await runner._protected_scope_violations_not_restored_to_remote_branch(
        workspace_id=workspace_id,
        status_stdout="?? .github/workflows/ci.yml\n",
        violations=[
            QualityGateViolation(
                path=".github/workflows/ci.yml",
                protected_pattern=".github/**",
            )
        ],
        remote_branch=f"awf/{workspace_id}",
    )

    assert remaining == []
    assert [call.args for call in cmd.calls] == [
        _git_worktree_command(worktree, "fetch", "origin", f"refs/heads/awf/{workspace_id}"),
        _git_worktree_command(
            worktree,
            "rev-parse",
            "--verify",
            "FETCH_HEAD:.github/workflows/ci.yml^{blob}",
        ),
        _git_worktree_command(
            worktree,
            "hash-object",
            "--path",
            ".github/workflows/ci.yml",
            "--",
            ".github/workflows/ci.yml",
        ),
    ]
