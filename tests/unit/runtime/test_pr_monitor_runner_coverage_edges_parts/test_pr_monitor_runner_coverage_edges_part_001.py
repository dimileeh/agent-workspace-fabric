"""Focused branch-coverage tests for PR monitor runner edge behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.bitbucket_client import (
    BITBUCKET_API_ERROR,
    BITBUCKET_AUTH_FAILED,
    BITBUCKET_MERGE_IN_PROGRESS,
    BITBUCKET_MERGE_TASK_TIMEOUT,
    BITBUCKET_RATE_LIMITED,
    BITBUCKET_TRANSPORT_ERROR,
    BitbucketClientError,
)
from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GITHUB_API_ERROR, GitHubClientError, RepoRef
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
from awf.db.models import Workspace
from awf.db.repositories import (
    OperationRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    MonitorState,
)
from awf.runtime.pr_monitor_runner.helpers import (
    _as_utc,
    _increment_base_fetch_retry_count,
    _is_transient_base_fetch_error,
    _is_transient_bitbucket_client_error,
    _is_transient_github_client_error,
    _redact_and_truncate_forge_error,
)
from awf.runtime.pr_monitor_runner.types import (
    BaseFetchError,
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


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


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


def _bitbucket_retry_events(ws: Workspace) -> list:
    return [
        event
        for event in ws.events
        if event.event_type == "monitor.bitbucket_transient_error_retrying"
    ]


def _git_worktree_command(worktree_path: Path, *args: str) -> list[str]:
    return ["git", "-c", f"safe.directory={worktree_path}", "-C", str(worktree_path), *args]


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


@pytest.mark.unit
async def test_monitor_run_fails_cleanly_when_pr_number_is_missing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(factory)
    await _update_workspace(factory, workspace_id, pr_number=None)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert "without a pr_number" in (ws.failure_message or "")
    assert cmd.calls == []


@pytest.mark.unit
async def test_monitor_run_fails_cleanly_when_sync_workspace_has_no_remote_push_branch(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(factory)
    await _update_workspace(
        factory,
        workspace_id,
        task_kind="sync_feature_pr",
        branch_name="feature-sync/local-only",
        remote_push_branch=None,
    )
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=0, stdout=pr_payload())
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert "no remote_push_branch" in (ws.failure_message or "")
        assert "sync_feature_pr" in (ws.failure_message or "")
    worktree = tmp_path / "worktrees" / workspace_id
    assert cmd.calls[0].args == _git_worktree_command(
        worktree,
        "fetch",
        "origin",
        "+refs/heads/development:refs/remotes/origin/development",
    )
    assert cmd.calls[1].args == _git_worktree_command(
        worktree,
        "rev-list",
        "--count",
        "HEAD..origin/development",
    )
    assert cmd.calls[2].args[:3] == ["gh", "api", "graphql"]


@pytest.mark.unit
async def test_monitor_run_terminates_on_github_status_error(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(factory)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=1, stderr="gh auth failed")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert "github error" in (ws.failure_message or "")
        assert "gh auth failed" in (ws.failure_message or "")
        # The fetch_pr_status GitHub termination records the forge reason_code
        # (GITHUB_API_ERROR), matching the _execute path so both GitHub
        # termination paths write identical DB state rather than the
        # MONITOR_ABORT default.
        assert ws.events[-1].reason_code == GITHUB_API_ERROR
        assert _retry_events(ws) == []


@pytest.mark.unit
async def test_monitor_run_terminates_on_bitbucket_execute_error(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A deterministic ``BitbucketClientError`` escaping ``_execute`` terminates.

    Regression for PRRT_kwDOSJAM6s6Hnm9b. ``_execute`` drives non-merge forge
    actions (thread-resolve, CI-rerun, fix-cycle) whose action arms catch
    ``GitHubClientError`` alone; a Bitbucket workspace's forge raises
    ``BitbucketClientError`` instead. The runner's outer ``execute_action`` arm
    must catch it so a deterministic fault marks the workspace failed (preserving
    the reason code) rather than escaping ``run()`` and crashing the background
    monitor task.

    Merge faults are NOT routed here — they follow the merge-blocker
    notify-and-keep-polling path instead (see
    ``test_deterministic_bitbucket_merge_failure_notifies_and_keeps_polling`` in
    ``test_pr_monitor_merge_methods.py``), so this exercises a non-merge action
    via a stubbed ``_execute``.
    """
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    # A clean, mergeable poll so ``decide`` reaches an action; the stubbed
    # ``_execute`` then raises the deterministic non-merge Bitbucket fault.
    cmd.queue_result(returncode=0)  # poll: git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # poll: base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload())  # poll: mergeable PR
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=DefaultMergeMethodGitHubClient(cmd),
    )

    async def fake_execute(**_kwargs: object) -> bool:
        # Model a non-merge forge call (e.g. resolve_thread) raising a
        # deterministic Bitbucket fault the GitHubClientError-only arm misses. A
        # non-auth 4xx (``BITBUCKET_API_ERROR`` 404) is genuinely deterministic —
        # 401/403 are now bounded-retryable (#515), so they would NOT terminate
        # immediately here.
        raise BitbucketClientError(
            operation="bitbucket resolve_thread",
            status=404,
            body="thread not found",
            reason_code=BITBUCKET_API_ERROR,
        )

    runner._execute = fake_execute  # type: ignore[method-assign]

    # Must not escape ``run()`` and crash the background monitor task.
    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert "bitbucket error" in (ws.failure_message or "")
        assert ws.events[-1].reason_code == BITBUCKET_API_ERROR
        # Deterministic fault terminates rather than entering the transient
        # re-poll loop.
        assert _bitbucket_retry_events(ws) == []
        assert sleep_fn.calls == []


@pytest.mark.unit
async def test_monitor_run_retries_transient_bitbucket_execute_error(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    # The merge attempt hits a transient Bitbucket blip (5xx). It is classified
    # as a merge blocker, so the merge-blocker arm (context ``merge_pr``) waits
    # and re-polls; the PR then shows merged upstream so the monitor
    # short-circuits to completed instead of crashing or terminating.
    cmd.queue_result(returncode=0)  # poll: git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # poll: base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload())  # poll: mergeable PR → Merge
    cmd.queue_result(returncode=0)  # retry poll: git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # retry poll: base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))  # merged upstream
    cmd.queue_result(returncode=0)  # compose down

    class _TransientThenMergedClient(DefaultMergeMethodGitHubClient):
        def __init__(self, inner: FakeCommandRunner) -> None:
            super().__init__(inner)
            self.merge_attempts = 0

        async def merge_pr(
            self,
            *,
            repo: RepoRef,
            pr_number: int,
            method: str = "squash",
            delete_branch: bool = True,
        ) -> str:
            del repo, pr_number, method, delete_branch
            self.merge_attempts += 1
            raise BitbucketClientError(
                operation="bitbucket merge_pr",
                status=503,
                body="service unavailable",
                reason_code=BITBUCKET_API_ERROR,
            )

    gh = _TransientThenMergedClient(cmd)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=gh,
    )

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert sleep_fn.calls == [5]
    assert gh.merge_attempts == 1
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value
        retry_events = _bitbucket_retry_events(ws)
        assert len(retry_events) == 1
        # The transient merge fault is handled by the merge-blocker arm, which
        # tags the retry with the ``merge_pr`` context.
        assert retry_events[0].payload["context"] == "merge_pr"


@pytest.mark.unit
async def test_transient_bitbucket_execute_error_discards_unconfirmed_addressed_state(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """A transient ``BitbucketClientError`` escaping ``_execute`` must not persist
    the in-flight addressed markers it mutated.

    Regression for PRRT_kwDOSJAM6s6HntiJ. The fix cycle marks a thread addressed
    in-memory *before* the forge ``resolve_thread`` call. For a Bitbucket
    workspace that call raises ``BitbucketClientError`` — which the
    ``GitHubClientError``-only fix-cycle arm neither catches nor rolls back — so
    it escapes to ``run()``. On a recoverable blip the runner must discard those
    unconfirmed mutations and re-poll from clean DB state, mirroring the
    status-fetch transient arms. Persisting them would leave the thread
    marked-addressed-but-open, and ``decide()`` would skip it forever, letting
    auto-merge bypass live feedback (the #305 failure mode).
    """
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    # Two outer iterations, three status-fetch commands each.
    for _ in range(2):
        cmd.queue_result(returncode=0)  # poll: git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # poll: base-behind
        cmd.queue_result(returncode=0, stdout=pr_payload())  # poll: mergeable PR
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        gh=DefaultMergeMethodGitHubClient(cmd),
    )

    calls = {"n": 0}

    async def fake_execute(*, state: MonitorState, **_kwargs: object) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            # Mirror the fix cycle marking a thread addressed before the forge
            # resolve_thread call, then a transient Bitbucket fault on resolve.
            state.threads_addressed_ids["T_inflight"] = "fix_committed"
            raise BitbucketClientError(
                operation="bitbucket resolve_thread",
                status=503,
                body="service unavailable",
                reason_code=BITBUCKET_API_ERROR,
            )
        return True  # terminal: end the monitor loop cleanly

    runner._execute = fake_execute  # type: ignore[method-assign]

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert calls["n"] == 2
    assert sleep_fn.calls == [5]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        # The unconfirmed addressed marker from the failed _execute must NOT be
        # persisted — otherwise decide() would treat the still-open thread as
        # handled on the next poll.
        assert "T_inflight" not in (ws.monitor_threads_addressed or {})
        retry_events = _bitbucket_retry_events(ws)
        assert len(retry_events) == 1
        assert retry_events[0].payload["context"] == "execute_action"


@pytest.mark.unit
async def test_monitor_run_transient_status_fetch_preserves_state_operations_and_lifecycle(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    sleep_fn = _StopAfterRetrySleep()
    workspace_id = await seed_monitoring_workspace(factory)
    operation_id = await _seed_running_operation(factory, workspace_id)
    started_at = datetime(2026, 1, 2, tzinfo=UTC)
    await _update_workspace(
        factory,
        workspace_id,
        monitor_iter_count=7,
        monitor_threads_addressed={"T_old": "defer"},
        monitor_last_commit_sha="oldsha",
        monitor_started_at=started_at,
    )
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(
        returncode=1,
        stderr="HTTP 502 Bad Gateway for token ghp_statusretrysecret",
    )
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )

    with pytest.raises(_StopAfterRetryError):
        await runner.run(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )

    assert sleep_fn.calls == [5]
    worktree = tmp_path / "worktrees" / workspace_id
    assert cmd.calls[0].args == _git_worktree_command(
        worktree,
        "fetch",
        "origin",
        "+refs/heads/development:refs/remotes/origin/development",
    )
    assert cmd.calls[1].args == _git_worktree_command(
        worktree,
        "rev-list",
        "--count",
        "HEAD..origin/development",
    )
    assert cmd.calls[2].args[:3] == ["gh", "api", "graphql"]
    assert not any(call.args[:3] == ["gh", "pr", "comment"] for call in cmd.calls)
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        assert ws.failure_message is None
        assert ws.monitor_iter_count == 7
        # The transient retry persists the per-context forge retry counter
        # alongside the preserved pre-existing markers so the bounded budget
        # survives the reload on the next poll (#515).
        assert ws.monitor_threads_addressed == {
            "T_old": "defer",
            "__awf_forge_transient_retry_count:fetch_pr_status": "1",
        }
        assert ws.monitor_last_commit_sha == "oldsha"
        assert _as_utc(ws.monitor_started_at) == started_at
        operation = await OperationRepository(s).get(operation_id)
        assert operation is not None
        assert operation.status == OperationStatus.running.value
        assert operation.payload == {"source": "test", "keep": True}
        events = _retry_events(ws)
        assert len(events) == 1
        assert events[0].reason_code == "GITHUB_TRANSIENT_RETRY"
        assert events[0].old_state == WorkspaceStatus.monitoring_pr.value
        assert events[0].new_state == WorkspaceStatus.monitoring_pr.value
        assert events[0].payload == {
            "context": "fetch_pr_status",
            "operation": "gh api graphql",
            "returncode": 1,
            "pr_number": 42,
            "wait_seconds": 5,
            "retry_number": 1,
            "max_retries": 5,
            "message": (
                "gh api graphql failed (exit=1): HTTP 502 Bad Gateway for token <redacted>"
            ),
            "stderr": "HTTP 502 Bad Gateway for token <redacted>",
        }


@pytest.mark.unit
async def test_monitor_run_retries_transient_github_status_error(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    workspace_id = await seed_monitoring_workspace(factory)
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=1, stderr="HTTP 502 Bad Gateway")
    cmd.queue_result(returncode=0)
    cmd.queue_result(returncode=0, stdout="0\n")
    cmd.queue_result(returncode=0, stdout=pr_payload(merged=True))
    cmd.queue_result(returncode=0)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    assert sleep_fn.calls == [5]
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.completed.value


@pytest.mark.unit
def test_transient_github_error_classifier_keeps_auth_errors_terminal() -> None:
    assert _is_transient_github_client_error(
        GitHubClientError(
            operation="gh api graphql",
            returncode=1,
            stderr="HTTP 503 Service Unavailable",
        )
    )
    assert _is_transient_github_client_error(
        GitHubClientError(
            operation="gh pr merge",
            returncode=1,
            stderr="secondary rate limit hit; please try again",
        )
    )
    assert _is_transient_github_client_error(
        GitHubClientError(
            operation="gh api repo",
            returncode=1,
            stderr=(
                "GitHub repository response omitted merge method flags; "
                "API response may be temporarily unavailable, try again: allow_rebase_merge"
            ),
        )
    )
    # #515: a bare ``Requires authentication (HTTP 401)`` blip on a valid token is
    # an ambiguous transient — it must now be bounded-retryable, not terminal.
    assert _is_transient_github_client_error(
        GitHubClientError(
            operation="gh api graphql",
            returncode=1,
            stderr="Requires authentication (HTTP 401)",
        )
    )
    # The exact #515 failure string.
    assert _is_transient_github_client_error(
        GitHubClientError(
            operation="gh api graphql",
            returncode=1,
            stderr="gh api graphql failed (exit=1): gh: Requires authentication (HTTP 401)",
        )
    )
    # GitHub can also spell the ambiguous 401 blip as ``Bad credentials``.
    assert _is_transient_github_client_error(
        GitHubClientError(
            operation="gh api graphql",
            returncode=1,
            stderr="gh api graphql failed (exit=1): gh: Bad credentials (HTTP 401)",
        )
    )
    # Strong, unambiguous permanent markers still fail fast.
    assert not _is_transient_github_client_error(
        GitHubClientError(
            operation="gh api graphql",
            returncode=1,
            stderr="not logged in to any GitHub hosts",
        )
    )
    assert not _is_transient_github_client_error(
        GitHubClientError(
            operation="gh auth status",
            returncode=1,
            stderr="To get started with GitHub CLI, please run gh auth login",
        )
    )
    assert not _is_transient_github_client_error(
        GitHubClientError(
            operation="gh api graphql",
            returncode=1,
            stderr="could not resolve to a Repository with the name 'org/repo'",
        )
    )
    assert not _is_transient_github_client_error(
        GitHubClientError(
            operation="gh api graphql",
            returncode=1,
            stderr="repository not found",
        )
    )
    assert not _is_transient_github_client_error(
        GitHubClientError(
            operation="gh api graphql",
            returncode=1,
            stderr="review is required before merging",
        )
    )


@pytest.mark.unit
def test_transient_base_fetch_classifier_and_corrupt_retry_count_recovery() -> None:
    assert _is_transient_base_fetch_error(
        BaseFetchError("git fetch origin development failed: HTTP 500 server error")
    )
    assert _is_transient_base_fetch_error(
        BaseFetchError(
            "git fetch base failed with exit code 1: error: cannot lock ref "
            "'refs/remotes/origin/codex/awf-post-merge-fixes': is at "
            "dffa1db03af61da5db52e16a6e79163c35b88d5d but expected "
            "cc82a8d265b6d63593417a13d3d9507cc0ede8d5\n"
            "From https://github.com/dimileeh/agent-workspace-fabric\n"
            " ! cc82a8d2..dffa1db0  codex/awf-post-merge-fixes -> "
            "origin/codex/awf-post-merge-fixes  (unable to update local ref)"
        )
    )
    assert _is_transient_base_fetch_error(
        BaseFetchError("git fetch origin main failed: gh: Bad credentials (HTTP 401)")
    )
    assert not _is_transient_base_fetch_error(
        BaseFetchError("git fetch origin development failed: repository not found")
    )
    # #515 regression: narrowing the GitHub non-transient markers (dropping the
    # broad ``"authentication"`` substring) must NOT make git auth failures
    # retryable — git's wording contains neither ``http 401`` nor
    # ``requires authentication`` so it still classifies non-transient (terminate).
    assert not _is_transient_base_fetch_error(
        BaseFetchError("fatal: Authentication failed for 'https://github.com/org/repo.git/'")
    )
    assert not _is_transient_base_fetch_error(
        BaseFetchError(
            "fatal: unable to access 'https://github.com/org/repo.git/': "
            "The requested URL returned error: 401"
        )
    )
    # Full #515 symmetry: ambiguous GitHub 401 text is bounded-retryable on the
    # base-fetch path too.
    assert _is_transient_base_fetch_error(
        BaseFetchError("error: RPC failed; HTTP 401 curl 22 The requested URL returned error: 401")
    )
    assert not _is_transient_base_fetch_error(
        BaseFetchError("fatal: not logged in to any GitHub hosts")
    )
    assert not _is_transient_base_fetch_error(
        BaseFetchError("To get started with GitHub CLI, please run gh auth login")
    )
    retry_key = "__awf_base_fetch_retry_count:sync_base"
    state = MonitorState(threads_addressed_ids={retry_key: "not-an-integer"})

    retry_number = _increment_base_fetch_retry_count(state, "sync_base")

    assert retry_number == 1
    assert state.threads_addressed_ids[retry_key] == "1"


@pytest.mark.unit
def test_dns_base_fetch_errors_classified_transient() -> None:
    assert _is_transient_base_fetch_error(
        BaseFetchError(
            "fatal: unable to access 'https://github.com/org/repo.git': "
            "Could not resolve host: github.com"
        )
    )
    assert _is_transient_base_fetch_error(
        BaseFetchError(
            "fatal: unable to access 'https://github.com/org/repo.git': "
            "Temporary failure in name resolution"
        )
    )
    assert _is_transient_base_fetch_error(
        BaseFetchError(
            "fatal: unable to access 'https://github.com/org/repo.git': Name or service not known"
        )
    )
    assert _is_transient_base_fetch_error(
        BaseFetchError(
            "fatal: unable to access 'https://github.com/org/repo.git': Could not resolve proxy"
        )
    )
    assert not _is_transient_base_fetch_error(
        BaseFetchError(
            "could not resolve to a repository: The org/repo.git repository was renamed or removed"
        )
    )
    assert not _is_transient_base_fetch_error(BaseFetchError("could not resolve to a node"))


@pytest.mark.unit
def test_github_error_redaction_covers_app_jwt_and_bearer_tokens() -> None:
    app_token = "gha_11AA22BB33CC44DD"
    jwt_token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123"
    bearer_token = "opaqueBearerToken123"
    redacted = _redact_and_truncate_forge_error(
        f"HTTP 503 {app_token} jwt={jwt_token} Authorization: Bearer {bearer_token}"
    )

    assert app_token not in redacted
    assert jwt_token not in redacted
    assert bearer_token not in redacted
    assert redacted.count("<redacted>") == 3
    assert "Authorization: Bearer <redacted>" in redacted


@pytest.mark.unit
async def test_transient_retry_event_payload_is_structured_and_redacted(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    secret = "github_pat_11AA22BB33CC44DD"
    noisy_stderr = (
        f"HTTP 503 Service Unavailable for {secret} at "
        f"https://user:{secret}@github.com/example/repo " + ("x" * 600)
    )

    state = MonitorState()
    retried = await runner._wait_after_transient_github_error(
        GitHubClientError(operation="gh api graphql", returncode=1, stderr=noisy_stderr),
        workspace_id=workspace_id,
        pr_number=42,
        context="fetch_pr_status",
        state=state,
        monitor_log=None,
    )

    assert retried is True
    assert sleep_fn.calls == [5]
    # The bounded-retry counter is tracked per context and persisted.
    assert state.threads_addressed_ids["__awf_forge_transient_retry_count:fetch_pr_status"] == "1"
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        events = _retry_events(ws)
        assert len(events) == 1
        event = events[0]
        assert event.reason_code == "GITHUB_TRANSIENT_RETRY"
        payload = event.payload
        assert payload is not None
        assert payload["context"] == "fetch_pr_status"
        assert payload["operation"] == "gh api graphql"
        assert payload["returncode"] == 1
        assert payload["pr_number"] == 42
        assert payload["wait_seconds"] == 5
        assert payload["retry_number"] == 1
        assert payload["max_retries"] == 5
        assert secret not in str(payload)
        assert "https://<redacted>@github.com/example/repo" in payload["stderr"]
        assert len(payload["stderr"]) <= 400
        assert len(payload["message"]) <= 400


@pytest.mark.unit
def test_is_transient_bitbucket_client_error_classifies_recoverable_blips() -> None:
    def err(*, status: int | None, reason_code: str) -> BitbucketClientError:
        return BitbucketClientError(
            operation="bitbucket fetch_pr_status",
            status=status,
            body="boom",
            reason_code=reason_code,
        )

    # Recoverable: rate limit that survived internal backoff, transport blip,
    # and 5xx server faults — symmetric to GitHub's transient markers.
    assert _is_transient_bitbucket_client_error(err(status=429, reason_code=BITBUCKET_RATE_LIMITED))
    assert _is_transient_bitbucket_client_error(
        err(status=None, reason_code=BITBUCKET_TRANSPORT_ERROR)
    )
    for status in (500, 502, 503, 504):
        assert _is_transient_bitbucket_client_error(
            err(status=status, reason_code=BITBUCKET_API_ERROR)
        )
    # A 409 on the merge POST is re-raised as BITBUCKET_MERGE_IN_PROGRESS so the
    # monitor re-polls fetch_pr_status instead of terminating on an already
    # in-flight merge that may still be completing.
    assert _is_transient_bitbucket_client_error(
        err(status=409, reason_code=BITBUCKET_MERGE_IN_PROGRESS)
    )
    # An exhausted async-merge poll budget (still-PENDING task) is recoverable the
    # same way: Bitbucket may still complete the merge server-side, so the monitor
    # must wait and re-poll rather than post a spurious "merge rejected" — even
    # though it carries ``status=None`` like the deterministic safety aborts.
    assert _is_transient_bitbucket_client_error(
        err(status=None, reason_code=BITBUCKET_MERGE_TASK_TIMEOUT)
    )
    # #515: a Bitbucket 401/403 auth fault (``BITBUCKET_AUTH_FAILED``, set only for
    # 401/403) is now bounded-retryable, symmetric with GitHub's ambiguous-401
    # handling — a momentary blip recovers, a real bad token exhausts the budget.
    assert _is_transient_bitbucket_client_error(err(status=401, reason_code=BITBUCKET_AUTH_FAILED))
    assert _is_transient_bitbucket_client_error(err(status=403, reason_code=BITBUCKET_AUTH_FAILED))

    # Deterministic: non-auth 4xx client error, JSON parse (2xx body), and
    # the pagination/SSRF safety aborts — which also carry ``status=None`` but
    # map to ``BITBUCKET_API_ERROR`` and must fail fast, not loop.
    assert not _is_transient_bitbucket_client_error(
        err(status=404, reason_code=BITBUCKET_API_ERROR)
    )
    assert not _is_transient_bitbucket_client_error(
        err(status=200, reason_code=BITBUCKET_API_ERROR)
    )
    assert not _is_transient_bitbucket_client_error(
        err(status=None, reason_code=BITBUCKET_API_ERROR)
    )


@pytest.mark.unit
async def test_wait_after_transient_bitbucket_error_retries_and_records_event(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    secret = "bbtoken_11AA22BB33CC44DD"
    exc = BitbucketClientError(
        operation="bitbucket fetch_pr_status",
        status=503,
        body=f"Service Unavailable: https://user:{secret}@bitbucket.org/repo " + ("x" * 600),
        reason_code=BITBUCKET_API_ERROR,
    )

    state = MonitorState()
    retried = await runner._wait_after_transient_bitbucket_error(
        exc,
        workspace_id=workspace_id,
        pr_number=42,
        context="fetch_pr_status",
        state=state,
        monitor_log=None,
    )

    assert retried is True
    assert sleep_fn.calls == [5]
    assert state.threads_addressed_ids["__awf_forge_transient_retry_count:fetch_pr_status"] == "1"
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        events = _bitbucket_retry_events(ws)
        assert len(events) == 1
        event = events[0]
        assert event.reason_code == "BITBUCKET_TRANSIENT_RETRY"
        payload = event.payload
        assert payload is not None
        assert payload["context"] == "fetch_pr_status"
        assert payload["operation"] == "bitbucket fetch_pr_status"
        assert payload["status"] == 503
        assert payload["reason_code"] == BITBUCKET_API_ERROR
        assert payload["pr_number"] == 42
        assert payload["wait_seconds"] == 5
        assert payload["retry_number"] == 1
        assert payload["max_retries"] == 5
        assert secret not in str(payload)
        assert len(payload["message"]) <= 400


@pytest.mark.unit
async def test_wait_after_transient_bitbucket_error_fails_fast_on_deterministic_fault(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    # A non-auth 4xx is genuinely deterministic: 401/403 are now bounded-retryable
    # (#515), so use a 404 ``BITBUCKET_API_ERROR`` to exercise the fail-fast path.
    exc = BitbucketClientError(
        operation="bitbucket fetch_pr_status",
        status=404,
        body="not found",
        reason_code=BITBUCKET_API_ERROR,
    )

    state = MonitorState()
    retried = await runner._wait_after_transient_bitbucket_error(
        exc,
        workspace_id=workspace_id,
        pr_number=42,
        context="fetch_pr_status",
        state=state,
        monitor_log=None,
    )

    assert retried is False
    assert sleep_fn.calls == []
    # A deterministic fault must not touch the bounded-retry counter.
    assert "__awf_forge_transient_retry_count:fetch_pr_status" not in state.threads_addressed_ids
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert _bitbucket_retry_events(ws) == []
