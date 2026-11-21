"""The outer safety net limits repeated work decisions, including across waits."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import GitHubClientError
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceEventRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import (
    CheckState,
    Merge,
    MergeableState,
    MergeStateStatus,
    PRStatus,
    RerunTransientCI,
    SyncBase,
    WaitForCI,
    WaitForTransientCI,
)
from awf.runtime.pr_monitor_models import ActionsRunsState
from awf.runtime.pr_monitor_runner import runner as monitor_runner
from awf.service.merge_queue import MergeQueueBlocker
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

_CAP = 2
_WAIT_POLLS = 3


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _status(check_state: CheckState) -> PRStatus:
    return PRStatus(
        number=42,
        head_sha="abc1234567890def",
        mergeable=MergeableState.MERGEABLE,
        check_state=check_state,
        unresolved_inline_threads=(),
        unresolved_review_comments=(),
        base_behind_count=0,
        merge_state_status=MergeStateStatus.CLEAN,
        actions_runs=ActionsRunsState.SETTLED,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("action_name", "check_state", "auto_merge", "wait_seconds", "iter_count"),
    [
        ("NotifyHuman", CheckState.SUCCESS, False, 60, 0),
        ("WaitForCI", CheckState.PENDING, True, 60, 0),
        ("WaitForTransientCI", CheckState.FAILURE, True, 120, _WAIT_POLLS),
    ],
)
async def test_passive_waits_outlast_outer_iterations_cap(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action_name: str,
    check_state: CheckState,
    auto_merge: bool,
    wait_seconds: float,
    iter_count: int,
) -> None:
    ws_id = await seed_monitoring_workspace(factory, auto_merge=auto_merge)
    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        auto_merge=auto_merge,
        max_outer_iterations=_CAP,
    )
    if action_name == "WaitForTransientCI":
        # Isolate the outer budget from the separate transient-CI escalation policy.
        monkeypatch.setattr(
            monitor_runner,
            "decide",
            lambda *_: WaitForTransientCI(failures=(), wait_seconds=wait_seconds, wait_count=1),
        )
    fetch = AsyncMock(side_effect=[_status(check_state)] * _WAIT_POLLS + [asyncio.CancelledError])
    write_log = AsyncMock(wraps=runner._write_monitor_log)
    monkeypatch.setattr(runner, "_fetch_status_for_decision", fetch)
    monkeypatch.setattr(runner, "_write_monitor_log", write_log)

    with structlog.testing.capture_logs() as captured, pytest.raises(asyncio.CancelledError):
        await runner.run(
            workspace_id=ws_id, compose_project="proj", compose_file=tmp_path / "c.yml"
        )

    assert fetch.await_count == _WAIT_POLLS + 1
    assert [r["action"] for r in captured if r.get("event") == "monitor.action"] == [
        action_name
    ] * _WAIT_POLLS
    assert sleep_fn.calls == [wait_seconds] * _WAIT_POLLS
    assert not any(call.args[1]["event"] == "monitor.failed" for call in write_log.await_args_list)
    if action_name == "NotifyHuman":
        [comment] = [call for call in cmd.calls if call.args[:3] == ["gh", "pr", "comment"]]
        assert "is ready to merge" in " ".join(comment.args)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        assert ws.failure_message is None
        assert ws.monitor_iter_count == iter_count


@pytest.mark.unit
@pytest.mark.parametrize("poll_interval_seconds", [0, -1])
@pytest.mark.parametrize("auto_merge", [True, False], ids=["ci-wait", "human-wait"])
async def test_immediate_waits_exhaust_outer_iterations_cap(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    poll_interval_seconds: float,
    auto_merge: bool,
) -> None:
    ws_id = await seed_monitoring_workspace(factory, auto_merge=auto_merge)
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        auto_merge=auto_merge,
        max_outer_iterations=_CAP,
    )
    runner._config = replace(runner._config, poll_interval_seconds=poll_interval_seconds)
    status = _status(CheckState.PENDING if auto_merge else CheckState.SUCCESS)
    fetch = AsyncMock(
        side_effect=[status] * _CAP + [AssertionError("monitor exceeded the iteration cap")]
    )
    monkeypatch.setattr(runner, "_fetch_status_for_decision", fetch)

    await runner.run(workspace_id=ws_id, compose_project="proj", compose_file=tmp_path / "c.yml")

    assert fetch.await_count == _CAP
    assert sleep_fn.calls == [poll_interval_seconds] * _CAP
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert "max_outer_iterations" in (ws.failure_message or "")


@pytest.mark.unit
@pytest.mark.parametrize("poll_seconds", [60, 0], ids=["delayed", "immediate"])
async def test_scope_policy_human_wait_budget(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    poll_seconds: float,
) -> None:
    ws_id = await seed_monitoring_workspace(factory)
    async with factory.begin() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        ws.owned_paths = ["src/owned.py"]
        ws.task_policy = {"out_of_scope_changes": {"mode": "block"}}

    cmd = FakeCommandRunner()
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        max_outer_iterations=_CAP,
    )
    runner._config = replace(runner._config, poll_interval_seconds=poll_seconds)
    status = replace(_status(CheckState.SUCCESS), changed_paths=("src/outside.py",))
    merge_sha = "0123456789abcdef"
    merge = AsyncMock(return_value=merge_sha)

    async def fetch_status(**_kwargs: object) -> PRStatus:
        assert fetch.await_count <= _WAIT_POLLS + 2, "scope approval did not unblock the merge"
        if fetch.await_count == _WAIT_POLLS + 1:
            merge.assert_not_awaited()
            async with factory.begin() as session:
                ws = await WorkspaceRepository(session).get(ws_id)
                assert ws is not None
                assert ws.status == WorkspaceStatus.monitoring_pr.value
                assert ws.failure_message is None
                assert ws.awaiting_human_since is not None
                ws.task_policy = {
                    "out_of_scope_changes": {
                        "mode": "block",
                        "allowlist_patterns": ["src/outside.py"],
                    }
                }
        return status

    fetch = AsyncMock(side_effect=fetch_status)
    monkeypatch.setattr(runner, "_fetch_status_for_decision", fetch)
    monkeypatch.setattr(runner._deps.gh, "merge_pr", merge)

    await runner.run(workspace_id=ws_id, compose_project="proj", compose_file=tmp_path / "c.yml")

    expected_polls = _WAIT_POLLS if poll_seconds > 0 else _CAP
    assert sleep_fn.calls == [poll_seconds] * expected_polls
    [comment] = [call for call in cmd.calls if call.args[:3] == ["gh", "pr", "comment"]]
    assert "OUT_OF_SCOPE_CHANGE" in " ".join(comment.args)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        if poll_seconds > 0:
            merge.assert_awaited_once()
            assert ws.status == WorkspaceStatus.completed.value
            assert ws.pr_merge_sha == merge_sha
            assert ws.failure_message is None
            assert ws.awaiting_human_since is None
        else:
            merge.assert_not_awaited()
            assert ws.status == WorkspaceStatus.failed.value
            assert "max_outer_iterations" in (ws.failure_message or "")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("method_result", "poll_seconds", "settle_seconds"),
    [
        pytest.param("no-method", 60, 0, id="no-method"),
        pytest.param("preflight-rejected", 60, 0, id="preflight-rejected"),
        pytest.param("method-rejected", 60, 0, id="method-rejected"),
        pytest.param("no-method", 0, 5, id="immediate-after-settle"),
    ],
)
async def test_merge_method_human_wait_budget(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_result: str,
    poll_seconds: float,
    settle_seconds: float,
) -> None:
    ws_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        max_outer_iterations=1,
        pre_merge_settle_seconds=settle_seconds,
    )
    runner._config = replace(runner._config, poll_interval_seconds=poll_seconds)
    status = _status(CheckState.SUCCESS)
    fetch = AsyncMock(side_effect=[status, status, asyncio.CancelledError])
    monkeypatch.setattr(runner, "_fetch_status_for_decision", fetch)
    attempt_merge = method_result == "method-rejected"
    repo_methods = AsyncMock(return_value=("squash",))
    if method_result == "preflight-rejected":
        repo_methods.side_effect = GitHubClientError(
            operation="gh api", returncode=1, stderr="HTTP 403 Resource not accessible"
        )
    monkeypatch.setattr(runner._deps.gh, "fetch_repo_merge_methods", repo_methods)
    monkeypatch.setattr(
        runner._deps.gh,
        "fetch_branch_pull_request_allowed_merge_methods",
        AsyncMock(return_value=("squash",) if attempt_merge else ("merge",)),
    )
    merge = AsyncMock(
        side_effect=GitHubClientError(
            operation="gh pr merge",
            returncode=1,
            stderr="GraphQL: Squash merges are not allowed on this repository.",
        )
    )
    monkeypatch.setattr(runner._deps.gh, "merge_pr", merge)

    refunds = not attempt_merge and poll_seconds > 0
    with pytest.raises(asyncio.CancelledError) if refunds else nullcontext():
        await runner.run(
            workspace_id=ws_id, compose_project="proj", compose_file=tmp_path / "c.yml"
        )

    assert merge.await_count == int(attempt_merge)
    assert sleep_fn.calls == ([settle_seconds, poll_seconds] if settle_seconds else [poll_seconds])
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        if refunds:
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.failure_message is None
        else:
            assert ws.status == WorkspaceStatus.failed.value
            assert "max_outer_iterations" in (ws.failure_message or "")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("after_lock", "poll_seconds", "settle_seconds", "expected_polls"),
    [
        pytest.param(False, 60, 0, _WAIT_POLLS, id="before-lock"),
        pytest.param(True, 60, 0, _WAIT_POLLS, id="after-lock"),
        pytest.param(True, 0, 5, _CAP, id="immediate-wait-after-lock-and-settle"),
    ],
)
async def test_merge_queue_wait_budget(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    after_lock: bool,
    poll_seconds: float,
    settle_seconds: float,
    expected_polls: int,
) -> None:
    ws_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        max_outer_iterations=_CAP,
        pre_merge_settle_seconds=settle_seconds,
    )
    runner._config = replace(runner._config, poll_interval_seconds=poll_seconds)
    blocker = MergeQueueBlocker(
        candidate_id="older-candidate",
        workspace_id="older-workspace",
        attempt_id="older-attempt",
        task_id="older-task",
        title="Older candidate",
        pr_url=None,
        pr_number=41,
        status="open",
        blocker_state="ready",
    )
    queue_checks = [[], [blocker]] if after_lock else [[blocker]]
    blockers = AsyncMock(side_effect=queue_checks * expected_polls)
    fetch = AsyncMock(
        side_effect=[_status(CheckState.SUCCESS)] * (len(queue_checks) * expected_polls)
        + [asyncio.CancelledError("scripted polls complete")]
    )
    merge = AsyncMock(side_effect=AssertionError("a queued PR cannot attempt a merge"))
    monkeypatch.setattr(runner, "_merge_queue_blockers_for_workspace", blockers)
    monkeypatch.setattr(runner, "_fetch_status_for_decision", fetch)
    monkeypatch.setattr(runner._deps.gh, "merge_pr", merge)

    refunds = poll_seconds > 0
    stop = pytest.raises(asyncio.CancelledError, match="scripted polls complete")
    with stop if refunds else nullcontext():
        await runner.run(
            workspace_id=ws_id, compose_project="proj", compose_file=tmp_path / "c.yml"
        )

    assert blockers.await_count == len(queue_checks) * expected_polls
    sleeps_per_poll = [settle_seconds, poll_seconds] if settle_seconds else [poll_seconds]
    assert sleep_fn.calls == sleeps_per_poll * expected_polls
    merge.assert_not_awaited()
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        assert ws.status == (
            WorkspaceStatus.monitoring_pr.value if refunds else WorkspaceStatus.failed.value
        )
        [event] = await WorkspaceEventRepository(session).list(
            workspace_id=ws_id, event_type="workspace.merge_queue_waiting"
        )
        assert event.reason_code == blocker.reason_code


@pytest.mark.unit
@pytest.mark.parametrize(
    "first_wait",
    [None, "queue", "human"],
    ids=["without-wait", "after-queue-wait", "after-human-wait"],
)
async def test_merge_retries_with_backoff_exhaust_outer_iterations_cap(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    first_wait: str | None,
) -> None:
    ws_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
        max_outer_iterations=_CAP,
    )
    if first_wait is not None:
        # Reuse monitor state to exercise the runner's reset independently of the loader.
        state = runner._load_state(await runner._load_workspace(ws_id))
        monkeypatch.setattr(runner, "_load_state", lambda _: state)
    if first_wait == "queue":
        blocker = MergeQueueBlocker(
            candidate_id="older-candidate",
            workspace_id="older-workspace",
            attempt_id="older-attempt",
            task_id="older-task",
            title="Older candidate",
            pr_url=None,
            pr_number=41,
            status="open",
            blocker_state="ready",
        )
        monkeypatch.setattr(
            runner,
            "_merge_queue_blockers_for_workspace",
            AsyncMock(side_effect=[[blocker]] + [[]] * (2 * _CAP)),
        )
    elif first_wait == "human":
        monkeypatch.setattr(
            runner,
            "_refresh_scope_policy_for_merge",
            AsyncMock(side_effect=[True] + [False] * _CAP),
        )
    fetch = AsyncMock(
        side_effect=[_status(CheckState.SUCCESS)] * (2 * _CAP + int(first_wait is not None))
        + [AssertionError("monitor exceeded the iteration cap")]
    )
    merge = AsyncMock(
        side_effect=GitHubClientError(
            operation="gh pr merge", returncode=1, stderr="HTTP 502 Bad Gateway"
        )
    )
    monkeypatch.setattr(runner, "_fetch_status_for_decision", fetch)
    monkeypatch.setattr(runner._deps.gh, "merge_pr", merge)

    await runner.run(workspace_id=ws_id, compose_project="proj", compose_file=tmp_path / "c.yml")

    assert merge.await_count == _CAP
    assert sleep_fn.calls == ([60, 5, 10] if first_wait is not None else [5, 10])
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_message == (
            "monitor: hit max_outer_iterations without a terminal action (likely a decision loop bug)"
        )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("decisions", "sleeping_iterations"),
    [
        pytest.param((SyncBase(), SyncBase()), (), id="sync-base"),
        pytest.param((Merge(), Merge()), (), id="merge"),
        pytest.param((RerunTransientCI(()), RerunTransientCI(())), (), id="rerun-ci"),
        pytest.param((SyncBase(), WaitForCI(), SyncBase()), (2,), id="sync-base-with-wait"),
        pytest.param((WaitForCI(), WaitForCI(), WaitForCI()), (1,), id="wait-without-sleep"),
        pytest.param((object(), object()), (1, 2), id="new-action-with-sleep"),
    ],
)
async def test_decision_loop_exhausts_outer_iterations_cap(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decisions: tuple[object, ...],
    sleeping_iterations: tuple[int, ...],
) -> None:
    ws_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        max_outer_iterations=_CAP,
    )
    actions = iter(decisions)
    write_log = AsyncMock(wraps=runner._write_monitor_log)

    async def execute_action(**kwargs: object) -> bool:
        if execute.await_count in sleeping_iterations:
            await runner._deps.sleep(60)
        return False

    execute = AsyncMock(side_effect=execute_action)
    monkeypatch.setattr(monitor_runner, "decide", lambda *_: next(actions))
    monkeypatch.setattr(
        runner,
        "_fetch_status_for_decision",
        AsyncMock(
            side_effect=[
                *([_status(CheckState.SUCCESS)] * len(decisions)),
                AssertionError("monitor polled past its outer iteration budget"),
            ]
        ),
    )
    monkeypatch.setattr(runner, "_execute", execute)
    monkeypatch.setattr(runner, "_write_monitor_log", write_log)

    await runner.run(workspace_id=ws_id, compose_project="proj", compose_file=tmp_path / "c.yml")

    assert tuple(call.kwargs["action"] for call in execute.await_args_list) == decisions
    assert [
        call.args[1]["reason"]
        for call in write_log.await_args_list
        if call.args[1]["event"] == "monitor.failed"
    ] == ["max_outer_iterations"]
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_message == (
            "monitor: hit max_outer_iterations without a terminal action (likely a decision loop bug)"
        )
        transitions = await WorkspaceEventRepository(session).list(
            workspace_id=ws_id, event_type="workspace.state_changed"
        )
        [failure] = [
            event for event in transitions if event.new_state == WorkspaceStatus.failed.value
        ]
        assert failure.reason_code == "MONITOR_ABORT"
