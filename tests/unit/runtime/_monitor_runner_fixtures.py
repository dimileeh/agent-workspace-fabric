"""Shared helpers for the new PR-monitor runner unit tests.

The action-logging, bot-defer, and defer-signal-artifact tests all need
the same setup: a real in-memory SQLite, a ``FakeCommandRunner``, a
``FakeAdapter`` returning canned verdicts, and a workspace row already in
``monitoring_pr``. Reuse the same shapes as
``tests/integration/runtime/test_pr_monitor_runner.py`` (which is where
the baseline end-to-end flows live) but keep the helpers local to the
new suites so the integration file doesn't become a cross-test import
surface.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentAdapter, AgentRunError, AgentRunResult
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.github_client import GitHubClient
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.runtime.logs import LogStore
from awf.runtime.pr_monitor import MonitorConfig
from awf.runtime.pr_monitor_runner import (
    MonitorRunnerConfig,
    PullRequestMonitorRunner,
)


@dataclass
class FakeAdapter(AgentAdapter):
    runtime = AgentRuntime.claude_code
    _queued: list[AgentRunResult] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    workspace_ids: list[str | None] = field(default_factory=list)

    def __init__(self) -> None:  # type: ignore[override]
        super().__init__(runner=None)  # type: ignore[arg-type]
        self._queued = []
        self.calls = []
        self.workspace_ids = []

    @property
    def name(self) -> AgentRuntime:  # type: ignore[override]
        return AgentRuntime.claude_code

    def _cli_args(self, *, prompt: str, model: str | None) -> list[str]:  # type: ignore[override]
        return []

    def queue(self, *, stdout: str = "", returncode: int = 0) -> None:
        self._queued.append(AgentRunResult(returncode=returncode, stdout=stdout, stderr=""))

    async def run(  # type: ignore[override]
        self,
        *,
        compose_project: str,
        compose_file: Path,
        prompt: str,
        model: str | None = None,
        workspace_id: str | None = None,
    ) -> AgentRunResult:
        self.calls.append(prompt)
        self.workspace_ids.append(workspace_id)
        if not self._queued:
            raise AssertionError(
                "FakeAdapter.run called with empty queue; queue() a result "
                "in the test before this dispatch to avoid masking setup bugs"
            )
        r = self._queued.pop(0)
        if r.returncode != 0:
            raise AgentRunError(
                agent=AgentRuntime.claude_code,
                result=CommandResult(returncode=r.returncode, stdout=r.stdout, stderr=r.stderr),
            )
        return r


class RecordedSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def pr_payload(
    *,
    closed: bool = False,
    merged: bool = False,
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
                        "headRefOid": "abc1234567890def",
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
                        "comments": {"nodes": comments or []},
                    }
                }
            }
        }
    )


def thread_node(
    *,
    tid: str,
    author: str,
    path: str = "src/foo.py",
    line: int = 42,
    body: str = "tiny nit",
) -> dict:
    return {
        "id": tid,
        "isResolved": False,
        "isOutdated": False,
        "path": path,
        "line": line,
        "comments": {"nodes": [{"bodyText": body, "author": {"login": author}}]},
    }


def review_node(*, cid: int, author: str, body: str = "see below") -> dict:
    return {
        "databaseId": cid,
        "body": body,
        "state": "COMMENTED",
        "author": {"login": author},
    }


def issue_comment_node(
    *,
    cid: int,
    author: str,
    body: str,
    minimized: bool = False,
) -> dict:
    return {
        "databaseId": cid,
        "body": body,
        "isMinimized": minimized,
        "author": {"login": author},
    }


async def seed_monitoring_workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    pr_number: int = 42,
) -> str:
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:dimileeh/aira-web.git",
            branch_base="development",
            task_title="monitor test",
            task_prompt="x",
            agent="claude_code",
            test_commands=["pytest -q"],
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
        ws.branch_name = f"awf/{ws.id}"
        ws.remote_push_branch = ws.branch_name
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.pr_url = f"https://github.com/dimileeh/aira-web/pull/{pr_number}"
        ws.pr_number = pr_number
        await s.commit()
        return ws.id


def make_runner(
    *,
    factory: async_sessionmaker[AsyncSession],
    cmd: FakeCommandRunner,
    adapter: FakeAdapter,
    sleep_fn: RecordedSleep,
    worktrees_root: Path,
    auto_merge: bool = True,
    pre_merge_settle_seconds: float = 0,
    initial_review_grace_period_seconds: float = 0,
    artifacts_root: Path | None = None,
    log_store: LogStore | None = None,
) -> PullRequestMonitorRunner:
    kwargs: dict = {
        "session_factory": factory,
        "runner": cmd,
        "adapter": adapter,
        "gh": GitHubClient(cmd),
        "monitor_config": MonitorConfig(
            auto_merge=auto_merge,
            poll_interval_seconds=60,
            settle_interval_seconds=30,
            initial_review_grace_period_seconds=initial_review_grace_period_seconds,
            pre_merge_settle_seconds=pre_merge_settle_seconds,
        ),
        "runner_config": MonitorRunnerConfig(max_outer_iterations=20, max_fix_cycle_passes=3),
        "sleep": sleep_fn,
        "worktrees_root": worktrees_root,
        "log_store": log_store,
    }
    if artifacts_root is not None:
        kwargs["artifacts_root"] = artifacts_root
    return PullRequestMonitorRunner(**kwargs)
