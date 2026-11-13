"""The runner populates ``PRStatus.actions_runs`` before every decision (#1230)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from pathlib import Path
from types import MethodType

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClient, GitHubClientError, RepoRef
from awf.db.repositories import OperationRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckFailure,
    CheckState,
    MergeableState,
    MergeStateStatus,
    MonitorState,
    PRStatus,
    WaitForCI,
)
from awf.runtime.pr_monitor_models import ActionsRunsState
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    pr_payload,
    seed_monitoring_workspace,
)

_REPO = RepoRef(owner="o", name="r")
_HEAD = "abc1234567890def"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _green_status() -> PRStatus:
    return PRStatus(
        number=42,
        head_sha=_HEAD,
        mergeable=MergeableState.MERGEABLE,
        check_state=CheckState.SUCCESS,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
    )


class _ActionsGH:
    def __init__(
        self,
        status: PRStatus,
        *,
        actions_runs: ActionsRunsState = ActionsRunsState.PENDING,
        error: GitHubClientError | None = None,
    ) -> None:
        self.status = status
        self.actions_runs = actions_runs
        self.error = error
        self.actions_requests: list[tuple[RepoRef, str, bool]] = []
        self.failing_log_requests: list[str] = []

    async def fetch_pr_status(
        self, *, repo: RepoRef, pr_number: int, base_behind_count: int, retry: bool = True
    ) -> PRStatus:
        del repo, pr_number, retry
        return replace(self.status, base_behind_count=base_behind_count)

    async def fetch_actions_runs_state(
        self, *, repo: RepoRef, head_sha: str, retry: bool = True
    ) -> ActionsRunsState:
        self.actions_requests.append((repo, head_sha, retry))
        if self.error is not None:
            raise self.error
        return self.actions_runs

    async def fetch_failing_check_logs(
        self,
        *,
        repo: RepoRef,
        pr_number: int,
        head_sha: str,
        pytest_fallback_commands: Sequence[str] = (),
        rollup_checks: object = (),
    ) -> tuple[CheckFailure, ...]:
        del repo, pr_number, pytest_fallback_commands, rollup_checks
        self.failing_log_requests.append(head_sha)
        return ()


async def _fetch(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    gh: _ActionsGH,
    *,
    retry: bool = True,
) -> PRStatus:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # rev-list HEAD..origin/<base>
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    runner._deps.gh = gh  # type: ignore[assignment]
    return await runner._fetch_status_for_decision(
        repo=_REPO,
        pr_number=42,
        workspace_id="ws_actions",
        base_branch="development",
        retry=retry,
    )


@pytest.mark.unit
@pytest.mark.parametrize("retry", [True, False])
@pytest.mark.parametrize("runs", [ActionsRunsState.PENDING, ActionsRunsState.SETTLED])
async def test_green_status_carries_the_head_actions_runs_state(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    retry: bool,
    runs: ActionsRunsState,
) -> None:
    gh = _ActionsGH(_green_status(), actions_runs=runs)

    status = await _fetch(factory, tmp_path, gh, retry=retry)

    assert status.actions_runs is runs
    assert gh.actions_requests == [(_REPO, _HEAD, retry)]


@pytest.mark.unit
async def test_fetch_error_leaves_unknown_and_logs_reason_code(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    error = GitHubClientError(
        operation="list_actions_runs_for_sha", returncode=1, stderr="HTTP 502"
    )
    gh = _ActionsGH(_green_status(), error=error)

    with structlog.testing.capture_logs() as captured:
        status = await _fetch(factory, tmp_path, gh)

    assert status.actions_runs is ActionsRunsState.UNKNOWN
    events = [e for e in captured if e["event"] == "monitor.actions_runs_unavailable"]
    assert len(events) == 1
    assert events[0]["reason_code"] == "ACTIONS_RUNS_UNAVAILABLE"
    assert events[0]["pr_number"] == 42
    assert events[0]["head_sha"] == _HEAD[:10]
    assert events[0]["operation"] == "list_actions_runs_for_sha"


@pytest.mark.unit
@pytest.mark.parametrize(
    "overrides",
    [{"check_state": CheckState.FAILURE}, {"merged": True}, {"closed": True}],
)
async def test_failure_and_terminal_statuses_skip_the_actions_runs_read(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    overrides: dict[str, object],
) -> None:
    gh = _ActionsGH(replace(_green_status(), **overrides))  # type: ignore[arg-type]

    status = await _fetch(factory, tmp_path, gh)

    assert gh.actions_requests == []
    assert status.actions_runs is ActionsRunsState.UNKNOWN
    expected_logs = [_HEAD] if overrides.get("check_state") == CheckState.FAILURE else []
    assert gh.failing_log_requests == expected_logs


@pytest.mark.unit
@pytest.mark.parametrize(
    ("reason", "message"),
    [
        (
            "actions_runs_pending",
            "GitHub Actions workflow runs for the PR head are still queued or running.",
        ),
        (
            "actions_runs_unknown",
            "AWF could not read GitHub Actions workflow runs for the PR head; "
            "waiting instead of certifying.",
        ),
    ],
)
async def test_actions_runs_wait_records_check_wait_with_its_reason(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    reason: str,
    message: str,
) -> None:
    sleep_fn = RecordedSleep()
    cmd = FakeCommandRunner()
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        initial_review_grace_period_seconds=0,
    )

    terminal = await runner._execute(
        action=WaitForCI(reason=reason),  # type: ignore[arg-type]
        workspace_id=workspace_id,
        repo_url="git@github.com:o/r.git",
        repo=_REPO,
        pr_number=42,
        status=_green_status(),
        state=MonitorState(),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    async with factory() as session:
        [operation] = await OperationRepository(session).list_all(workspace_id=workspace_id)
    assert terminal is False
    assert sleep_fn.calls == [60]
    assert operation.payload["action"] == "check_wait"
    assert operation.payload["reason_code"] == "CHECK_WAIT"
    assert operation.payload["wait_reason"] == reason
    assert operation.payload["reason"] == message
    assert not any(call.args[:3] == ["gh", "pr", "comment"] for call in cmd.calls)


def _runs_payload(*statuses: str) -> str:
    return json.dumps([{"databaseId": i, "status": status} for i, status in enumerate(statuses, 1)])


def _queue_poll(cmd: FakeCommandRunner, *run_statuses: str) -> None:
    cmd.queue_result(returncode=0)  # git fetch origin <base>
    cmd.queue_result(returncode=0, stdout="0\n")  # base-behind
    cmd.queue_result(returncode=0, stdout=pr_payload())  # green rollup: SUCCESS/MERGEABLE/CLEAN
    cmd.queue_result(returncode=0, stdout=_runs_payload(*run_statuses))  # gh run list


@pytest.mark.unit
@pytest.mark.parametrize("auto_merge", [True, False])
async def test_runner_waits_on_pending_actions_runs_then_takes_the_ready_path(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    auto_merge: bool,
) -> None:
    # #1230 end to end: poll -> decide -> execute through the real runner loop.
    # Poll 1: green rollup, but a head workflow run is still queued with zero
    # jobs -> check_wait, no merge, no READY_TO_MERGE cert. Poll 2: every run
    # completed -> the unchanged merge (auto_merge) or ready-cert path.
    workspace_id = await seed_monitoring_workspace(factory, auto_merge=auto_merge)
    cmd = FakeCommandRunner()
    _queue_poll(cmd, "completed", "queued")
    _queue_poll(cmd, "completed", "completed")
    if auto_merge:
        _queue_poll(cmd, "completed", "completed")  # always-on pre-merge recheck
        cmd.queue_result(returncode=0)  # gh pr merge
        cmd.queue_result(returncode=0, stdout="MERGESHA\n")  # sha lookup
    else:
        cmd.queue_result(returncode=0)  # gh pr comment (ready cert)
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        auto_merge=auto_merge,
        max_outer_iterations=2,
    )
    # Use the real ``gh run list`` read instead of the harness's SETTLED stub.
    gh = runner._deps.gh
    gh.fetch_actions_runs_state = MethodType(  # type: ignore[method-assign]
        GitHubClient.fetch_actions_runs_state, gh
    )

    with structlog.testing.capture_logs() as captured:
        await runner.run(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
        )

    actions = [r["action"] for r in captured if r.get("event") == "monitor.action"]
    assert actions == ["WaitForCI", "Merge" if auto_merge else "NotifyHuman"]
    run_list_calls = [call.args for call in cmd.calls if call.args[:3] == ["gh", "run", "list"]]
    assert len(run_list_calls) == (3 if auto_merge else 2)
    assert all(args[args.index("--commit") + 1] == _HEAD for args in run_list_calls)
    async with factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    [wait_op] = [
        op
        for op in operations
        if isinstance(op.payload, dict) and op.payload.get("action") == "check_wait"
    ]
    assert wait_op.payload is not None
    assert wait_op.payload["wait_reason"] == "actions_runs_pending"
    merges = [call for call in cmd.calls if call.args[:3] == ["gh", "pr", "merge"]]
    comments = [call for call in cmd.calls if call.args[:3] == ["gh", "pr", "comment"]]
    # Nothing merged or certified while the run was queued: the only merge /
    # cert comes after the second (settled) poll's gh run list.
    second_run_list = [
        i for i, call in enumerate(cmd.calls) if call.args[:3] == ["gh", "run", "list"]
    ][1]
    assert all(cmd.calls.index(call) > second_run_list for call in merges + comments)
    if auto_merge:
        assert len(merges) == 1
        assert comments == []
    else:
        assert merges == []
        [comment] = comments
        body = " ".join(comment.args) + (comment.input_bytes or b"").decode()
        assert "is ready to merge" in body
