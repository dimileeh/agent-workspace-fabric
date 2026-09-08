"""Shared fixtures and fakes for the PullRequestMonitorRunner integration parts.

Every ``test_pr_monitor_runner_part_*`` module carried a byte-identical copy of
this block, which pushed each part close to the first-party line budget as new
regressions landed. It lives here once and is loaded as a pytest plugin by each
part; ``_make_runner`` stays in the parts because it has diverged between them.
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
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import (
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine


@dataclass
class FakeAdapter(AgentAdapter):
    """Canned-response CLI. Each ``run`` call pops one verdict stdout."""

    runtime = AgentRuntime.claude_code
    _queued: list[AgentRunResult] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    workspace_ids: list[str | None] = field(default_factory=list)
    worktree_paths: list[Path | None] = field(default_factory=list)

    def __init__(self) -> None:  # type: ignore[override]
        super().__init__(runner=None)  # type: ignore[arg-type]
        self._queued = []
        self.calls = []
        self.workspace_ids = []
        self.worktree_paths = []

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
        # The monitor's local repair path passes the workspace worktree so the
        # idle watchdog can watch it for activity (#932).
        worktree_path: Path | None = None,
    ) -> AgentRunResult:
        self.calls.append(prompt)
        self.workspace_ids.append(workspace_id)
        self.worktree_paths.append(worktree_path)
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
    auto_merge: bool = True,
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
        ws.auto_merge = auto_merge
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
