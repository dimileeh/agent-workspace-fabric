"""Integration tests for PullRequestMonitorRunner.

'Integration' here means: real SQLAlchemy (in-memory SQLite), the real
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
from awf.db.base import Base
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_engine, make_session_factory
from awf.runtime.pr_monitor import MonitorConfig
from awf.runtime.pr_monitor_runner import (
    MonitorRunnerConfig,
    PullRequestMonitorRunner,
    _parse_verdict,
)

# ── Fakes ──────────────────────────────────────────────────────────────────


@dataclass
class FakeAdapter(AgentAdapter):
    """Canned-response CLI. Each ``run`` call pops one verdict stdout."""

    runtime = AgentRuntime.claude_code
    _queued: list[AgentRunResult] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    def __init__(self) -> None:  # type: ignore[override]
        super().__init__(runner=None)  # type: ignore[arg-type]
        self._queued = []
        self.calls = []

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
        self, *, compose_project: str, compose_file: Path, prompt: str, model: str | None = None
    ) -> AgentRunResult:
        self.calls.append(prompt)
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
    mergeable: str = "MERGEABLE",
    merge_state_status: str = "CLEAN",
    check_state: str = "SUCCESS",
    threads: list[dict] | None = None,
    reviews: list[dict] | None = None,
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
                        "baseRef": {"name": "development", "target": {"oid": "base0"}},
                        "commits": {
                            "nodes": [{"commit": {"statusCheckRollup": {"state": check_state}}}]
                        },
                        "reviewThreads": {"nodes": threads or []},
                        "reviews": {"nodes": reviews or []},
                    }
                }
            }
        }
    )


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'awf.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


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
    pr_number: int = 42,
) -> str:
    """Insert a workspace already in ``monitoring_pr`` state."""
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:dimileeh/aira-web.git",
            branch_base="development",
            task_title="monitor test",
            task_prompt="x",
            agent=agent,
            test_commands=["pytest -q"],
            requires_database=False,
        )
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
        ws.branch_name = f"awf/{ws.id}"
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.pr_url = f"https://github.com/dimileeh/aira-web/pull/{pr_number}"
        ws.pr_number = pr_number
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
    iter_cap: int = 10,
) -> PullRequestMonitorRunner:
    return PullRequestMonitorRunner(
        session_factory=factory,
        runner=cmd,
        adapter=adapter,
        gh=GitHubClient(cmd),
        monitor_config=MonitorConfig(
            iter_cap=iter_cap,
            auto_merge=auto_merge,
            poll_interval_seconds=60,
            settle_interval_seconds=30,
        ),
        runner_config=MonitorRunnerConfig(max_outer_iterations=20, max_fix_cycle_passes=3),
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
        """Branch protection blocks the merge → runner posts ready-to-merge
        and completes (no failure)."""
        ws_id = await _seed_monitoring_workspace(factory)
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload())
        cmd.queue_result(returncode=1, stderr="branch protection rule blocks merge")
        cmd.queue_result(returncode=0)  # gh pr comment
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
            assert ws.pr_merge_sha is None
        # gh pr comment was invoked with the ready-to-merge body.
        comment_args = next(c.args for c in cmd.calls if c.args[:3] == ["gh", "pr", "comment"])
        assert any("Ready" in a or "ready" in a.lower() for a in comment_args)


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
    async def test_defer_verdict_does_not_resolve_thread(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
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
        # No resolve_thread call queued — test checks we didn't hit it.
        # After a second outer iteration the thread is "addressed" in state
        # so decide() returns Merge; queue the merge.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload(threads=[thread]))  # still there
        cmd.queue_result(returncode=0)  # merge (thread addressed in state, gate passes)
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
        # Specifically assert no resolveReviewThread mutation fired.
        for c in cmd.calls:
            query_args = [a for a in c.args if a.startswith("query=")]
            assert not any("resolveReviewThread" in q for q in query_args), (
                "defer verdict must NOT resolve the thread"
            )


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
    pile up, eventually hitting iter_cap. Recovery: fetch the feature
    branch + reset local hard to remote (GitHub is truth for pushed
    state), then next outer-loop iteration works on a fresh aligned
    worktree."""

    @pytest.mark.unit
    async def test_push_rejection_triggers_fetch_and_reset_hard(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_workspace(factory)
        # Outer iter 1: DIRTY state forces SyncBase; merge creates a
        # local commit; push gets rejected (non-fast-forward); recovery
        # fetch + reset --hard kick in.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="1\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload(merge_state_status="DIRTY"))
        cmd.queue_result(returncode=0)  # git merge --abort (defense)
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0)  # git merge --no-edit (clean)
        # Push rejected.
        cmd.queue_result(
            returncode=1,
            stderr=(
                "To github.com:dimileeh/aira-agent.git\n"
                " ! [rejected]        awf/test -> awf/test (fetch first)\n"
                "error: failed to push some refs ..."
            ),
        )
        # Recovery sequence: rev-parse branch, fetch branch, reset --hard.
        cmd.queue_result(returncode=0, stdout="awf/test-branch\n")  # rev-parse --abbrev-ref
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
            iter_cap=1,
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


class TestAbortOnIterCap:
    @pytest.mark.unit
    async def test_iter_cap_terminates_with_failed(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        ws_id = await _seed_monitoring_workspace(factory)
        async with factory() as s:
            ws = await WorkspaceRepository(s).get(ws_id)
            assert ws is not None
            ws.monitor_iter_count = 5  # at cap already
            await s.commit()
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
        cmd.queue_result(returncode=0, stdout=_pr_payload())  # no reason to be over cap;
        #                                                       but we set iter_count pre-run.
        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            iter_cap=5,
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
            assert "iter_cap_reached" in (ws.failure_message or "")


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
            ws.monitor_threads_addressed = {"T1": "fix_committed"}
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
    async def test_cli_crash_during_address_thread_defers(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """CLI process dies mid-address — monitor logs + records 'defer'
        rather than aborting the whole workspace."""
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
        # Iter 2: thread addressed-as-defer in state → Merge gate.
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
            assert ws.monitor_threads_addressed.get("T_crash") == "defer"

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
        # Iter 2: abort on iter_cap with small cap for speed.
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=0, stdout="0\n")
        cmd.queue_result(returncode=0, stdout=_pr_payload())
        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            iter_cap=1,
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
        # Iter 2: iter_cap reached.
        runner = _make_runner(
            factory=factory,
            cmd=cmd,
            adapter=adapter,
            sleep_fn=sleep_fn,
            worktrees_root=tmp_path / "worktrees",
            iter_cap=1,
        )
        await runner.run(
            workspace_id=ws_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )
        assert any(c.args[:2] == ["git", "-C"] and "push" in c.args for c in cmd.calls)


class TestBaseBehindEdges:
    @pytest.mark.unit
    async def test_rev_list_error_treats_base_as_up_to_date(
        self,
        factory: async_sessionmaker[AsyncSession],
        cmd: FakeCommandRunner,
        adapter: FakeAdapter,
        sleep_fn: RecordedSleep,
        tmp_path: Path,
    ) -> None:
        """Failed rev-list (e.g. origin/<base> not yet fetched) should not
        trip the monitor — we just get base_behind=0 and carry on."""
        ws_id = await _seed_monitoring_workspace(factory)
        cmd.queue_result(returncode=0)  # git fetch origin <base>
        cmd.queue_result(returncode=1, stderr="unknown revision")  # base-behind fails
        cmd.queue_result(returncode=0, stdout=_pr_payload())  # PR green
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
            assert ws.status == WorkspaceStatus.completed.value

    @pytest.mark.unit
    async def test_rev_list_garbage_output_treats_base_as_up_to_date(
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
            assert ws.status == WorkspaceStatus.completed.value


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
