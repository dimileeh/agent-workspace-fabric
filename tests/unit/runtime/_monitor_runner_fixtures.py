"""Shared helpers for the new PR-monitor runner unit tests.

The action-logging, bot-defer, and defer-signal-artifact tests all need
the same setup: a real PostgreSQL, a ``FakeCommandRunner``, a
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
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentAdapter, AgentRunError, AgentRunResult
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import (
    MergeCandidateRepository,
    TaskAttemptRepository,
    TaskRepository,
    ValidationRunRepository,
    WorkspaceRepository,
    sync_candidate_readiness,
)
from awf.runtime.logs import LogStore
from awf.runtime.pr_monitor import MonitorConfig
from awf.runtime.pr_monitor_runner import (
    MonitorRunnerConfig,
    PullRequestMonitorRunner,
)
from tests.shared.monitor_runner import (
    DefaultMergeMethodGitHubClient,
)
from tests.shared.monitor_runner import (
    mock_completed_compose_manager as _mock_completed_compose_manager,
)

# Preserve the existing fixture-module import path for older runtime tests.
mock_completed_compose_manager = _mock_completed_compose_manager


@dataclass
class FakeAdapter(AgentAdapter):
    """Test-only AgentAdapter stub used by PR-monitor unit tests."""

    runtime = AgentRuntime.claude_code
    _queued: list[AgentRunResult] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    workspace_ids: list[str | None] = field(default_factory=list)

    def __init__(self, *, default_model: str | None = None) -> None:  # type: ignore[override]
        """Store queue-backed run results for this test adapter."""
        super().__init__(runner=None, default_model=default_model)  # type: ignore[arg-type]
        self._queued = []
        self.calls = []
        self.workspace_ids = []

    def get_provider(self, model: str | None) -> str:
        """Return the fixed fake provider identifier."""
        return "fake"

    @property
    def name(self) -> AgentRuntime:  # type: ignore[override]
        """Expose the fake adapter runtime name."""
        return AgentRuntime.claude_code

    def _cli_args(self, *, model: str | None) -> list[str]:
        """Return an empty CLI argument list for the fake adapter."""
        return []

    def queue(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        exc: Exception | None = None,
    ) -> None:
        """Append a scripted command result or exception to the fake queue."""
        self._queued.append(
            exc
            if exc is not None
            else AgentRunResult(returncode=returncode, stdout=stdout, stderr=stderr)
        )

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
        """Consume one queued result and log the dispatched prompt."""
        self.calls.append(prompt)
        self.workspace_ids.append(workspace_id)
        if self._log_store and workspace_id:
            sinks = await self._log_store.open_command_streams(
                workspace_id=workspace_id,
                base_stream_id=log_source,
                source=log_source,
                name=log_source,
            )
            await sinks.write_stdout("mock output\n")
            await sinks.close()
        if not self._queued:
            raise AssertionError(
                "FakeAdapter.run called with empty queue; queue() a result "
                "in the test before this dispatch to avoid masking setup bugs"
            )
        r = self._queued.pop(0)
        if isinstance(r, Exception):
            raise r
        if r.returncode != 0:
            raise AgentRunError(
                agent=AgentRuntime.claude_code,
                result=CommandResult(returncode=r.returncode, stdout=r.stdout, stderr=r.stderr),
            )
        return r


class RecordedSleep:
    """Async sleep shim that captures sleep invocations in unit tests."""

    def __init__(self) -> None:
        """Initialize the in-memory sleep call tracker."""
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        """Record the requested sleep duration without delaying."""
        self.calls.append(seconds)


def pr_payload(
    *,
    head_sha: str = "abc1234567890def",
    created_at: str = "2026-05-06T10:00:00Z",
    committed_date: str = "2026-05-06T10:00:00Z",
    closed: bool = False,
    merged: bool = False,
    merge_commit_sha: str = "mergecommit1234567890",
    mergeable: str = "MERGEABLE",
    merge_state_status: str = "CLEAN",
    check_state: str = "SUCCESS",
    check_contexts: list[dict] | None = None,
    check_contexts_total_count: int | None = None,
    threads: list[dict] | None = None,
    reviews: list[dict] | None = None,
    comments: list[dict] | None = None,
) -> str:
    """Build a GraphQL-like PR payload for monitor status helpers.

    ``check_contexts_total_count`` sets the rollup ``contexts.totalCount`` that
    drives ``PRStatus.no_checks_observed`` (a present-but-empty rollup reports
    ``totalCount == 0`` — the transient post-push CI-start window). When left
    ``None`` the key is omitted, preserving the legacy ``no_checks_observed=False``
    shape for existing callers.
    """
    contexts: dict[str, object] = {"nodes": check_contexts or []}
    if check_contexts_total_count is not None:
        contexts["totalCount"] = check_contexts_total_count
    return json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "number": 42,
                        "createdAt": created_at,
                        "headRefOid": head_sha,
                        "mergeable": mergeable,
                        "mergeStateStatus": merge_state_status,
                        "isDraft": False,
                        "closed": closed,
                        "merged": merged,
                        "mergeCommit": {"oid": merge_commit_sha} if merged else None,
                        "baseRef": {"name": "development", "target": {"oid": "base0"}},
                        "commits": {
                            "nodes": [
                                {
                                    "commit": {
                                        "statusCheckRollup": {
                                            "state": check_state,
                                            "contexts": contexts,
                                        },
                                        "committedDate": committed_date,
                                    }
                                }
                            ]
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
    """Create a lightweight review-thread node fixture."""
    return {
        "id": tid,
        "isResolved": False,
        "isOutdated": False,
        "path": path,
        "line": line,
        "comments": {"nodes": [{"bodyText": body, "author": {"login": author}}]},
    }


def review_node(*, cid: int, author: str, body: str = "see below") -> dict:
    """Create a mocked review node for outside-diff comments."""
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
    """Create a mocked issue-comment payload for outside-diff comment scenarios."""
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
    head_sha: str = "abc1234567890def",
    auto_merge: bool = True,
    pr_merge_sha: str | None = None,
    test_commands: list[str] | None = None,
) -> str:
    """Create and seed a workspace in monitoring_pr state for unit tests."""
    async with factory() as s:
        repo = WorkspaceRepository(s)
        ws = await repo.create(
            repo_url="git@github.com:dimileeh/aira-web.git",
            branch_base="development",
            task_title="monitor test",
            task_prompt="x",
            agent="claude_code",
            test_commands=["pytest -q"] if test_commands is None else test_commands,
            requires_database=False,
            auto_merge=auto_merge,
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
        ws.compose_file_path = f"/tmp/awf/{ws.id}/compose.yml"
        ws.pr_url = f"https://github.com/dimileeh/aira-web/pull/{pr_number}"
        ws.pr_number = pr_number
        ws.pr_merge_sha = pr_merge_sha
        task = await TaskRepository(s).create_or_get(
            repo_url=ws.repo_url,
            base_branch=ws.branch_base,
            title=ws.task_title,
            prompt=ws.task_prompt,
            external_id=ws.task_external_id,
            idempotency_key=None,
            task_class=ws.task_class,
            owned_paths=list(ws.owned_paths),
        )
        attempt = await TaskAttemptRepository(s).create_for_workspace(
            task=task,
            workspace=ws,
        )
        attempt.is_canonical_for_merge = True
        candidate = await MergeCandidateRepository(s).create_or_update_open_for_attempt(
            task=task,
            attempt=attempt,
            workspace=ws,
            head_sha=head_sha,
            base_sha=ws.base_commit,
        )
        validation_repo = ValidationRunRepository(s)
        validation_run = await validation_repo.start(
            workspace_id=ws.id,
            attempt_id=attempt.id,
            tier=1,
            commands=[],
            base_commit=ws.base_commit,
            target_branch=ws.remote_push_branch,
            workspace_head_sha=head_sha,
            target_head_sha=head_sha,
            log_stream_refs={},
        )
        await validation_repo.finish(
            validation_run.id,
            status="succeeded",
            reason_code="VALIDATION_OK",
        )
        sync_candidate_readiness(candidate, workspace=ws, attempt=attempt)
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
    non_check_reviewer_settle_seconds: float = 0,
    non_check_reviewer_logins: tuple[str, ...] | list[str] = ("greptile-apps",),
    stale_pending_check_warning_seconds: float = 900,
    max_outer_iterations: int = 20,
    pre_push_validation_fix_passes: int = 1,
    artifacts_root: Path | None = None,
    log_store: LogStore | None = None,
    merge_coordinator: object | None = None,
    post_merge_target_reconciler: Any | None = None,
    provider_recovery_default_model: str | None = None,
    gh: Any | None = None,
) -> PullRequestMonitorRunner:
    """Construct a PullRequestMonitorRunner wired for integration-style unit tests."""
    kwargs: dict = {
        "session_factory": factory,
        "runner": cmd,
        "adapter": adapter,
        "gh": gh if gh is not None else DefaultMergeMethodGitHubClient(cmd),
        "monitor_config": MonitorConfig(
            auto_merge=auto_merge,
            poll_interval_seconds=60,
            settle_interval_seconds=30,
            initial_review_grace_period_seconds=initial_review_grace_period_seconds,
            pre_merge_settle_seconds=pre_merge_settle_seconds,
            non_check_reviewer_settle_seconds=non_check_reviewer_settle_seconds,
            non_check_reviewer_logins=tuple(non_check_reviewer_logins),
            stale_pending_check_warning_seconds=stale_pending_check_warning_seconds,
        ),
        "runner_config": MonitorRunnerConfig(
            max_outer_iterations=max_outer_iterations,
            max_fix_cycle_passes=3,
            pre_push_validation_fix_passes=pre_push_validation_fix_passes,
        ),
        "sleep": sleep_fn,
        "worktrees_root": worktrees_root,
        "log_store": log_store,
        "provider_recovery_default_model": provider_recovery_default_model,
    }
    if artifacts_root is not None:
        kwargs["artifacts_root"] = artifacts_root
    if merge_coordinator is not None:
        kwargs["merge_coordinator"] = merge_coordinator
    if post_merge_target_reconciler is not None:
        kwargs["post_merge_target_reconciler"] = post_merge_target_reconciler
    return PullRequestMonitorRunner(**kwargs)
