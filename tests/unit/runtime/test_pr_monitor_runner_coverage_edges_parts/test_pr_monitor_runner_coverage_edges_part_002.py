"""Focused branch-coverage tests for PR monitor runner edge behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.bitbucket_client import (
    BITBUCKET_API_ERROR,
    BITBUCKET_RATE_LIMITED,
    BITBUCKET_TASK_RESOLVE_FORBIDDEN,
    BitbucketClientError,
)
from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.enums import OperationStatus, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    OperationRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.ownership import AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
from awf.runtime.pr_monitor import (
    CheckState,
    CheckTiming,
    Merge,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    NotifyHuman,
    PRStatus,
    ReviewComment,
    ReviewThread,
    _review_thread_body_state_key,
)
from awf.runtime.pr_monitor_runner.remote_ops import (
    _GitPushResult,
)
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorHeadObjectMissingError,
    _MonitorPolicyBlockedError,
)
from tests.postgres import postgres_test_engine
from tests.shared.monitor_runner import DefaultMergeMethodGitHubClient
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
)


class _BitbucketResolveThreadClient(DefaultMergeMethodGitHubClient):
    """Command-based gh double whose ``resolve_thread`` raises a Bitbucket error.

    A Bitbucket workspace's ``self._deps.gh`` is a ``BitbucketClient`` that raises
    ``BitbucketClientError`` (not ``GitHubClientError``) from ``resolve_thread``.
    Overriding only that method keeps the push/settle-poll flow command-based while
    exercising the fix cycle's forge-neutral Bitbucket resolve arm.
    """

    def __init__(self, runner: FakeCommandRunner, exc: BitbucketClientError) -> None:
        super().__init__(runner)
        self._resolve_exc = exc

    async def resolve_thread(self, *, thread_id: str) -> None:
        del thread_id
        raise self._resolve_exc


class _BitbucketSettlePollClient(DefaultMergeMethodGitHubClient):
    """Command-based gh double whose ``fetch_pr_status`` raises a Bitbucket error.

    A Bitbucket workspace's ``self._deps.gh`` is a ``BitbucketClient`` that raises
    ``BitbucketClientError`` (not ``GitHubClientError``) from ``fetch_pr_status``.
    Overriding only that method keeps the push/resolve flow command-based while
    exercising the fix cycle's settle re-poll Bitbucket arm.
    """

    def __init__(self, runner: FakeCommandRunner, exc: BitbucketClientError) -> None:
        super().__init__(runner)
        self._fetch_exc = exc

    async def fetch_pr_status(
        self, *, repo: RepoRef, pr_number: int, base_behind_count: int
    ) -> PRStatus:
        del repo, pr_number, base_behind_count
        raise self._fetch_exc


class _BitbucketPostCommentClient(DefaultMergeMethodGitHubClient):
    """Command-based gh double whose ``post_comment`` raises a Bitbucket error.

    A Bitbucket workspace's ``self._deps.gh`` is a ``BitbucketClient`` that raises
    ``BitbucketClientError`` (not ``GitHubClientError``) from ``post_comment``.
    Overriding only that method keeps the rest of the flow command-based while
    exercising the human-notification Bitbucket arms.
    """

    def __init__(self, runner: FakeCommandRunner, exc: BitbucketClientError) -> None:
        super().__init__(runner)
        self._post_comment_exc = exc

    async def post_comment(self, *, repo: RepoRef, pr_number: int, body: str) -> None:
        del repo, pr_number, body
        raise self._post_comment_exc


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _status_for_helpers(
    *,
    head_sha: str = "abc1234567890def",
    threads: tuple[ReviewThread, ...] = (),
    reviews: tuple[ReviewComment, ...] = (),
    blocking_reviews: tuple[ReviewComment, ...] | None = None,
    checks: tuple[CheckTiming, ...] = (),
) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=head_sha,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=threads,
        unresolved_review_comments=reviews,
        blocking_reviews=(
            tuple(review for review in reviews if review.blocks_merge)
            if blocking_reviews is None
            else blocking_reviews
        ),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        checks=checks,
    )


class _RecordingLogSink:
    stream_id = "monitor.log"

    def __init__(self) -> None:
        self.lines: list[str] = []

    async def write(self, data: str) -> None:
        self.lines.append(data)


def _retry_events(ws: Workspace) -> list:
    return [
        event
        for event in ws.events
        if event.event_type == "monitor.github_transient_error_retrying"
    ]


def _git_worktree_command(worktree_path: Path, *args: str) -> list[str]:
    return ["git", "-c", f"safe.directory={worktree_path}", "-C", str(worktree_path), *args]


@pytest.mark.unit
async def test_transient_github_merge_error_retries_without_human_escalation(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    cmd.queue_result(returncode=1, stderr="HTTP 504 Gateway Timeout")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert sleep_fn.calls == [5]
    assert len(cmd.calls) == 1
    assert cmd.calls[0].args[:3] == ["gh", "pr", "merge"]
    # Only the bounded-retry bookkeeping key is left behind — no thread markers.
    assert state.threads_addressed_ids == {"__awf_forge_transient_retry_count:merge_pr": "1"}
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        assert ws.failure_message is None
        events = _retry_events(ws)
        assert len(events) == 1
        assert events[0].reason_code == "GITHUB_TRANSIENT_RETRY"
        assert events[0].payload["context"] == "merge_pr"
        assert events[0].payload["operation"] == "gh pr merge"
        assert events[0].payload["wait_seconds"] == 5


@pytest.mark.unit
async def test_non_transient_github_merge_error_records_failed_audit_and_redacts(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    secret_stderr = (
        "Resource not accessible by integration for "
        "https://user:raw_secret_value@github.com/org/repo "
        "Authorization: Bearer opaqueBearerToken123"
    )
    cmd.queue_result(returncode=1, stderr=secret_stderr)
    cmd.queue_result(returncode=0)  # gh pr comment fallback
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    state = MonitorState()
    monitor_log = _RecordingLogSink()

    terminal = await runner._execute(
        action=Merge(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=monitor_log,  # type: ignore[arg-type]
    )

    assert terminal is False
    assert sleep_fn.calls == [60]
    assert [call.args[:3] for call in cmd.calls] == [
        ["gh", "pr", "merge"],
        ["gh", "pr", "comment"],
    ]
    async with factory() as s:
        operations = await OperationRepository(s).list_all(
            workspace_id=workspace_id,
            limit=20,
        )
        attempt_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.merge_attempt",
            limit=10,
        )
        result_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.merge_result",
            limit=10,
        )

    merge_operation = next(
        operation
        for operation in operations
        if operation.type == "monitor_state"
        and isinstance(operation.payload, dict)
        and operation.payload.get("action") == "merge"
    )
    assert merge_operation.status == OperationStatus.failed.value
    assert merge_operation.error_code == "GITHUB_MERGE_FAILED"
    assert merge_operation.error_message is not None
    assert "raw_secret_value" not in merge_operation.error_message
    assert "opaqueBearerToken123" not in merge_operation.error_message
    assert "https://[redacted]@github.com/org/repo" in merge_operation.error_message

    assert len(attempt_events) == 1
    assert attempt_events[0].payload == {
        "schema": "control_audit.v1",
        "actor": "pr_monitor",
        "source": "pr_monitor",
        "action": "merge",
        "outcome": "attempted",
        "reason_code": "MERGE",
        "operation_id": merge_operation.id,
        "operation_type": "monitor_state",
        "pr_number": 42,
        "pr_url": "https://github.com/dimileeh/aira-web/pull/42",
        "source_head_sha": "abc1234567890def",
        "source_base_sha": "a" * 40,
        "target_branch": "development",
        "remote_branch": f"awf/{workspace_id}",
        "branch_name": f"awf/{workspace_id}",
        "evidence": {"log_stream_refs": {"monitor": "monitor.log"}},
    }
    assert len(result_events) == 1
    assert result_events[0].reason_code == "GITHUB_MERGE_FAILED"
    assert result_events[0].payload is not None
    assert result_events[0].payload["outcome"] == "failed"
    assert result_events[0].payload["operation_id"] == merge_operation.id
    assert result_events[0].payload["pr_number"] == 42
    assert result_events[0].payload["pr_url"] == ("https://github.com/dimileeh/aira-web/pull/42")
    assert result_events[0].payload["source_head_sha"] == "abc1234567890def"
    assert result_events[0].payload["target_branch"] == "development"
    assert result_events[0].payload["evidence"]["operation"] == "merge_pr"
    assert result_events[0].payload["evidence"]["log_stream_refs"] == {"monitor": "monitor.log"}
    assert "raw_secret_value" not in repr(result_events[0].payload)
    assert "opaqueBearerToken123" not in repr(result_events[0].payload)
    assert "https://[redacted]@github.com/org/repo" in repr(result_events[0].payload)


@pytest.mark.unit
async def test_transient_human_notification_comment_error_retries_without_crashing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    cmd.queue_result(returncode=1, stderr="HTTP 502 Bad Gateway")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=NotifyHuman(message="branch protection temporarily unavailable"),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert sleep_fn.calls == [5]
    assert len(cmd.calls) == 1
    assert cmd.calls[0].args[:3] == ["gh", "pr", "comment"]
    # Only the bounded-retry bookkeeping key is left behind — no thread markers.
    assert state.threads_addressed_ids == {
        "__awf_forge_transient_retry_count:post_human_notification": "1"
    }
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        assert ws.failure_message is None
        events = _retry_events(ws)
        assert len(events) == 1
        assert events[0].reason_code == "GITHUB_TRANSIENT_RETRY"
        assert events[0].payload["context"] == "post_human_notification"
        assert events[0].payload["operation"] == "gh pr comment"
        assert events[0].payload["wait_seconds"] == 5


@pytest.mark.unit
async def test_non_transient_human_notification_comment_error_raises(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1, stderr="bad credentials")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    with pytest.raises(GitHubClientError, match="bad credentials"):
        await runner._execute(
            action=NotifyHuman(message="manual review needed"),
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            status=_status_for_helpers(),
            state=MonitorState(),
            base_branch="development",
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            monitor_log=None,
        )


@pytest.mark.unit
async def test_transient_bitbucket_human_notification_comment_error_retries(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # A Bitbucket workspace posts the NotifyHuman comment through BitbucketClient,
    # whose post_comment raises BitbucketClientError (not GitHubClientError). A
    # transient blip (rate limit) must wait and keep polling instead of escaping
    # _execute uncaught — mirroring the GitHub transient arm.
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=_BitbucketPostCommentClient(
            cmd,
            BitbucketClientError(
                operation="bitbucket post_comment",
                status=429,
                body="rate limited",
                reason_code=BITBUCKET_RATE_LIMITED,
            ),
        ),
    )
    state = MonitorState()

    terminal = await runner._execute(
        action=NotifyHuman(message="branch protection temporarily unavailable"),
        workspace_id=workspace_id,
        repo_url="git@bitbucket.org:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=_status_for_helpers(),
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    assert terminal is False
    assert sleep_fn.calls == [5]
    # Only the bounded-retry bookkeeping key is left behind — no thread markers.
    assert state.threads_addressed_ids == {
        "__awf_forge_transient_retry_count:post_human_notification": "1"
    }
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        assert ws.failure_message is None
        retry_events = [
            event
            for event in ws.events
            if event.event_type == "monitor.bitbucket_transient_error_retrying"
        ]
        assert len(retry_events) == 1
        assert retry_events[0].reason_code == "BITBUCKET_TRANSIENT_RETRY"
        assert retry_events[0].payload["context"] == "post_human_notification"
        assert retry_events[0].payload["operation"] == "bitbucket post_comment"


@pytest.mark.unit
async def test_non_transient_bitbucket_human_notification_comment_error_raises(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # A permanent Bitbucket fault (403, token lacks the scope) during the
    # NotifyHuman comment must propagate like the GitHub non-transient arm rather
    # than being swallowed.
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=_BitbucketPostCommentClient(
            cmd,
            BitbucketClientError(
                operation="bitbucket post_comment",
                status=403,
                body="forbidden: missing scope",
            ),
        ),
    )

    with pytest.raises(BitbucketClientError, match="forbidden: missing scope"):
        await runner._execute(
            action=NotifyHuman(message="manual review needed"),
            workspace_id=workspace_id,
            repo_url="git@bitbucket.org:dimileeh/aira-web.git",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            status=_status_for_helpers(),
            state=MonitorState(),
            base_branch="development",
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            monitor_log=None,
        )


@pytest.mark.unit
async def test_fix_cycle_treats_transient_settle_poll_as_retryable(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    adapter = FakeAdapter()
    workspace_id = await seed_monitoring_workspace(factory)
    adapter.queue(stdout="Committed fix locally.")
    cmd.queue_result(returncode=1, stderr="HTTP 502 Bad Gateway")
    cmd.queue_result(returncode=0, stderr="Everything up-to-date")
    cmd.queue_result(returncode=0, stdout="{}")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    thread = ReviewThread(
        thread_id="T_retry",
        path="src/foo.py",
        line=12,
        body_excerpt="please adjust this",
        author="review-bot",
    )
    state = MonitorState()

    await runner._run_fix_cycle(
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

    assert sleep_fn.calls == [30, 5]
    assert state.threads_addressed_ids["T_retry"] == "fix_committed"
    assert _review_thread_body_state_key("T_retry") in state.threads_addressed_ids
    worktree = tmp_path / "worktrees" / workspace_id
    assert cmd.calls[0].args[:3] == ["gh", "api", "graphql"]
    assert cmd.calls[1].args[:5] == _git_worktree_command(worktree)
    assert cmd.calls[1].args[5] == "push"
    assert cmd.calls[2].args[:3] == ["gh", "api", "graphql"]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        events = _retry_events(ws)
        assert len(events) == 1
        assert events[0].reason_code == "GITHUB_TRANSIENT_RETRY"
        assert events[0].payload["context"] == "fix_cycle_settle_fetch_pr_status"
        assert events[0].payload["operation"] == "gh api graphql"


@pytest.mark.unit
async def test_resolve_thread_transient_failure_requeues_thread_safely(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    adapter = FakeAdapter()
    adapter.queue(stdout="Committed fix locally.")
    cmd.queue_result(returncode=0, stdout=pr_payload())
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="newsha\n")
    cmd.queue_result(returncode=1, stderr="HTTP 502 Bad Gateway")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    thread = ReviewThread(
        thread_id="T_resolve",
        path="src/foo.py",
        line=12,
        body_excerpt="please adjust this",
        author="review-bot",
    )
    state = MonitorState()

    await runner._run_fix_cycle(
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

    assert sleep_fn.calls == [30, 5]
    assert "T_resolve" not in state.threads_addressed_ids
    assert state.last_push_sha == "newsha"
    worktree = tmp_path / "worktrees" / workspace_id
    assert cmd.calls[0].args[:3] == ["gh", "api", "graphql"]
    assert cmd.calls[1].args[:5] == _git_worktree_command(worktree)
    assert cmd.calls[2].args[:5] == _git_worktree_command(worktree)
    assert cmd.calls[3].args[:3] == ["gh", "api", "graphql"]
    assert cmd.calls[1].args[5] == "push"
    assert cmd.calls[2].args[5:7] == ["rev-parse", "HEAD"]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        events = _retry_events(ws)
        resolution_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.comment_resolution",
            limit=10,
        )
        assert len(events) == 1
        assert events[0].reason_code == "GITHUB_TRANSIENT_RETRY"
        assert events[0].payload["context"] == "resolve_thread"
        assert events[0].payload["operation"] == "gh api graphql"
    assert len(resolution_events) == 1
    assert resolution_events[0].payload is not None
    assert resolution_events[0].payload["action"] == "resolve_thread"
    assert resolution_events[0].payload["outcome"] == "requeued"
    assert resolution_events[0].payload["evidence"] == {
        "thread_ids": ["T_resolve"],
        "resolved_thread_count": 0,
        "requeued_thread_count": 1,
        "error_message": "gh api graphql failed (exit=1): HTTP 502 Bad Gateway",
    }
    assert "please adjust this" not in repr(resolution_events[0].payload)


@pytest.mark.unit
async def test_resolve_thread_transient_bitbucket_failure_requeues_thread_safely(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # Bitbucket workspaces resolve threads via BitbucketClient, which raises
    # BitbucketClientError (not GitHubClientError). A transient blip (rate limit)
    # must requeue the thread and keep the monitor polling — without the Bitbucket
    # resolve arm the error escapes the fix cycle and the runner terminates the
    # workspace instead of re-addressing the still-open thread.
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    adapter = FakeAdapter()
    adapter.queue(stdout="Committed fix locally.")
    cmd.queue_result(returncode=0, stdout=pr_payload())
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="newsha\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=_BitbucketResolveThreadClient(
            cmd,
            BitbucketClientError(
                operation="bitbucket resolve_thread",
                status=429,
                body="rate limited",
                reason_code=BITBUCKET_RATE_LIMITED,
            ),
        ),
    )
    thread = ReviewThread(
        thread_id="T_resolve",
        path="src/foo.py",
        line=12,
        body_excerpt="please adjust this",
        author="review-bot",
    )
    state = MonitorState()

    await runner._run_fix_cycle(
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

    # The addressed marker is rolled back so the next poll re-addresses the
    # still-open thread; the push bookkeeping is unaffected.
    assert "T_resolve" not in state.threads_addressed_ids
    assert state.last_push_sha == "newsha"
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        # Not terminated: the Bitbucket resolve fault is handled in-loop.
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        retry_events = [
            event
            for event in ws.events
            if event.event_type == "monitor.bitbucket_transient_error_retrying"
        ]
        resolution_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.comment_resolution",
            limit=10,
        )
    assert len(retry_events) == 1
    assert retry_events[0].reason_code == "BITBUCKET_TRANSIENT_RETRY"
    assert retry_events[0].payload["context"] == "resolve_thread"
    assert len(resolution_events) == 1
    assert resolution_events[0].payload is not None
    assert resolution_events[0].payload["action"] == "resolve_thread"
    assert resolution_events[0].payload["outcome"] == "requeued"
    assert resolution_events[0].payload["evidence"] == {
        "thread_ids": ["T_resolve"],
        "resolved_thread_count": 0,
        "requeued_thread_count": 1,
        "error_message": "bitbucket resolve_thread failed (status=429): rate limited",
    }
    assert "please adjust this" not in repr(resolution_events[0].payload)


@pytest.mark.unit
async def test_resolve_thread_permanent_bitbucket_failure_keeps_monitor_alive(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # A permanent Bitbucket fault during resolve_thread must forward the
    # forge-native reason code and clear the addressed marker WITHOUT escaping the
    # fix cycle — mirroring the GitHub arm's "do NOT drop out of the monitor"
    # behaviour rather than terminating the workspace, and keeping the fault
    # diagnosable instead of collapsing it to a generic placeholder. A non-auth 4xx
    # (``BITBUCKET_API_ERROR`` 404) is genuinely deterministic — 401/403 are now
    # bounded-retryable (#515), so they would route through the transient arm.
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    adapter = FakeAdapter()
    adapter.queue(stdout="Committed fix locally.")
    cmd.queue_result(returncode=0, stdout=pr_payload())
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="newsha\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=_BitbucketResolveThreadClient(
            cmd,
            BitbucketClientError(
                operation="bitbucket resolve_thread",
                status=404,
                body="thread not found",
                reason_code=BITBUCKET_API_ERROR,
            ),
        ),
    )
    thread = ReviewThread(
        thread_id="T_resolve",
        path="src/foo.py",
        line=12,
        body_excerpt="please adjust this",
        author="review-bot",
    )
    state = MonitorState()

    # No raise: the Bitbucket resolve fault is caught and handled in-loop.
    await runner._run_fix_cycle(
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

    # decide() filters addressed IDs, so a failed resolve must not leave the marker
    # behind (it would treat the open thread as handled forever).
    assert "T_resolve" not in state.threads_addressed_ids
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        # Not terminated: the workspace keeps polling.
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        retry_events = [
            event
            for event in ws.events
            if event.event_type == "monitor.bitbucket_transient_error_retrying"
        ]
        resolution_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.comment_resolution",
            limit=10,
        )
    # A deterministic fault does not record a transient-retry event.
    assert retry_events == []
    assert len(resolution_events) == 1
    assert resolution_events[0].payload is not None
    assert resolution_events[0].payload["action"] == "resolve_thread"
    assert resolution_events[0].payload["outcome"] == "failed"
    assert resolution_events[0].payload["reason_code"] == BITBUCKET_API_ERROR
    assert resolution_events[0].payload["evidence"] == {
        "thread_ids": ["T_resolve"],
        "resolved_thread_count": 0,
        "failed_thread_count": 1,
        "error_message": "bitbucket resolve_thread failed (status=404): thread not found",
    }
    assert "please adjust this" not in repr(resolution_events[0].payload)


@pytest.mark.unit
async def test_fix_cycle_treats_transient_bitbucket_settle_poll_as_retryable(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # Bitbucket workspaces re-poll the PR during the settle window via
    # BitbucketClient, which raises BitbucketClientError (not GitHubClientError).
    # A transient blip must break settle and proceed to push the locally committed
    # fixes — without the Bitbucket settle arm the error escapes _execute and the
    # runner continues the monitor instead, stranding the committed fixes.
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    adapter = FakeAdapter()
    adapter.queue(stdout="Committed fix locally.")
    cmd.queue_result(returncode=0, stderr="Everything up-to-date")
    cmd.queue_result(returncode=0, stdout="{}")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=_BitbucketSettlePollClient(
            cmd,
            BitbucketClientError(
                operation="bitbucket fetch_pr_status",
                status=429,
                body="rate limited",
                reason_code=BITBUCKET_RATE_LIMITED,
            ),
        ),
    )
    thread = ReviewThread(
        thread_id="T_settle",
        path="src/foo.py",
        line=12,
        body_excerpt="please adjust this",
        author="review-bot",
    )
    state = MonitorState()

    await runner._run_fix_cycle(
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

    # The transient blip breaks the settle loop and proceeds to push + resolve;
    # the addressed marker survives because the fix shipped.
    assert sleep_fn.calls == [30, 5]
    assert state.threads_addressed_ids["T_settle"] == "fix_committed"
    worktree = tmp_path / "worktrees" / workspace_id
    assert cmd.calls[0].args[:5] == _git_worktree_command(worktree)
    assert cmd.calls[0].args[5] == "push"
    assert cmd.calls[1].args[:3] == ["gh", "api", "graphql"]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        # Not terminated: the Bitbucket settle fault is handled in-loop.
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        retry_events = [
            event
            for event in ws.events
            if event.event_type == "monitor.bitbucket_transient_error_retrying"
        ]
    assert len(retry_events) == 1
    assert retry_events[0].reason_code == "BITBUCKET_TRANSIENT_RETRY"
    assert retry_events[0].payload["context"] == "fix_cycle_settle_fetch_pr_status"


@pytest.mark.unit
async def test_fix_cycle_reraises_permanent_bitbucket_settle_poll_error(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # A permanent Bitbucket fault (403, token lacks the scope) during the settle
    # re-poll must propagate like the GitHub arm's non-transient branch rather than
    # being swallowed.
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(stdout="Committed fix locally.")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        gh=_BitbucketSettlePollClient(
            cmd,
            BitbucketClientError(
                operation="bitbucket fetch_pr_status",
                status=403,
                body="forbidden: missing scope",
            ),
        ),
    )
    thread = ReviewThread(
        thread_id="T_settle_perm",
        path="src/foo.py",
        line=12,
        body_excerpt="please adjust this",
        author="review-bot",
    )

    with pytest.raises(BitbucketClientError, match="forbidden: missing scope"):
        await runner._run_fix_cycle(
            workspace_id="ws_bb_settle_perm",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            pr_head_sha="abc1234567890def",
            initial_threads=(thread,),
            initial_reviews=(),
            state=MonitorState(),
            remote_branch="awf/ws_bb_settle_perm",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )


@pytest.mark.unit
async def test_fix_cycle_reraises_non_transient_settle_poll_error(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    adapter = FakeAdapter()
    adapter.queue(stdout="Committed fix locally.")
    cmd.queue_result(returncode=1, stderr="bad credentials")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    thread = ReviewThread(
        thread_id="T_auth",
        path="src/foo.py",
        line=12,
        body_excerpt="please adjust this",
        author="reviewer",
    )

    with pytest.raises(GitHubClientError, match="bad credentials"):
        await runner._run_fix_cycle(
            workspace_id="ws_auth",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            pr_head_sha="abc1234567890def",
            initial_threads=(thread,),
            initial_reviews=(),
            state=MonitorState(),
            remote_branch="awf/ws_auth",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )


@pytest.mark.unit
async def test_fix_cycle_returns_failed_push_when_thread_fix_hits_policy_block(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    thread = ReviewThread(
        thread_id="T_supply",
        path="src/foo.py",
        line=12,
        body_excerpt="please adjust this",
        author="reviewer",
    )

    async def _blocked_thread(**_kwargs: object) -> str:
        raise _MonitorPolicyBlockedError("Supply-chain policy blocked thread fix.")

    monkeypatch.setattr(runner, "_address_thread", _blocked_thread)

    result = await runner._run_fix_cycle(
        workspace_id="ws_supply_thread",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(thread,),
        initial_reviews=(),
        state=MonitorState(),
        remote_branch="awf/ws_supply_thread",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.pushed is False
    assert result.returncode == 1
    assert "Supply-chain policy blocked thread fix" in result.stderr


@pytest.mark.unit
async def test_fix_cycle_returns_failed_push_when_thread_fix_hits_ownership_repair_failure(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    thread = ReviewThread(
        thread_id="T_owned",
        path="src/foo.py",
        line=12,
        body_excerpt="please adjust this",
        author="reviewer",
    )

    async def _ownership_repair_failed(**_kwargs: object) -> str:
        raise _MonitorAgentRuntimeOwnershipRepairFailedError(
            AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
        )

    monkeypatch.setattr(runner, "_address_thread", _ownership_repair_failed)

    result = await runner._run_fix_cycle(
        workspace_id="ws_ownership_fix_cycle",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(thread,),
        initial_reviews=(),
        state=MonitorState(),
        remote_branch="awf/ws_ownership_fix_cycle",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.pushed is False
    assert result.returncode == 1
    assert result.reason_code == AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE
    assert result.stderr == AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE


@pytest.mark.unit
async def test_fix_cycle_returns_failed_push_when_thread_fix_hits_head_object_missing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    thread = ReviewThread(
        thread_id="T_head",
        path="src/foo.py",
        line=12,
        body_excerpt="please adjust this",
        author="reviewer",
    )

    async def _head_object_missing(**_kwargs: object) -> str:
        raise _MonitorHeadObjectMissingError(
            "HEAD_OBJECT_MISSING_UNRECOVERABLE",
            "HEAD object missing for workspace ws_head_thread and recovery failed",
        )

    monkeypatch.setattr(runner, "_address_thread", _head_object_missing)

    result = await runner._run_fix_cycle(
        workspace_id="ws_head_thread",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(thread,),
        initial_reviews=(),
        state=MonitorState(),
        remote_branch="awf/ws_head_thread",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.pushed is False
    assert result.returncode == 1
    assert result.reason_code == "HEAD_OBJECT_MISSING_UNRECOVERABLE"
    assert "HEAD object missing" in result.stderr


@pytest.mark.unit
async def test_fix_cycle_falls_back_when_per_item_head_object_is_poisoned(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # cat-file start^{commit}
    cmd.queue_result(returncode=128, stderr="fatal: Not a valid object name poisoned")
    worktrees_root = tmp_path / "worktrees"
    worktree_path = worktrees_root / "ws_poisoned_head"
    worktree_path.mkdir(parents=True)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=worktrees_root,
    )
    threads = (
        ReviewThread(
            thread_id="T_first",
            path="src/foo.py",
            line=12,
            body_excerpt="please adjust this first",
            author="reviewer",
        ),
        ReviewThread(
            thread_id="T_second",
            path="src/foo.py",
            line=13,
            body_excerpt="please adjust this second",
            author="reviewer",
        ),
    )
    operation_start_heads: list[str | None] = []

    async def _start_head(**_kwargs: object) -> tuple[str, None]:
        return ("start", None)

    async def _no_dirty(**_kwargs: object) -> None:
        return None

    current_heads = iter(("start", "poisoned"))

    async def _rev_parse_head(_worktree_path: Path) -> str | None:
        return next(current_heads)

    async def _address(**kwargs: object) -> str:
        operation_start_heads.append(cast(str | None, kwargs["operation_start_head"]))
        return "false_positive"

    async def _clean_status(**_kwargs: object) -> PRStatus:
        return PRStatus(
            number=42,
            head_sha="start",
            mergeable=MergeableState.MERGEABLE,
            check_state=CheckState.SUCCESS,
            unresolved_inline_threads=(),
            unresolved_review_comments=(),
            base_behind_count=0,
            merge_state_status=MergeStateStatus.CLEAN,
        )

    async def _no_block(**_kwargs: object) -> None:
        return None

    async def _validated(**_kwargs: object) -> _GitPushResult:
        return _GitPushResult(pushed=False, failed=False, returncode=0)

    async def _resolve_thread(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(runner, "_pre_existing_dirty_repair_worktree_result", _no_dirty)
    monkeypatch.setattr(runner, "_repair_operation_start_head_result", _start_head)
    monkeypatch.setattr(runner, "_rev_parse_head", _rev_parse_head)
    monkeypatch.setattr(runner, "_address_thread", _address)
    monkeypatch.setattr(runner._deps.gh, "fetch_pr_status", _clean_status)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _no_block)
    monkeypatch.setattr(runner, "_validated_git_push_result", _validated)
    monkeypatch.setattr(runner._deps.gh, "resolve_thread", _resolve_thread)

    await runner._run_fix_cycle(
        workspace_id="ws_poisoned_head",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="start",
        initial_threads=threads,
        initial_reviews=(),
        state=MonitorState(),
        remote_branch="awf/ws_poisoned_head",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert operation_start_heads == ["start", "start"]
    cat_file_calls = [call for call in cmd.calls if call.args[-3:-1] == ["cat-file", "-e"]]
    assert [call.args[-1] for call in cat_file_calls] == [
        "start^{commit}",
        "poisoned^{commit}",
    ]


@pytest.mark.unit
async def test_fix_cycle_returns_failed_push_when_review_fix_hits_head_object_missing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    review = ReviewComment(
        comment_id="C_head",
        body_excerpt="please adjust this",
        author="reviewer",
    )

    async def _head_object_missing(**_kwargs: object) -> object:
        raise _MonitorHeadObjectMissingError(
            "HEAD_OBJECT_MISSING_UNRECOVERABLE",
            "HEAD object missing for workspace ws_head_review and recovery failed",
        )

    monkeypatch.setattr(runner, "_address_review_comment_result", _head_object_missing)

    result = await runner._run_fix_cycle(
        workspace_id="ws_head_review",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(),
        initial_reviews=(review,),
        state=MonitorState(),
        remote_branch="awf/ws_head_review",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.pushed is False
    assert result.returncode == 1
    assert result.reason_code == "HEAD_OBJECT_MISSING_UNRECOVERABLE"
    assert "HEAD object missing" in result.stderr


@pytest.mark.unit
async def test_fix_cycle_clears_addressed_thread_state_on_protected_scope_early_return(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    fixed_thread = ReviewThread(
        thread_id="T_fixed",
        path="src/foo.py",
        line=12,
        body_excerpt="please adjust this first",
        author="reviewer",
    )
    blocked_thread = ReviewThread(
        thread_id="T_blocked",
        path="src/foo.py",
        line=24,
        body_excerpt="then protected scope diff fails",
        author="reviewer",
    )
    state = MonitorState()

    async def _address_thread(**kwargs: object) -> str:
        thread = kwargs["thread"]
        assert isinstance(thread, ReviewThread)
        if thread.thread_id == fixed_thread.thread_id:
            return "fix_committed"
        raise ProtectedScopeDiffError("diff baseline unavailable")

    async def _protected_scope_result(**kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr=str(kwargs["exc"]),
            reason_code="PROTECTED_SCOPE_DIFF_UNAVAILABLE",
        )

    monkeypatch.setattr(runner, "_address_thread", _address_thread)
    monkeypatch.setattr(
        runner,
        "_protected_scope_diff_unavailable_push_result",
        _protected_scope_result,
    )

    result = await runner._run_fix_cycle(
        workspace_id="ws_protected_thread",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(fixed_thread, blocked_thread),
        initial_reviews=(),
        state=state,
        remote_branch="awf/ws_protected_thread",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert "T_fixed" not in state.threads_addressed_ids
    assert _review_thread_body_state_key("T_fixed") not in state.threads_addressed_ids


@pytest.mark.unit
async def test_fix_cycle_clears_addressed_thread_state_on_policy_blocked_thread(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #305: a _MonitorPolicyBlockedError on a later thread must roll back items
    # already addressed this cycle (e.g. a captured defer in publish_dependent_ids),
    # or they stay marked-addressed-but-unresolved and wedge the merge gate.
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    fixed_thread = ReviewThread(
        thread_id="T_fixed", path="src/foo.py", line=12, body_excerpt="fix me first", author="rev"
    )
    blocked_thread = ReviewThread(
        thread_id="T_blocked",
        path="src/foo.py",
        line=24,
        body_excerpt="then policy blocks",
        author="rev",
    )
    state = MonitorState()

    async def _address_thread(**kwargs: object) -> str:
        thread = kwargs["thread"]
        assert isinstance(thread, ReviewThread)
        if thread.thread_id == fixed_thread.thread_id:
            return "fix_committed"
        raise _MonitorPolicyBlockedError("Supply-chain policy blocked thread fix.")

    monkeypatch.setattr(runner, "_address_thread", _address_thread)

    result = await runner._run_fix_cycle(
        workspace_id="ws_policy_thread",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(fixed_thread, blocked_thread),
        initial_reviews=(),
        state=state,
        remote_branch="awf/ws_policy_thread",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert "Supply-chain policy blocked" in result.stderr
    assert "T_fixed" not in state.threads_addressed_ids
    assert _review_thread_body_state_key("T_fixed") not in state.threads_addressed_ids


@pytest.mark.unit
async def test_task_resolve_forbidden_blocks_as_needs_human_without_retry_storm(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A Bitbucket reviewer task whose resolution PUT is forbidden (403) must
    downgrade to ``needs_human`` rather than clear the addressed marker (#445).

    Clearing it like a comment thread would re-route the task to AddressComments
    next poll and re-run the agent forever against a fault it cannot fix (a retry
    storm). Instead the verdict becomes ``needs_human``: the task stays addressed so
    it does NOT re-route to the agent, the still-open task keeps blocking merge, and
    decide() escalates to NotifyHuman. The task-resolution reason code is preserved.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    adapter = FakeAdapter()
    adapter.queue(stdout="Committed fix locally.")
    cmd.queue_result(returncode=0, stdout=pr_payload())
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="newsha\n")
    task_thread_id = "bbtask:dimileeh/aira-web#42:7"
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=_BitbucketResolveThreadClient(
            cmd,
            BitbucketClientError(
                operation="bitbucket resolve_task",
                status=403,
                body="no task-resolution scope",
                reason_code=BITBUCKET_TASK_RESOLVE_FORBIDDEN,
            ),
        ),
    )
    task_thread = ReviewThread(
        thread_id=task_thread_id,
        path=None,
        line=None,
        body_excerpt="please add a regression test",
        author="reviewer",
    )
    state = MonitorState()

    await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(task_thread,),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    # The task is downgraded to needs_human (kept addressed → no re-address storm),
    # NOT cleared.
    assert state.threads_addressed_ids.get(task_thread_id) == "needs_human"
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        # Not terminated: the forbidden task-resolve is handled in-loop as a blocker.
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        retry_events = [
            event
            for event in ws.events
            if event.event_type == "monitor.bitbucket_transient_error_retrying"
        ]
        resolution_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.comment_resolution",
            limit=10,
        )
    # A deterministic 403 fault must not record transient retry events.
    assert retry_events == []
    assert len(resolution_events) == 1
    payload = resolution_events[0].payload
    assert payload is not None
    assert payload["action"] == "resolve_thread"
    assert payload["outcome"] == "needs_human"
    assert resolution_events[0].reason_code == BITBUCKET_TASK_RESOLVE_FORBIDDEN
    assert payload["evidence"]["needs_human_thread_count"] == 1
    # Task body_excerpt should not leak into event payloads.
    assert "please add a regression test" not in repr(resolution_events[0].payload)


@pytest.mark.unit
async def test_resolve_thread_exhausted_transient_blocks_as_needs_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """When a still-open comment thread's resolve keeps hitting a transient forge
    fault until the bounded retry budget is exhausted, the fix cycle must escalate
    to ``needs_human`` rather than clear the addressed marker.

    Clearing the marker (the deterministic-fault path) would re-route the still-open
    thread through AddressComments next poll, immediately re-exhaust the deliberately
    persisted counter on the next resolve, and re-run the agent every poll — the
    exact storm the bounded budget exists to prevent. ``needs_human`` keeps the
    thread UNRESOLVED so the merge gate keeps blocking and decide() routes to
    NotifyHuman, and the exhausted reason code stays diagnosable in the audit event.
    """
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    adapter = FakeAdapter()
    adapter.queue(stdout="Committed fix locally.")
    cmd.queue_result(returncode=0, stdout=pr_payload())
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="newsha\n")
    cmd.queue_result(returncode=1, stderr="HTTP 502 Bad Gateway")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    # A budget of 0 retries exhausts on the first transient resolve fault — no
    # backoff sleep, straight to the exhausted path within this single fix cycle.
    object.__setattr__(runner._runner_config, "transient_forge_max_retries", 0)
    thread = ReviewThread(
        thread_id="T_resolve",
        path="src/foo.py",
        line=12,
        body_excerpt="please adjust this",
        author="review-bot",
    )
    state = MonitorState()

    await runner._run_fix_cycle(
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

    # Escalated to needs_human (kept addressed → no re-address storm), NOT cleared.
    assert state.threads_addressed_ids.get("T_resolve") == "needs_human"
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        # Not terminated: the workspace keeps polling.
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        # Exhaustion is recorded as such; no further backoff sleep happened.
        exhausted_events = [
            event
            for event in ws.events
            if event.event_type == "monitor.github_transient_error_retry_exhausted"
        ]
        assert _retry_events(ws) == []
        resolution_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.comment_resolution",
            limit=10,
        )
    assert len(exhausted_events) == 1
    assert exhausted_events[0].reason_code == "GITHUB_TRANSIENT_RETRY_EXHAUSTED"
    assert len(resolution_events) == 1
    payload = resolution_events[0].payload
    assert payload is not None
    assert payload["action"] == "resolve_thread"
    assert payload["outcome"] == "needs_human"
    assert resolution_events[0].reason_code == "GITHUB_TRANSIENT_RETRY_EXHAUSTED"
    assert payload["evidence"]["needs_human_thread_count"] == 1
    assert payload["evidence"]["thread_ids"] == ["T_resolve"]
    assert "please adjust this" not in repr(resolution_events[0].payload)


@pytest.mark.unit
async def test_resolve_thread_exhausted_transient_bitbucket_blocks_as_needs_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Bitbucket symmetry: an exhausted transient resolve budget on a still-open
    comment thread escalates to ``needs_human`` with the Bitbucket exhausted reason
    code, rather than clearing the marker and re-running the agent forever."""
    workspace_id = await seed_monitoring_workspace(factory)
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    adapter = FakeAdapter()
    adapter.queue(stdout="Committed fix locally.")
    cmd.queue_result(returncode=0, stdout=pr_payload())
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="newsha\n")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=adapter,
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=_BitbucketResolveThreadClient(
            cmd,
            BitbucketClientError(
                operation="bitbucket resolve_thread",
                status=429,
                body="rate limited",
                reason_code=BITBUCKET_RATE_LIMITED,
            ),
        ),
    )
    object.__setattr__(runner._runner_config, "transient_forge_max_retries", 0)
    thread = ReviewThread(
        thread_id="T_resolve",
        path="src/foo.py",
        line=12,
        body_excerpt="please adjust this",
        author="review-bot",
    )
    state = MonitorState()

    await runner._run_fix_cycle(
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

    assert state.threads_addressed_ids.get("T_resolve") == "needs_human"
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        exhausted_events = [
            event
            for event in ws.events
            if event.event_type == "monitor.bitbucket_transient_error_retry_exhausted"
        ]
        resolution_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.comment_resolution",
            limit=10,
        )
    assert len(exhausted_events) == 1
    assert exhausted_events[0].reason_code == "BITBUCKET_TRANSIENT_RETRY_EXHAUSTED"
    assert len(resolution_events) == 1
    payload = resolution_events[0].payload
    assert payload is not None
    assert payload["action"] == "resolve_thread"
    assert payload["outcome"] == "needs_human"
    assert resolution_events[0].reason_code == "BITBUCKET_TRANSIENT_RETRY_EXHAUSTED"
    assert payload["evidence"]["needs_human_thread_count"] == 1
    assert "please adjust this" not in repr(resolution_events[0].payload)
