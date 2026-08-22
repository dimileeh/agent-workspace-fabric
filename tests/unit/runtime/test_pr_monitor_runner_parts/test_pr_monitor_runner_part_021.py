"""Workflow-scope requeue and notify-human regressions split from part_006."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

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
    _review_thread_body_hash,
    decide,
)
from awf.runtime.pr_monitor_runner import fix_cycle
from awf.runtime.pr_monitor_runner.comments import (
    VerdictResult,
    _address_review_comment_result,
    _address_thread,
)
from awf.runtime.pr_monitor_runner.fix_cycle import (
    _requeue_workflow_scope_publish_dependent_items,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _defer_reason_state_key,
    _notify_human_reason,
)
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


@pytest.mark.unit
async def test_later_generic_push_failure_keeps_workflow_scope_requeued_defer_retryable(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify requeued defers stay retryable without duplicate capture issues."""
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

    async def _commit_dirty(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(runner, "_commit_dirty_worktree", _commit_dirty)
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
        thread = kwargs["thread"]
        marker = fix_cycle._deferred_issue_filed_marker(
            thread.thread_id,
            _review_thread_body_hash(thread),
        )
        if marker not in state.threads_addressed_ids:
            captured_threads.append(thread.thread_id)
            state.mark_addressed(marker, f"https://github.example/issues/{len(captured_threads)}")
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
    assert "T_defer" not in state.threads_addressed_ids
    assert "T_workflow" not in state.threads_addressed_ids
    assert "__review_thread_body_hash__:T_workflow" not in state.threads_addressed_ids

    action = decide(
        _status(inline=(deferred_thread, workflow_thread, later_thread)),
        state,
        MonitorConfig(),
    )

    assert isinstance(action, AddressComments)
    assert action.threads == (deferred_thread, workflow_thread, later_thread)

    adapter.queue(stdout="AWF-VERDICT: DEFER: track follow-up separately")
    adapter.queue(stdout="AWF-VERDICT: FIXED: updated publish workflow")
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
    assert "T_defer" not in state.threads_addressed_ids
    assert "__review_thread_body_hash__:T_defer" not in state.threads_addressed_ids
    assert _defer_reason_state_key("T_defer") not in state.threads_addressed_ids
    assert any(
        key.startswith("__deferred_issue_filed__:T_defer:") for key in state.threads_addressed_ids
    )
    assert "T_workflow" not in state.threads_addressed_ids
    assert "T_later" not in state.threads_addressed_ids
    assert len(adapter.calls) == 5


@pytest.mark.unit
def test_workflow_scope_requeue_clears_publish_dependent_fixes() -> None:
    """Verify workflow-scope failures keep publish-dependent fixes retryable."""
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
        resolution_dependent_ids=["T_false_positive", "T_defer"],
        reason=expected_reason,
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
    assert "issue:fixed" not in state.threads_addressed_ids
    assert "__review_comment_body_hash__:issue:fixed" not in state.threads_addressed_ids
    assert "__needs_human_reason__:issue:fixed" not in state.threads_addressed_ids


@pytest.mark.unit
async def test_direct_comment_repair_propagates_owned_path_lookup_failure_before_cli(
    tmp_path: Path,
) -> None:
    """Verify direct comment-repair callers do not prompt without owned paths."""
    thread = ReviewThread(
        thread_id="T_owned_paths",
        path="src/awf/runtime/example.py",
        line=12,
        body_excerpt="please address this",
        author="cursor[bot]",
    )
    review = ReviewComment(
        comment_id="issue:owned_paths",
        body_excerpt="please address this review comment",
        author="cursor[bot]",
    )
    prompts: list[str] = []

    def _broken_session_factory() -> object:
        raise TypeError("legacy test double")

    async def _invoke_cli_for_verdict_result(**kwargs: object) -> VerdictResult:
        prompts.append(str(kwargs["prompt"]))
        return VerdictResult(verdict="false_positive", reason="already handled")

    runner = SimpleNamespace(
        _deps=SimpleNamespace(session_factory=_broken_session_factory),
        _workspace_runtime_context="",
        _invoke_cli_for_verdict_result=_invoke_cli_for_verdict_result,
    )

    with pytest.raises(TypeError, match="legacy test double"):
        await _address_thread(
            runner,  # type: ignore[arg-type]
            workspace_id="ws_prompt_fallback",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            thread=thread,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            state=MonitorState(),
        )
    with pytest.raises(TypeError, match="legacy test double"):
        await _address_review_comment_result(
            runner,  # type: ignore[arg-type]
            workspace_id="ws_prompt_fallback",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            comment=review,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            state=MonitorState(),
        )

    assert prompts == []


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


@pytest.mark.unit
def test_notify_human_reason_preserves_opaque_stored_reason() -> None:
    """Verify verdict reasons remain opaque instead of being interpreted."""
    thread = ReviewThread(
        thread_id="T_checkout",
        path="apps/api/checkout_policy.py",
        line=102,
        body_excerpt="policy tradeoff still needs a decision",
        author="cursor[bot]",
    )
    state = MonitorState(
        threads_addressed_ids={
            "T_checkout": "needs_human",
            "__needs_human_reason__:T_checkout": '<what you need> and exit."',
        }
    )

    assert _notify_human_reason(_status(inline=(thread,)), state) == '<what you need> and exit."'
