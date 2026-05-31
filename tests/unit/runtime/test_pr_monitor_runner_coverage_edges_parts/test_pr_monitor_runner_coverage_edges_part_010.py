"""Continuation coverage tests for PR monitor runner comment repair edges."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.enums import OperationStatus, WorkspaceStatus
from awf.db.repositories import (
    OperationRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    AddressComments,
    MonitorConfig,
    MonitorState,
    ReviewThread,
    decide,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
)

from .test_pr_monitor_runner_coverage_edges_part_003 import _status_for_helpers


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Create an isolated async session factory for monitor runner tests."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_monitor_comment_repair_workflow_scope_failure_requeues_fix_without_terminating(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Verify workflow-scope comment repair failures leave fixes retryable."""
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(stdout="fixed locally")
    workspace_id = await seed_monitoring_workspace(factory)
    thread = ReviewThread(
        thread_id="T_workflow_scope",
        path=".github/workflows/publish.yml",
        line=12,
        body_excerpt="publish workflow needs the reviewed fix",
        author="reviewer",
    )
    cmd.queue_result(returncode=0, stdout=pr_payload())
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
    state = MonitorState()

    terminal = await runner._execute(
        action=AddressComments(threads=(thread,), review_comments=()),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(threads=(thread,)),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert state.iter_count == 1
    assert "T_workflow_scope" not in state.threads_addressed_ids
    assert "__review_thread_body_hash__:T_workflow_scope" not in state.threads_addressed_ids
    assert "__needs_human_reason__:T_workflow_scope" not in state.threads_addressed_ids
    action = decide(_status_for_helpers(threads=(thread,)), state, MonitorConfig())
    assert isinstance(action, AddressComments)
    assert action.threads == (thread,)
    assert action.review_comments == ()
    comment_calls = [call for call in cmd.calls if call.args[:3] == ["gh", "pr", "comment"]]
    assert len(comment_calls) == 1
    body = comment_calls[0].args[comment_calls[0].args.index("--body") + 1]
    assert "GitHub rejected the workflow-file push" in body
    assert "`workflow` scope for .github/workflows/publish.yml" in body
    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id, limit=20)
        push_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )
        resolution_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.comment_resolution",
            limit=10,
        )

    assert workspace is not None
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    comment_operation = next(
        operation for operation in operations if operation.type == "comment_repair"
    )
    assert comment_operation.status == OperationStatus.failed.value
    assert comment_operation.error_code == "GITHUB_WORKFLOW_SCOPE_REQUIRED"
    assert len(push_events) == 1
    assert push_events[0].reason_code == "GITHUB_WORKFLOW_SCOPE_REQUIRED"
    assert push_events[0].payload is not None
    assert push_events[0].payload["action"] == "comment_repair_push"
    assert push_events[0].payload["outcome"] == "failed"
    assert push_events[0].payload["operation_id"] == comment_operation.id
    assert push_events[0].payload["operation_type"] == "comment_repair"
    assert push_events[0].payload["evidence"]["reason_code"] == ("GITHUB_WORKFLOW_SCOPE_REQUIRED")
    assert resolution_events == []


@pytest.mark.unit
async def test_monitor_comment_repair_workflow_scope_notification_failure_keeps_fix_retryable(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Notification failures must not mask retryable workflow-scope push state."""
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(stdout="fixed locally")
    workspace_id = await seed_monitoring_workspace(factory)
    thread = ReviewThread(
        thread_id="T_workflow_scope",
        path=".github/workflows/publish.yml",
        line=12,
        body_excerpt="publish workflow needs the reviewed fix",
        author="reviewer",
    )
    cmd.queue_result(returncode=0, stdout=pr_payload())
    cmd.queue_result(
        returncode=1,
        stderr=(
            "remote: refusing to allow a Personal Access Token to create or update workflow "
            "`.github/workflows/publish.yml` without `workflow` scope"
        ),
    )
    cmd.queue_result(returncode=1, stderr="bad credentials")  # gh pr comment notification
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=AddressComments(threads=(thread,), review_comments=()),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(threads=(thread,)),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert state.iter_count == 1
    assert "T_workflow_scope" not in state.threads_addressed_ids
    assert "__review_thread_body_hash__:T_workflow_scope" not in state.threads_addressed_ids
    assert "__needs_human_reason__:T_workflow_scope" not in state.threads_addressed_ids
    action = decide(_status_for_helpers(threads=(thread,)), state, MonitorConfig())
    assert isinstance(action, AddressComments)
    assert action.threads == (thread,)
    assert action.review_comments == ()
    comment_calls = [call for call in cmd.calls if call.args[:3] == ["gh", "pr", "comment"]]
    assert len(comment_calls) == 1
    async with factory() as s:
        workspace = await WorkspaceRepository(s).get(workspace_id)
        operations = await OperationRepository(s).list_all(workspace_id=workspace_id, limit=20)
        push_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )

    assert workspace is not None
    assert workspace.status == WorkspaceStatus.monitoring_pr.value
    comment_operation = next(
        operation for operation in operations if operation.type == "comment_repair"
    )
    assert comment_operation.status == OperationStatus.failed.value
    assert comment_operation.error_code == "GITHUB_WORKFLOW_SCOPE_REQUIRED"
    assert comment_operation.result is not None
    assert comment_operation.result["outcome"] == "github_workflow_scope_required"
    assert comment_operation.result["reason_code"] == "GITHUB_WORKFLOW_SCOPE_REQUIRED"
    assert len(push_events) == 1
    assert push_events[0].reason_code == "GITHUB_WORKFLOW_SCOPE_REQUIRED"
    assert push_events[0].payload is not None
    assert push_events[0].payload["action"] == "comment_repair_push"
    assert push_events[0].payload["outcome"] == "failed"
    assert push_events[0].payload["operation_id"] == comment_operation.id
    assert push_events[0].payload["operation_type"] == "comment_repair"
    assert push_events[0].payload["evidence"]["reason_code"] == "GITHUB_WORKFLOW_SCOPE_REQUIRED"
