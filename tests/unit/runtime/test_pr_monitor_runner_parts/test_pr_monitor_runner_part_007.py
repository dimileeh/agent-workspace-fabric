"""Additional unit tests for focused ``pr_monitor_runner`` merge behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.enums import OperationStatus, OperationType, TaskClass, WorkspaceStatus
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckState,
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
)
from awf.runtime.pr_monitor_runner.helpers import _non_check_reviewer_settle_started_key
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a PostgreSQL-backed async session factory for monitor tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _green_status(*, pr_number: int = 42, head_sha: str = "abc1234567890def") -> PRStatus:
    return PRStatus(
        number=pr_number,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )


def _gh_pr_merge_calls(cmd: FakeCommandRunner) -> list[list[str]]:
    return [call.args for call in cmd.calls if call.args[:3] == ["gh", "pr", "merge"]]


async def _mark_refactor_task(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    auto_merge: bool = True,
) -> None:
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.task_class = TaskClass.refactor_task.value
        workspace.auto_merge = auto_merge
        await session.commit()


@pytest.mark.unit
async def test_auto_merge_dispatches_validation_recovery_before_merge(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Queue validation recovery when auto-merge lacks fresh validation."""
    pr_number = 81
    head_sha = "b" * 40
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=pr_number,
        head_sha=head_sha,
    )
    await _mark_refactor_task(factory, workspace_id)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
    )

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=pr_number,
        status=_green_status(pr_number=pr_number, head_sha=head_sha),
        state=MonitorState(started_at=0.0),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)

    assert terminal is True
    assert _gh_pr_merge_calls(cmd) == []
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.ready.value
    assert len(operations) == 1
    assert operations[0].type == "validate"
    assert operations[0].status == OperationStatus.pending.value
    assert operations[0].payload["action"] == "validate_only"
    assert operations[0].payload["reason_code"] == "VALIDATION_INSUFFICIENT_TIER"
    assert operations[0].payload["source_head_sha"] == head_sha


@pytest.mark.unit
async def test_auto_merge_waits_for_reviewer_settle_before_validation_recovery(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Wait through reviewer settle before queueing validation recovery."""
    pr_number = 811
    head_sha = "8" * 40
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(
        factory,
        pr_number=pr_number,
        head_sha=head_sha,
    )
    await _mark_refactor_task(factory, workspace_id)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
        non_check_reviewer_settle_seconds=900,
    )
    state = MonitorState(started_at=1000.0)

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=pr_number,
        status=_green_status(pr_number=pr_number, head_sha=head_sha),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)

    assert terminal is False
    assert sleep_fn.calls == [60]
    assert workspace is not None
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    assert not [op for op in operations if op.type == OperationType.validate.value]
    settle_operations = [
        op
        for op in operations
        if op.type == OperationType.monitor_state.value
        and op.payload.get("reason_code") == "NON_CHECK_REVIEWER_SETTLE"
    ]
    assert len(settle_operations) == 1
    assert (
        _non_check_reviewer_settle_started_key(pr_number=pr_number, head_sha=head_sha)
        in state.threads_addressed_ids
    )
