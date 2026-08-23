"""Focused PR-monitor regressions for workflow-scope push requeue (part 22)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.repositories import PRFeedbackResolutionRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    AddressComments,
    MonitorConfig,
    MonitorState,
    ReviewComment,
    ReviewThread,
    _review_thread_body_hash,
    decide,
)
from awf.runtime.pr_monitor_runner import fix_cycle
from awf.runtime.pr_monitor_runner.helpers import (
    _defer_reason_state_key,
    _review_comment_body_state_key,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
    thread_node,
)
from tests.unit.runtime.test_pr_monitor import _status


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield a database session factory for PR monitor regressions."""
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_workflow_scope_push_failure_honors_latest_false_positive_thread_verdict(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify stale fix_committed workflow bookkeeping does not override re-triage."""
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(stdout="AWF-VERDICT: FIXED: updated publish workflow")
    adapter.queue(stdout="AWF-VERDICT: FALSE POSITIVE: reviewer follow-up is already handled")
    cmd = FakeCommandRunner()
    cmd.queue_result(
        returncode=0,
        stdout=pr_payload(
            threads=[
                thread_node(
                    tid="T_multi",
                    author="cursor[bot]",
                    path=".github/workflows/publish.yml",
                    line=12,
                    body="reviewer follow-up is already handled",
                )
            ]
        ),
    )
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

    async def _commit_dirty(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty)
    initial_thread = ReviewThread(
        thread_id="T_multi",
        path=".github/workflows/publish.yml",
        line=12,
        body_excerpt="publish workflow still needs the reviewed fix",
        author="cursor[bot]",
    )
    latest_thread = ReviewThread(
        thread_id="T_multi",
        path=".github/workflows/publish.yml",
        line=12,
        body_excerpt="reviewer follow-up is already handled",
        author="cursor[bot]",
    )
    state = MonitorState()

    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(initial_thread,),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "GITHUB_WORKFLOW_SCOPE_REQUIRED"
    assert len(adapter.calls) == 2
    assert "T_multi" not in state.threads_addressed_ids
    assert "__review_thread_body_hash__:T_multi" not in state.threads_addressed_ids
    assert "__needs_human_reason__:T_multi" not in state.threads_addressed_ids

    action = decide(_status(inline=(latest_thread,)), state, MonitorConfig())

    assert isinstance(action, AddressComments)
    assert action.threads == (latest_thread,)
    assert action.review_comments == ()


@pytest.mark.unit
async def test_workflow_scope_push_failure_preserves_false_positive_review_comment_resolution(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Verify workflow-scope pushes preserve durable false-positive review verdicts."""
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(stdout="AWF-VERDICT: FALSE POSITIVE: existing code already handles it")
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=pr_payload(reviews=[]))
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
    comment = ReviewComment(
        comment_id="issue:workflow",
        body="Review-level workflow concern",
        body_excerpt="Review-level workflow concern",
        author="chatgpt-codex-connector[bot]",
        url="https://github.example/comment/issue-workflow",
    )
    state = MonitorState()

    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(),
        initial_reviews=(comment,),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "GITHUB_WORKFLOW_SCOPE_REQUIRED"
    assert state.threads_addressed_ids["issue:workflow"] == "false_positive"
    assert "__review_comment_body_hash__:issue:workflow" in state.threads_addressed_ids
    assert "__needs_human_reason__:issue:workflow" not in state.threads_addressed_ids

    async with factory() as session:
        rows = await PRFeedbackResolutionRepository(session).list_for_pr(
            scm_provider="github",
            repository_key="dimileeh/aira-web",
            pull_request_key="42",
        )

    assert len(rows) == 1
    row = rows[0]
    assert row.feedback_kind == "review_comment"
    assert row.feedback_id == "issue:workflow"
    assert row.head_sha == "abc1234567890def"
    assert row.verdict == "false_positive"
    assert row.reason == "existing code already handles it"

    changed = await runner._apply_pr_feedback_resolution_state(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status(reviews=(comment,)),
        state=state,
    )

    assert changed is False
    assert state.threads_addressed_ids["issue:workflow"] == "false_positive"
    assert _review_comment_body_state_key("issue:workflow") in state.threads_addressed_ids

    action = decide(_status(reviews=(comment,)), state, MonitorConfig())

    assert not isinstance(action, AddressComments)


@pytest.mark.unit
async def test_workflow_scope_push_failure_requeues_captured_defer_thread_state(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify workflow-scope failures retry deferred thread resolution later."""
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(stdout="AWF-VERDICT: DEFER: track follow-up separately")
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
    deferred_thread = ReviewThread(
        thread_id="T_defer",
        path="src/awf/runtime/example.py",
        line=3,
        body_excerpt="defer this follow-up",
        author="cursor[bot]",
    )
    state = MonitorState()
    deferred_issue_marker = fix_cycle._deferred_issue_filed_marker(
        deferred_thread.thread_id,
        _review_thread_body_hash(deferred_thread),
    )

    async def _capture_deferred(*_args: object, **kwargs: object) -> bool:
        assert kwargs["thread"] == deferred_thread
        state.mark_addressed(deferred_issue_marker, "https://github.example/issues/305")
        return True

    monkeypatch.setattr(fix_cycle, "_capture_deferred_review_thread", _capture_deferred)

    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(deferred_thread,),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "GITHUB_WORKFLOW_SCOPE_REQUIRED"
    assert "T_defer" not in state.threads_addressed_ids
    assert "__review_thread_body_hash__:T_defer" not in state.threads_addressed_ids
    assert _defer_reason_state_key("T_defer") not in state.threads_addressed_ids
    assert "__needs_human_reason__:T_defer" not in state.threads_addressed_ids
    assert state.threads_addressed_ids[deferred_issue_marker].endswith("/issues/305")

    action = decide(_status(inline=(deferred_thread,)), state, MonitorConfig())

    assert isinstance(action, AddressComments)
    assert action.threads == (deferred_thread,)
