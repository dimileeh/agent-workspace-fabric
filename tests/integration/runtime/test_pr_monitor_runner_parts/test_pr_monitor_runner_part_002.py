"""Integration tests for PullRequestMonitorRunner.

'Integration' here means: real SQLAlchemy against PostgreSQL, the real
decision core (``decide``), the real prompt templates, and a real
``GitHubClient`` — but backed by ``FakeCommandRunner`` and a tiny fake
adapter so no subprocesses spawn. Tests drive full loops end-to-end.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentAdapter, AgentRunError, AgentRunResult
from awf.common.bitbucket_client import BitBucketClientError
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import (
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    MonitorConfig,
    MonitorState,
    ReviewThread,
    _mark_review_thread_addressed,
)
from awf.runtime.pr_monitor_runner import (
    MonitorRunnerConfig,
    PullRequestMonitorRunner,
)
from awf.runtime.pr_monitor_runner.helpers import _initial_review_grace_started_key
from tests.postgres import postgres_test_engine
from tests.shared.monitor_runner import DefaultMergeMethodGitHubClient


@dataclass
class FakeAdapter(AgentAdapter):
    """Canned-response CLI. Each ``run`` call pops one verdict stdout."""

    runtime = AgentRuntime.claude_code
    _queued: list[AgentRunResult] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    workspace_ids: list[str | None] = field(default_factory=list)

    def __init__(self) -> None:  # type: ignore[override]
        super().__init__(runner=None)  # type: ignore[arg-type]
        self._queued = []
        self.calls = []
        self.workspace_ids = []

    def get_provider(self, model: str | None) -> str:
        return "fake"

    @property
    def name(self) -> AgentRuntime:  # type: ignore[override]
        return AgentRuntime.claude_code

    def _cli_args(self, *, model: str | None) -> list[str]:
        return []

    def queue(self, *, stdout: str = "", returncode: int = 0, raise_error: bool = False) -> None:
        self._queued.append(AgentRunResult(returncode=returncode, stdout=stdout, stderr=""))
        if raise_error:
            self._queued[-1] = AgentRunResult(returncode=returncode, stdout=stdout, stderr="err")

    async def run(  # type: ignore[override]
        self,
        *,
        compose_project: str,
        compose_file: Path,
        prompt: str,
        model: str | None = None,
        workspace_id: str | None = None,
        log_source: str = "agent",
    ) -> AgentRunResult:
        self.calls.append(prompt)
        self.workspace_ids.append(workspace_id)
        if not self._queued:
            return AgentRunResult(returncode=0, stdout="fixed it", stderr="")
        r = self._queued.pop(0)
        if r.returncode != 0:
            raise AgentRunError(
                agent=AgentRuntime.claude_code,
                result=CommandResult(returncode=r.returncode, stdout=r.stdout, stderr=r.stderr),
            )
        return r


class RecordedSleep:
    """Replacement for ``asyncio.sleep`` so tests don't actually sleep."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _git_calls(cmd: FakeCommandRunner, *tokens: str) -> list:
    return [
        call
        for call in cmd.calls
        if call.args[:1] == ["git"] and all(token in call.args for token in tokens)
    ]


def _pr_payload(
    *,
    closed: bool = False,
    merged: bool = False,
    merge_commit_sha: str = "mergecommit1234567890",
    mergeable: str = "MERGEABLE",
    merge_state_status: str = "CLEAN",
    check_state: str = "SUCCESS",
    threads: list[dict] | None = None,
    reviews: list[dict] | None = None,
    comments: list[dict] | None = None,
) -> str:
    return json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": 42,
                        "headRefOid": "abc123",
                        "mergeable": mergeable,
                        "mergeStateStatus": merge_state_status,
                        "isDraft": False,
                        "closed": closed,
                        "merged": merged,
                        "mergeCommit": {"oid": merge_commit_sha} if merged else None,
                        "baseRef": {"name": "development", "target": {"oid": "base0"}},
                        "commits": {
                            "nodes": [{"commit": {"statusCheckRollup": {"state": check_state}}}]
                        },
                        "reviewThreads": {"nodes": threads or []},
                        "reviews": {"nodes": reviews or []},
                        "comments": {"nodes": comments or []},
                    }
                }
            }
        }
    )


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.fixture
def cmd() -> FakeCommandRunner:
    return FakeCommandRunner()


@pytest.fixture
def adapter() -> FakeAdapter:
    return FakeAdapter()


@pytest.fixture
def sleep_fn() -> RecordedSleep:
    return RecordedSleep()


async def _seed_monitoring_workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    agent: str = "claude_code",
    repo_url: str = "git@github.com:dimileeh/aira-web.git",
    pr_number: int = 42,
    branch_name: str | None = None,
    remote_push_branch: str | None = None,
    task_kind: str = "feature_branch_pr",
    task_policy: dict[str, object] | None = None,
) -> str:
    """Insert a workspace already in ``monitoring_pr`` state.

    ``branch_name`` defaults to ``awf/<ws.id>`` (the feature-branch-PR
    convention). ``remote_push_branch`` defaults to ``branch_name`` —
    which is what the monitor falls back to when the column is unset,
    preserving backward-compat semantics for pre-migration rows.
    """
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url=repo_url,
            branch_base="development",
            task_title="monitor test",
            task_prompt="x",
            agent=agent,
            test_commands=["pytest -q"],
            requires_database=False,
            task_kind=task_kind,
            task_policy=task_policy or {},
        )
        attempt = await TaskAttemptRepository(s).create_for_workspace(
            task=await TaskRepository(s).create_or_get(
                repo_url=ws.repo_url,
                base_branch=ws.branch_base,
                title=ws.task_title,
                prompt=ws.task_prompt,
                external_id=ws.task_external_id,
                idempotency_key=None,
                task_class=ws.task_class,
                owned_paths=list(ws.owned_paths),
            ),
            workspace=ws,
        )
        ws.branch_name = branch_name or f"awf/{ws.id}"
        ws.remote_push_branch = remote_push_branch or ws.branch_name
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.pr_url = f"https://github.com/dimileeh/aira-web/pull/{pr_number}"
        ws.pr_number = pr_number
        # Walk requested → provisioning → ready → running → validating → pushing → monitoring_pr
        for target in (
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
            WorkspaceStatus.monitoring_pr,
        ):
            await repo.transition(ws, to=target, reason_code="X")
        validation_repo = ValidationRunRepository(s)
        validation_run = await validation_repo.start(
            workspace_id=ws.id,
            attempt_id=attempt.id,
            tier=1,
            commands=[],
            base_commit=ws.base_commit,
            target_branch=ws.remote_push_branch,
            target_head_sha="abc123",
            log_stream_refs={},
        )
        await validation_repo.finish(
            validation_run.id,
            status="succeeded",
            reason_code="VALIDATION_OK",
        )
        await s.commit()
        return ws.id


def _make_runner(
    *,
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    worktrees_root: Path,
    auto_merge: bool = True,
    max_outer_iterations: int = 20,
    initial_review_grace_period_seconds: float = 0,
    gh: object | None = None,
) -> PullRequestMonitorRunner:
    return PullRequestMonitorRunner(
        session_factory=factory,
        runner=cmd,
        adapter=adapter,
        gh=gh if gh is not None else DefaultMergeMethodGitHubClient(cmd),
        monitor_config=MonitorConfig(
            auto_merge=auto_merge,
            poll_interval_seconds=60,
            settle_interval_seconds=30,
            initial_review_grace_period_seconds=initial_review_grace_period_seconds,
            pre_merge_settle_seconds=0,
            non_check_reviewer_settle_seconds=0,
        ),
        runner_config=MonitorRunnerConfig(
            max_outer_iterations=max_outer_iterations, max_fix_cycle_passes=3
        ),
        sleep=sleep_fn,
        worktrees_root=worktrees_root,
    )


class TestPushRejectRecovery:
    """Push is rejected when local diverged from remote. Without
    recovery, the monitor loops retrying SyncBase while local commits
    pile up and the head SHA on GitHub never moves. Recovery: fetch
    the feature branch + reset local hard to remote (GitHub is truth
    for pushed state), then the next outer-loop iteration works on a
    fresh aligned worktree."""

    @pytest.mark.unit
    async def test_push_rejection_triggers_fetch_and_reset_hard(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_workspace(factory, branch_name="awf/test-branch")
        # Outer iter 1: DIRTY state forces SyncBase; merge creates a
        # local commit; push gets rejected (non-fast-forward); recovery
        # fetch + reset --hard kick in.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="1\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload(merge_state_status="DIRTY"))
        cmd.queue_result(returncode=0)  # git merge --abort (defense)
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0)  # git merge --no-edit (clean)
        # Push rejected. (The monitor now passes the explicit remote_branch
        # into the push command as ``HEAD:refs/heads/awf/test-branch``, so
        # there's no ambiguous ``HEAD`` refspec that could be redirected
        # by leaked git config — see the 2026-04-23 aira-web incident.)
        cmd.queue_result(
            returncode=1,
            stderr=(
                "To github.com:dimileeh/aira-agent.git\n"
                " ! [rejected]        awf/test -> awf/test (fetch first)\n"
                "error: failed to push some refs ..."
            ),
        )
        # Recovery sequence: fetch branch, reset --hard. The monitor no
        # longer needs ``rev-parse --abbrev-ref`` to discover the branch
        # name — it already has ``remote_push_branch`` from the workspace
        # row, which is the authoritative source.
        cmd.queue_result(returncode=0)  # git fetch origin awf/test-branch
        cmd.queue_result(returncode=0)  # git reset --hard origin/awf/test-branch
        # Outer iter 2: reset worked; GitHub now reports CLEAN; merge.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload(merge_state_status="CLEAN"))
        cmd.queue_result(returncode=0)  # gh pr merge
        cmd.queue_result(returncode=0, stdout="MERGE-SHA\n")

        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        # Assert push used an explicit refspec — no bare ``HEAD`` arg.
        push_calls = _git_calls(cmd, "push")
        assert push_calls, "expected at least one push"
        for pc in push_calls:
            assert "HEAD:refs/heads/awf/test-branch" in pc.args, (
                "monitor must push with an explicit "
                "``HEAD:refs/heads/<branch>`` refspec to prevent git "
                "config from redirecting the push to another branch "
                "(2026-04-23 regression guard)"
            )
        # Assert fetch + reset --hard were called on the feature branch.
        fetch_branch_calls = [
            c
            for c in cmd.calls
            if c.args[:1] == ["git"]
            and "fetch" in c.args
            and any(a == "awf/test-branch" for a in c.args)
        ]
        assert fetch_branch_calls, "must fetch the feature branch for resync"
        reset_calls = [
            c
            for c in cmd.calls
            if c.args[:1] == ["git"]
            and "reset" in c.args
            and "--hard" in c.args
            and any("origin/awf/test-branch" in a for a in c.args)
        ]
        assert reset_calls, "must reset --hard to origin/<branch>"
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value

    @pytest.mark.unit
    async def test_non_rejection_push_failure_does_not_trigger_recovery(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """Auth / network / other push failures must NOT silently
        ``reset --hard`` — we'd wipe legitimate local state."""
        ws_id = await _seed_monitoring_workspace(factory)
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="1\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload(merge_state_status="BEHIND"))
        cmd.queue_result(returncode=0)  # git merge --abort
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0)  # git merge (clean)
        cmd.queue_result(returncode=128, stderr="ssh: Permission denied (publickey)")
        # Iter 2: cap at 1 so it bails fast.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="1\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload(merge_state_status="BEHIND"))

        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            max_outer_iterations=2,
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        # No reset --hard should have happened (auth failure is not a
        # divergence signal).
        reset_calls = [
            c
            for c in cmd.calls
            if c.args[:1] == ["git"] and "reset" in c.args and "--hard" in c.args
        ]
        assert not reset_calls, (
            f"reset --hard must not fire on non-rejection failures; got {reset_calls}"
        )


class TestDirtyConflictResolution:
    @pytest.mark.unit
    async def test_github_dirty_triggers_cli_conflict_resolve_and_recovery(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """GitHub reports mergeStateStatus=DIRTY (conflict against base).
        The monitor routes to SyncBase; the local ``git merge`` hits the
        conflict, the coding CLI is invoked with the conflict-resolve
        prompt, commits the resolution, push lands, next poll sees
        CLEAN, PR merges. The critical assertion here is that the CLI
        was invoked — confirms the LLM is in the loop for conflict
        decisions, not just waved through."""
        ws_id = await _seed_monitoring_workspace(factory)
        # Outer iter 1: GitHub says DIRTY; local rev-list says 0 behind.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind (local-stale)
        cmd.queue_result(returncode=0, stdout=_pr_payload(merge_state_status="DIRTY"))  # PR state
        cmd.queue_result(returncode=0)  # git merge --abort (no-op)
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=1, stderr="CONFLICT (content): src/foo.py")  # git merge fails
        cmd.queue_result(returncode=0, stdout="UU src/foo.py\n")  # git status --porcelain
        adapter.queue(stdout="resolved the merge conflict")
        cmd.queue_result(returncode=0)  # git push
        cmd.queue_result(returncode=0, stdout="SYNC-BASE-SHA\n")  # rev-parse origin/<base>
        # Outer iter 2: CLEAN → merge.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload(merge_state_status="CLEAN"))
        cmd.queue_result(returncode=0)  # gh pr merge
        cmd.queue_result(returncode=0, stdout="MERGE-SHA\n")

        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        # The CLI saw a conflict-resolve prompt.
        assert any(
            "CONFLICT" in p or "merge conflicts" in p or "conflicts" in p for p in adapter.calls
        )
        # Workspace terminated cleanly on merge.
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value

    @pytest.mark.unit
    async def test_sync_base_starts_with_merge_abort_for_crash_safety(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """If the worktree is in a MERGING state from a prior failed
        sync, the next sync must ``git merge --abort`` first or the new
        ``git merge`` would refuse."""
        ws_id = await _seed_monitoring_workspace(factory)
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="3\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload())
        cmd.queue_result(returncode=0)  # git merge --abort ← defense
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0)  # git merge (clean)
        cmd.queue_result(returncode=0)  # git push
        # Outer iter 2: clean → merge.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload())
        cmd.queue_result(returncode=0)  # merge
        cmd.queue_result(returncode=0, stdout="M\n")

        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        abort_calls = [
            c
            for c in cmd.calls
            if c.args[:1] == ["git"] and "merge" in c.args and "--abort" in c.args
        ]
        assert len(abort_calls) == 1, "git merge --abort must fire exactly once before sync"


class TestWaitForCi:
    @pytest.mark.unit
    async def test_pending_sleeps_without_bumping_iter(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_workspace(factory)
        # Outer iter 1: PENDING → sleep (no iter bump).
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload(check_state="PENDING"))
        # Outer iter 2: SUCCESS → merge.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload())
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="M\n")
        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        # We slept with the poll interval (60s) on iter 1.
        assert 60.0 in sleep_fn.calls
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.monitor_iter_count == 0  # no non-passive actions ran


class TestStatePersistence:
    @pytest.mark.unit
    async def test_crash_safe_resume_does_not_re_address_thread(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """Persisted ``monitor_threads_addressed`` shields a re-run from
        re-invoking the CLI on the same thread."""
        ws_id = await _seed_monitoring_workspace(factory)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            state = MonitorState()
            _mark_review_thread_addressed(
                state,
                ReviewThread(
                    thread_id="T1",
                    path="a",
                    line=1,
                    body_excerpt="",
                ),
                "fix_committed",
            )
            ws.monitor_threads_addressed = dict(state.threads_addressed_ids)
            await s.commit()
        thread = {
            "id": "T1",
            "isResolved": False,  # upstream hasn't re-read after our previous resolve
            "isOutdated": False,
            "path": "a",
            "line": 1,
            "comments": {"nodes": []},
        }
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload(threads=[thread]))
        # decide() filters T1 out of the batch → falls through to Merge.
        cmd.queue_result(returncode=0)  # gh pr merge
        cmd.queue_result(returncode=0, stdout="MERGE\n")
        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        # CLI never invoked again on T1.
        assert adapter.calls == []


class TestExternalTermination:
    @pytest.mark.unit
    async def test_workspace_not_in_monitoring_pr_returns_early(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """If the workspace row is already ``completed``/``failed``/``cancelled``
        (e.g. an operator ran a cancel op in parallel), the monitor silently
        exits without touching anything."""
        ws_id = await _seed_monitoring_workspace(factory)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            await WorkspaceRepository(s).transition(
                ws, to=WorkspaceStatus.cancelled, reason_code="EXTERNAL_CANCEL"
            )
            await s.commit()
        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        # Zero gh calls — we bailed before fetching anything.
        assert cmd.calls == []


class TestGitHubApiError:
    @pytest.mark.unit
    async def test_github_api_error_terminates_with_failure(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """Rate-limit / auth / malformed response from GitHub → failure
        (rather than hanging the monitor forever on retries)."""
        ws_id = await _seed_monitoring_workspace(factory)
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=1, stderr="rate-limited: try again in 60s")
        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "github error" in (ws.failure_message or "")


class _BitBucketErrorForgeClient:
    """Forge client stub whose ``fetch_pr_status`` raises like a real
    ``BitBucketClient`` does on an API/transport failure."""

    def __init__(self, exc: BitBucketClientError) -> None:
        self._exc = exc
        self.closed = False

    async def fetch_pr_status(self, **_kwargs: object) -> object:
        raise self._exc

    async def aclose(self) -> None:
        self.closed = True


class TestBitBucketApiError:
    @pytest.mark.unit
    async def test_bitbucket_api_error_terminates_with_failure(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """A ``BitBucketClientError`` from ``fetch_pr_status`` must terminate the
        workspace failed with the exception's reason code preserved — not escape
        ``run()`` and crash the background monitor task (PR #443 review)."""
        ws_id = await _seed_monitoring_workspace(factory)
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        gh = _BitBucketErrorForgeClient(
            BitBucketClientError(
                operation="bitbucket fetch_pr_status",
                status=500,
                body="boom",
                reason_code="BITBUCKET_API_ERROR",
            )
        )
        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            gh=gh,
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "bitbucket error" in (ws.failure_message or "")
            events = await WorkspaceEventRepository(s).list(workspace_id=ws_id)
        assert any(e.reason_code == "BITBUCKET_API_ERROR" for e in events)
        # The single-use forge client is closed on the terminal exit.
        assert gh.closed is True


class TestAgentRunErrorResilience:
    @pytest.mark.unit
    async def test_cli_crash_during_address_thread_records_agent_failed(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """CLI process dies mid-address — monitor logs + records a retryable
        agent failure rather than aborting the whole workspace."""
        ws_id = await _seed_monitoring_workspace(factory)
        thread = {
            "id": "T_crash",
            "isResolved": False,
            "isOutdated": False,
            "path": "a",
            "line": 1,
            "comments": {"nodes": [{"bodyText": "?", "author": {"login": "cr"}}]},
        }
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload(threads=[thread]))
        # CLI raises AgentRunError mid-fix.
        adapter.queue(returncode=2, raise_error=True)
        cmd.queue_result(returncode=0, stdout=_pr_payload())  # settle refetch
        cmd.queue_result(returncode=0)  # push (maybe nothing, still called)
        # Iter 2: thread addressed-as-agent-failed in state remains retryable.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload(threads=[thread]))
        cmd.queue_result(returncode=0)  # merge
        cmd.queue_result(returncode=0, stdout="M\n")
        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.monitor_threads_addressed.get("T_crash") == "agent_failed"

    @pytest.mark.unit
    async def test_cli_crash_during_sync_base_continues_to_push(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_workspace(factory)
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="2\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload())
        cmd.queue_result(returncode=0)  # git merge --abort (no-op)
        cmd.queue_result(returncode=0)  # fetch
        cmd.queue_result(returncode=1, stderr="CONFLICT")  # merge fails
        cmd.queue_result(returncode=0, stdout="UU a\n")  # status
        adapter.queue(returncode=2, raise_error=True)  # CLI dies
        cmd.queue_result(returncode=0)  # push (still attempted)
        # Iter 2: PR ends up clean, monitor proceeds to Merge.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload())
        cmd.queue_result(returncode=0)  # gh pr merge
        cmd.queue_result(returncode=0, stdout="M\n")
        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        # Push was invoked despite CLI crash.
        assert _git_calls(cmd, "push")

    @pytest.mark.unit
    async def test_cli_crash_during_ci_fix_still_pushes(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_workspace(factory)
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload(check_state="FAILURE"))
        cmd.queue_result(
            returncode=0,
            stdout=json.dumps(
                [{"databaseId": 1, "name": "lint", "conclusion": "FAILURE", "status": "completed"}]
            ),
        )
        cmd.queue_result(returncode=0, stdout="log")  # log fetch
        adapter.queue(returncode=2, raise_error=True)  # CLI dies mid-ci-fix
        cmd.queue_result(returncode=0)  # push
        # Iter 2: PR clean, merge.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload())
        cmd.queue_result(returncode=0)  # gh pr merge
        cmd.queue_result(returncode=0, stdout="M\n")
        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        assert _git_calls(cmd, "push")


class TestBaseBehindEdges:
    @pytest.mark.unit
    async def test_rev_list_error_fails_monitor_instead_of_assuming_up_to_date(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """Failed rev-list means AWF cannot trust local base freshness."""
        ws_id = await _seed_monitoring_workspace(factory)
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=1, stderr="unknown revision")  # base-behind fails
        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_message is not None
            assert "unknown revision" in ws.failure_message

    @pytest.mark.unit
    async def test_rev_list_garbage_output_fails_monitor_instead_of_assuming_zero(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_workspace(factory)
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="not-a-number\n")  # garbage
        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_message is not None
            assert "not-a-number" in ws.failure_message


class TestResumePreservesMonitorStartedAt:
    @pytest.mark.unit
    async def test_preexisting_started_at_is_reused(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """``monitor_started_at`` set by a previous run survives a resume.
        We load it, compute elapsed, and the wall-clock cap applies from
        original entry — not from this restart."""
        from datetime import UTC as _UTC
        from datetime import datetime as _dt
        from datetime import timedelta

        ws_id = await _seed_monitoring_workspace(factory)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            # Claim the monitor started 2h ago.
            ws.monitor_started_at = _dt.now(_UTC) - timedelta(hours=2)
            await s.commit()
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload())
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="M\n")
        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        # Merged successfully despite being 2h into the phase.
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value


class TestMonitorInvariantFailures:
    """Failure paths at the top of ``run()`` that terminate the
    workspace cleanly instead of crashing the background runner. These
    are invariant violations seeded upstream; the monitor's job is to
    fail fast with a readable message, not propagate AssertionError."""

    @pytest.mark.unit
    async def test_missing_pr_number_terminates_failed(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """If a workspace reaches ``monitoring_pr`` with ``pr_number=None``
        (upstream provisioning bug), the monitor transitions it to
        ``failed`` with a readable message and returns — no GitHub
        calls, no agent runs."""
        ws_id = await _seed_monitoring_workspace(factory)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            ws.pr_number = None
            await s.commit()

        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "pr_number" in (ws.failure_message or "")

    @pytest.mark.unit
    async def test_missing_branch_and_remote_push_branch_terminates_failed(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """Workspace with no branch_name AND no remote_push_branch.
        Monitor must refuse to push rather than guess."""
        ws_id = await _seed_monitoring_workspace(factory)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            ws.branch_name = None
            ws.remote_push_branch = None
            await s.commit()

        # Monitor will call fetch_base + fetch_pr_status before the
        # branch check, so queue results for those too.
        cmd.queue_result(returncode=0)  # fetch base
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload())

        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert "branch_name" in (ws.failure_message or "")


class TestMonitorDbHelpers:
    """Direct-call coverage for the repository-adjacent helpers that
    tests can't hit via the full loop."""

    @pytest.mark.unit
    async def test_load_workspace_missing_raises(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        with pytest.raises(RuntimeError, match="disappeared"):
            await runner._load_workspace("ws_nonexistent")

    @pytest.mark.unit
    async def test_persist_state_noops_when_workspace_missing(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        from awf.runtime.pr_monitor import MonitorState

        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        import time as _t

        # Should silently return without raising — a missing ws is a
        # race with external termination, not an error.
        await runner._persist_state(
            "ws_missing",
            MonitorState(
                iter_count=1,
                last_push_sha="abc",
                threads_addressed_ids={},
                started_at=_t.monotonic(),
            ),
        )

    @pytest.mark.unit
    async def test_terminate_completed_noops_when_workspace_missing(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        await runner._terminate_completed("ws_missing", pr_merge_sha="x")

    @pytest.mark.unit
    async def test_terminate_failed_noops_when_workspace_missing(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        await runner._terminate_failed("ws_missing", message="gone")

    @pytest.mark.unit
    async def test_load_state_handles_naive_datetime(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """Some DB drivers return
        naive datetimes. The loader must treat them as UTC so elapsed
        math doesn't go sideways."""
        from datetime import datetime as _dt

        ws_id = await _seed_monitoring_workspace(factory)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            ws.monitor_started_at = _dt(2026, 4, 23, 10, 0, 0)  # naive
            await s.commit()

        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            state = runner._load_state(ws)
            # If tzinfo wasn't applied, we'd have gotten a naive/aware
            # subtract TypeError — reaching here proves the branch ran.
            assert state.iter_count == 0

    @pytest.mark.unit
    async def test_load_state_converts_wall_clock_grace_marker_to_monotonic(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        from datetime import UTC as _UTC
        from datetime import datetime as _dt
        from datetime import timedelta

        ws_id = await _seed_monitoring_workspace(factory)
        started_wall = _dt.now(_UTC) - timedelta(minutes=10)
        started_key = _initial_review_grace_started_key(42)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            ws.monitor_threads_addressed = {started_key: f"{started_wall.timestamp():.6f}"}
            await s.commit()

        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            state = runner._load_state(ws)

        started_monotonic = float(state.threads_addressed_ids[started_key])
        expected_elapsed_before = (_dt.now(_UTC) - started_wall).total_seconds()
        actual_elapsed = time.monotonic() - started_monotonic
        expected_elapsed_after = (_dt.now(_UTC) - started_wall).total_seconds()
        assert expected_elapsed_before - 2 <= actual_elapsed <= expected_elapsed_after + 2

    @pytest.mark.unit
    async def test_load_state_rebases_legacy_grace_marker_from_monitor_started_at(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        from datetime import UTC as _UTC
        from datetime import datetime as _dt
        from datetime import timedelta

        ws_id = await _seed_monitoring_workspace(factory)
        started_wall = _dt.now(_UTC) - timedelta(minutes=10)
        started_key = _initial_review_grace_started_key(42)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            ws.monitor_started_at = started_wall
            ws.monitor_threads_addressed = {started_key: "1000.000000"}
            await s.commit()

        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            state = runner._load_state(ws)

        started_monotonic = float(state.threads_addressed_ids[started_key])
        expected_elapsed_before = (_dt.now(_UTC) - started_wall).total_seconds()
        actual_elapsed = time.monotonic() - started_monotonic
        expected_elapsed_after = (_dt.now(_UTC) - started_wall).total_seconds()
        assert expected_elapsed_before - 2 <= actual_elapsed <= expected_elapsed_after + 2

    @pytest.mark.unit
    async def test_persist_state_writes_wall_clock_grace_marker(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_workspace(factory)
        started_key = _initial_review_grace_started_key(42)
        started_monotonic = time.monotonic() - 300
        state = MonitorState(
            threads_addressed_ids={started_key: f"{started_monotonic:.6f}"},
            started_at=time.monotonic(),
        )
        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )

        await runner._persist_state(ws_id, state)

        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            started_wall = float(ws.monitor_threads_addressed[started_key])
        assert started_wall > 1_000_000_000
        elapsed = time.time() - started_wall
        assert elapsed >= 300
        assert elapsed < 360
