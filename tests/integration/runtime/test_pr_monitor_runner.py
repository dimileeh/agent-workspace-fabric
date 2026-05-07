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
    MonitorState,
    ReviewThread,
    _mark_review_thread_addressed,
)
from awf.runtime.pr_monitor_runner import (
    MonitorRunnerConfig,
    PullRequestMonitorRunner,
    _initial_review_grace_started_key,
    _parse_verdict,
)
from tests.postgres import postgres_test_engine

# ── Fakes ──────────────────────────────────────────────────────────────────


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

    def _cli_args(self, *, prompt: str, model: str | None) -> list[str]:  # type: ignore[override]
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


# ── GraphQL payload helpers — mirror test_github_client.py ─────────────────


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


# ── Fixtures ───────────────────────────────────────────────────────────────


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


# ── Tests ──────────────────────────────────────────────────────────────────


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

        push_calls = [c for c in cmd.calls if c.args[:2] == ["git", "-C"] and "push" in c.args]
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

    @pytest.mark.unit
    async def test_defer_verdict_does_not_resolve_thread_and_blocks_merge(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """Two contracts around ``DEFER``:

        1. The thread is NOT resolved on GitHub — "defer" means the
           agent couldn't decide, a human has to. Resolving would
           sweep the question under the rug.
        2. The merge gate MUST NOT fire while a deferred thread is
           still unresolved on GitHub. Previously (the bug CodeRabbit
           flagged on PR #2) the filter in ``decide()`` treated
           deferred threads as "addressed" at step 2 and let step 8
           merge silently. Now a dedicated gate at step 7.5 returns
           NotifyHuman instead — the maintainer sees the "ready to
           review" comment AND the deferred thread still standing.
        """
        ws_id = await _seed_monitoring_workspace(factory)
        thread = {
            "id": "T_defer",
            "isResolved": False,
            "isOutdated": False,
            "path": "a",
            "line": 1,
            "comments": {"nodes": [{"bodyText": "?", "author": {"login": "cr"}}]},
        }
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload(threads=[thread]))
        adapter.queue(stdout="DEFER: need design input from maintainer")
        cmd.queue_result(returncode=0, stdout=_pr_payload())  # settle
        cmd.queue_result(returncode=0, stderr="Everything up-to-date")  # push
        # No resolve_thread call queued — contract #1.
        # Second outer iteration: thread still unresolved on GitHub.
        # decide() should now hit the deferred-still-open gate and
        # return NotifyHuman — queue the ``gh pr comment`` call, NOT a
        # merge.
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
                "defer verdict must NOT resolve the thread"
            )
        # Contract #2: no merge call fired. The merge would look like
        # ``gh pr merge ...``; the NotifyHuman path is ``gh pr comment``.
        assert not any(c.args[:3] == ["gh", "pr", "merge"] for c in cmd.calls), (
            "deferred-still-open must block merge — maintainer-driven only"
        )
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.completed.value


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
        push_calls = [c for c in cmd.calls if c.args[:2] == ["git", "-C"] and "push" in c.args]
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
            if c.args[:2] == ["git", "-C"] and "merge" in c.args and "--abort" not in c.args
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
        push_calls = [c for c in cmd.calls if c.args[:2] == ["git", "-C"] and "push" in c.args]
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
            if c.args[:2] == ["git", "-C"]
            and "fetch" in c.args
            and any(a == "awf/test-branch" for a in c.args)
        ]
        assert fetch_branch_calls, "must fetch the feature branch for resync"
        reset_calls = [
            c
            for c in cmd.calls
            if c.args[:2] == ["git", "-C"]
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
            if c.args[:2] == ["git", "-C"] and "reset" in c.args and "--hard" in c.args
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
            if c.args[:2] == ["git", "-C"] and "merge" in c.args and "--abort" in c.args
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


# ── verdict parser tests ───────────────────────────────────────────────────


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
        assert any(c.args[:2] == ["git", "-C"] and "push" in c.args for c in cmd.calls)

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
        assert any(c.args[:2] == ["git", "-C"] and "push" in c.args for c in cmd.calls)


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
        assert time.monotonic() - started_monotonic == pytest.approx(600, abs=2)

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
        assert time.monotonic() - started_monotonic == pytest.approx(600, abs=2)

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
        assert time.time() - started_wall == pytest.approx(300, abs=2)


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
        push_calls = [c for c in cmd.calls if c.args[:2] == ["git", "-C"] and "push" in c.args]
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
        push_calls = [c for c in cmd.calls if c.args[:2] == ["git", "-C"] and "push" in c.args]
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
        push_calls = [c for c in cmd.calls if c.args[:2] == ["git", "-C"] and "push" in c.args]
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
        push_calls = [c for c in cmd.calls if c.args[:2] == ["git", "-C"] and "push" in c.args]
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
        push_calls = [c for c in cmd.calls if c.args[:2] == ["git", "-C"] and "push" in c.args]
        assert push_calls
        for pc in push_calls:
            assert "HEAD:refs/heads/awf/legacy-row" in pc.args


class TestParseVerdict:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout, expected",
        [
            ("", "defer"),
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
