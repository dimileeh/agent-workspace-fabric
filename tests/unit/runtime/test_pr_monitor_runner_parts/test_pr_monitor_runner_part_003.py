"""Unit tests for focused ``pr_monitor_runner`` behavior.

Most cases cover the pure, side-effect-free helpers: ``_parse_verdict`` (CLI
reply → structured verdict) and ``_collect_defer_items`` (PRStatus +
MonitorState → bot/human defer buckets for the terminal artifact). Focused
runtime-path regressions live here when the unit suite needs to cover a
specific merge-gate branch without running the full monitor integration loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunError
from awf.adapters.provider_failures import AGENT_PROVIDER_CAPACITY_EXHAUSTED
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.enums import (
    AgentRuntime,
    TaskClass,
    WorkspaceStatus,
)
from awf.db.models import Operation, Workspace
from awf.db.repositories import (
    PRFeedbackResolutionRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckFailure,
    CheckState,
    Merge,
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
from awf.runtime.pr_monitor_runner import (
    PullRequestMonitorRunner,
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
    BaseBehindCountError,
    BaseFetchError,
    ProviderRecoveryRetryError,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class PersistCheckingSleep(RecordedSleep):
    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        workspace_id: str,
        state_key: str,
        expected_value: str,
    ) -> None:
        super().__init__()
        self._factory = factory
        self._workspace_id = workspace_id
        self._state_key = state_key
        self._expected_value = expected_value

    async def __call__(self, seconds: float) -> None:
        async with self._factory() as session:
            workspace = await WorkspaceRepository(session).get(self._workspace_id)
            assert workspace is not None
            assert workspace.monitor_threads_addressed[self._state_key] == self._expected_value
        await super().__call__(seconds)


class PersistCheckingCommandRunner(FakeCommandRunner):
    def __init__(
        self,
        *,
        factory: async_sessionmaker[AsyncSession],
        workspace_id: str,
        state_key: str,
        expected_value: str,
    ) -> None:
        super().__init__()
        self._factory = factory
        self._workspace_id = workspace_id
        self._state_key = state_key
        self._expected_value = expected_value

    async def run(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        cwd: str | None = None,
    ) -> CommandResult:
        if args[:3] == ["gh", "run", "rerun"]:
            async with self._factory() as session:
                workspace = await WorkspaceRepository(session).get(self._workspace_id)
                assert workspace is not None
                assert (
                    workspace.monitor_threads_addressed.get(self._state_key) == self._expected_value
                )
        return await super().run(args, input_bytes=input_bytes, cwd=cwd)


def _monitor_runner(
    tmp_path: Path,
    fake: FakeCommandRunner,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    workspace_runtime_context: str = "",
) -> PullRequestMonitorRunner:
    return PullRequestMonitorRunner(
        session_factory=session_factory or object(),  # type: ignore[arg-type]
        runner=fake,
        adapter=object(),  # type: ignore[arg-type]
        gh=object(),  # type: ignore[arg-type]
        worktrees_root=tmp_path / "work" / "git" / "worktrees",
        workspace_runtime_context=workspace_runtime_context,
    )


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


class _CommandIterable:
    def __iter__(self) -> Iterator[object]:
        return iter(("pytest -q", object(), "ruff check ."))


def _gh_pr_merge_calls(cmd: FakeCommandRunner) -> list[list[str]]:
    return [call.args for call in cmd.calls if call.args[:3] == ["gh", "pr", "merge"]]


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


async def _dispatch_merge_recovery(
    *,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    workspace_id: str,
    pr_number: int,
    head_sha: str,
    sleep_fn: RecordedSleep | None = None,
) -> bool:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn or RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
    )
    return await runner._execute(
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


@pytest.mark.unit
async def test_run_closes_forge_client_on_exit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # Regression (issue:4640573294): the per-monitor forge client (a BitBucket
    # client owns an httpx connection pool) must be released when run() finishes,
    # not leaked until GC. run() owns the lifecycle of the client the factory
    # built for it, so it calls gh.aclose() in its finally on every exit path.
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
    gh = _CapturingGH()
    runner._deps.gh = gh  # type: ignore[assignment]

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert gh.closed is True


@pytest.mark.unit
async def test_run_retries_transient_base_fetch_500_and_completes(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    cmd.queue_result(
        returncode=128,
        stderr=(
            "remote: Internal Server Error\n"
            "fatal: unable to access 'https://github.com/example/repo.git/': "
            "The requested URL returned error: 500"
        ),
    )
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=0, stdout=pr_payload(closed=True, merged=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.gh = _CapturingGH(  # type: ignore[assignment]
        status=replace(
            _green_status(),
            closed=True,
            merged=True,
            merge_commit_sha="mergecommit1234567890",
        )
    )

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert sleep_fn.calls == [5.0]
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.completed.value
        assert any(
            event.reason_code == "GIT_BASE_FETCH_TRANSIENT_RETRY" for event in workspace.events
        )


@pytest.mark.unit
async def test_run_retries_remote_tracking_ref_lock_race_and_completes(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    cmd.queue_result(
        returncode=1,
        stderr=(
            "error: cannot lock ref "
            "'refs/remotes/origin/codex/awf-post-merge-fixes': is at "
            "dffa1db03af61da5db52e16a6e79163c35b88d5d but expected "
            "cc82a8d265b6d63593417a13d3d9507cc0ede8d5\n"
            "From https://github.com/dimileeh/aira-agent-workspace-fabric\n"
            " ! cc82a8d2..dffa1db0  codex/awf-post-merge-fixes -> "
            "origin/codex/awf-post-merge-fixes  (unable to update local ref)"
        ),
    )
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=0, stdout=pr_payload(closed=True, merged=True))
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.gh = _CapturingGH(  # type: ignore[assignment]
        status=replace(
            _green_status(),
            closed=True,
            merged=True,
            merge_commit_sha="mergecommit1234567890",
        )
    )

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert sleep_fn.calls == [5.0]
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.completed.value
        assert any(
            event.reason_code == "GIT_BASE_FETCH_TRANSIENT_RETRY" for event in workspace.events
        )


@pytest.mark.unit
async def test_run_fails_after_transient_base_fetch_retry_budget_is_exhausted(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    transient_stderr = (
        "remote: Internal Server Error\n"
        "fatal: unable to access 'https://github.com/example/repo.git/': "
        "The requested URL returned error: 500"
    )
    cmd.queue_result(returncode=128, stderr=transient_stderr)
    cmd.queue_result(returncode=128, stderr=transient_stderr)
    cmd.queue_result(returncode=128, stderr=transient_stderr)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    object.__setattr__(runner._runner_config, "transient_base_fetch_max_retries", 2)
    object.__setattr__(
        runner._runner_config,
        "transient_base_fetch_initial_backoff_seconds",
        3.0,
    )
    object.__setattr__(
        runner._runner_config,
        "transient_base_fetch_max_backoff_seconds",
        10.0,
    )
    runner._deps.gh = _CapturingGH()  # type: ignore[assignment]

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert sleep_fn.calls == [3.0, 6.0]
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.failed.value
        assert workspace.failure_reason == "infrastructure_failure"
        assert workspace.failure_message is not None
        assert "could not refresh base branch" in workspace.failure_message
        assert any(
            event.reason_code == "GIT_BASE_FETCH_TRANSIENT_RETRY_EXHAUSTED"
            for event in workspace.events
        )
        failed_transitions = [
            event
            for event in workspace.events
            if event.event_type == "workspace.state_changed"
            and event.new_state == WorkspaceStatus.failed.value
        ]
        assert failed_transitions[-1].reason_code == ("GIT_BASE_FETCH_TRANSIENT_RETRY_EXHAUSTED")


@pytest.mark.unit
async def test_sync_base_transient_base_fetch_retry_budget_survives_status_refresh(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    transient_stderr = (
        "remote: Internal Server Error\n"
        "fatal: unable to access 'https://github.com/example/repo.git/': "
        "The requested URL returned error: 500"
    )
    for _ in range(3):
        cmd.queue_result(returncode=0)  # top-of-loop git fetch origin development
        cmd.queue_result(returncode=0, stdout="1\n")  # base branch is still ahead
        cmd.queue_result(returncode=0)  # sync_base merge --abort
        cmd.queue_result(returncode=128, stderr=transient_stderr)  # sync_base fetch
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        max_outer_iterations=3,
    )
    object.__setattr__(runner._runner_config, "transient_base_fetch_max_retries", 2)
    object.__setattr__(
        runner._runner_config,
        "transient_base_fetch_initial_backoff_seconds",
        5.0,
    )
    object.__setattr__(
        runner._runner_config,
        "transient_base_fetch_max_backoff_seconds",
        30.0,
    )
    runner._deps.gh = _CapturingGH()  # type: ignore[assignment]

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert sleep_fn.calls == [5.0, 10.0]
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.failed.value
        assert workspace.failure_reason == "infrastructure_failure"
        assert workspace.failure_message is not None
        assert "could not refresh base branch" in workspace.failure_message
        assert any(
            event.reason_code == "GIT_BASE_FETCH_TRANSIENT_RETRY_EXHAUSTED"
            and event.payload.get("context") == "sync_base"
            for event in workspace.events
        )
        failed_transitions = [
            event
            for event in workspace.events
            if event.event_type == "workspace.state_changed"
            and event.new_state == WorkspaceStatus.failed.value
        ]
        assert failed_transitions[-1].reason_code == ("GIT_BASE_FETCH_TRANSIENT_RETRY_EXHAUSTED")


@pytest.mark.unit
async def test_base_behind_count_failure_is_explicit_not_zero(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=128, stderr="fatal: bad object")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    with pytest.raises(BaseBehindCountError):
        await runner._count_base_behind(
            worktree_path=tmp_path / "worktrees" / "ws_count",
            base_branch="development",
        )


@pytest.mark.unit
async def test_sync_base_no_progress_state_is_persisted_across_restarts(
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

    await runner._persist_state(
        workspace_id,
        MonitorState(
            sync_base_no_progress_signature="abc|CONFLICTING|DIRTY|base_behind=0",
            sync_base_no_progress_count=2,
            threads_addressed_ids={"T1": "fix_committed"},
        ),
    )

    workspace = await runner._load_workspace(workspace_id)
    state = runner._load_state(workspace)

    assert state.sync_base_no_progress_signature == "abc|CONFLICTING|DIRTY|base_behind=0"
    assert state.sync_base_no_progress_count == 2
    assert state.threads_addressed_ids == {"T1": "fix_committed"}


@pytest.mark.unit
async def test_pr_feedback_resolution_body_change_creates_new_comment_identity(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        repo = PRFeedbackResolutionRepository(session)
        await repo.record_resolution(
            scm_provider="github",
            repository_key="dimileeh/aira-web",
            pull_request_key="42",
            pull_request_url="https://github.com/dimileeh/aira-web/pull/42",
            head_sha="old-head",
            feedback_kind="review_comment",
            feedback_id="issue:4391271818",
            feedback_body="old body",
            feedback_author="chatgpt-codex-connector[bot]",
            feedback_url="https://github.example/comment/4391271818",
            verdict="false_positive",
            reason="old comment body",
            source_workspace_id=workspace_id,
        )
        await repo.record_resolution(
            scm_provider="github",
            repository_key="dimileeh/aira-web",
            pull_request_key="42",
            pull_request_url="https://github.com/dimileeh/aira-web/pull/42",
            head_sha="new-head",
            feedback_kind="review_comment",
            feedback_id="issue:4391271818",
            feedback_body="new body with new actionable content",
            feedback_author="chatgpt-codex-connector[bot]",
            feedback_url="https://github.example/comment/4391271818",
            verdict="defer",
            reason="body changed, so the monitor must re-evaluate it",
            source_workspace_id=workspace_id,
        )
        await session.commit()

        rows = await repo.list_for_pr(
            scm_provider="github",
            repository_key="dimileeh/aira-web",
            pull_request_key="42",
        )

    assert len(rows) == 2
    assert {row.reason for row in rows} == {
        "old comment body",
        "body changed, so the monitor must re-evaluate it",
    }
