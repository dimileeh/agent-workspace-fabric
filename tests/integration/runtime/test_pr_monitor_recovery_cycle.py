"""End-to-end integration: monitor recovery cycle preserves plan files.

The monitor decides a stale workspace needs validation recovery. After
grace elapses, RECOVERY_DISPATCH transitions the workspace through
ready → running → validating → pushing → monitoring_pr without ever
re-running planning, the feature task prompt, or the agent. The plan
file content survives the cycle byte-identical.
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
from awf.db.enums import (
    AgentRuntime,
    OperationStatus,
    OperationType,
    TaskClass,
    WorkspaceStatus,
)
from awf.db.repositories import (
    OperationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import MonitorConfig
from awf.runtime.pr_monitor_runner import (
    MonitorRunnerConfig,
    PullRequestMonitorRunner,
)
from awf.runtime.pr_monitor_runner.helpers import _non_check_reviewer_settle_started_key
from tests.postgres import postgres_test_engine


@dataclass
class _FakeAdapter(AgentAdapter):
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
            return AgentRunResult(returncode=0, stdout="ok", stderr="")
        r = self._queued.pop(0)
        if r.returncode != 0:
            raise AgentRunError(
                agent=AgentRuntime.claude_code,
                result=CommandResult(returncode=r.returncode, stdout=r.stdout, stderr=r.stderr),
            )
        return r


class _RecordedSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _pr_payload(*, head_sha: str = "abc1234567890def") -> str:
    return json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": 42,
                        "headRefOid": head_sha,
                        "mergeable": "MERGEABLE",
                        "mergeStateStatus": "CLEAN",
                        "isDraft": False,
                        "closed": False,
                        "merged": False,
                        "baseRef": {"name": "development", "target": {"oid": "base0"}},
                        "commits": {
                            "nodes": [
                                {
                                    "commit": {
                                        "statusCheckRollup": {
                                            "state": "SUCCESS",
                                            "contexts": {"nodes": []},
                                        }
                                    }
                                }
                            ]
                        },
                        "reviewThreads": {"nodes": []},
                        "reviews": {"nodes": []},
                        "comments": {"nodes": []},
                    }
                }
            }
        }
    )


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


async def _seed_monitoring_workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    pr_number: int = 42,
) -> str:
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:dimileeh/aira-web.git",
            branch_base="development",
            task_title="recovery cycle",
            task_prompt="implement-thing",
            task_class=TaskClass.refactor_task.value,
            agent="claude_code",
            test_commands=["pytest -q"],
            requires_database=False,
        )
        await TaskAttemptRepository(s).create_for_workspace(
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
        ws.branch_name = f"awf/{ws.id}"
        ws.remote_push_branch = ws.branch_name
        ws.base_commit = "a" * 40
        ws.compose_project_name = f"awf_{ws.id}"
        ws.pr_url = f"https://github.com/dimileeh/aira-web/pull/{pr_number}"
        ws.pr_number = pr_number
        ws.auto_merge = True
        # Mark the initial-review grace window as already elapsed so the
        # monitor proceeds straight to recovery dispatch in tests that are
        # specifically about recovery handoff rather than review quieting.
        ws.monitor_threads_addressed = {
            "__awf_initial_review_grace_done__:42": "elapsed",
            "__awf_non_check_reviewer_settle_done__:42:abc1234567890def": "elapsed",
        }
        for target in (
            WorkspaceStatus.provisioning,
            WorkspaceStatus.ready,
            WorkspaceStatus.running,
            WorkspaceStatus.validating,
            WorkspaceStatus.pushing,
            WorkspaceStatus.monitoring_pr,
        ):
            await repo.transition(ws, to=target, reason_code="X")
        await s.commit()
        return ws.id


@pytest.mark.unit
async def test_recovery_round_trip_does_not_rewrite_plan_files(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Drive the monitor through one cycle past RECOVERY_DISPATCH:
    `monitoring_pr` (stale, grace elapsed) → recovery operation row.
    Verify the dispatch payload, that the runner did not invoke the
    feature task prompt, and that the plan file content survives.
    """
    workspace_id = await _seed_monitoring_workspace(factory)

    worktrees_root = tmp_path / "worktrees"
    workspace_dir = worktrees_root / workspace_id
    plan_dir = workspace_dir / "docs" / "awf-plans"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_path = plan_dir / f"{workspace_id}.md"
    sentinel = "# Plan — DO NOT REWRITE\n"
    plan_path.write_text(sentinel, encoding="utf-8")

    cmd = FakeCommandRunner()
    sleep_fn = _RecordedSleep()
    adapter = _FakeAdapter()

    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=_pr_payload())  # PR snapshot — green

    runner = PullRequestMonitorRunner(
        session_factory=factory,
        runner=cmd,
        adapter=adapter,
        gh=GitHubClient(cmd),
        monitor_config=MonitorConfig(
            auto_merge=True,
            poll_interval_seconds=60,
            initial_review_grace_period_seconds=900,
            pre_merge_settle_seconds=0,
        ),
        runner_config=MonitorRunnerConfig(max_outer_iterations=3, max_fix_cycle_passes=3),
        sleep=sleep_fn,
        worktrees_root=worktrees_root,
    )

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.ready.value
        ops = await OperationRepository(s).list_all(workspace_id=workspace_id)

    pr_monitor_ops = [
        op
        for op in ops
        if isinstance(op.payload, dict) and op.payload.get("source") == "pr_monitor"
    ]
    assert len(pr_monitor_ops) == 1
    op = pr_monitor_ops[0]
    assert op.type == OperationType.validate.value
    assert op.payload == {
        "owner": "pr_monitor",
        "source": "pr_monitor",
        "action": "validate_only",
        "requested_action": "validate",
        "reason": "Required validation tier has not passed for this merge candidate.",
        "reason_code": "VALIDATION_INSUFFICIENT_TIER",
        "stale_reason": "validation_insufficient_tier",
        "recovery_mode": "validate_only",
        "pr_number": 42,
        "pr_url": "https://github.com/dimileeh/aira-web/pull/42",
        "source_head_sha": "abc1234567890def",
        "source_base_sha": "a" * 40,
        "target_branch": "development",
        "remote_branch": f"awf/{workspace_id}",
    }
    assert op.status == OperationStatus.pending.value

    # The agent adapter must not have been invoked at all (recovery
    # runs via the executor, which we did not run in this test). The
    # monitor itself never invokes the feature task prompt.
    assert adapter.calls == []
    # The plan file survives byte-identical.
    assert plan_path.read_text(encoding="utf-8") == sentinel


@pytest.mark.unit
async def test_review_quiet_window_keeps_workspace_in_monitoring_pr(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """When review quieting is active and the PR is otherwise merge-ready,
    the runner sleeps and stays in monitoring_pr — no recovery operation
    is created."""
    workspace_id = await _seed_monitoring_workspace(factory)
    # Reset monitor state so the review quiet window appears active.
    async with factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        ws.monitor_threads_addressed = {}
        await s.commit()

    cmd = FakeCommandRunner()
    sleep_fn = _RecordedSleep()
    adapter = _FakeAdapter()

    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=_pr_payload())  # green PR

    runner = PullRequestMonitorRunner(
        session_factory=factory,
        runner=cmd,
        adapter=adapter,
        gh=GitHubClient(cmd),
        monitor_config=MonitorConfig(
            auto_merge=True,
            poll_interval_seconds=60,
            initial_review_grace_period_seconds=900,
            pre_merge_settle_seconds=0,
        ),
        runner_config=MonitorRunnerConfig(max_outer_iterations=1, max_fix_cycle_passes=3),
        sleep=sleep_fn,
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
        # Workspace stayed in monitoring_pr (max_outer_iterations=1
        # so the quiet defer ran once, then loop exited via the iter cap).
        assert ws.status in {
            WorkspaceStatus.monitoring_pr.value,
            WorkspaceStatus.failed.value,  # iter cap fail is acceptable
        }
        ops = await OperationRepository(s).list_all(workspace_id=workspace_id)
        # No recovery operation was created during the review quiet window.
        pr_monitor_ops = [
            op
            for op in ops
            if isinstance(op.payload, dict) and op.payload.get("source") == "pr_monitor"
        ]
        assert [(op.type, op.payload.get("action")) for op in pr_monitor_ops] == [
            (OperationType.monitor_state.value, "reviewer_settle_wait")
        ]
        settle_op = pr_monitor_ops[0]
        assert settle_op.status == OperationStatus.succeeded.value
        assert settle_op.payload["reason_code"] == "NON_CHECK_REVIEWER_SETTLE"
        assert settle_op.payload["requested_action"] == "validate"
        # The quiet-window started key is persisted so it doesn't restart on
        # the next iteration after the runner restarts.
        threads = ws.monitor_threads_addressed or {}
        assert (
            _non_check_reviewer_settle_started_key(
                pr_number=42,
                head_sha="abc1234567890def",
            )
            in threads
        )

    # The runner slept for the quiet-window remainder (capped to poll_interval=60).
    assert sleep_fn.calls and sleep_fn.calls[0] == 60
