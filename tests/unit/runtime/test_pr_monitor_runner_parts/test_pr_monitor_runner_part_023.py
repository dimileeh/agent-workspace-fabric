"""``AddressComments`` park-for-a-human disposition in ``_execute`` (#935)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
import pytest_mock
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.common.github_client import RepoRef
from awf.db.enums import OperationStatus, WorkspaceStatus
from awf.db.repositories import OperationRepository, WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import AddressComments, MonitorState, ReviewThread, WaitForCI
from awf.runtime.pr_monitor_runner.remote_ops import _GitPushResult
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)

from .test_pr_monitor_runner_part_004 import _green_status

_PARK_REASON_CODE = "COMMENT_REPAIR_UNPUBLISHED_PROVENANCE_MISSING"


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_parked_comment_repair_keeps_the_workspace_monitoring_and_awaiting_human(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    sleep_fn = RecordedSleep()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=sleep_fn,
        worktrees_root=tmp_path / "worktrees",
    )
    thread = ReviewThread(
        thread_id="PRRT_park",
        path="src/app.py",
        line=12,
        body_excerpt="please fix",
        author="reviewer",
    )
    status = replace(_green_status(), unresolved_inline_threads=(thread,))
    park_result = _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr="Preserved 2 unpushed comment-repair commits: 3195fc8, aa194c9.",
        reason_code=_PARK_REASON_CODE,
        parked_needs_human=True,
        details={"phase": "comment_repair_recovery", "pushed": False},
    )

    async def _parked_fix_cycle(**_kwargs: object) -> _GitPushResult:
        return park_result

    mocker.patch.object(runner, "_run_fix_cycle", _parked_fix_cycle)

    ended = await runner._execute(
        action=AddressComments(threads=(thread,), review_comments=()),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=status,
        state=MonitorState(started_at=0.0),
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )

    # NOT terminal: the parked wait is held inside this monitor's polling loop
    # (one poll-interval sleep, like ``NotifyHuman``). Exiting the runner while the
    # row stays ``monitoring_pr`` would let the worker reclaim it and restart a
    # monitor every cycle, since monitor claims do not exclude awaiting-human rows.
    assert ended is False
    assert sleep_fn.calls == [runner._config.poll_interval_seconds]
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        operations = await OperationRepository(session).list_all(workspace_id=workspace_id)
    assert ws is not None
    assert ws.status == WorkspaceStatus.monitoring_pr.value
    assert isinstance(ws.awaiting_human_since, datetime)
    operation = operations[0]
    assert operation.status == OperationStatus.failed.value
    assert operation.error_code == _PARK_REASON_CODE
    assert operation.result is not None
    assert operation.result["outcome"] == "comment_repair_unpublished_parked"
    assert operation.result["reason_code"] == _PARK_REASON_CODE


@pytest.mark.unit
async def test_parked_repair_keeps_one_attention_episode_across_polls(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    """The parked monitor keeps polling ``AddressComments``, so the top-of-poll resume
    clear must not null and re-stamp ``awaiting_human_since`` every poll — the operator
    would see the wait restart forever (issue:5561887966)."""
    workspace_id = await seed_monitoring_workspace(factory)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    thread = ReviewThread(
        thread_id="PRRT_park",
        path="src/app.py",
        line=12,
        body_excerpt="please fix",
        author="reviewer",
    )
    status = replace(_green_status(), unresolved_inline_threads=(thread,))
    park_result = _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr="Preserved 1 unpushed local commit(s).",
        reason_code=_PARK_REASON_CODE,
        parked_needs_human=True,
        details={"phase": "comment_repair_recovery", "pushed": False},
    )
    state = MonitorState(started_at=0.0)

    async def _parked_fix_cycle(**kwargs: object) -> _GitPushResult:
        # Mirrors the recovery path: parking marks the episode on the live state.
        cycle_state = kwargs["state"]
        assert isinstance(cycle_state, MonitorState)
        cycle_state.mark_parked_unpublished_repair("no_comment_repair_provenance:bbb:aaa")
        return park_result

    mocker.patch.object(runner, "_run_fix_cycle", _parked_fix_cycle)

    async def _poll() -> None:
        await runner._execute(
            action=AddressComments(threads=(thread,), review_comments=()),
            workspace_id=workspace_id,
            repo_url="git@github.com:dimileeh/aira-web.git",
            repo=RepoRef(owner="dimileeh", name="aira-web"),
            pr_number=42,
            status=status,
            state=state,
            base_branch="development",
            remote_branch=f"awf/{workspace_id}",
            compose_project="proj",
            compose_file=tmp_path / "compose.yml",
            monitor_log=None,
        )

    await _poll()
    async with factory() as session:
        first = await WorkspaceRepository(session).get(workspace_id)
    assert first is not None
    awaiting_since = first.awaiting_human_since
    assert isinstance(awaiting_since, datetime)

    await _poll()
    async with factory() as session:
        second = await WorkspaceRepository(session).get(workspace_id)
    assert second is not None
    assert second.awaiting_human_since == awaiting_since

    # Once ``decide()`` leaves the repair arm the episode is over: the marker is
    # dropped and the normal resume clear retires the attention flag.
    await runner._execute(
        action=WaitForCI(),
        workspace_id=workspace_id,
        repo_url="git@github.com:dimileeh/aira-web.git",
        repo=RepoRef(owner="dimileeh", name="aira-web"),
        pr_number=42,
        status=status,
        state=state,
        base_branch="development",
        remote_branch=f"awf/{workspace_id}",
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
        monitor_log=None,
    )
    assert state.parked_unpublished_repair is None
    async with factory() as session:
        third = await WorkspaceRepository(session).get(workspace_id)
    assert third is not None
    assert third.awaiting_human_since is None


@pytest.mark.unit
def test_parked_push_result_is_not_a_terminal_monitor_failure() -> None:
    parked = _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr="parked",
        reason_code=_PARK_REASON_CODE,
        parked_needs_human=True,
    )
    unparked = _GitPushResult(
        pushed=False,
        failed=True,
        returncode=1,
        stderr="missing provenance",
        reason_code=_PARK_REASON_CODE,
    )

    assert parked.terminal_monitor_failure is False
    # #935: the reason code itself must never terminally fail the workspace again.
    assert unparked.terminal_monitor_failure is False
