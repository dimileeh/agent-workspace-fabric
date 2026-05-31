"""Focused PR-monitor regressions for protected owned workflow repairs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    AddressComments,
    MonitorConfig,
    MonitorState,
    ReviewComment,
    ReviewThread,
    decide,
)
from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner, fix_cycle
from awf.runtime.pr_monitor_runner.fix_cycle import (
    _requeue_workflow_scope_publish_dependent_items,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _needs_human_reason_state_key,
    _notify_human_reason,
)
from awf.runtime.pr_monitor_runner.remote_ops import _workflow_scope_push_block
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
    """Yield a database session factory for PR monitor regressions."""
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
    """Verify protected status diffs skip owned protected paths."""
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
    """Verify workflow-scope push rejections keep caller-specific terminal policy."""
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
    assert result.workflow_scope_required is True
    assert result.terminal_monitor_failure is False
    assert len(cmd.calls) == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    "stderr",
    [
        pytest.param(
            "remote: error: refusing to allow a GitHub App to create or update "
            "workflow `.github/workflows/publish.yml` because the workflows "
            "permission is required",
            id="workflow-permission-required",
        ),
        pytest.param(
            "remote: error: `.github/workflows/publish.yml` must have workflow "
            "permission\n ! [remote rejected] HEAD -> awf/ws (protected branch "
            "hook declined)",
            id="must-have-workflow-permission",
        ),
        pytest.param(
            "remote: error: workflow scope required to update "
            ".github/workflows/publish.yml\n ! [remote rejected] HEAD -> awf/ws",
            id="workflow-scope-required",
        ),
        pytest.param(
            "remote: error: missing the `workflow` scope while attempting to "
            "create or update workflow `.github/workflows/publish.yml`",
            id="missing-workflow-scope",
        ),
        pytest.param(
            "remote: error: `.github/workflows/publish.yml` lacks workflow "
            "permission\n ! [remote rejected] HEAD -> awf/ws",
            id="lacks-workflow-permission",
        ),
        pytest.param(
            "remote: error: token does not have workflow scope for "
            ".github/workflows/publish.yml\n ! [remote rejected] HEAD -> awf/ws",
            id="does-not-have-workflow-scope",
        ),
        pytest.param(
            "remote: error: token doesn't have workflow permission to create "
            "or update workflow `.github/workflows/publish.yml`",
            id="doesnt-have-workflow-permission",
        ),
        pytest.param(
            "remote: error: token has no workflow permission for workflow-file "
            ".github/workflows/publish.yml",
            id="has-no-workflow-permission",
        ),
        pytest.param(
            "remote: error: updating .github/workflows/publish.yml requires "
            "the workflow scope\n ! [remote rejected] HEAD -> awf/ws",
            id="requires-workflow-scope",
        ),
        pytest.param(
            "remote: error: workflow-file .github/workflows/publish.yml needs "
            "a workflow permission",
            id="needs-workflow-permission",
        ),
        pytest.param(
            "remote: error: create or update workflow "
            "`.github/workflows/publish.yml` must include the workflow scope",
            id="must-include-workflow-scope",
        ),
    ],
)
def test_workflow_scope_push_block_handles_alternate_github_wording(stderr: str) -> None:
    """Verify workflow-scope detection handles nearby GitHub wording variants."""
    block = _workflow_scope_push_block(stderr)

    assert block.blocked is True
    assert block.paths == (".github/workflows/publish.yml",)
    assert ".github/workflows/publish.yml" in block.message
    assert "`workflow` scope" in block.message


@pytest.mark.unit
def test_workflow_scope_push_block_ignores_unrelated_workflow_output() -> None:
    """Verify generic workflow text does not become a token-scope failure."""
    block = _workflow_scope_push_block(
        "remote: workflow validation failed without a required status check"
    )

    assert block.blocked is False


@pytest.mark.unit
async def test_fix_cycle_fetches_prompt_owned_paths_once_for_comment_batch(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify fix cycles prefetch owned paths once for a comment batch."""
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    for _ in range(4):
        adapter.queue(stdout="AWF-VERDICT: NEEDS_HUMAN: operator decision required")
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=pr_payload(threads=[]))
    cmd.queue_result(returncode=0, stderr="Everything up-to-date")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    load_count = 0

    async def _load_owned_paths(
        loaded_runner: PullRequestMonitorRunner,
        loaded_workspace_id: str,
    ) -> list[str]:
        nonlocal load_count
        assert loaded_runner is runner
        assert loaded_workspace_id == workspace_id
        load_count += 1
        return [".github/workflows/publish.yml"]

    monkeypatch.setattr(fix_cycle, "_owned_paths_for_prompt", _load_owned_paths)
    threads = (
        ReviewThread(
            thread_id="T_one",
            path=".github/workflows/publish.yml",
            line=12,
            body_excerpt="please update workflow publishing",
            author="reviewer",
        ),
        ReviewThread(
            thread_id="T_two",
            path="src/awf/runtime/example.py",
            line=3,
            body_excerpt="please check runtime behavior",
            author="reviewer",
        ),
    )
    reviews = (
        ReviewComment(
            comment_id="issue:1",
            body_excerpt="review-level workflow concern",
            author="reviewer",
        ),
        ReviewComment(
            comment_id="issue:2",
            body_excerpt="review-level runtime concern",
            author="reviewer",
        ),
    )
    state = MonitorState()

    await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=threads,
        initial_reviews=reviews,
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert load_count == 1
    assert len(adapter.calls) == 4
    assert all(".github/workflows/publish.yml" in prompt for prompt in adapter.calls)


@pytest.mark.unit
async def test_fix_cycle_stores_needs_human_reasons_for_threads_and_reviews(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Verify needs-human verdict reasons survive comment repair state."""
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(stdout="AWF-VERDICT: NEEDS_HUMAN: thread requires workflow scope")
    adapter.queue(stdout="AWF-VERDICT: NEEDS_HUMAN: review needs operator approval")
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=pr_payload(threads=[], reviews=[]))
    cmd.queue_result(returncode=0, stderr="Everything up-to-date")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    thread = ReviewThread(
        thread_id="T_needs",
        path=".github/workflows/publish.yml",
        line=12,
        body_excerpt="please update workflow publishing",
        author="reviewer",
    )
    review = ReviewComment(
        comment_id="issue:needs",
        body_excerpt="review-level workflow concern",
        author="reviewer",
    )
    state = MonitorState()

    await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(thread,),
        initial_reviews=(review,),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert state.threads_addressed_ids["T_needs"] == "needs_human"
    assert (
        state.threads_addressed_ids[_needs_human_reason_state_key("T_needs")]
        == "thread requires workflow scope"
    )
    assert state.threads_addressed_ids["issue:needs"] == "needs_human"
    assert (
        state.threads_addressed_ids[_needs_human_reason_state_key("issue:needs")]
        == "review needs operator approval"
    )


@pytest.mark.unit
async def test_workflow_scope_push_failure_requeues_fix_committed_thread_state(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Verify workflow-scope push failures keep committed fixes retryable."""
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
    assert "T_workflow" not in state.threads_addressed_ids
    assert "__review_thread_body_hash__:T_workflow" not in state.threads_addressed_ids
    assert "__needs_human_reason__:T_workflow" not in state.threads_addressed_ids

    action = decide(_status(inline=(thread,)), state, MonitorConfig())

    assert isinstance(action, AddressComments)
    assert action.threads == (thread,)
    assert action.review_comments == ()


@pytest.mark.unit
async def test_workflow_scope_push_failure_requeues_false_positive_thread_state(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Verify workflow-scope push failures requeue false-positive thread state."""
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
    assert "T_false_positive" not in state.threads_addressed_ids
    assert "__review_thread_body_hash__:T_false_positive" not in state.threads_addressed_ids
    assert "__needs_human_reason__:T_false_positive" not in state.threads_addressed_ids
    assert "T_workflow" not in state.threads_addressed_ids
    assert "__review_thread_body_hash__:T_workflow" not in state.threads_addressed_ids
    assert "__needs_human_reason__:T_workflow" not in state.threads_addressed_ids

    action = decide(
        _status(inline=(false_positive_thread, workflow_thread)),
        state,
        MonitorConfig(),
    )

    assert isinstance(action, AddressComments)
    assert action.threads == (false_positive_thread, workflow_thread)
    assert action.review_comments == ()


@pytest.mark.unit
async def test_workflow_scope_push_failure_requeues_captured_defer_thread_state(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify workflow-scope push failures requeue captured-defer thread state."""
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

    async def _capture_deferred(*_args: object, **kwargs: object) -> bool:
        assert kwargs["thread"] == deferred_thread
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
    assert "__needs_human_reason__:T_defer" not in state.threads_addressed_ids

    action = decide(_status(inline=(deferred_thread,)), state, MonitorConfig())

    assert isinstance(action, AddressComments)
    assert action.threads == (deferred_thread,)
    assert action.review_comments == ()


@pytest.mark.unit
def test_workflow_scope_requeue_clears_inline_threads_dependent_on_resolution() -> None:
    """Verify workflow-scope requeue clears inline states needing resolution."""
    deferred_issue_marker = fix_cycle._deferred_issue_filed_marker("T_defer", "defer-hash")
    state = MonitorState(
        threads_addressed_ids={
            "T_false_positive": "false_positive",
            "__review_thread_body_hash__:T_false_positive": "fp-hash",
            "T_defer": "defer",
            "__review_thread_body_hash__:T_defer": "defer-hash",
            "__defer_reason__:T_defer": "captured defer reason",
            deferred_issue_marker: "https://github.com/dimileeh/aira-web/issues/305",
            "T_workflow": "fix_committed",
            "__review_thread_body_hash__:T_workflow": "workflow-hash",
            "__needs_human_reason__:T_workflow": "old reason",
            "issue:1": "false_positive",
            "__review_comment_body_hash__:issue:1": "comment-hash",
        }
    )

    _requeue_workflow_scope_publish_dependent_items(
        state,
        ["T_false_positive", "T_defer", "T_workflow", "issue:1"],
        inline_thread_ids=["T_false_positive", "T_defer", "T_workflow"],
    )

    assert "T_false_positive" not in state.threads_addressed_ids
    assert "__review_thread_body_hash__:T_false_positive" not in state.threads_addressed_ids
    assert "__needs_human_reason__:T_false_positive" not in state.threads_addressed_ids
    assert "T_defer" not in state.threads_addressed_ids
    assert "__review_thread_body_hash__:T_defer" not in state.threads_addressed_ids
    assert "__needs_human_reason__:T_defer" not in state.threads_addressed_ids
    assert "__defer_reason__:T_defer" not in state.threads_addressed_ids
    assert state.threads_addressed_ids[deferred_issue_marker].endswith("/issues/305")
    assert "T_workflow" not in state.threads_addressed_ids
    assert "__review_thread_body_hash__:T_workflow" not in state.threads_addressed_ids
    assert "__needs_human_reason__:T_workflow" not in state.threads_addressed_ids
    assert state.threads_addressed_ids["issue:1"] == "false_positive"
    assert state.threads_addressed_ids["__review_comment_body_hash__:issue:1"] == "comment-hash"


@pytest.mark.unit
def test_notify_human_reason_prefers_stored_needs_human_reason() -> None:
    """Verify human notifications prefer stored needs-human reasons."""
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
