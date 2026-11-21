"""The outer safety net limits repeated work decisions, including across waits."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
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
