"""Unit tests for focused ``pr_monitor_runner`` behavior.

Most cases cover the pure, side-effect-free helpers: ``_parse_verdict`` (CLI
reply → structured verdict) and ``_collect_defer_items`` (PRStatus +
MonitorState → bot/human defer buckets for the terminal artifact). Focused
runtime-path regressions live here when the unit suite needs to cover a
specific merge-gate branch without running the full monitor integration loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunError
from awf.adapters.provider_failures import AGENT_PROVIDER_CAPACITY_EXHAUSTED
from awf.common.bitbucket_client import BitbucketAuth, BitbucketClient
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.github_client import GitHubClient, GitHubClientError, RepoRef
from awf.db.enums import (
    AgentRuntime,
    WorkspaceStatus,
)
from awf.db.models import Operation, Workspace
from awf.db.repositories import (
    PRFeedbackResolutionRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.monitor_state_keys import _outdated_resolve_requeued_key
from awf.runtime.pr_monitor import (
    CheckFailure,
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    ReviewComment,
    ReviewThread,
    ReviewThreadComment,
    _mark_review_thread_addressed,
    _review_thread_body_state_key,
)
from awf.runtime.pr_monitor_runner.comments import VerdictResult
from awf.runtime.pr_monitor_runner.helpers import (
    _drop_stale_review_comment_addressed_state,
    _drop_stale_review_thread_addressed_state,
    _mark_review_comment_addressed,
    _pending_review_feedback_count,
    _review_comment_body_state_key,
)
from awf.runtime.pr_monitor_runner.types import (
    BaseFetchError,
    ProviderRecoveryRetryError,
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


class _CapturingGH:
    def __init__(self, status: PRStatus | None = None) -> None:
        self.status = status or _green_status()
        self.base_behind_counts: list[int] = []
        self.failing_log_requests: list[tuple[RepoRef, int, str, tuple[str, ...]]] = []
        self.posted_comments: list[tuple[RepoRef, int, str]] = []
        self.post_errors: list[GitHubClientError] = []
        self.closed = False

    async def fetch_pr_status(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        base_behind_count: int,
    ) -> PRStatus:
        del repo, pr_number
        self.base_behind_counts.append(base_behind_count)
        return replace(self.status, base_behind_count=base_behind_count)

    async def fetch_failing_check_logs(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        head_sha: str,
        pytest_fallback_commands: Sequence[str] = (),
    ) -> tuple[CheckFailure, ...]:
        self.failing_log_requests.append(
            (repo, pr_number, head_sha, tuple(pytest_fallback_commands))
        )
        return ()

    async def post_comment(self, *, repo: RepoRef, pr_number: int, body: str) -> None:
        if self.post_errors:
            raise self.post_errors.pop(0)
        self.posted_comments.append((repo, pr_number, body))

    async def aclose(self) -> None:
        # The runner closes its forge client in run()'s finally; record it so the
        # leak-fix regression test can assert the client was released.
        self.closed = True


def _provider_recovery_policy(
    *,
    fallback_agent: str = "codex",
    fallback_provider: str = "openai",
    fallback_model: str = "gpt-5.3-codex",
    max_same_provider_retries: int = 1,
) -> dict[str, object]:
    return {
        "fallbacks": [
            {
                "agent": fallback_agent,
                "provider": fallback_provider,
                "model": fallback_model,
            }
        ],
        "max_fallback_attempts": 1,
        "max_same_provider_retries": max_same_provider_retries,
        "cooldown_seconds": 600,
        "circuit_breaker": {
            "failure_threshold": 2,
            "cooldown_seconds": 900,
        },
    }


async def _configure_provider_monitor_workspace(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    agent: str = "gemini",
    model: str = "gemini-2.5-pro",
    fallback_agent: str = "codex",
    fallback_provider: str = "openai",
    fallback_model: str = "gpt-5.3-codex",
    max_same_provider_retries: int = 1,
) -> None:
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.agent = agent
        workspace.auto_merge = False
        workspace.initial_review_grace_period_seconds = 75
        workspace.task_policy = {
            "agent_model": model,
            "provider_recovery": _provider_recovery_policy(
                fallback_agent=fallback_agent,
                fallback_provider=fallback_provider,
                fallback_model=fallback_model,
                max_same_provider_retries=max_same_provider_retries,
            ),
            "pr_monitor": {"review_grace_seconds": 75},
        }
        await session.commit()


async def _provider_recovery_snapshot(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> tuple[dict[str, object], list[dict[str, object]], list[Operation], list[str]]:
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        source_events = [
            event
            for event in workspace.events
            if event.event_type == "workspace.provider_recovery_requested"
        ]
        operations = list((await session.execute(select(Operation))).scalars())
        requested_ids = list(
            (
                await session.execute(
                    select(Workspace.id).where(Workspace.status == WorkspaceStatus.requested.value)
                )
            ).scalars()
        )
        return (
            dict(workspace.task_policy),
            [dict(event.payload or {}) for event in source_events],
            operations,
            requested_ids,
        )


@pytest.mark.unit
async def test_pr_feedback_resolution_requires_postgresql_dialect(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        repo = PRFeedbackResolutionRepository(session, dialect_name="mysql")

        with pytest.raises(RuntimeError, match="requires PostgreSQL"):
            await repo.record_resolution(
                scm_provider="github",
                repository_key="dimileeh/aira-web",
                pull_request_key="42",
                pull_request_url="https://github.com/dimileeh/aira-web/pull/42",
                head_sha="abc1234567890def",
                feedback_kind="review_comment",
                feedback_id="issue:4391271818",
                feedback_body="body",
                feedback_author="reviewer",
                feedback_url=None,
                verdict="false_positive",
                reason="postgres-only persistence guard",
                source_workspace_id=workspace_id,
            )


@pytest.mark.unit
async def test_agent_failed_review_verdict_is_not_recorded_as_handled(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    await runner._record_pr_feedback_resolution(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        comment=ReviewComment(
            comment_id="issue:4391271818",
            body_excerpt="The agent failed before reaching a comment verdict.",
            body="The agent failed before reaching a comment verdict.",
            author="reviewer",
        ),
        verdict_result=VerdictResult(verdict="agent_failed", reason="adapter crashed"),
        operation_id="op-failed",
    )

    async with factory() as session:
        rows = await PRFeedbackResolutionRepository(session).list_for_pr(
            scm_provider="github",
            repository_key="dimileeh/aira-web",
            pull_request_key="42",
        )

    assert rows == []


@pytest.mark.unit
async def test_pr_feedback_resolution_state_ignores_absent_or_already_current_comments(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        await PRFeedbackResolutionRepository(session).record_resolution(
            scm_provider="github",
            repository_key="dimileeh/aira-web",
            pull_request_key="42",
            pull_request_url="https://github.com/dimileeh/aira-web/pull/42",
            head_sha="abc1234567890def",
            feedback_kind="review_comment",
            feedback_id="issue:handled",
            feedback_body="old body",
            feedback_author="reviewer",
            feedback_url=None,
            verdict="false_positive",
            reason="already handled",
            source_workspace_id=workspace_id,
        )
        await session.commit()

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    state = MonitorState()
    _mark_review_comment_addressed(
        state,
        ReviewComment(
            comment_id="issue:handled",
            body_excerpt="old body",
            body="old body",
            author="reviewer",
        ),
        "false_positive",
    )
    status = PRStatus(
        number=42,
        head_sha="new-head-after-repair-push",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(
            ReviewComment(
                comment_id="issue:handled",
                body_excerpt="old body",
                body="old body",
                author="reviewer",
            ),
            ReviewComment(
                comment_id="issue:unknown",
                body_excerpt="unseen body",
                body="unseen body",
                author="reviewer",
            ),
        ),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )

    changed = await runner._apply_pr_feedback_resolution_state(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=status,
        state=state,
    )

    assert changed is False
    assert state.threads_addressed_ids["issue:handled"] == "false_positive"
    assert _review_comment_body_state_key("issue:handled") in state.threads_addressed_ids


@pytest.mark.unit
def test_changed_review_comment_body_requeues_private_verdict() -> None:
    state = MonitorState()
    _mark_review_comment_addressed(
        state,
        ReviewComment(
            comment_id="issue:handled",
            body_excerpt="old body",
            body="old body",
            author="reviewer",
        ),
        "false_positive",
    )
    status = PRStatus(
        number=42,
        head_sha="new-head-after-review-edit",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(
            ReviewComment(
                comment_id="issue:handled",
                body_excerpt="new body that must be evaluated again",
                body="new body that must be evaluated again",
                author="reviewer",
            ),
        ),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )

    changed = _drop_stale_review_comment_addressed_state(status, state)

    assert changed is True
    assert "issue:handled" not in state.threads_addressed_ids
    assert _review_comment_body_state_key("issue:handled") not in state.threads_addressed_ids


@pytest.mark.unit
def test_pending_review_feedback_count_excludes_blocking_reviews_and_honors_state_hash() -> None:
    """Verify resolved, stale, and merge-blocking feedback is handled when counting pending triage items."""
    state = MonitorState()
    pending_comment = ReviewComment(
        comment_id="issue:pending",
        body_excerpt="please add test coverage",
        body="please add test coverage",
        author="reviewer",
    )
    handled_comment = ReviewComment(
        comment_id="issue:handled",
        body_excerpt="already handled",
        body="already handled",
        author="reviewer",
    )
    _mark_review_comment_addressed(state, handled_comment, "false_positive")
    agent_failed_comment = ReviewComment(
        comment_id="issue:agent-failed",
        body_excerpt="still failing",
        body="still failing",
        author="coderabbitai",
    )
    _mark_review_comment_addressed(state, agent_failed_comment, "agent_failed")
    blocked_comment = ReviewComment(
        comment_id="issue:blocked",
        body_excerpt="changes requested",
        body="changes requested",
        author="reviewer",
        blocks_merge=True,
    )
    stale_comment_old = ReviewComment(
        comment_id="issue:stale-body",
        body_excerpt="old body",
        body="old body",
        author="reviewer",
    )
    _mark_review_comment_addressed(state, stale_comment_old, "false_positive")
    stale_comment_current = ReviewComment(
        comment_id="issue:stale-body",
        body_excerpt="new body that requires new triage",
        body="new body that requires new triage",
        author="reviewer",
    )

    status = PRStatus(
        number=42,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(
            pending_comment,
            handled_comment,
            agent_failed_comment,
            stale_comment_current,
            blocked_comment,
        ),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        blocking_reviews=(blocked_comment,),
    )

    assert _pending_review_feedback_count(status, state) == 3
    assert len(status.unresolved_review_comments) == 5


@pytest.mark.unit
def test_pending_review_feedback_count_includes_triageable_blocking_issue_comment() -> None:
    """Verify merge-blocking issue comments from bots can still count as pending feedback."""
    state = MonitorState()
    triageable_blocker = ReviewComment(
        comment_id="issue:blocked-bot",
        body_excerpt="Please update required checks",
        body="Please update required checks",
        author="coderabbitai[bot]",
        source_kind="issue",
        blocks_merge=True,
    )

    status = PRStatus(
        number=42,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(triageable_blocker,),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )

    assert _pending_review_feedback_count(status, state) == 1


@pytest.mark.unit
def test_changed_review_thread_history_requeues_private_verdict() -> None:
    state = MonitorState()
    original = ReviewThread(
        thread_id="T_handled",
        path="src/awf/runtime/pr_monitor_runner.py",
        line=698,
        body_excerpt="bot finding",
        author="chatgpt-codex-connector",
        comments=(
            ReviewThreadComment(
                comment_id="101",
                body="bot finding",
                author="chatgpt-codex-connector",
            ),
        ),
    )
    _mark_review_thread_addressed(state, original, "false_positive")
    # A stale outdated-resolve requeue flag from a prior poll must also be wiped
    # when the thread's addressed state is fully reset for re-triage.
    state.threads_addressed_ids[_outdated_resolve_requeued_key("T_handled")] = "requeued"
    status = PRStatus(
        number=42,
        head_sha="new-head-after-thread-reply",
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(
            ReviewThread(
                thread_id="T_handled",
                path="src/awf/runtime/pr_monitor_runner.py",
                line=698,
                body_excerpt="bot finding",
                author="chatgpt-codex-connector",
                comments=(
                    ReviewThreadComment(
                        comment_id="101",
                        body="bot finding",
                        author="chatgpt-codex-connector",
                    ),
                    ReviewThreadComment(
                        comment_id="102",
                        body="maintainer says this still needs a fix",
                        author="dimileeh",
                    ),
                ),
            ),
        ),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )

    changed = _drop_stale_review_thread_addressed_state(status, state)

    assert changed is True
    assert "T_handled" not in state.threads_addressed_ids
    assert _review_thread_body_state_key("T_handled") not in state.threads_addressed_ids
    assert _outdated_resolve_requeued_key("T_handled") not in state.threads_addressed_ids


@pytest.mark.unit
async def test_ci_fix_usage_limit_failure_records_recovery_and_source_cooldown(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    await _configure_provider_monitor_workspace(
        factory,
        workspace_id,
        agent="codex",
        model="gpt-5.3-codex-spark",
        fallback_agent="gemini",
        fallback_provider="google",
        fallback_model="gemini-2.5-pro",
        max_same_provider_retries=0,
    )
    adapter = FakeAdapter()
    adapter.queue(
        returncode=1,
        stderr="Codex Spark: you've hit your usage limit. Switch to another model.",
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    with pytest.raises(ProviderRecoveryRetryError):
        await runner._run_ci_fix(
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            failures=(CheckFailure(name="tests", conclusion="FAILURE", log_excerpt="boom"),),
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            workspace_id=workspace_id,
            remote_branch=f"awf/{workspace_id}",
        )
    suppressed = await runner._provider_recovery_suppresses_cli(workspace_id)

    source_policy, recovery_events, operations, requested_ids = await _provider_recovery_snapshot(
        factory,
        workspace_id,
    )
    state = source_policy["provider_recovery_state"]

    assert suppressed is False
    assert isinstance(state, dict)
    assert state["action"] == "fallback"
    assert state["target_agent"] == "gemini"
    assert state["target_provider"] == "google"
    assert state["target_model"] == "gemini-2.5-pro"
    assert "not_before" not in state
    assert requested_ids == []
    assert [operation for operation in operations if operation.type == "retry"] == []
    assert len(recovery_events) == 1
    assert recovery_events[0]["provider_recovery"]["failure_type"] == "usage_limit"


@pytest.mark.unit
async def test_monitor_provider_failure_on_configured_default_retries_without_builtin_fallback(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.agent = "codex"
        workspace.task_policy = {"pr_monitor": {"review_grace_seconds": 75}}
        await session.commit()

    adapter = FakeAdapter(default_model="gpt-5.3-codex-spark")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    exc = AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(
            returncode=1,
            stdout="",
            stderr="Codex Spark MODEL_CAPACITY_EXHAUSTED",
        ),
        reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
        details={"provider": "openai", "model": "gpt-5.3-codex-spark"},
    )

    action = await runner._record_provider_agent_run_error(workspace_id, exc)

    source_policy, recovery_events, operations, requested_ids = await _provider_recovery_snapshot(
        factory,
        workspace_id,
    )
    state = source_policy["provider_recovery_state"]
    retry_operations = [operation for operation in operations if operation.type == "retry"]
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None

    assert action == "retry"
    assert isinstance(state, dict)
    assert state["action"] == "retry"
    assert state["target_agent"] == "codex"
    assert state["target_provider"] == "openai"
    assert state["target_model"] == "gpt-5.3-codex-spark"
    assert state["decision_reason_code"] == "PROVIDER_RETRY_DELAYED"
    assert "agent_model" not in source_policy
    assert workspace.agent == "codex"
    assert retry_operations == []
    assert requested_ids == []
    assert len(recovery_events) == 1
    assert recovery_events[0]["provider_recovery"]["action"] == "retry"
    assert recovery_events[0]["provider_recovery"]["target_model"] == "gpt-5.3-codex-spark"


@pytest.mark.unit
async def test_monitor_explicit_model_capacity_falls_back_to_configured_default(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    explicit_model = "gpt-5.3-codex-spark"
    configured_default = "gpt-5.4-mini"
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.agent = "codex"
        workspace.task_policy = {
            "agent_model": explicit_model,
            "pr_monitor": {"review_grace_seconds": 75},
        }
        await session.commit()

    # Production handoff binds explicit task policy into the adapter default.
    adapter = FakeAdapter(default_model=explicit_model)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        provider_recovery_default_model=configured_default,
    )
    exc = AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(
            returncode=1,
            stdout="",
            stderr="Codex Spark MODEL_CAPACITY_EXHAUSTED",
        ),
        reason_code=AGENT_PROVIDER_CAPACITY_EXHAUSTED,
        details={"provider": "openai", "model": explicit_model},
    )

    action = await runner._record_provider_agent_run_error(workspace_id, exc)

    source_policy, recovery_events, operations, requested_ids = await _provider_recovery_snapshot(
        factory,
        workspace_id,
    )
    state = source_policy["provider_recovery_state"]
    retry_operations = [operation for operation in operations if operation.type == "retry"]

    assert action == "retry"
    assert source_policy["agent_model"] == configured_default
    assert isinstance(state, dict)
    assert state["action"] == "fallback"
    assert state["target_agent"] == "codex"
    assert state["target_provider"] == "openai"
    assert state["target_model"] == configured_default
    assert retry_operations == []
    assert requested_ids == []
    assert len(recovery_events) == 1
    assert recovery_events[0]["provider_recovery"]["action"] == "fallback"
    assert recovery_events[0]["provider_recovery"]["target_model"] == configured_default


@pytest.mark.unit
async def test_sync_base_provider_failure_records_recovery_and_source_cooldown(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    await _configure_provider_monitor_workspace(factory, workspace_id)
    adapter = FakeAdapter()
    adapter.queue(
        returncode=1,
        stderr="Gemini RESOURCE_EXHAUSTED RetryableQuotaError retry after 120",
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=1, stderr="merge conflict")
    cmd.queue_result(returncode=0, stdout="UU src/conflict.py\n")
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    with pytest.raises(ProviderRecoveryRetryError):
        await runner._run_sync_base(
            workspace_id=workspace_id,
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            base_branch="development",
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
    suppressed = await runner._provider_recovery_suppresses_cli(workspace_id)

    source_policy, recovery_events, operations, requested_ids = await _provider_recovery_snapshot(
        factory,
        workspace_id,
    )
    state = source_policy["provider_recovery_state"]

    assert suppressed is True
    assert isinstance(state, dict)
    assert state["action"] == "retry"
    assert state["source_workspace_id"] == workspace_id
    assert state["source_reason_code"] == "AGENT_PROVIDER_CAPACITY_EXHAUSTED"
    assert "not_before" in state
    assert requested_ids == []
    assert [operation for operation in operations if operation.type == "retry"] == []
    assert len(recovery_events) == 1
    assert recovery_events[0]["provider_recovery"]["retry_after_seconds"] == 120


@pytest.mark.unit
async def test_fetch_status_repairs_orphaned_broken_awf_ref_before_counting_base(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(
        returncode=128,
        stderr="fatal: bad object refs/heads/awf/ws_deadbeef1234567890",
    )
    cmd.queue_result(returncode=0)  # update-ref -d broken orphan branch
    cmd.queue_result(returncode=0)  # worktree prune stale metadata
    cmd.queue_result(returncode=0)  # retry fetch with explicit base refspec
    cmd.queue_result(returncode=0, stdout="2\n")  # rev-list HEAD..origin/base
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    gh = _CapturingGH()
    runner._deps.gh = gh  # type: ignore[assignment]

    status = await runner._fetch_status_for_decision(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        workspace_id="ws_current",
        base_branch="development",
    )

    assert status.base_behind_count == 2
    assert gh.base_behind_counts == [2]
    assert cmd.calls[0].args[-3:] == [
        "fetch",
        "origin",
        "+refs/heads/development:refs/remotes/origin/development",
    ]
    assert cmd.calls[1].args[-3:] == [
        "update-ref",
        "-d",
        "refs/heads/awf/ws_deadbeef1234567890",
    ]
    assert cmd.calls[2].args[-2:] == ["worktree", "prune"]
    assert cmd.calls[3].args[-3:] == [
        "fetch",
        "origin",
        "+refs/heads/development:refs/remotes/origin/development",
    ]


@pytest.mark.unit
async def test_fetch_status_repairs_orphaned_broken_task_tagged_awf_ref(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Tagged workspaces leave ``<tag>-awf/ws_...`` refs; repair must delete them.

    With ``--task-tag`` the provisioner names the local branch
    ``PROJ-123-awf/ws_...`` (see ``_provision_local_branch_name``), so an
    orphaned broken ref surfaces as ``refs/heads/PROJ-123-awf/ws_...``. The
    fetch-repair must recognise the tagged prefix and delete the exact ref,
    otherwise the monitor stays wedged on the base-fetch error.
    """
    cmd = FakeCommandRunner()
    cmd.queue_result(
        returncode=128,
        stderr="fatal: bad object refs/heads/PROJ-123-awf/ws_deadbeef1234567890",
    )
    cmd.queue_result(returncode=0)  # update-ref -d broken orphan branch
    cmd.queue_result(returncode=0)  # worktree prune stale metadata
    cmd.queue_result(returncode=0)  # retry fetch with explicit base refspec
    cmd.queue_result(returncode=0, stdout="2\n")  # rev-list HEAD..origin/base
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.gh = _CapturingGH()  # type: ignore[assignment]

    status = await runner._fetch_status_for_decision(
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        workspace_id="ws_current",
        base_branch="development",
    )

    assert status.base_behind_count == 2
    assert cmd.calls[1].args[-3:] == [
        "update-ref",
        "-d",
        "refs/heads/PROJ-123-awf/ws_deadbeef1234567890",
    ]
    assert cmd.calls[2].args[-2:] == ["worktree", "prune"]


@pytest.mark.unit
async def test_fetch_status_supplies_workspace_test_commands_to_ci_log_evidence(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(
        factory,
        test_commands=[
            "ruff check .",
            "uv run --python 3.12 --extra dev pytest --cov=awf --cov-fail-under=99",
        ],
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    gh = _CapturingGH(status=replace(_green_status(), check_state=CheckState.FAILURE))
    runner._deps.gh = gh  # type: ignore[assignment]
    repo = RepoRef(owner="dimileeh", name="aira-web")

    await runner._fetch_status_for_decision(
        repo=repo,
        pr_number=42,
        workspace_id=workspace_id,
        base_branch="development",
    )

    assert gh.failing_log_requests == [
        (
            repo,
            42,
            "abc1234567890def",
            (
                "ruff check .",
                "uv run --python 3.12 --extra dev pytest --cov=awf --cov-fail-under=99",
            ),
        )
    ]


@pytest.mark.unit
async def test_fetch_status_refuses_to_delete_broken_ref_for_active_workspace(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    broken_workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(
        returncode=128,
        stderr=f"fatal: bad object refs/heads/awf/{broken_workspace_id}",
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.gh = _CapturingGH()  # type: ignore[assignment]

    with pytest.raises(BaseFetchError) as exc:
        await runner._fetch_status_for_decision(
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            workspace_id=broken_workspace_id,
            base_branch="development",
        )

    assert "refs/heads/awf/" in str(exc.value)
    assert len(cmd.calls) == 1


@pytest.mark.unit
async def test_fetch_status_keeps_failure_when_broken_ref_delete_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(
        returncode=128,
        stderr="fatal: bad object refs/heads/awf/ws_deletefail123456",
    )
    cmd.queue_result(returncode=1, stderr="cannot lock ref")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.gh = _CapturingGH()  # type: ignore[assignment]

    with pytest.raises(BaseFetchError) as exc:
        await runner._fetch_status_for_decision(
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            workspace_id="ws_current",
            base_branch="development",
        )

    assert "bad object refs/heads/awf/ws_deletefail123456" in str(exc.value)
    assert len(cmd.calls) == 2


@pytest.mark.unit
async def test_fetch_status_keeps_failure_when_retry_fetch_still_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(
        returncode=128,
        stderr="fatal: bad object refs/heads/awf/ws_retryfail123456",
    )
    cmd.queue_result(returncode=0)  # update-ref -d broken orphan branch
    cmd.queue_result(returncode=0)  # worktree prune
    cmd.queue_result(returncode=128, stderr="fatal: remote hung up")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.gh = _CapturingGH()  # type: ignore[assignment]

    with pytest.raises(BaseFetchError) as exc:
        await runner._fetch_status_for_decision(
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            workspace_id="ws_current",
            base_branch="development",
        )

    assert "remote hung up" in str(exc.value)
    assert len(cmd.calls) == 4


@pytest.mark.unit
async def test_run_fails_workspace_when_base_fetch_cannot_be_refreshed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=128, stderr="fatal: could not fetch base")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.gh = _CapturingGH()  # type: ignore[assignment]

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.failed.value
        assert workspace.failure_reason == "infrastructure_failure"
        assert workspace.failure_message is not None
        assert "could not refresh base branch" in workspace.failure_message


def _bitbucket_forge_client() -> BitbucketClient:
    """A BitbucketClient instance for forge detection (no HTTP calls are made)."""
    return BitbucketClient(
        client=httpx.AsyncClient(base_url="https://api.bitbucket.org"),
        auth=BitbucketAuth(mode="bearer", api_token="bb-token-aaaaaaaaaaaa"),
    )


@pytest.mark.unit
async def test_bitbucket_feedback_resolution_records_bitbucket_provenance(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A Bitbucket workspace records feedback provenance under the bitbucket provider.

    Regression for #445: ``feedback_state`` previously hardcoded
    ``scm_provider="github"`` + a github.com PR URL, so Bitbucket feedback poisoned
    GitHub provenance/replay rows. The provider + URL must derive from the resolved
    forge client instead.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=_bitbucket_forge_client(),
    )

    await runner._record_pr_feedback_resolution(
        workspace_id=workspace_id,
        repo=RepoRef(owner="workspace", name="repo", forge="bitbucket"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        comment=ReviewComment(
            comment_id="bbcomment:99",
            body_excerpt="please fix",
            body="please fix",
            author="reviewer",
        ),
        verdict_result=VerdictResult(verdict="false_positive", reason="not a real issue"),
        operation_id="op-bb",
    )

    async with factory() as session:
        bb_rows = await PRFeedbackResolutionRepository(session).list_for_pr(
            scm_provider="bitbucket",
            repository_key="workspace/repo",
            pull_request_key="42",
        )
        gh_rows = await PRFeedbackResolutionRepository(session).list_for_pr(
            scm_provider="github",
            repository_key="workspace/repo",
            pull_request_key="42",
        )

    assert len(bb_rows) == 1
    assert bb_rows[0].pull_request_url == "https://bitbucket.org/workspace/repo/pull-requests/42"
    # Must NOT poison the GitHub provider rows.
    assert gh_rows == []


@pytest.mark.unit
async def test_github_feedback_resolution_records_github_provenance_unchanged(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A GitHub workspace still records github provenance + a github.com PR URL."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    await runner._record_pr_feedback_resolution(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        comment=ReviewComment(
            comment_id="issue:55",
            body_excerpt="please fix",
            body="please fix",
            author="reviewer",
        ),
        verdict_result=VerdictResult(verdict="false_positive", reason="not a real issue"),
        operation_id="op-gh",
    )

    async with factory() as session:
        rows = await PRFeedbackResolutionRepository(session).list_for_pr(
            scm_provider="github",
            repository_key="dimileeh/aira-web",
            pull_request_key="42",
        )

    assert len(rows) == 1
    assert rows[0].pull_request_url == "https://github.com/dimileeh/aira-web/pull/42"


@pytest.mark.unit
def test_forge_scm_provider_maps_known_forges_and_rejects_unknown() -> None:
    """``_forge_scm_provider`` maps each forge to its key and fails loudly otherwise.

    Regression for the #454 review note: an unknown forge client must NOT silently
    fall back to ``"github"`` (which would alias a third forge's feedback rows under
    the GitHub provider key). It raises ``NotImplementedError`` so a newly wired
    forge is forced to declare its own provider key.
    """
    from types import SimpleNamespace

    from awf.runtime.pr_monitor_runner import feedback_state

    cmd = FakeCommandRunner()
    github_self = SimpleNamespace(_deps=SimpleNamespace(gh=GitHubClient(cmd)))
    bitbucket_self = SimpleNamespace(_deps=SimpleNamespace(gh=_bitbucket_forge_client()))

    assert feedback_state._forge_scm_provider(github_self) == "github"
    assert feedback_state._forge_scm_provider(bitbucket_self) == "bitbucket"

    unknown_self = SimpleNamespace(_deps=SimpleNamespace(gh=object()))
    with pytest.raises(NotImplementedError, match="unknown forge client type: object"):
        feedback_state._forge_scm_provider(unknown_self)


@pytest.mark.unit
def test_forge_pr_url_maps_known_forges_and_rejects_unknown() -> None:
    """``_forge_pr_url`` builds the forge-correct URL and fails loudly otherwise.

    Regression for the #454 review note: an unknown forge client must NOT silently
    fall back to a github.com URL (which would persist a nonexistent link in
    provenance rows). It raises ``NotImplementedError``, mirroring
    ``_forge_scm_provider`` so a newly wired forge is forced to declare its URL shape.
    """
    from types import SimpleNamespace

    from awf.runtime.pr_monitor_runner import feedback_state

    cmd = FakeCommandRunner()
    repo = RepoRef(owner="acme", name="widget")
    github_self = SimpleNamespace(_deps=SimpleNamespace(gh=GitHubClient(cmd)))
    bitbucket_self = SimpleNamespace(_deps=SimpleNamespace(gh=_bitbucket_forge_client()))

    assert (
        feedback_state._forge_pr_url(github_self, repo, 7)
        == "https://github.com/acme/widget/pull/7"
    )
    assert (
        feedback_state._forge_pr_url(bitbucket_self, repo, 7)
        == "https://bitbucket.org/acme/widget/pull-requests/7"
    )

    unknown_self = SimpleNamespace(_deps=SimpleNamespace(gh=object()))
    with pytest.raises(NotImplementedError, match="unknown forge client type: object"):
        feedback_state._forge_pr_url(unknown_self, repo, 7)
