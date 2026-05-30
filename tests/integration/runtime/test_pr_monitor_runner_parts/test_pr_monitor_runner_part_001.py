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
from awf.db.enums import AgentRuntime, TaskClass, WorkspaceStatus
from awf.db.repositories import (
    MergeCandidateRepository,
    StaleReasonCreate,
    StaleReasonRepository,
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


class TestTerminalShortCircuit:
    @pytest.mark.unit
    async def test_already_merged_pr_transitions_to_completed(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_workspace(factory)
        # base-behind count, then GraphQL payload reports merged=True.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload(merged=True))
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
            assert ws.status == WorkspaceStatus.completed.value

    @pytest.mark.unit
    async def test_closed_pr_transitions_to_failed(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_workspace(factory)
        cmd.queue_result(returncode=0)  # git fetch origin <base>
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
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.failure_message is not None
            assert "pr_closed_externally" in ws.failure_message


class TestHappyMerge:
    @pytest.mark.unit
    async def test_all_green_merges_and_completes(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_workspace(factory)
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload())  # PR state
        cmd.queue_result(returncode=0)  # gh pr merge
        cmd.queue_result(returncode=0, stdout="MERGESHA123\n")  # merge sha lookup
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
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.pr_merge_sha == "MERGESHA123"
        # gh pr merge was called with --squash.
        merge_args = next(c.args for c in cmd.calls if "merge" in c.args)
        assert "--squash" in merge_args
        assert "--delete-branch" in merge_args

    @pytest.mark.unit
    async def test_green_pr_waits_initial_review_grace_before_auto_merge(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_workspace(factory)
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload())  # initially merge-ready
        # Keep the test finite by simulating that a human/bot merged it while
        # AWF was respecting the initial review window.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload(merged=True))
        cmd.queue_result(returncode=0)  # docker compose down
        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            initial_review_grace_period_seconds=900,
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        assert sleep_fn.calls == [60]
        assert not any(call.args[:3] == ["gh", "pr", "merge"] for call in cmd.calls)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value

    @pytest.mark.unit
    async def test_missing_started_at_is_persisted_before_initial_grace_sleep(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_workspace(factory)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            ws.monitor_started_at = None
            await s.commit()

        sleep_calls: list[float] = []

        async def sleep_after_asserting_started(seconds: float) -> None:
            sleep_calls.append(seconds)
            async with factory() as s:
                ws = await WorkspaceRepository(s).get(ws_id)
                assert ws is not None
                assert ws.monitor_started_at is not None

        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload())  # initially merge-ready
        # Keep the test finite by simulating an external merge while AWF waits
        # out the initial review window.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload(merged=True))
        cmd.queue_result(returncode=0)  # docker compose down

        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_after_asserting_started,
            worktrees_root=tmp_path / "worktrees",
            initial_review_grace_period_seconds=900,
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )

        assert sleep_calls == [60]


class TestMergeBlockedFallsBackToNotify:
    @pytest.mark.unit
    async def test_merge_failure_falls_back_to_post_comment(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """Branch protection blocks the merge → runner posts human-attention
        and keeps monitoring until the PR is actually merged."""
        ws_id = await _seed_monitoring_workspace(factory)
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload())
        cmd.queue_result(returncode=1, stderr="branch protection rule blocks merge")
        cmd.queue_result(returncode=0)  # gh pr comment
        cmd.queue_result(returncode=0)  # git fetch origin <base>
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
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.pr_merge_sha == "mergecommit1234567890"
        # gh pr comment was invoked with the human-attention body.
        comment_args = next(c.args for c in cmd.calls if c.args[:3] == ["gh", "pr", "comment"])
        body = comment_args[comment_args.index("--body") + 1]
        assert "needs human attention" in body
        assert "branch protection rule blocks merge" in body


class TestNotifyHumanVariant:
    @pytest.mark.unit
    async def test_release_variant_posts_comment_without_merging(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_workspace(factory)
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload())  # PR state
        cmd.queue_result(returncode=0)  # gh pr comment
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
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
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        # No gh pr merge anywhere.
        assert not any(c.args[:3] == ["gh", "pr", "merge"] for c in cmd.calls)
        # One gh pr comment with ready body.
        comment_calls = [c for c in cmd.calls if c.args[:3] == ["gh", "pr", "comment"]]
        assert len(comment_calls) == 1
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value


class TestAddressComments:
    @pytest.mark.unit
    async def test_single_unresolved_thread_addressed_pushed_resolved_then_merged(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_workspace(factory)

        # Outer loop iter 1: PR has 1 unresolved thread.
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
        cmd.queue_result(returncode=0, stdout=_pr_payload(threads=[thread]))  # PR state
        # CLI addresses the thread (implicit "fixed in commit X").
        adapter.queue(stdout="fixed in commit abc")
        # After settle, re-fetch — no new threads.
        cmd.queue_result(returncode=0, stdout=_pr_payload())  # fetch in fix_cycle
        cmd.queue_result(returncode=0, stderr="")  # git push
        cmd.queue_result(returncode=0, stdout="newhead123\n")  # git rev-parse HEAD
        cmd.queue_result(  # resolve_thread mutation
            returncode=0,
            stdout=json.dumps(
                {"data": {"resolveReviewThread": {"thread": {"id": "T1", "isResolved": True}}}}
            ),
        )
        # Outer loop iter 2: thread now resolved upstream, no other blockers → merge.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload())  # clean
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
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.monitor_iter_count == 1  # one AddressComments iteration
            assert "T1" in ws.monitor_threads_addressed
            assert ws.monitor_threads_addressed["T1"] == "fix_committed"
        assert len(adapter.calls) == 1
        assert "T1" in adapter.calls[0]

    @pytest.mark.unit
    async def test_adopted_fork_pr_pushes_comment_fix_to_head_repository(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_workspace(
            factory,
            repo_url="git@github.com:base/aira-web.git",
            branch_name="feature-sync/ws_fork",
            remote_push_branch="fix/fork-review",
            task_kind="sync_feature_pr",
            task_policy={
                "pr_adoption": {
                    "repo_slug": "base/aira-web",
                    "pr_number": 42,
                    "pr_url": "https://github.com/base/aira-web/pull/42",
                    "head_ref": "fix/fork-review",
                    "head_repo_slug": "contributor/aira-web",
                    "head_repo_url": "https://github.com/contributor/aira-web.git",
                    "base_ref": "development",
                    "head_sha": "h" * 40,
                    "base_sha": "b" * 40,
                }
            },
        )

        thread = {
            "id": "T_fork",
            "isResolved": False,
            "isOutdated": False,
            "path": "src/x.ts",
            "line": 10,
            "comments": {"nodes": [{"bodyText": "rename", "author": {"login": "cr"}}]},
        }
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload(threads=[thread]))  # PR state
        adapter.queue(stdout="fixed in commit abc")
        cmd.queue_result(returncode=0, stdout=_pr_payload())  # fetch in fix_cycle
        cmd.queue_result(returncode=0, stderr="")  # git push
        cmd.queue_result(returncode=0, stdout="newhead123\n")  # git rev-parse HEAD
        cmd.queue_result(
            returncode=0,
            stdout=json.dumps(
                {"data": {"resolveReviewThread": {"thread": {"id": "T_fork", "isResolved": True}}}}
            ),
        )
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload())  # clean
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

        push_calls = _git_calls(cmd, "push")
        assert len(push_calls) == 1
        assert "git@github.com:contributor/aira-web.git" in push_calls[0].args
        assert "HEAD:refs/heads/fix/fork-review" in push_calls[0].args
        assert "origin" not in push_calls[0].args[push_calls[0].args.index("push") + 1 :]

    @pytest.mark.unit
    async def test_false_positive_verdict_does_not_trigger_resolve_mutation(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_workspace(factory)

        thread = {
            "id": "T_fp",
            "isResolved": False,
            "isOutdated": False,
            "path": "a",
            "line": 1,
            "comments": {"nodes": [{"bodyText": "wrong", "author": {"login": "cr"}}]},
        }
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload(threads=[thread]))
        adapter.queue(stdout="FALSE POSITIVE: the existing code is correct")
        cmd.queue_result(returncode=0, stdout=_pr_payload())  # settle refetch
        cmd.queue_result(returncode=0, stderr="Everything up-to-date")  # push noop
        # Even on "false_positive" verdict, the runner resolves the thread
        # on GitHub (the reviewer's concern has been addressed with a reply
        # — the thread shouldn't remain unresolved forever).
        cmd.queue_result(  # resolve_thread
            returncode=0,
            stdout=json.dumps(
                {"data": {"resolveReviewThread": {"thread": {"id": "T_fp", "isResolved": True}}}}
            ),
        )
        # Outer loop iter 2: clean, merge.
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
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.monitor_threads_addressed["T_fp"] == "false_positive"


class TestFixCyclePasses:
    @pytest.mark.unit
    async def test_new_comments_during_fix_trigger_second_pass(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """Fix cycle: first pass addresses T1; while we were fixing, T2
        arrived; second pass addresses T2; settle window clears; push ONCE."""
        ws_id = await _seed_monitoring_workspace(factory)

        t1 = {
            "id": "T1",
            "isResolved": False,
            "isOutdated": False,
            "path": "a",
            "line": 1,
            "comments": {"nodes": [{"bodyText": "fix T1", "author": {"login": "cr"}}]},
        }
        t2 = {
            "id": "T2",
            "isResolved": False,
            "isOutdated": False,
            "path": "b",
            "line": 2,
            "comments": {"nodes": [{"bodyText": "fix T2", "author": {"login": "cr"}}]},
        }
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload(threads=[t1]))  # initial
        adapter.queue(stdout="fixed T1")  # fix T1
        cmd.queue_result(  # settle refetch #1: now T1 still + new T2
            returncode=0,
            stdout=_pr_payload(threads=[t1, t2]),
        )
        adapter.queue(stdout="fixed T2")  # fix T2
        cmd.queue_result(returncode=0, stdout=_pr_payload())  # settle refetch #2: quiet
        cmd.queue_result(returncode=0)  # git push
        cmd.queue_result(returncode=0, stdout="head2\n")  # rev-parse HEAD
        cmd.queue_result(returncode=0, stdout=json.dumps({"data": {}}))  # resolve T1
        cmd.queue_result(returncode=0, stdout=json.dumps({"data": {}}))  # resolve T2
        # Outer iter 2: merge.
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
        # CLI was called twice (T1, T2) — one AddressComments action,
        # two passes inside fix_cycle.
        assert len(adapter.calls) == 2
        push_calls = _git_calls(cmd, "push")
        # Exactly ONE push for the whole burst.
        assert len(push_calls) == 1


class TestCiFailure:
    @pytest.mark.unit
    async def test_failure_triggers_cli_fix_and_push(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_workspace(factory)
        # Outer iter 1: CI FAILURE, no comments, runner calls fetch_failing_check_logs.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload(check_state="FAILURE"))  # PR state
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
        cmd.queue_result(returncode=0, stdout="log tail here")  # gh run view --log-failed
        adapter.queue(stdout="fix(ci): lint — ...")  # CLI fix
        cmd.queue_result(returncode=0)  # git push after fix
        # Outer iter 2: green → merge.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload())
        cmd.queue_result(returncode=0, stdout=json.dumps([]))  # no failures (extra safety)
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
            assert ws.status == WorkspaceStatus.completed.value
        assert any("fix" in p.lower() and "ci" in p.lower() for p in adapter.calls)


class TestSyncBase:
    @pytest.mark.unit
    async def test_base_behind_triggers_git_merge_and_push(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_workspace(factory)
        # Outer iter 1: base is behind by 2.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="2\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload())  # PR state (mergeable OK)
        cmd.queue_result(returncode=0)  # git merge --abort (no-op)
        cmd.queue_result(returncode=0)  # git fetch
        cmd.queue_result(returncode=0)  # git merge (clean)
        cmd.queue_result(returncode=0)  # git push
        # Outer iter 2: base synced, merge.
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
        # git merge was invoked with origin/<base>. Skip the defensive
        # ``git merge --abort`` that now runs first.
        merge_call = next(
            c
            for c in cmd.calls
            if c.args[:1] == ["git"] and "merge" in c.args and "--abort" not in c.args
        )
        assert "origin/development" in merge_call.args

    @pytest.mark.unit
    async def test_base_behind_conflict_hands_off_to_cli(
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
        cmd.queue_result(returncode=1, stderr="CONFLICT (content): src/x")  # merge fails
        cmd.queue_result(returncode=0, stdout="UU src/x\nUU src/y\n")  # git status --porcelain
        adapter.queue(stdout="fixed conflicts")
        cmd.queue_result(returncode=0)  # push
        # Outer iter 2: clean merge.
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
        # CLI was invoked with a conflict-resolve prompt.
        assert any("CONFLICT" in p or "conflicts" in p for p in adapter.calls)

    @pytest.mark.unit
    async def test_sync_base_resolves_stale_target_advanced_reason(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """SyncBase must resolve any active STALE_TARGET_ADVANCED row on
        the candidate after a successful push.

        Regression scenario observed in T143 (aira-agent PR #480): two
        ``sync_base`` operations succeeded (monitor saw ``base_behind=0``)
        but the staleness row from the initial detection stayed
        ``status=active`` with ``blocks_merge=true``, gating every
        subsequent merge attempt. Without this test, the runner could
        regress to that wedge silently — symptoms surface only after a
        second PR merges to the target branch on a parallel workspace.
        """
        original_base = "a" * 40
        new_base = "b" * 40
        ws_id = await _seed_monitoring_workspace(factory)
        # Seed an open merge candidate + an active STALE_TARGET_ADVANCED
        # reason so the post-SyncBase resolve path has something to clear.
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            ws.base_commit = original_base
            attempt = await TaskAttemptRepository(s).get_by_workspace_id(ws_id)
            assert attempt is not None
            task = await TaskRepository(s).get(attempt.task_id)
            assert task is not None
            candidate = await MergeCandidateRepository(s).create_or_update_open_for_attempt(
                task=task,
                attempt=attempt,
                workspace=ws,
                head_sha="h" * 40,
                base_sha=original_base,
            )
            await StaleReasonRepository(s).replace_active_findings(
                workspace_id=ws_id,
                candidate_id=candidate.id,
                attempt_id=attempt.id,
                task_id=task.id,
                findings=[
                    StaleReasonCreate(
                        reason_code="STALE_TARGET_ADVANCED",
                        trigger_type="target_advanced",
                        trigger_ref=new_base,
                        explanation=(
                            "Target branch 'development' advanced 2 commit(s) past validation base."
                        ),
                    )
                ],
            )
            candidate.stale = True
            candidate.stale_reason = "stale"
            await s.commit()
            candidate_id = candidate.id

        # Outer iter 1: rev-list says base-behind=2 → SyncBase action.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="2\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload())
        cmd.queue_result(returncode=0)  # git merge --abort
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0)  # git merge --no-edit
        cmd.queue_result(returncode=0)  # git push (sync_base)
        cmd.queue_result(returncode=0, stdout=f"{new_base}\n")  # rev-parse origin/<base>
        # Outer iter 2: clean → merge.
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

        async with factory() as s:
            reasons = await StaleReasonRepository(s).list_for_workspace(ws_id)
            assert reasons, "seeded reason should still exist as a historical row"
            target_advanced = [r for r in reasons if r.reason_code == "STALE_TARGET_ADVANCED"]
            assert target_advanced, "STALE_TARGET_ADVANCED row should remain"
            assert all(r.resolved_at is not None for r in target_advanced), (
                "SyncBase success must mark STALE_TARGET_ADVANCED rows resolved; "
                "otherwise the merge gate stays blocked even though monitor sees "
                "base_behind=0 (T143 wedge regression)."
            )
            candidate = await MergeCandidateRepository(s).get_by_attempt_id(
                (await TaskAttemptRepository(s).get_by_workspace_id(ws_id)).id
            )
            assert candidate is not None
            assert candidate.id == candidate_id
            assert candidate.stale is False, "candidate.stale should flip to False"
            assert candidate.base_sha == new_base, (
                "candidate.base_sha should advance to the SHA we just merged in"
            )

    @pytest.mark.unit
    async def test_sync_base_preserves_docs_task_scope_violation(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """SyncBase resolves *target-derived* staleness but must leave an
        intrinsic ``docs_task_scope_violation`` active.

        Regression for PRRT_kwDOSJAM6s6EA4b1: the post-SyncBase refresh used
        to resolve *every* active stale reason and clear ``candidate.stale``.
        A rebase brings ``base_sha`` up to the target, which legitimately
        clears ``STALE_TARGET_ADVANCED`` — but it does NOT remediate a docs
        task that claims non-docs paths. Clearing that reason here would let
        ``_merge_gate_for_workspace`` merge the PR without the scope decision /
        current-head validation it normally requires.
        """
        original_base = "a" * 40
        new_base = "b" * 40
        ws_id = await _seed_monitoring_workspace(factory)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            ws.base_commit = original_base
            # Docs task that claims a non-docs path → docs_task_scope_violation.
            ws.task_class = TaskClass.docs_task.value
            ws.owned_paths = ["docs/guide.md", "src/awf/not_docs.py"]
            attempt = await TaskAttemptRepository(s).get_by_workspace_id(ws_id)
            assert attempt is not None
            task = await TaskRepository(s).get(attempt.task_id)
            assert task is not None
            candidate = await MergeCandidateRepository(s).create_or_update_open_for_attempt(
                task=task,
                attempt=attempt,
                workspace=ws,
                head_sha="h" * 40,
                base_sha=original_base,
            )
            # Seed one resolvable (target-derived) reason and one intrinsic one.
            await StaleReasonRepository(s).replace_active_findings(
                workspace_id=ws_id,
                candidate_id=candidate.id,
                attempt_id=attempt.id,
                task_id=task.id,
                findings=[
                    StaleReasonCreate(
                        reason_code="STALE_TARGET_ADVANCED",
                        trigger_type="target_advanced",
                        trigger_ref=new_base,
                        explanation=(
                            "Target branch 'development' advanced 2 commit(s) past validation base."
                        ),
                    ),
                    StaleReasonCreate(
                        reason_code="docs_task_scope_violation",
                        trigger_type="task_scope",
                        trigger_ref="docs_task",
                        explanation="Changed files are outside the docs task scope.",
                    ),
                ],
            )
            candidate.stale = True
            candidate.stale_reason = "docs_task_scope_violation"
            await s.commit()
            candidate_id = candidate.id

        cmd.queue_result(returncode=0, stdout=f"{new_base}\n")  # rev-parse origin/<base>

        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        await runner._refresh_staleness_after_sync_base(
            workspace_id=ws_id,
            base_branch="development",
        )

        async with factory() as s:
            reasons = await StaleReasonRepository(s).list_for_candidate(candidate_id)
            target_rows = [r for r in reasons if r.reason_code == "STALE_TARGET_ADVANCED"]
            docs_rows = [r for r in reasons if r.reason_code == "docs_task_scope_violation"]
            assert target_rows, "expected a STALE_TARGET_ADVANCED row"
            assert all(r.resolved_at is not None for r in target_rows), (
                "target-derived staleness should resolve once base_sha catches up"
            )
            assert docs_rows, "expected a docs_task_scope_violation row"
            assert all(r.status == "active" and r.resolved_at is None for r in docs_rows), (
                "docs_task_scope_violation is intrinsic to the task scope; a "
                "SyncBase/rebase does not remediate it and must not resolve the row."
            )
            candidate = await MergeCandidateRepository(s).get_by_attempt_id(
                (await TaskAttemptRepository(s).get_by_workspace_id(ws_id)).id
            )
            assert candidate is not None
            assert candidate.base_sha == new_base, "base_sha should still advance"
            assert candidate.stale is True, (
                "candidate must stay stale while the docs scope violation is unresolved"
            )
            assert candidate.stale_reason == "docs_task_scope_violation"

    @pytest.mark.unit
    async def test_sync_base_advances_base_sha_without_resolvable_reasons(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """SyncBase must advance ``candidate.base_sha`` even when there is no
        target-derived staleness to resolve.

        Regression for PR #275 review: the post-SyncBase refresh used to early
        return before touching ``base_sha`` whenever the candidate carried only
        intrinsic findings (or none). The staleness service measures target
        advancement as ``<base_sha>..origin/<base>``, so leaving ``base_sha`` at
        the old commit makes the next refresh re-derive ``STALE_TARGET_ADVANCED``
        against an already-merged base and re-block the merge gate.
        """
        original_base = "a" * 40
        new_base = "b" * 40
        ws_id = await _seed_monitoring_workspace(factory)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            ws.base_commit = original_base
            # Docs task that claims a non-docs path → docs_task_scope_violation,
            # an intrinsic reason that is NOT in
            # ``_SYNC_BASE_RESOLVABLE_STALE_REASONS``, so ``resolvable`` is empty.
            ws.task_class = TaskClass.docs_task.value
            ws.owned_paths = ["docs/guide.md", "src/awf/not_docs.py"]
            attempt = await TaskAttemptRepository(s).get_by_workspace_id(ws_id)
            assert attempt is not None
            task = await TaskRepository(s).get(attempt.task_id)
            assert task is not None
            candidate = await MergeCandidateRepository(s).create_or_update_open_for_attempt(
                task=task,
                attempt=attempt,
                workspace=ws,
                head_sha="h" * 40,
                base_sha=original_base,
            )
            await StaleReasonRepository(s).replace_active_findings(
                workspace_id=ws_id,
                candidate_id=candidate.id,
                attempt_id=attempt.id,
                task_id=task.id,
                findings=[
                    StaleReasonCreate(
                        reason_code="docs_task_scope_violation",
                        trigger_type="task_scope",
                        trigger_ref="docs_task",
                        explanation="Changed files are outside the docs task scope.",
                    ),
                ],
            )
            candidate.stale = True
            candidate.stale_reason = "docs_task_scope_violation"
            await s.commit()
            candidate_id = candidate.id

        cmd.queue_result(returncode=0, stdout=f"{new_base}\n")  # rev-parse origin/<base>

        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
        )
        await runner._refresh_staleness_after_sync_base(
            workspace_id=ws_id,
            base_branch="development",
        )

        async with factory() as s:
            candidate = await MergeCandidateRepository(s).get_by_attempt_id(
                (await TaskAttemptRepository(s).get_by_workspace_id(ws_id)).id
            )
            assert candidate is not None
            assert candidate.id == candidate_id
            assert candidate.base_sha == new_base, (
                "base_sha must advance to the merged SHA even when there is no "
                "target-derived staleness to resolve, or the next refresh will "
                "re-derive STALE_TARGET_ADVANCED against an already-merged base."
            )
            # The intrinsic reason and stale flag are left untouched: a rebase
            # does not remediate the docs scope violation.
            reasons = await StaleReasonRepository(s).list_for_candidate(candidate_id)
            scope_rows = [r for r in reasons if r.reason_code == "docs_task_scope_violation"]
            assert scope_rows, "intrinsic docs_task_scope_violation row should remain"
            assert any(r.status == "active" for r in scope_rows)
            assert all(r.resolved_at is None for r in scope_rows)
            assert candidate.stale is True
            assert candidate.stale_reason == "docs_task_scope_violation"
