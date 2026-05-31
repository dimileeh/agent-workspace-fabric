"""Focused PR-monitor regressions for protected owned workflow repairs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.repositories import PRFeedbackResolutionRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    AddressComments,
    MonitorConfig,
    MonitorState,
    NotifyHuman,
    ReviewComment,
    ReviewThread,
    decide,
)
from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner, fix_cycle
from awf.runtime.pr_monitor_runner.fix_cycle import (
    _requeue_workflow_scope_publish_dependent_items,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _defer_reason_state_key,
    _needs_human_reason_state_key,
    _notify_human_reason,
    _review_comment_body_state_key,
)
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult, _workflow_scope_push_block
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    issue_comment_node,
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
async def test_git_push_result_detects_workflow_scope_rejection_across_streams(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Verify workflow-scope rejection detection scans stderr and stdout."""
    cmd = FakeCommandRunner()
    stdout = (
        "remote: refusing to allow a Personal Access Token to create or update workflow "
        "`.github/workflows/publish.yml` without `workflow` scope\n"
        " ! [remote rejected] HEAD -> awf/ws (protected branch hook declined)"
    )
    cmd.queue_result(returncode=1, stdout=stdout, stderr="remote: pre-receive hook failed\n")
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

    assert result.reason_code == "GITHUB_WORKFLOW_SCOPE_REQUIRED"
    assert result.workflow_scope_required is True
    assert ".github/workflows/publish.yml" in (result.error_message or "")
    assert len(cmd.calls) == 1


@pytest.mark.unit
async def test_git_push_result_logs_unmatched_workflow_file_push_output(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Verify workflow-file push misses are observable before generic handling."""
    cmd = FakeCommandRunner()
    stderr = "remote: repository rules rejected .github/workflows/publish.yml\n"
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

    with structlog.testing.capture_logs() as captured:
        result = await runner._git_push_result(
            worktree_path=worktree,
            remote_branch=f"awf/{workspace_id}",
        )

    assert result.reason_code == "GIT_PUSH_FAILED"
    assert any(
        entry["event"] == "monitor.push_failed_unmatched_workflow_file_context"
        and ".github/workflows/publish.yml" in entry["output"]
        for entry in captured
    )


@pytest.mark.unit
async def test_address_comments_workflow_scope_push_failure_requeues_monitor(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify workflow-scope comment repair failures keep monitoring alive."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
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
    push_result = _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr=(
            "remote: refusing to allow a Personal Access Token to create or update workflow "
            "`.github/workflows/publish.yml` without `workflow` scope"
        ),
        reason_code="GITHUB_WORKFLOW_SCOPE_REQUIRED",
    )
    terminations: list[tuple[str, str, object | None]] = []
    notifications: list[str | None] = []

    async def _workflow_scope_rejection(**kwargs: object) -> _GitPushResult:
        assert kwargs["initial_threads"] == (thread,)
        return push_result

    async def _record_termination(
        terminated_workspace_id: str,
        *,
        message: str,
        reason_code: object | None = None,
    ) -> None:
        terminations.append((terminated_workspace_id, message, reason_code))

    async def _record_notification(**kwargs: object) -> None:
        assert kwargs["repo"] == RepoRef(owner="dimileeh", name="aira-web")
        assert kwargs["pr_number"] == 42
        assert kwargs["status"].head_sha == "abc1234567890def"
        notifications.append(kwargs["blocker_reason"])

    monkeypatch.setattr(runner, "_run_fix_cycle", _workflow_scope_rejection)
    monkeypatch.setattr(runner, "_terminate_failed", _record_termination)
    monkeypatch.setattr(runner, "_post_human_notification_once", _record_notification)

    terminal = await runner._execute(
        action=AddressComments(threads=(thread,), review_comments=()),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status(head_sha="abc1234567890def", inline=(thread,)),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert terminations == []
    assert notifications == [push_result.error_message]
    assert state.iter_count == 1


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
def test_workflow_scope_push_block_handles_terse_hook_output_without_workflow_path() -> None:
    """Verify terse GitHub hook output still maps to missing workflow scope."""
    block = _workflow_scope_push_block(
        "remote: error: workflow permissions required\n"
        " ! [remote rejected] HEAD -> awf/ws (pre-receive hook declined)"
    )

    assert block.blocked is True
    assert block.paths == ()
    assert "`workflow` scope" in block.message


@pytest.mark.unit
def test_workflow_scope_push_block_ignores_unrelated_workflow_output() -> None:
    """Verify generic workflow text does not become a token-scope failure."""
    block = _workflow_scope_push_block(
        "remote: workflow validation failed without a required status check"
    )

    assert block.blocked is False


@pytest.mark.unit
def test_workflow_scope_push_block_ignores_remote_rejected_without_workflow_file_context() -> None:
    """Verify generic push rejection footers do not provide workflow-file context."""
    block = _workflow_scope_push_block(
        "remote: protected branch hook declined\n"
        "remote: runbook note: publishing workflows requires workflow scope\n"
        " ! [remote rejected] HEAD -> awf/ws (protected branch hook declined)"
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
async def test_generic_push_failure_preserves_review_comment_needs_human_after_later_pass(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Verify stale publish rollback does not clear a later review needs-human verdict."""
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(stdout="AWF-VERDICT: FIXED: initial repair committed")
    adapter.queue(stdout="AWF-VERDICT: NEEDS_HUMAN: reviewer follow-up needs operator input")
    cmd = FakeCommandRunner()
    cmd.queue_result(
        returncode=0,
        stdout=pr_payload(
            comments=[
                issue_comment_node(
                    cid=4585067239,
                    author="greptile-apps",
                    body="updated review summary now needs operator input",
                )
            ]
        ),
    )
    cmd.queue_result(returncode=0, stdout=pr_payload(comments=[]))
    cmd.queue_result(returncode=1, stderr="remote: pre-receive hook declined")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    comment = ReviewComment(
        comment_id="issue:4585067239",
        body_excerpt="initial review summary asks for a code fix",
        body="initial review summary asks for a code fix",
        author="greptile-apps",
        source_kind="issue",
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
    assert result.reason_code == "GIT_PUSH_FAILED"
    assert state.threads_addressed_ids["issue:4585067239"] == "needs_human"
    assert (
        state.threads_addressed_ids[_needs_human_reason_state_key("issue:4585067239")]
        == "reviewer follow-up needs operator input"
    )
    assert _review_comment_body_state_key("issue:4585067239") in state.threads_addressed_ids
    assert len(adapter.calls) == 2


@pytest.mark.unit
async def test_workflow_scope_push_failure_marks_fix_committed_thread_needs_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Verify workflow-scope push failures block committed fixes for humans."""
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
    expected_reason = (
        "GitHub rejected the workflow-file push because the token lacks "
        "`workflow` scope for .github/workflows/publish.yml. Grant a GitHub token "
        "with workflow push permission, then rerun the monitor repair."
    )
    assert state.threads_addressed_ids["T_workflow"] == "needs_human"
    assert "__review_thread_body_hash__:T_workflow" in state.threads_addressed_ids
    assert state.threads_addressed_ids[_needs_human_reason_state_key("T_workflow")] == (
        expected_reason
    )

    action = decide(_status(inline=(thread,)), state, MonitorConfig())

    assert isinstance(action, NotifyHuman)
    assert _notify_human_reason(_status(inline=(thread,)), state) == expected_reason


@pytest.mark.unit
async def test_workflow_scope_push_failure_requeues_false_positive_thread_state(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Verify workflow-scope failures requeue inline verdicts needing resolution."""
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
    expected_reason = (
        "GitHub rejected the workflow-file push because the token lacks "
        "`workflow` scope for .github/workflows/publish.yml. Grant a GitHub token "
        "with workflow push permission, then rerun the monitor repair."
    )
    assert "T_false_positive" not in state.threads_addressed_ids
    assert "__review_thread_body_hash__:T_false_positive" not in state.threads_addressed_ids
    assert "__needs_human_reason__:T_false_positive" not in state.threads_addressed_ids
    assert state.threads_addressed_ids["T_workflow"] == "needs_human"
    assert "__review_thread_body_hash__:T_workflow" in state.threads_addressed_ids
    assert state.threads_addressed_ids[_needs_human_reason_state_key("T_workflow")] == (
        expected_reason
    )

    action = decide(
        _status(inline=(false_positive_thread, workflow_thread)),
        state,
        MonitorConfig(),
    )

    assert isinstance(action, AddressComments)
    assert action.threads == (false_positive_thread,)
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
async def test_workflow_scope_push_failure_preserves_captured_defer_thread_state(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify workflow-scope failures preserve captured-defer inline verdicts."""
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
    assert state.threads_addressed_ids["T_defer"] == "defer"
    assert "__review_thread_body_hash__:T_defer" in state.threads_addressed_ids
    assert state.threads_addressed_ids[_defer_reason_state_key("T_defer")] == (
        "track follow-up separately"
    )
    assert "__needs_human_reason__:T_defer" not in state.threads_addressed_ids

    action = decide(_status(inline=(deferred_thread,)), state, MonitorConfig())

    assert isinstance(action, NotifyHuman)


@pytest.mark.unit
async def test_later_generic_push_failure_keeps_workflow_scope_preserved_defer_state(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify preserved defer state is not re-addressed by a later push failure."""
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(stdout="AWF-VERDICT: DEFER: track follow-up separately")
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
    deferred_thread = ReviewThread(
        thread_id="T_defer",
        path="src/awf/runtime/example.py",
        line=3,
        body_excerpt="defer this follow-up",
        author="cursor[bot]",
    )
    workflow_thread = ReviewThread(
        thread_id="T_workflow",
        path=".github/workflows/publish.yml",
        line=12,
        body_excerpt="publish workflow still needs the reviewed fix",
        author="cursor[bot]",
    )
    later_thread = ReviewThread(
        thread_id="T_later",
        path="src/awf/runtime/later.py",
        line=9,
        body_excerpt="new follow-up after credential rotation",
        author="cursor[bot]",
    )
    state = MonitorState()
    captured_threads: list[str] = []

    async def _capture_deferred(*_args: object, **kwargs: object) -> bool:
        captured_threads.append(kwargs["thread"].thread_id)
        return True

    monkeypatch.setattr(fix_cycle, "_capture_deferred_review_thread", _capture_deferred)

    workflow_scope_result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(deferred_thread, workflow_thread),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert workflow_scope_result.reason_code == "GITHUB_WORKFLOW_SCOPE_REQUIRED"
    assert captured_threads == ["T_defer"]
    assert state.threads_addressed_ids["T_defer"] == "defer"
    assert state.threads_addressed_ids["T_workflow"] == "needs_human"

    action = decide(
        _status(inline=(deferred_thread, workflow_thread, later_thread)),
        state,
        MonitorConfig(),
    )

    assert isinstance(action, AddressComments)
    assert action.threads == (later_thread,)

    adapter.queue(stdout="AWF-VERDICT: FIXED: handled later follow-up")
    cmd.queue_result(returncode=0, stdout=pr_payload(threads=[]))
    cmd.queue_result(returncode=1, stderr="remote: pre-receive hook declined")

    generic_push_result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=action.threads,
        initial_reviews=action.review_comments,
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert generic_push_result.reason_code == "GIT_PUSH_FAILED"
    assert captured_threads == ["T_defer"]
    assert state.threads_addressed_ids["T_defer"] == "defer"
    assert "__review_thread_body_hash__:T_defer" in state.threads_addressed_ids
    assert state.threads_addressed_ids[_defer_reason_state_key("T_defer")] == (
        "track follow-up separately"
    )
    assert "T_later" not in state.threads_addressed_ids
    assert len(adapter.calls) == 3


@pytest.mark.unit
def test_workflow_scope_requeue_marks_publish_dependent_fixes_needs_human() -> None:
    """Verify workflow-scope failures store per-item human-blocking reasons."""
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
            "issue:fixed": "fix_committed",
            "__review_comment_body_hash__:issue:fixed": "fixed-comment-hash",
        }
    )
    expected_reason = (
        "GitHub rejected the workflow-file push because the token lacks "
        "`workflow` scope for .github/workflows/publish.yml. Grant a GitHub token "
        "with workflow push permission, then rerun the monitor repair."
    )

    _requeue_workflow_scope_publish_dependent_items(
        state,
        ["T_workflow", "issue:fixed"],
        resolution_dependent_ids=["T_false_positive"],
        reason=expected_reason,
    )

    assert "T_false_positive" not in state.threads_addressed_ids
    assert "__review_thread_body_hash__:T_false_positive" not in state.threads_addressed_ids
    assert "__needs_human_reason__:T_false_positive" not in state.threads_addressed_ids
    assert state.threads_addressed_ids["T_defer"] == "defer"
    assert state.threads_addressed_ids["__review_thread_body_hash__:T_defer"] == "defer-hash"
    assert "__needs_human_reason__:T_defer" not in state.threads_addressed_ids
    assert state.threads_addressed_ids["__defer_reason__:T_defer"] == "captured defer reason"
    assert state.threads_addressed_ids[deferred_issue_marker].endswith("/issues/305")
    assert state.threads_addressed_ids["T_workflow"] == "needs_human"
    assert state.threads_addressed_ids["__review_thread_body_hash__:T_workflow"] == (
        "workflow-hash"
    )
    assert state.threads_addressed_ids[_needs_human_reason_state_key("T_workflow")] == (
        expected_reason
    )
    assert state.threads_addressed_ids["issue:1"] == "false_positive"
    assert state.threads_addressed_ids["__review_comment_body_hash__:issue:1"] == "comment-hash"
    assert state.threads_addressed_ids["issue:fixed"] == "needs_human"
    assert state.threads_addressed_ids["__review_comment_body_hash__:issue:fixed"] == (
        "fixed-comment-hash"
    )
    assert state.threads_addressed_ids[_needs_human_reason_state_key("issue:fixed")] == (
        expected_reason
    )


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
