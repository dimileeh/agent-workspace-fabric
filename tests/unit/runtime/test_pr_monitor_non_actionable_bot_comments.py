"""Regression tests for non-actionable bot review boilerplate.

CodeRabbit can report "review skipped" / disabled-review status as a
review-level body rather than a top-level PR comment. AWF must ignore that
boilerplate without ending the monitor or hiding later actionable feedback.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.base import Base
from awf.db.enums import OperationType, WorkspaceStatus
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.runtime.pr_monitor import Merge, MonitorState, decide
from awf.runtime.pr_monitor_runner import _initial_review_grace_started_key
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    issue_comment_node,
    make_runner,
    pr_payload,
    review_node,
    seed_monitoring_workspace,
)

REPO_URL = "git@github.com:dimileeh/aira-web.git"
REPO = RepoRef.from_url(REPO_URL)


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'awf.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


def _disabled_review_boilerplate() -> dict:
    return review_node(
        cid=7801,
        author="coderabbitai",
        body=(
            "> [!IMPORTANT]\n"
            "> ## Review skipped\n\n"
            "Auto reviews are disabled on base/target branches other than "
            "the configured development branch.\n\n"
            "- [ ] Trigger review"
        ),
    )


def _disabled_issue_comment_boilerplate() -> dict:
    return issue_comment_node(
        cid=7803,
        author="coderabbitai",
        body=(
            "> [!IMPORTANT]\n"
            "> ## Review skipped\n\n"
            "Auto reviews are disabled on base/target branches other than "
            "the configured development branch.\n\n"
            "- [ ] Trigger review"
        ),
    )


def _late_actionable_review() -> dict:
    return review_node(
        cid=7802,
        author="human-reviewer",
        body="late actionable review: document the monitor behavior before merging.",
    )


def _action_entries(records: list[dict]) -> list[dict]:
    return [record for record in records if record.get("event") == "monitor.action"]


async def _workspace_status(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> str:
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        return workspace.status


@pytest.mark.unit
async def test_only_non_actionable_bot_review_body_does_not_trigger_human_wait(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    sleep_fn = RecordedSleep()
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(
        returncode=0,
        stdout=pr_payload(reviews=[_disabled_review_boilerplate()]),
    )
    cmd.queue_result(returncode=0)  # gh pr merge
    cmd.queue_result(returncode=0, stdout="merge-sha\n")  # merge commit lookup
    cmd.queue_result(returncode=0)  # docker compose down
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=tmp_path / "artifacts",
        initial_review_grace_period_seconds=0,
    )

    with structlog.testing.capture_logs() as captured:
        await runner.run(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )

    actions = [entry["action"] for entry in _action_entries(captured)]
    assert actions == ["Merge"]
    assert adapter.calls == []
    assert not any(call.args[:3] == ["gh", "pr", "comment"] for call in cmd.calls)
    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    assert not any(operation.type == OperationType.human_wait.value for operation in operations)


@pytest.mark.unit
async def test_only_non_actionable_bot_issue_comment_does_not_trigger_human_wait(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    sleep_fn = RecordedSleep()
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(
        returncode=0,
        stdout=pr_payload(comments=[_disabled_issue_comment_boilerplate()]),
    )
    cmd.queue_result(returncode=0)  # gh pr merge
    cmd.queue_result(returncode=0, stdout="merge-sha\n")  # merge commit lookup
    cmd.queue_result(returncode=0)  # docker compose down
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=tmp_path / "artifacts",
        initial_review_grace_period_seconds=0,
    )

    with structlog.testing.capture_logs() as captured:
        await runner.run(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )

    actions = [entry["action"] for entry in _action_entries(captured)]
    assert actions == ["Merge"]
    assert adapter.calls == []
    assert not any(call.args[:3] == ["gh", "pr", "comment"] for call in cmd.calls)
    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    assert not any(operation.type == OperationType.human_wait.value for operation in operations)


@pytest.mark.unit
async def test_non_actionable_bot_review_body_during_initial_grace_waits_without_merge(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    sleep_fn = RecordedSleep()
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(
        returncode=0,
        stdout=pr_payload(reviews=[_disabled_review_boilerplate()]),
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=tmp_path / "artifacts",
        initial_review_grace_period_seconds=900,
    )
    status = await runner._fetch_status_for_decision(
        repo=REPO,
        pr_number=42,
        workspace_id=workspace_id,
        base_branch="development",
    )
    state = MonitorState()
    action = decide(status, state, runner._config)
    assert isinstance(action, Merge)

    terminal = await runner._execute(
        action=action,
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=REPO,
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
    assert sleep_fn.calls == [60]
    assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
    assert _initial_review_grace_started_key(42) in state.threads_addressed_ids
    assert await _workspace_status(factory, workspace_id) == WorkspaceStatus.monitoring_pr.value


@pytest.mark.unit
async def test_later_actionable_review_after_ignored_boilerplate_routes_to_address_comments(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    disabled = _disabled_review_boilerplate()
    actionable = _late_actionable_review()
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    sleep_fn = RecordedSleep()
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(reviews=[disabled]))
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(reviews=[disabled, actionable]))
    adapter.queue(stdout="fixed")
    cmd.queue_result(returncode=0, stdout=pr_payload(reviews=[disabled]))  # settle fetch
    cmd.queue_result(returncode=0, stderr="Everything up-to-date")  # git push
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))
    cmd.queue_result(returncode=0)  # docker compose down
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=tmp_path / "artifacts",
        initial_review_grace_period_seconds=900,
    )

    with structlog.testing.capture_logs() as captured:
        await runner.run(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )

    actions = [entry["action"] for entry in _action_entries(captured)]
    assert actions == ["Merge", "AddressComments", "ShortCircuitCompleted"]
    assert len(adapter.calls) == 1
    assert "late actionable review" in adapter.calls[0]
    assert "Auto reviews are disabled" not in adapter.calls[0]
    assert not any(call.args[:3] == ["gh", "pr", "comment"] for call in cmd.calls)


@pytest.mark.unit
async def test_ignored_boilerplate_does_not_complete_monitor_by_itself(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    artifacts_root = tmp_path / "artifacts"
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    sleep_fn = RecordedSleep()
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(
        returncode=0,
        stdout=pr_payload(reviews=[_disabled_review_boilerplate()]),
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=artifacts_root,
        initial_review_grace_period_seconds=900,
    )
    status = await runner._fetch_status_for_decision(
        repo=REPO,
        pr_number=42,
        workspace_id=workspace_id,
        base_branch="development",
    )
    state = MonitorState()
    action = decide(status, state, runner._config)
    assert isinstance(action, Merge)

    terminal = await runner._execute(
        action=action,
        workspace_id=workspace_id,
        repo_url=REPO_URL,
        repo=REPO,
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
    assert await _workspace_status(factory, workspace_id) == WorkspaceStatus.monitoring_pr.value
    assert not (artifacts_root / f"{workspace_id}.defer-signal.json").exists()
    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    assert operations == []
