"""Unit tests for focused ``pr_monitor_runner`` behavior.

Most cases cover the pure, side-effect-free helpers: ``_parse_verdict`` (CLI
reply → structured verdict) and ``_collect_defer_items`` (PRStatus +
MonitorState → bot/human defer buckets for the terminal artifact). Focused
runtime-path regressions live here when the unit suite needs to cover a
specific merge-gate branch without running the full monitor integration loop.
(split part)
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClientError, RepoRef
from awf.db.enums import (
    WorkspaceStatus,
)
from awf.db.repositories import (
    PRFeedbackResolutionRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckFailure,
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
)
from awf.runtime.pr_monitor_runner.types import (
    BaseBehindCountError,
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


@pytest.mark.unit
async def test_run_closes_forge_client_on_exit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    # Regression (issue:4640573294): the per-monitor forge client (a Bitbucket
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
