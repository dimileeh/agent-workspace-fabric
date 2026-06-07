"""Focused branch-coverage tests for PR monitor runner edge behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import structlog
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.bitbucket_client import BITBUCKET_RATE_LIMITED, BitBucketClientError
from awf.common.commands import FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.common.github_client import GitHubClientError, RepoRef
from awf.control.quality_gates import QualityGateViolation
from awf.db.enums import OperationStatus, OperationType, TaskClass, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    OperationRepository,
    WorkspaceEventCreate,
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
from awf.runtime.pr_monitor_runner import (
    PullRequestMonitorRunner,
)
from awf.runtime.pr_monitor_runner import comments as pr_monitor_runner_comments
from awf.runtime.pr_monitor_runner.helpers import (
    _review_comment_body_state_key,
)
from awf.runtime.pr_monitor_runner.remote_ops import (
    _GitPushResult,
    _ProtectedScopePushBlock,
)
from awf.runtime.pr_monitor_runner.types import (
    ProtectedScopeDiffError,
    _MonitorAgentRuntimeOwnershipRepairFailedError,
    _MonitorPolicyBlockedError,
)
from awf.service.merge_queue import MergeQueueBlocker
from tests.postgres import postgres_test_engine
from tests.shared.monitor_runner import DefaultMergeMethodGitHubClient
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
)


class _BitBucketResolveThreadClient(DefaultMergeMethodGitHubClient):
    """Command-based gh double whose ``resolve_thread`` raises a BitBucket error.

    A BitBucket workspace's ``self._deps.gh`` is a ``BitBucketClient`` that raises
    ``BitBucketClientError`` (not ``GitHubClientError``) from ``resolve_thread``.
    Overriding only that method keeps the push/settle-poll flow command-based while
    exercising the fix cycle's forge-neutral BitBucket resolve arm.
    """

    def __init__(self, runner: FakeCommandRunner, exc: BitBucketClientError) -> None:
        super().__init__(runner)
        self._resolve_exc = exc

    async def resolve_thread(self, *, thread_id: str) -> None:
        del thread_id
        raise self._resolve_exc


class _BitBucketSettlePollClient(DefaultMergeMethodGitHubClient):
    """Command-based gh double whose ``fetch_pr_status`` raises a BitBucket error.

    A BitBucket workspace's ``self._deps.gh`` is a ``BitBucketClient`` that raises
    ``BitBucketClientError`` (not ``GitHubClientError``) from ``fetch_pr_status``.
    Overriding only that method keeps the push/resolve flow command-based while
    exercising the fix cycle's settle re-poll BitBucket arm.
    """

    def __init__(self, runner: FakeCommandRunner, exc: BitBucketClientError) -> None:
        super().__init__(runner)
        self._fetch_exc = exc

    async def fetch_pr_status(
        self, *, repo: RepoRef, pr_number: int, base_behind_count: int
    ) -> PRStatus:
        del repo, pr_number, base_behind_count
        raise self._fetch_exc


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


_PROTECTED_WORKFLOW_OLD = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
  lint:
    runs-on: ubuntu-latest
    steps:
      - name: Run ruff
        run: uv run ruff check
""".strip()
_PROTECTED_WORKFLOW_BLOCKED = """
name: CI
on: [pull_request]
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - name: Run pytest
        run: uv run pytest
        continue-on-error: true
""".strip()


def _queue_protected_workflow_diff(
    cmd: FakeCommandRunner,
    *,
    old_text: str = _PROTECTED_WORKFLOW_OLD,
    new_text: str = _PROTECTED_WORKFLOW_BLOCKED,
) -> None:
    cmd.queue_result(returncode=0)  # cat-file base:path
    cmd.queue_result(returncode=0, stdout=old_text)
    cmd.queue_result(returncode=0)  # cat-file HEAD:path
    cmd.queue_result(returncode=0, stdout=new_text)


class _FailingLogSink:
    async def write(self, data: str) -> None:
        del data
        raise RuntimeError("log sink unavailable")


class _RecordingLogSink:
    stream_id = "monitor.log"

    def __init__(self) -> None:
        self.lines: list[str] = []

    async def write(self, data: str) -> None:
        self.lines.append(data)


class _ExplodingRunner:
    async def run(self, args: list[str], **_kwargs: object) -> object:
        del args
        raise RuntimeError("runner unavailable")


class _CleanupFailingAdapter(FakeAdapter):
    async def run(self, **_kwargs: object) -> object:  # type: ignore[override]
        raise ComposeExecCleanupError(
            invocation_id="awf_monitor_cleanup_failed",
            source="agent",
            label="monitor",
            message="tagged process still alive",
        )


class _QueueAfterLockRunner(PullRequestMonitorRunner):
    def __init__(self, *, blocker: MergeQueueBlocker, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._blocker = blocker
        self.blocker_calls = 0

    async def _merge_queue_blockers_for_workspace(
        self,
        workspace_id: str,
    ) -> list[MergeQueueBlocker]:
        assert workspace_id
        self.blocker_calls += 1
        return [] if self.blocker_calls == 1 else [self._blocker]


class _StopAfterRetryError(RuntimeError):
    pass


class _StopAfterRetrySleep(RecordedSleep):
    async def __call__(self, seconds: float) -> None:
        await super().__call__(seconds)
        raise _StopAfterRetryError


def _retry_events(ws: Workspace) -> list:
    return [
        event
        for event in ws.events
        if event.event_type == "monitor.github_transient_error_retrying"
    ]


def _assert_committed_diff_phase_ran(
    cmd: FakeCommandRunner,
    *,
    worktree_path: Path,
    remote_branch: str,
    remote: str = "origin",
) -> None:
    call_args = [call.args for call in cmd.calls]
    assert (
        _git_worktree_command(
            worktree_path,
            "fetch",
            remote,
            f"refs/heads/{remote_branch}",
        )
        in call_args
    )
    assert (
        _git_worktree_command(
            worktree_path,
            "merge-base",
            "FETCH_HEAD",
            "HEAD",
        )
        in call_args
    )


def _git_worktree_command(worktree_path: Path, *args: str) -> list[str]:
    return ["git", "-c", f"safe.directory={worktree_path}", "-C", str(worktree_path), *args]


def _name_status_z(*paths: str) -> str:
    return "".join(f"M\0{path}\0" for path in paths)


async def _mark_refactor_task(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    auto_merge: bool,
) -> None:
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        ws.task_class = TaskClass.refactor_task.value
        ws.auto_merge = auto_merge
        await s.commit()


async def _seed_running_operation(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> str:
    async with factory() as s:
        operation = await OperationRepository(s).create(
            workspace_id=workspace_id,
            operation_type=OperationType.refresh,
            status=OperationStatus.running,
            payload={"source": "test", "keep": True},
            idempotency_key=f"op:{workspace_id}",
        )
        await s.commit()
        return operation.id


async def _update_workspace(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    **values: object,
) -> None:
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        for key, value in values.items():
            setattr(ws, key, value)
        await s.commit()


async def _force_workspace_status(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    status: WorkspaceStatus,
) -> None:
    async with factory() as s:
        await s.execute(
            sa_update(Workspace).where(Workspace.id == workspace_id).values(status=status.value)
        )
        await s.commit()


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
    assert sleep_fn.calls == [60]
    assert len(cmd.calls) == 1
    assert cmd.calls[0].args[:3] == ["gh", "pr", "merge"]
    assert state.threads_addressed_ids == {}
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
        assert events[0].payload["wait_seconds"] == 60


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
    assert sleep_fn.calls == [60]
    assert len(cmd.calls) == 1
    assert cmd.calls[0].args[:3] == ["gh", "pr", "comment"]
    assert state.threads_addressed_ids == {}
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
        assert events[0].payload["wait_seconds"] == 60


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

    assert sleep_fn.calls == [30, 60]
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

    assert sleep_fn.calls == [30, 60]
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
    # BitBucket workspaces resolve threads via BitBucketClient, which raises
    # BitBucketClientError (not GitHubClientError). A transient blip (rate limit)
    # must requeue the thread and keep the monitor polling — without the BitBucket
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
        gh=_BitBucketResolveThreadClient(
            cmd,
            BitBucketClientError(
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
        # Not terminated: the BitBucket resolve fault is handled in-loop.
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
    # A permanent BitBucket fault (403, token lacks the scope) during resolve_thread
    # must record COMMENT_RESOLUTION_FAILED and clear the addressed marker WITHOUT
    # escaping the fix cycle — mirroring the GitHub arm's "do NOT drop out of the
    # monitor" behaviour rather than terminating the workspace.
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
        gh=_BitBucketResolveThreadClient(
            cmd,
            BitBucketClientError(
                operation="bitbucket resolve_thread",
                status=403,
                body="forbidden: missing scope",
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

    # No raise: the BitBucket resolve fault is caught and handled in-loop.
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
    assert resolution_events[0].payload["reason_code"] == "COMMENT_RESOLUTION_FAILED"
    assert resolution_events[0].payload["evidence"] == {
        "thread_ids": ["T_resolve"],
        "resolved_thread_count": 0,
        "failed_thread_count": 1,
        "error_message": "bitbucket resolve_thread failed (status=403): forbidden: missing scope",
    }
    assert "please adjust this" not in repr(resolution_events[0].payload)


@pytest.mark.unit
async def test_fix_cycle_treats_transient_bitbucket_settle_poll_as_retryable(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # BitBucket workspaces re-poll the PR during the settle window via
    # BitBucketClient, which raises BitBucketClientError (not GitHubClientError).
    # A transient blip must break settle and proceed to push the locally committed
    # fixes — without the BitBucket settle arm the error escapes _execute and the
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
        gh=_BitBucketSettlePollClient(
            cmd,
            BitBucketClientError(
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
    assert sleep_fn.calls == [30, 60]
    assert state.threads_addressed_ids["T_settle"] == "fix_committed"
    worktree = tmp_path / "worktrees" / workspace_id
    assert cmd.calls[0].args[:5] == _git_worktree_command(worktree)
    assert cmd.calls[0].args[5] == "push"
    assert cmd.calls[1].args[:3] == ["gh", "api", "graphql"]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        # Not terminated: the BitBucket settle fault is handled in-loop.
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
    # A permanent BitBucket fault (403, token lacks the scope) during the settle
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
        gh=_BitBucketSettlePollClient(
            cmd,
            BitBucketClientError(
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

    with pytest.raises(BitBucketClientError, match="forbidden: missing scope"):
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
async def test_fix_cycle_clears_addressed_thread_state_on_policy_blocked_review(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The review-comment loop's policy-blocked exit must also roll back the
    # thread already addressed earlier in the same cycle.
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
    blocked_comment = ReviewComment(comment_id="C_blocked", body_excerpt="policy blocks review")
    state = MonitorState()

    async def _address_thread(**_kwargs: object) -> str:
        return "fix_committed"

    async def _address_review(**_kwargs: object) -> object:
        raise _MonitorPolicyBlockedError("Supply-chain policy blocked review fix.")

    monkeypatch.setattr(runner, "_address_thread", _address_thread)
    monkeypatch.setattr(runner, "_address_review_comment_result", _address_review)

    result = await runner._run_fix_cycle(
        workspace_id="ws_policy_review",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(fixed_thread,),
        initial_reviews=(blocked_comment,),
        state=state,
        remote_branch="awf/ws_policy_review",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert "Supply-chain policy blocked" in result.stderr
    assert "T_fixed" not in state.threads_addressed_ids


@pytest.mark.unit
async def test_fix_cycle_rolls_back_protected_scope_delta_and_keeps_comment_unaddressed(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # clean worktree before repair
    cmd.queue_result(returncode=0, stdout="start-sha\n")  # operation start HEAD
    cmd.queue_result(returncode=0, stdout="blocked-head-sha\n")  # attempted HEAD
    cmd.queue_result(
        returncode=0,
        stdout=_name_status_z(".github/workflows/ci.yml", "plans/PR282_CI_SETUP_UV_VALIDATION.md"),
    )
    cmd.queue_result(returncode=0, stdout="")
    cmd.queue_result(returncode=0, stdout="HEAD is now at start-sha\n")
    cmd.queue_result(returncode=0, stdout="")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    thread = ReviewThread(
        thread_id="T_protected",
        path="tests/unit/control/test_ci_workflow_toolchain.py",
        line=49,
        body_excerpt="this test requires a protected workflow edit",
        author="reviewer",
    )
    state = MonitorState()

    async def _address_thread(**_kwargs: object) -> str:
        return "fix_committed"

    async def _fetch_clean_status(**_kwargs: object) -> PRStatus:
        return _status_for_helpers()

    async def _protected_block(**_kwargs: object) -> _ProtectedScopePushBlock:
        return _ProtectedScopePushBlock(
            message="protected scope blocked",
            reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
            violations=(
                QualityGateViolation(
                    path=".github/workflows/ci.yml",
                    protected_pattern=".github/**",
                ),
            ),
        )

    async def _unexpected_push(**_kwargs: object) -> _GitPushResult:
        pytest.fail("protected-scope rollback must not push")

    monkeypatch.setattr(runner, "_address_thread", _address_thread)
    monkeypatch.setattr(runner._deps.gh, "fetch_pr_status", _fetch_clean_status)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _protected_block)
    monkeypatch.setattr(runner, "_git_push_result", _unexpected_push)

    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="start-sha",
        initial_threads=(thread,),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "PROTECTED_SCOPE_PUSH_BLOCKED"
    assert result.details is not None
    assert result.details["branch_restored"] is True
    assert "T_protected" not in state.threads_addressed_ids
    assert _review_thread_body_state_key("T_protected") not in state.threads_addressed_ids
    assert _git_worktree_command(worktree, "reset", "--hard", "start-sha") in [
        call.args for call in cmd.calls
    ]

    async with factory() as session:
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )

    assert any(
        event.payload
        and event.payload["action"] == "protected_scope_transactional_rollback"
        and event.payload["outcome"] == "succeeded"
        for event in events
    )


@pytest.mark.unit
async def test_fix_cycle_rolls_back_protected_scope_delta_when_diff_path_parse_fails(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    worktree = tmp_path / "worktrees" / workspace_id
    worktree.mkdir(parents=True)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="")  # clean worktree before repair
    cmd.queue_result(returncode=0, stdout="start-sha\n")  # operation start HEAD
    cmd.queue_result(returncode=0, stdout="blocked-head-sha\n")  # attempted HEAD
    cmd.queue_result(
        returncode=0,
        stdout="M\0.github/workflows/ci.yml\0R100\0docs/old.yml\0",
    )
    cmd.queue_result(returncode=0, stdout=".github/workflows/ci.yml\0plans/fallback.md\0")
    cmd.queue_result(returncode=0, stdout="?? plans/orphan dir/file one.md\0")
    cmd.queue_result(returncode=0, stdout="HEAD is now at start-sha\n")
    cmd.queue_result(returncode=0, stdout="")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    thread = ReviewThread(
        thread_id="T_protected_parse",
        path="tests/unit/control/test_ci_workflow_toolchain.py",
        line=49,
        body_excerpt="this test requires a protected workflow edit",
        author="reviewer",
    )
    state = MonitorState()

    async def _address_thread(**_kwargs: object) -> str:
        return "fix_committed"

    async def _fetch_clean_status(**_kwargs: object) -> PRStatus:
        return _status_for_helpers()

    async def _protected_block(**_kwargs: object) -> _ProtectedScopePushBlock:
        return _ProtectedScopePushBlock(
            message="protected scope blocked",
            reason_code="PROTECTED_SCOPE_PUSH_BLOCKED",
            violations=(
                QualityGateViolation(
                    path=".github/workflows/ci.yml",
                    protected_pattern=".github/**",
                ),
            ),
        )

    async def _unexpected_push(**_kwargs: object) -> _GitPushResult:
        pytest.fail("protected-scope rollback must not push")

    monkeypatch.setattr(runner, "_address_thread", _address_thread)
    monkeypatch.setattr(runner._deps.gh, "fetch_pr_status", _fetch_clean_status)
    monkeypatch.setattr(runner, "_protected_scope_push_block", _protected_block)
    monkeypatch.setattr(runner, "_git_push_result", _unexpected_push)

    result = await runner._run_fix_cycle(
        workspace_id=workspace_id,
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="start-sha",
        initial_threads=(thread,),
        initial_reviews=(),
        state=state,
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.reason_code == "PROTECTED_SCOPE_PUSH_BLOCKED"
    assert result.details is not None
    assert result.details["branch_restored"] is True
    assert result.details["reverted_paths"] == [
        ".github/workflows/ci.yml",
        "plans/fallback.md",
        "plans/orphan dir/file one.md",
    ]
    assert "reverted_path_collection_errors" not in result.details
    assert _git_worktree_command(worktree, "reset", "--hard", "start-sha") in [
        call.args for call in cmd.calls
    ]
    assert _git_worktree_command(
        worktree,
        "--literal-pathspecs",
        "clean",
        "-fd",
        "--",
        "plans/orphan dir/file one.md",
    ) in [call.args for call in cmd.calls]

    async with factory() as session:
        events = await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.audit.git_push",
            limit=10,
        )

    assert any(
        event.payload
        and event.payload["action"] == "protected_scope_transactional_rollback"
        and event.payload["outcome"] == "succeeded"
        for event in events
    )


@pytest.mark.unit
async def test_fix_cycle_returns_failed_push_when_review_fix_hits_policy_block(
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
        comment_id="R_supply",
        body_excerpt="please adjust this",
        author="reviewer",
    )

    async def _blocked_review(**_kwargs: object) -> str:
        raise _MonitorPolicyBlockedError("Supply-chain policy blocked review fix.")

    monkeypatch.setattr(runner, "_address_review_comment_result", _blocked_review)

    result = await runner._run_fix_cycle(
        workspace_id="ws_supply_review",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(),
        initial_reviews=(review,),
        state=MonitorState(),
        remote_branch="awf/ws_supply_review",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert result.pushed is False
    assert result.returncode == 1
    assert "Supply-chain policy blocked review fix" in result.stderr


@pytest.mark.unit
async def test_fix_cycle_clears_addressed_review_state_on_protected_scope_early_return(
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
    fixed_review = ReviewComment(
        comment_id="C_fixed",
        body_excerpt="please adjust this first",
        author="reviewer",
    )
    blocked_review = ReviewComment(
        comment_id="C_blocked",
        body_excerpt="then protected scope diff fails",
        author="reviewer",
    )
    state = MonitorState()

    async def _address_review_comment_result(
        **kwargs: object,
    ) -> pr_monitor_runner_comments.VerdictResult:
        comment = kwargs["comment"]
        assert isinstance(comment, ReviewComment)
        if comment.comment_id == fixed_review.comment_id:
            return pr_monitor_runner_comments.VerdictResult(verdict="fix_committed")
        raise ProtectedScopeDiffError("diff baseline unavailable")

    async def _protected_scope_result(**kwargs: object) -> _GitPushResult:
        return _GitPushResult(
            pushed=False,
            failed=True,
            returncode=1,
            stderr=str(kwargs["exc"]),
            reason_code="PROTECTED_SCOPE_DIFF_UNAVAILABLE",
        )

    monkeypatch.setattr(
        runner,
        "_address_review_comment_result",
        _address_review_comment_result,
    )
    monkeypatch.setattr(
        runner,
        "_protected_scope_diff_unavailable_push_result",
        _protected_scope_result,
    )

    result = await runner._run_fix_cycle(
        workspace_id="ws_protected_review",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(),
        initial_reviews=(fixed_review, blocked_review),
        state=state,
        remote_branch="awf/ws_protected_review",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert result.failed is True
    assert "C_fixed" not in state.threads_addressed_ids
    assert _review_comment_body_state_key("C_fixed") not in state.threads_addressed_ids


@pytest.mark.unit
async def test_fix_cycle_zero_passes_still_runs_push(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stderr="Everything up-to-date")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    object.__setattr__(runner._runner_config, "max_fix_cycle_passes", 0)

    await runner._run_fix_cycle(
        workspace_id="ws_zero_pass",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        pr_head_sha="abc1234567890def",
        initial_threads=(),
        initial_reviews=(),
        state=MonitorState(),
        remote_branch="awf/ws_zero_pass",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert len(cmd.calls) == 1
    assert cmd.calls[0].args[:5] == _git_worktree_command(tmp_path / "worktrees" / "ws_zero_pass")
    assert cmd.calls[0].args[5] == "push"


@pytest.mark.unit
async def test_best_effort_monitor_log_and_missing_workspace_event_append_do_not_raise(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    await runner._write_monitor_log(_FailingLogSink(), {"event": "monitor.test"})  # type: ignore[arg-type]
    await runner._append_workspace_events(
        workspace_id="ws_missing",
        events=[
            WorkspaceEventCreate(
                event_type="workspace.test",
                reason_code="TEST",
                payload={"ok": True},
            )
        ],
    )


@pytest.mark.unit
async def test_post_human_notification_dedup_skips_github_call(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    status = _status_for_helpers()
    state = MonitorState(
        threads_addressed_ids={"__awf_notify__:abc1234567890def:manual": "notified"}
    )

    await runner._post_human_notification_once(
        repo=RepoRef(owner="example", name="repo"),
        pr_number=42,
        status=status,
        state=state,
        blocker_reason="manual",
    )

    assert cmd.calls == []


@pytest.mark.unit
async def test_defer_signal_write_failure_is_best_effort(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    artifact_file = tmp_path / "artifacts-file"
    artifact_file.write_text("not a directory", encoding="utf-8")
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        artifacts_root=artifact_file,
    )

    runner._write_defer_signal(
        workspace_id="ws_defer",
        pr_number=42,
        terminal_action="Abort",
        merged=False,
        status=_status_for_helpers(),
        state=MonitorState(),
    )

    assert artifact_file.read_text(encoding="utf-8") == "not a directory"


@pytest.mark.unit
async def test_target_branch_reconcile_failure_appends_workspace_event(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)

    async def failing_reconciler(*, repo_url: str, branch: str, workspace_id: str) -> object:
        assert repo_url == "git@github.com:dimileeh/aira-web.git"
        assert branch == "development"
        raise RuntimeError("target branch locked")

    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        post_merge_target_reconciler=failing_reconciler,
    )

    with structlog.testing.capture_logs() as captured:
        await runner._reconcile_target_branch_after_merge(
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            base_branch="development",
        )

    failure_log = next(
        event
        for event in captured
        if event.get("event") == "monitor.target_branch_reconcile_failed"
    )
    assert failure_log["status"] == "failed"
    assert failure_log["reason_code"] == "TARGET_BRANCH_RECONCILE_FAILED"
    assert failure_log["error_type"] == "RuntimeError"
    assert failure_log["resolver_results"] == []
    assert failure_log["commit_sha"] is None
    assert failure_log["pushed"] is False
    assert failure_log["dry_run"] is None
    assert failure_log["commit_allowed"] is None

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.events[-1].event_type == "target_branch.reconcile_failed"
        assert ws.events[-1].reason_code == "TARGET_BRANCH_RECONCILE_FAILED"
        assert ws.events[-1].payload["error"] == "target branch locked"
