"""Focused PR-monitor regressions for protected owned workflow repairs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import MonitorState, ReviewThread
from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner
from awf.runtime.pr_monitor_runner.fix_cycle import _mark_publish_dependent_items_needs_human
from awf.runtime.pr_monitor_runner.helpers import _notify_human_reason
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
)
from tests.unit.runtime.test_pr_monitor import _status


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _monitor_runner(tmp_path: Path, fake: FakeCommandRunner) -> PullRequestMonitorRunner:
    return PullRequestMonitorRunner(
        session_factory=object(),  # type: ignore[arg-type]
        runner=fake,
        adapter=object(),  # type: ignore[arg-type]
        gh=object(),  # type: ignore[arg-type]
        worktrees_root=tmp_path / "work" / "git" / "worktrees",
    )


@pytest.mark.unit
async def test_protected_status_diff_skips_owned_protected_paths(
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout='[project]\nname = "demo"\n')
    runner = _monitor_runner(tmp_path, cmd)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "pyproject.toml").write_text('[project]\nname = "demo2"\n')

    diffs = await runner._protected_file_diffs_for_status_paths(
        worktree_path=worktree,
        changed_paths=[".github/workflows/publish.yml", "pyproject.toml"],
        owned_paths=[".github/workflows/publish.yml"],
    )

    assert set(diffs) == {"pyproject.toml"}
    assert all(".github/workflows/publish.yml" not in call.args for call in cmd.calls)


@pytest.mark.unit
async def test_git_push_result_maps_github_workflow_scope_rejection(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    stderr = (
        "remote: refusing to allow a Personal Access Token to create or update workflow "
        "`.github/workflows/publish.yml` without `workflow` scope\n"
        " ! [remote rejected] HEAD -> awf/ws (protected branch hook declined)"
    )
    cmd.queue_result(returncode=1, stderr=stderr)
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)

    result = await runner._git_push_result(
        worktree_path=worktree,
        remote_branch=f"awf/{workspace_id}",
    )

    assert result.failed is True
    assert result.reason_code == "GITHUB_WORKFLOW_SCOPE_REQUIRED"
    assert result.error_message is not None
    assert ".github/workflows/publish.yml" in result.error_message
    assert "`workflow` scope" in result.error_message
    assert len(cmd.calls) == 1


@pytest.mark.unit
async def test_workflow_scope_push_failure_preserves_needs_human_thread_state(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(stdout="AWF-VERDICT: FIXED: updated publish workflow")
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=pr_payload(threads=[]))
    cmd.queue_result(
        returncode=1,
        stderr=(
            "remote: refusing to allow a Personal Access Token to create or update workflow "
            "`.github/workflows/publish.yml` without `workflow` scope"
        ),
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    thread = ReviewThread(
        thread_id="T_workflow",
        path=".github/workflows/publish.yml",
        line=12,
        body_excerpt="publish workflow still needs the reviewed fix",
        author="cursor[bot]",
    )
    state = MonitorState()

    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(thread,),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "GITHUB_WORKFLOW_SCOPE_REQUIRED"
    assert state.threads_addressed_ids["T_workflow"] == "needs_human"
    reason = state.threads_addressed_ids["__needs_human_reason__:T_workflow"]
    assert ".github/workflows/publish.yml" in reason
    assert "`workflow` scope" in reason
    assert "protected file approval required" not in reason


@pytest.mark.unit
async def test_workflow_scope_push_failure_preserves_false_positive_thread_state(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(stdout="AWF-VERDICT: FALSE POSITIVE: reviewer misread the diff")
    adapter.queue(stdout="AWF-VERDICT: FIXED: updated publish workflow")
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=pr_payload(threads=[]))
    cmd.queue_result(
        returncode=1,
        stderr=(
            "remote: refusing to allow a Personal Access Token to create or update workflow "
            "`.github/workflows/publish.yml` without `workflow` scope"
        ),
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    false_positive_thread = ReviewThread(
        thread_id="T_false_positive",
        path="src/awf/runtime/example.py",
        line=3,
        body_excerpt="this concern is already handled",
        author="cursor[bot]",
    )
    workflow_thread = ReviewThread(
        thread_id="T_workflow",
        path=".github/workflows/publish.yml",
        line=12,
        body_excerpt="publish workflow still needs the reviewed fix",
        author="cursor[bot]",
    )
    state = MonitorState()

    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(false_positive_thread, workflow_thread),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "GITHUB_WORKFLOW_SCOPE_REQUIRED"
    assert state.threads_addressed_ids["T_false_positive"] == "false_positive"
    assert "__needs_human_reason__:T_false_positive" not in state.threads_addressed_ids
    assert state.threads_addressed_ids["T_workflow"] == "needs_human"
    reason = state.threads_addressed_ids["__needs_human_reason__:T_workflow"]
    assert ".github/workflows/publish.yml" in reason
    assert "`workflow` scope" in reason


@pytest.mark.unit
def test_workflow_scope_needs_human_marking_preserves_non_fix_verdicts() -> None:
    state = MonitorState(
        threads_addressed_ids={
            "T_false_positive": "false_positive",
            "T_defer": "defer",
            "T_workflow": "fix_committed",
        }
    )

    _mark_publish_dependent_items_needs_human(
        state,
        ["T_false_positive", "T_defer", "T_workflow"],
        "GitHub rejected the workflow push because the token lacks `workflow` scope.",
    )

    assert state.threads_addressed_ids["T_false_positive"] == "false_positive"
    assert "__needs_human_reason__:T_false_positive" not in state.threads_addressed_ids
    assert state.threads_addressed_ids["T_defer"] == "defer"
    assert "__needs_human_reason__:T_defer" not in state.threads_addressed_ids
    assert state.threads_addressed_ids["T_workflow"] == "needs_human"
    assert state.threads_addressed_ids["__needs_human_reason__:T_workflow"] == (
        "GitHub rejected the workflow push because the token lacks `workflow` scope."
    )


@pytest.mark.unit
def test_notify_human_reason_prefers_stored_needs_human_reason() -> None:
    thread = ReviewThread(
        thread_id="T_workflow",
        path=".github/workflows/publish.yml",
        line=12,
        body_excerpt="publish workflow still needs the reviewed fix",
        author="cursor[bot]",
    )
    state = MonitorState(
        threads_addressed_ids={
            "T_workflow": "needs_human",
            "__needs_human_reason__:T_workflow": (
                "GitHub rejected the workflow-file push because the token lacks "
                "`workflow` scope for .github/workflows/publish.yml."
            ),
        }
    )

    assert _notify_human_reason(_status(inline=(thread,)), state) == (
        "GitHub rejected the workflow-file push because the token lacks "
        "`workflow` scope for .github/workflows/publish.yml."
    )
