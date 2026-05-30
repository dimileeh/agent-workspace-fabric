"""Integration tests for PullRequestMonitorRunner.

'Integration' here means: real SQLAlchemy against PostgreSQL, the real
decision core (``decide``), the real prompt templates, and a real
``GitHubClient`` — but backed by ``FakeCommandRunner`` and a tiny fake
adapter so no subprocesses spawn. Tests drive full loops end-to-end.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentAdapter, AgentRunError, AgentRunResult
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.github_client import GitHubClient
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import (
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    MonitorConfig,
)
from awf.runtime.pr_monitor_runner import (
    MonitorRunnerConfig,
    PullRequestMonitorRunner,
)
from awf.runtime.pr_monitor_runner.helpers import _parse_verdict
from tests.postgres import postgres_test_engine


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
) -> PullRequestMonitorRunner:
    return PullRequestMonitorRunner(
        session_factory=factory,
        runner=cmd,
        adapter=adapter,
        gh=GitHubClient(cmd),
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


class TestCompleteWorkspaceTearsDownComposeStack:
    """2026-04-24 incident: Docker ran out of network subnets because
    every AWF workspace's compose stack survived its workspace's
    termination. ``_terminate_completed`` now runs
    ``docker compose down`` as a best-effort cleanup. Failed
    workspaces are preserved for operator inspection."""

    @pytest.mark.unit
    async def test_happy_merge_tears_down_compose(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_workspace(factory)
        cmd.queue_result(returncode=0)  # fetch base
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload())  # clean
        cmd.queue_result(returncode=0)  # gh pr merge
        cmd.queue_result(returncode=0, stdout="MERGE-SHA\n")  # gh pr view (sha)
        cmd.queue_result(returncode=0)  # docker compose down

        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="awf_ws_test",
            compose_file=tmp_path / "compose.yml",
        )
        teardown_calls = [
            c for c in cmd.calls if c.args[:2] == ["docker", "compose"] and "down" in c.args
        ]
        assert len(teardown_calls) == 1
        args = teardown_calls[0].args
        assert "-p" in args and "awf_ws_test" in args
        assert "--remove-orphans" in args
        assert "--volumes" in args

    @pytest.mark.unit
    async def test_failed_abort_does_not_tear_down_compose(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """Failed workspaces stay up so the operator can inspect the
        stack (read logs, exec into containers, etc.). Exercise via a
        PR closed externally → Abort(pr_closed_externally) → failed."""
        ws_id = await _seed_monitoring_workspace(factory)
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload(closed=True))
        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="awf_ws_abort",
            compose_file=tmp_path / "compose.yml",
        )
        teardown_calls = [
            c for c in cmd.calls if c.args[:2] == ["docker", "compose"] and "down" in c.args
        ]
        assert teardown_calls == [], (
            "failed workspaces must NOT be torn down automatically — "
            "operator may need the stack for inspection"
        )

    @pytest.mark.unit
    async def test_short_circuit_completed_tears_down_compose(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """PR was merged elsewhere before the monitor started. The
        ``ShortCircuitCompleted`` path completes the workspace — must
        tear down too."""
        ws_id = await _seed_monitoring_workspace(factory)
        cmd.queue_result(returncode=0)  # fetch base
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload(merged=True))
        cmd.queue_result(returncode=0)  # docker compose down

        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="awf_ws_short",
            compose_file=tmp_path / "compose.yml",
        )
        teardown_calls = [
            c for c in cmd.calls if c.args[:2] == ["docker", "compose"] and "down" in c.args
        ]
        assert len(teardown_calls) == 1
        assert "awf_ws_short" in teardown_calls[0].args

    @pytest.mark.unit
    async def test_merge_blocked_notify_human_tears_down_after_external_merge(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """``Merge`` blocked by branch protection → fallback to
        NotifyHuman and keep the workspace alive until the PR is merged."""
        ws_id = await _seed_monitoring_workspace(factory)
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload())
        cmd.queue_result(
            returncode=1, stderr="Pull request protected: approvals required"
        )  # gh pr merge blocked
        cmd.queue_result(returncode=0)  # post_comment (ready-to-merge)
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload(merged=True))
        cmd.queue_result(returncode=0)  # docker compose down

        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="awf_ws_blocked",
            compose_file=tmp_path / "compose.yml",
        )
        teardown_calls = [
            c for c in cmd.calls if c.args[:2] == ["docker", "compose"] and "down" in c.args
        ]
        assert len(teardown_calls) == 1
        assert "awf_ws_blocked" in teardown_calls[0].args

    @pytest.mark.unit
    async def test_plain_notify_human_tears_down_after_external_merge(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """Release-PR-style monitor (``auto_merge=False``) posts a
        ready-to-merge comment, keeps polling, then tears down after
        external merge."""
        ws_id = await _seed_monitoring_workspace(factory)
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload())
        cmd.queue_result(returncode=0)  # post_comment
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload(merged=True))
        cmd.queue_result(returncode=0)  # docker compose down

        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            auto_merge=False,
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="awf_ws_notify",
            compose_file=tmp_path / "compose.yml",
        )
        teardown_calls = [
            c for c in cmd.calls if c.args[:2] == ["docker", "compose"] and "down" in c.args
        ]
        assert len(teardown_calls) == 1
        assert "awf_ws_notify" in teardown_calls[0].args

    @pytest.mark.unit
    async def test_teardown_raised_exception_swallowed(
        self,
        factory: async_sessionmaker[AsyncSession],
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """``docker compose down`` subprocess RAISING (not exiting
        non-zero — raising) must not kill the monitor. Simulates
        FileNotFoundError when docker isn't on PATH."""

        class _RaisingRunner(FakeCommandRunner):
            def __init__(self) -> None:
                super().__init__()
                self.teardown_seen = False

            async def run(self, args, *, input_bytes=None, cwd=None):  # type: ignore[override]
                if args[:2] == ["docker", "compose"] and "down" in args:
                    self.teardown_seen = True
                    raise FileNotFoundError("docker: command not found")
                return await super().run(args, input_bytes=input_bytes, cwd=cwd)

        cmd_raising = _RaisingRunner()
        ws_id = await _seed_monitoring_workspace(factory)
        cmd_raising.queue_result(returncode=0)
        cmd_raising.queue_result(returncode=0, stdout="0\n")
        cmd_raising.queue_result(returncode=0, stdout=_pr_payload())
        cmd_raising.queue_result(returncode=0)  # gh pr merge
        cmd_raising.queue_result(returncode=0, stdout="M\n")  # gh pr view

        runner = _make_runner(
            factory=factory,
            cmd=cmd_raising,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        # Must NOT raise despite the teardown raising internally.
        await runner.run(
            workspace_id=ws_id,
            compose_project="awf_ws_missing_docker",
            compose_file=tmp_path / "compose.yml",
        )
        assert cmd_raising.teardown_seen
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value

    @pytest.mark.unit
    async def test_teardown_failure_does_not_mask_completion(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """``docker compose down`` exiting non-zero (stack already
        gone, permission issue) must NOT mask completion — the DB
        transition already landed before the teardown call."""
        ws_id = await _seed_monitoring_workspace(factory)
        cmd.queue_result(returncode=0)  # fetch base
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload())
        cmd.queue_result(returncode=0)  # gh pr merge
        cmd.queue_result(returncode=0, stdout="MERGE-SHA\n")  # gh pr view (sha)
        cmd.queue_result(returncode=1, stderr="no such compose project")  # teardown fails

        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="awf_ws_test",
            compose_file=tmp_path / "compose.yml",
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.pr_merge_sha == "MERGE-SHA"


class TestMaxOuterIterationsSafetyNet:
    """If ``max_outer_iterations`` is exhausted without the decision
    core reaching a terminal action, the runner terminates the
    workspace instead of silently returning — a decision-loop bug
    would otherwise leave the workspace wedged in ``monitoring_pr``
    forever."""

    @pytest.mark.unit
    async def test_iter_exhaustion_terminates_failed(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_workspace(factory)
        # Queue results for one passive iteration (WaitForCI). Since the
        # fake WaitForCI just sleeps and returns without transitioning,
        # we'll spin until max_outer_iterations exhausts.
        for _ in range(3):
            cmd.queue_result(returncode=0)  # fetch base
            cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
            cmd.queue_result(
                returncode=0,
                # PENDING status so decide() returns WaitForCI forever.
                stdout=_pr_payload(check_state="PENDING"),
            )

        runner = PullRequestMonitorRunner(
            session_factory=factory,
            runner=cmd,
            adapter=adapter,
            gh=GitHubClient(cmd),
            monitor_config=MonitorConfig(
                auto_merge=True,
                poll_interval_seconds=60,
                settle_interval_seconds=30,
                pre_merge_settle_seconds=0,
            ),
            runner_config=MonitorRunnerConfig(
                max_outer_iterations=3,  # tight cap so the safety net fires
                max_fix_cycle_passes=3,
            ),
            sleep=sleep_fn,
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
            assert "max_outer_iterations" in (ws.failure_message or "")


class TestReviewCommentAddressing:
    """The fix-cycle branch that exercises review-level comments (as
    distinct from inline threads). PR #338 review feedback: review
    comments need their own verdict path."""

    @pytest.mark.unit
    async def test_fix_cycle_addresses_review_comments(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_workspace(factory)
        # PR has a review-level comment (not a thread) with unresolved
        # feedback. The decide logic produces AddressComments with
        # review_comments populated.
        # Review-level (outside-diff) comment. The gh client looks for
        # ``databaseId`` + non-empty ``body`` to materialise a
        # ReviewComment.
        review = {
            "databaseId": 4999999,
            "body": "please clean this up — outside-diff review comment",
            "author": {"login": "cr"},
            "state": "COMMENTED",
        }
        cmd.queue_result(returncode=0)  # fetch base
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload(reviews=[review]))
        adapter.queue(stdout="fixed review comment")
        cmd.queue_result(returncode=0, stdout=_pr_payload())  # settle poll
        cmd.queue_result(returncode=0)  # git push
        cmd.queue_result(returncode=0, stdout="newhead\n")  # rev-parse HEAD
        # Iter 2: clean, merge
        cmd.queue_result(returncode=0)
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
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            # Review comment was addressed (recorded in state via the
            # databaseId-derived comment_id).
            assert "4999999" in ws.monitor_threads_addressed


class TestPushUsesExplicitRefspec:
    """Regression guard for the 2026-04-23 aira-web incident.

    On that day, four AWF feature-branch commits (``ebd3985``,
    ``59c7258``, ``3019a76``, ``61c8520``) landed on
    ``origin/development`` instead of the feature branch. Root cause:
    the monitor's ``git push origin HEAD`` resolved against
    ``push.default=upstream`` + ``branch.<X>.merge=refs/heads/development``
    — both set globally on the shared bare mirror's config by prior
    sync workspaces and auto-tracked-upstream at branch creation. With
    both configs active, ``HEAD`` got redirected to ``development``.

    The invariant these tests enforce: every ``git push`` issued from
    the monitor MUST carry an explicit ``HEAD:refs/heads/<branch>``
    refspec, so git ignores ``push.default`` and friends. If any code
    path reverts to bare ``HEAD``, the polluted-config scenario can
    repeat — so we assert the refspec form on every push exit.
    """

    @pytest.mark.unit
    async def test_fix_cycle_push_uses_explicit_refspec(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_workspace(
            factory, branch_name="awf/feature-x", remote_push_branch="awf/feature-x"
        )
        thread = {
            "id": "T1",
            "isResolved": False,
            "isOutdated": False,
            "path": "src/x.ts",
            "line": 10,
            "comments": {"nodes": [{"bodyText": "rename", "author": {"login": "cr"}}]},
        }
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload(threads=[thread]))
        adapter.queue(stdout="fixed in commit abc")
        cmd.queue_result(returncode=0, stdout=_pr_payload())  # settle poll
        cmd.queue_result(returncode=0, stderr="")  # git push (under inspection)
        cmd.queue_result(returncode=0, stdout="newhead\n")  # rev-parse HEAD
        cmd.queue_result(
            returncode=0,
            stdout=json.dumps(
                {"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}}
            ),
        )
        # Iter 2: clean so loop terminates.
        cmd.queue_result(returncode=0)  # fetch base
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload())
        cmd.queue_result(returncode=0)  # gh pr merge
        cmd.queue_result(returncode=0, stdout="SHA\n")
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
        push_calls = _git_calls(cmd, "push")
        assert push_calls, "fix_cycle must push"
        for pc in push_calls:
            # The bare ``HEAD`` arg would mean the ambiguous form
            # ``git push origin HEAD`` (the 2026-04-23 bug). The fix
            # requires an explicit src:dst refspec.
            assert "HEAD" not in pc.args, (
                f"monitor pushed with bare ``HEAD`` — that's the 2026-04-23 bug. "
                f"Use ``HEAD:refs/heads/<branch>`` instead. Full args: {pc.args}"
            )
            assert "HEAD:refs/heads/awf/feature-x" in pc.args, (
                f"push refspec must name the remote branch explicitly. Full args: {pc.args}"
            )

    @pytest.mark.unit
    async def test_sync_base_push_uses_explicit_refspec(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_workspace(
            factory, branch_name="awf/feature-y", remote_push_branch="awf/feature-y"
        )
        # Iter 1: SyncBase (base-behind=2, any mergeable state).
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="2\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload())
        cmd.queue_result(returncode=0)  # merge --abort
        cmd.queue_result(returncode=0)  # fetch base
        cmd.queue_result(returncode=0)  # merge --no-edit
        cmd.queue_result(returncode=0)  # git push (under inspection)
        # Iter 2: clean, merge.
        cmd.queue_result(returncode=0)
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
        push_calls = _git_calls(cmd, "push")
        assert push_calls
        for pc in push_calls:
            assert "HEAD:refs/heads/awf/feature-y" in pc.args

    @pytest.mark.unit
    async def test_ci_fix_push_uses_explicit_refspec(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_workspace(
            factory, branch_name="awf/feature-z", remote_push_branch="awf/feature-z"
        )
        # Iter 1: CI failure → ReportCiFailure → push.
        cmd.queue_result(returncode=0)  # fetch base
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload(check_state="FAILURE"))
        # gh run list (array of runs for fetch_failing_check_logs).
        cmd.queue_result(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "databaseId": 1,
                        "name": "lint",
                        "conclusion": "FAILURE",
                        "status": "completed",
                    }
                ]
            ),
        )
        cmd.queue_result(returncode=0, stdout="log tail")  # gh run view --log-failed
        adapter.queue(stdout="fixed CI")
        cmd.queue_result(returncode=0)  # git push (under inspection)
        # Iter 2: clean, merge.
        cmd.queue_result(returncode=0)
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload())
        cmd.queue_result(returncode=0, stdout=json.dumps([]))  # no failures
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
        push_calls = _git_calls(cmd, "push")
        assert push_calls
        for pc in push_calls:
            assert "HEAD:refs/heads/awf/feature-z" in pc.args

    @pytest.mark.unit
    async def test_sync_workspace_pushes_to_remote_not_local_branch(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """Sync workspaces (release-sync / feature-sync) use a
        per-workspace LOCAL ref (``release-sync/ws_X``) but push to a
        different REMOTE branch (e.g. ``development``). The monitor
        must honour ``remote_push_branch``, not ``branch_name``."""
        ws_id = await _seed_monitoring_workspace(
            factory,
            branch_name="release-sync/ws_abc",
            remote_push_branch="development",
        )
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="2\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload())
        cmd.queue_result(returncode=0)  # merge --abort
        cmd.queue_result(returncode=0)  # fetch base
        cmd.queue_result(returncode=0)  # merge
        cmd.queue_result(returncode=0)  # push (under inspection)
        # Iter 2: clean, merge.
        cmd.queue_result(returncode=0)
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
        push_calls = _git_calls(cmd, "push")
        assert push_calls
        for pc in push_calls:
            assert "HEAD:refs/heads/development" in pc.args, (
                "sync workspace must push to remote_push_branch "
                "(development), not local branch_name (release-sync/ws_abc)"
            )
            # And emphatically NOT the local branch.
            assert "HEAD:refs/heads/release-sync/ws_abc" not in pc.args

    @pytest.mark.unit
    async def test_remote_push_branch_falls_back_to_branch_name(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """Pre-migration rows may have ``remote_push_branch=None``.
        The monitor must fall back to ``branch_name`` so those rows
        keep working. New rows always set both."""
        async with factory() as s:
            repo = WorkspaceRepository(s)
            ws = await repo.create(
                repo_url="git@github.com:dimileeh/aira-web.git",
                branch_base="development",
                task_title="legacy",
                task_prompt="x",
                agent="claude_code",
                test_commands=[],
                requires_database=False,
            )
            for target in (
                WorkspaceStatus.provisioning,
                WorkspaceStatus.ready,
                WorkspaceStatus.running,
                WorkspaceStatus.validating,
                WorkspaceStatus.pushing,
                WorkspaceStatus.monitoring_pr,
            ):
                await repo.transition(ws, to=target, reason_code="X")
            ws.branch_name = "awf/legacy-row"
            ws.remote_push_branch = None  # legacy row, column unset
            ws.compose_project_name = f"awf_{ws.id}"
            ws.pr_url = "https://github.com/dimileeh/aira-web/pull/1"
            ws.pr_number = 1
            await s.commit()
            ws_id = ws.id

        cmd.queue_result(returncode=0)  # fetch base
        cmd.queue_result(returncode=0, stdout="2\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload())
        cmd.queue_result(returncode=0)  # merge --abort
        cmd.queue_result(returncode=0)  # fetch base
        cmd.queue_result(returncode=0)  # merge
        cmd.queue_result(returncode=0)  # push (under inspection)
        # Iter 2: clean, merge.
        cmd.queue_result(returncode=0)
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
        push_calls = _git_calls(cmd, "push")
        assert push_calls
        for pc in push_calls:
            assert "HEAD:refs/heads/awf/legacy-row" in pc.args


class TestParseVerdict:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout, expected",
        [
            # Empty agent output is a failure to produce, not a considered
            # deferral — it blocks the merge (needs_human), never auto-captured (#305).
            ("", "needs_human"),
            ("fixed in commit abc1234", "fix_committed"),
            ("FALSE POSITIVE: existing code is fine", "false_positive"),
            ("false positive: yep", "false_positive"),
            ("DEFER: need maintainer input", "defer"),
            ("DEFER : lowercase also fine", "defer"),
            ("Some chatty prose\nFALSE POSITIVE: ...", "false_positive"),
            ("Pushed fix. See commit.", "fix_committed"),
        ],
    )
    def test_parse_verdict_table(self, stdout: str, expected: str) -> None:
        assert _parse_verdict(stdout) == expected


class TestDeferredThreadCapture:
    """Two-kind defer (#305): a ``defer`` is a captured, resolvable
    follow-up; a ``needs_human`` blocks the merge for a human decision.
    """

    @pytest.mark.unit
    async def test_defer_verdict_captures_and_resolves_then_merges(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """A follow-up ``DEFER`` is durably captured then resolved (#305).

        Two-kind defer: a ``defer`` verdict is a capturable follow-up. The
        runner files a tracking issue and posts an explanatory PR comment, and
        only **then** resolves the thread — so the thread leaves GitHub's
        unresolved set, the merge gate clears, and the deferred work survives in
        the filed issue.
        """
        ws_id = await _seed_monitoring_workspace(factory)
        thread = {
            "id": "T_defer",
            "isResolved": False,
            "isOutdated": False,
            "path": "src/x.ts",
            "line": 10,
            "comments": {"nodes": [{"bodyText": "nit", "author": {"login": "cr"}}]},
        }
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload(threads=[thread]))  # PR state
        adapter.queue(stdout="AWF-VERDICT: DEFER: follow-up styling nit")
        cmd.queue_result(  # gh issue create -> tracking issue URL (capture)
            returncode=0, stdout="https://github.com/o/r/issues/77\n"
        )
        cmd.queue_result(returncode=0)  # gh pr comment (explanatory capture comment)
        cmd.queue_result(returncode=0, stdout=_pr_payload())  # settle fetch
        cmd.queue_result(returncode=0, stderr="")  # git push
        cmd.queue_result(returncode=0, stdout="newhead123\n")  # git rev-parse HEAD
        cmd.queue_result(  # resolve_thread mutation (after durable capture)
            returncode=0,
            stdout=json.dumps(
                {"data": {"resolveReviewThread": {"thread": {"id": "T_defer", "isResolved": True}}}}
            ),
        )
        cmd.queue_result(returncode=0)  # iter2 git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload())  # clean -> merge
        cmd.queue_result(returncode=0)  # gh pr merge
        cmd.queue_result(returncode=0, stdout="MERGE1\n")  # merge sha
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
        # The deferred work was captured as a tracking issue ...
        assert any(c.args[:3] == ["gh", "issue", "create"] for c in cmd.calls), (
            "a follow-up defer must file a tracking issue"
        )
        # ... and only then was the thread resolved, clearing the merge gate.
        assert any(
            any(a.startswith("query=") and "resolveReviewThread" in a for a in c.args)
            for c in cmd.calls
        ), "captured defer must resolve the thread"
        assert any(c.args[:3] == ["gh", "pr", "merge"] for c in cmd.calls), (
            "PR should merge once the captured defer thread is resolved"
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.monitor_threads_addressed.get("T_defer") == "defer"

    @pytest.mark.unit
    async def test_needs_human_verdict_does_not_resolve_thread_and_blocks_merge(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """Two contracts around ``NEEDS_HUMAN``:

        1. The thread is NOT resolved on GitHub — ``needs_human`` means the
           diff may be wrong or needs access the agent lacks; a human must
           decide. Resolving would sweep the question under the rug.
        2. The merge gate MUST NOT fire while such a thread is still unresolved
           on GitHub. A dedicated gate returns NotifyHuman instead — the
           maintainer sees the "ready to review" comment AND the thread still
           standing. (``defer`` is the *other* kind, captured + resolved; see
           ``test_defer_verdict_captures_and_resolves_then_merges``.)
        """
        ws_id = await _seed_monitoring_workspace(factory)
        thread = {
            "id": "T_needs_human",
            "isResolved": False,
            "isOutdated": False,
            "path": "a",
            "line": 1,
            "comments": {"nodes": [{"bodyText": "?", "author": {"login": "cr"}}]},
        }
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload(threads=[thread]))
        adapter.queue(stdout="AWF-VERDICT: NEEDS_HUMAN: need design input from maintainer")
        cmd.queue_result(returncode=0, stdout=_pr_payload())  # settle
        cmd.queue_result(returncode=0, stderr="Everything up-to-date")  # push
        # No resolve_thread call queued — contract #1 (never auto-resolved).
        # Second outer iteration: thread still unresolved on GitHub.
        # decide() should hit the unresolved-thread gate and return
        # NotifyHuman — queue the ``gh pr comment`` call, NOT a merge.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload(threads=[thread]))  # still there
        cmd.queue_result(returncode=0)  # gh pr comment
        # NotifyHuman is not terminal: the monitor stays alive and only
        # completes after the PR is actually merged.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload(merged=True, threads=[thread]))
        cmd.queue_result(returncode=0)  # docker compose down
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
        # Contract #1: no resolveReviewThread mutation fired.
        for c in cmd.calls:
            query_args = [a for a in c.args if a.startswith("query=")]
            assert not any("resolveReviewThread" in q for q in query_args), (
                "needs_human verdict must NOT resolve the thread"
            )
        # Contract #2: no merge call fired. The merge would look like
        # ``gh pr merge ...``; the NotifyHuman path is ``gh pr comment``.
        assert not any(c.args[:3] == ["gh", "pr", "merge"] for c in cmd.calls), (
            "unresolved needs_human must block merge — maintainer-driven only"
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value

    @pytest.mark.unit
    async def test_defer_capture_failure_downgrades_to_needs_human_and_blocks(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """If durable capture fails (e.g. token lacks the ``issues`` scope), the
        runner downgrades the verdict to ``needs_human`` so the thread is NOT
        resolved and the merge gate keeps blocking — fail safe (#305)."""
        ws_id = await _seed_monitoring_workspace(factory)
        thread = {
            "id": "T_defer_fail",
            "isResolved": False,
            "isOutdated": False,
            "path": "a",
            "line": 1,
            "comments": {"nodes": [{"bodyText": "?", "author": {"login": "cr"}}]},
        }
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload(threads=[thread]))
        adapter.queue(stdout="AWF-VERDICT: DEFER: follow-up styling nit")
        cmd.queue_result(  # gh issue create FAILS (missing issues scope)
            returncode=1, stderr="HTTP 403: Resource not accessible by integration"
        )
        cmd.queue_result(returncode=0, stdout=_pr_payload())  # settle
        cmd.queue_result(returncode=0, stderr="Everything up-to-date")  # push
        cmd.queue_result(returncode=0)  # iter2 git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload(threads=[thread]))  # still there
        cmd.queue_result(returncode=0)  # gh pr comment (NotifyHuman)
        cmd.queue_result(returncode=0)  # iter3 git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload(merged=True, threads=[thread]))
        cmd.queue_result(returncode=0)  # docker compose down
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
        # Capture was attempted but failed; the thread must NOT be resolved.
        assert any(c.args[:3] == ["gh", "issue", "create"] for c in cmd.calls)
        for c in cmd.calls:
            assert not any(a.startswith("query=") and "resolveReviewThread" in a for a in c.args), (
                "failed capture must NOT resolve the thread"
            )
        assert not any(c.args[:3] == ["gh", "pr", "merge"] for c in cmd.calls), (
            "failed capture downgrades to needs_human and must block merge"
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.monitor_threads_addressed.get("T_defer_fail") == "needs_human"

    @pytest.mark.unit
    async def test_defer_capture_comment_failure_still_resolves(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """Filing the tracking issue is the durable capture; the explanatory PR
        comment is best-effort. If ``gh issue create`` succeeds but the comment
        fails, capture still succeeds — the thread is resolved and the issue is
        recorded as filed so a retry never opens a duplicate (idempotency on
        partial success).
        """
        ws_id = await _seed_monitoring_workspace(factory)
        thread = {
            "id": "T_defer_partial",
            "isResolved": False,
            "isOutdated": False,
            "path": "src/x.ts",
            "line": 10,
            "comments": {"nodes": [{"bodyText": "nit", "author": {"login": "cr"}}]},
        }
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload(threads=[thread]))  # PR state
        adapter.queue(stdout="AWF-VERDICT: DEFER: follow-up styling nit")
        cmd.queue_result(  # gh issue create succeeds (durable capture)
            returncode=0, stdout="https://github.com/o/r/issues/88\n"
        )
        cmd.queue_result(returncode=1, stderr="HTTP 502")  # gh pr comment FAILS (best-effort)
        cmd.queue_result(returncode=0, stdout=_pr_payload())  # settle fetch
        cmd.queue_result(returncode=0, stderr="")  # git push
        cmd.queue_result(returncode=0, stdout="newhead123\n")  # git rev-parse HEAD
        cmd.queue_result(  # resolve_thread mutation (capture still succeeded)
            returncode=0,
            stdout=json.dumps(
                {
                    "data": {
                        "resolveReviewThread": {
                            "thread": {"id": "T_defer_partial", "isResolved": True}
                        }
                    }
                }
            ),
        )
        cmd.queue_result(returncode=0)  # iter2 git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload())  # clean -> merge
        cmd.queue_result(returncode=0)  # gh pr merge
        cmd.queue_result(returncode=0, stdout="MERGE1\n")  # merge sha
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
        # Exactly one tracking issue filed despite the comment failure.
        issue_creates = [c for c in cmd.calls if c.args[:3] == ["gh", "issue", "create"]]
        assert len(issue_creates) == 1, "comment failure must not re-file the issue"
        # Capture succeeded → thread resolved → PR merged.
        assert any(
            any(a.startswith("query=") and "resolveReviewThread" in a for a in c.args)
            for c in cmd.calls
        ), "a filed issue is durable capture; the thread should still resolve"
        assert any(c.args[:3] == ["gh", "pr", "merge"] for c in cmd.calls)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.monitor_threads_addressed.get("T_defer_partial") == "defer"
