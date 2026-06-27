"""Focused ``pr_monitor_runner`` tests for transient CI infra waits."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.enums import OperationStatus
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckFailure,
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    WaitForTransientCI,
    _ci_transient_rerun_state_key,
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


def _failed_status(head_sha: str, failure: CheckFailure) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.FAILURE,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        ci_failures=(failure,),
    )


@pytest.mark.unit
async def test_wait_for_transient_ci_records_wait_without_adapter_repair(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    failure = CheckFailure(
        name="python-coverage-shards (7)",
        conclusion="FAILURE",
        log_excerpt=(
            "/usr/bin/docker pull postgres:16\n"
            "context deadline exceeded\n"
            "Docker pull failed with exit code 1"
        ),
        run_id="25655330295",
    )
    head_sha = "abc1234567890def"
    status = _failed_status(head_sha, failure)
    state = MonitorState()
    state.threads_addressed_ids[_ci_transient_rerun_state_key(head_sha, (failure,))] = "2"
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )

    terminal = await runner._execute(
        action=WaitForTransientCI(failures=(failure,), wait_seconds=120.0, wait_count=1),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=status,
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert adapter.calls == []
    assert cmd.calls == []
    assert sleep_fn.calls == [120.0]
    assert any(
        key.startswith("__awf_ci_infra_wait:") and key.endswith(":count") and value == "1"
        for key, value in state.threads_addressed_ids.items()
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)

    assert workspace is not None
    events = [
        event
        for event in workspace.events
        if event.event_type == "workspace.monitor_ci_transient_infra_waiting"
    ]
    assert len(events) == 1
    assert events[0].payload is not None
    assert events[0].payload["run_ids"] == ["25655330295"]
    assert events[0].payload["wait_count"] == 1
    wait_operations = [
        op for op in operations if (op.payload or {}).get("action") == "ci_transient_infra_wait"
    ]
    assert len(wait_operations) == 1
    assert wait_operations[0].status == OperationStatus.succeeded.value
    assert wait_operations[0].payload["rerun_attempts"] == 2
