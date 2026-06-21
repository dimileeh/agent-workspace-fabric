"""Protected-scope revert edge tests for PR monitor runner."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.control.quality_gates import QualityGateViolation
from awf.runtime.pr_monitor_runner.remote_ops import _ProtectedScopePushBlock
from awf.runtime.pr_monitor_runner.types import ProtectedScopeDiffError
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
async def test_protected_scope_commit_repair_missing_start_head_does_not_push_or_repair(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="blocked-head-sha\n")
    adapter = FakeAdapter()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    push_result = await runner._repair_protected_scope_commits_before_push(
        workspace_id=workspace_id,
        pr_number=42,
        protected_scope_block=_ProtectedScopePushBlock(
            message="protected scope blocked",
            reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
            violations=(
                QualityGateViolation(
                    path=".github/workflows/ci.yml",
                    protected_pattern=".github/**",
                ),
            ),
        ),
        compose_project=f"awf_{workspace_id}",
        compose_file=tmp_path / "compose.yml",
        remote_branch=f"awf/{workspace_id}",
    )

    assert push_result.failed is True
    assert push_result.pushed is False
    assert push_result.reason_code == "PROTECTED_SCOPE_PUSH_BLOCKED"
    assert "operation start commit was unavailable" in push_result.stderr
    assert push_result.details is not None
    assert push_result.details["rollback_status"] == "skipped_missing_operation_start_head"
    assert push_result.details["branch_restored"] is False
    assert adapter.calls == []
    assert not any(call.args[:1] == ["git"] and "push" in call.args for call in cmd.calls)
    assert _git_worktree_command(worktree, "reset", "--hard", "start-sha") not in [
        call.args for call in cmd.calls
    ]


@pytest.mark.unit
async def test_protected_scope_revert_verifies_tracked_restore_against_fetch_head(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    remaining = await runner._protected_scope_violations_not_restored_to_remote_branch(
        workspace_id=workspace_id,
        status_stdout=" M .github/workflows/ci.yml\n",
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
            "diff",
            "--quiet",
            "FETCH_HEAD",
            "--",
            ".github/workflows/ci.yml",
        ),
    ]


@pytest.mark.unit
async def test_protected_scope_revert_skips_empty_violation_list(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    remaining = await runner._protected_scope_violations_not_restored_to_remote_branch(
        workspace_id=workspace_id,
        status_stdout="",
        violations=[],
        remote_branch=f"awf/{workspace_id}",
    )

    assert remaining == []
    assert cmd.calls == []


@pytest.mark.unit
async def test_protected_scope_revert_raises_when_remote_fetch_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=128, stdout="", stderr="no such ref")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    with pytest.raises(ProtectedScopeDiffError, match="fetch refs/heads"):
        await runner._protected_scope_violations_not_restored_to_remote_branch(
            workspace_id=workspace_id,
            status_stdout=" M .github/workflows/ci.yml\n",
            violations=[
                QualityGateViolation(
                    path=".github/workflows/ci.yml",
                    protected_pattern=".github/**",
                )
            ],
            remote_branch=f"awf/{workspace_id}",
        )
