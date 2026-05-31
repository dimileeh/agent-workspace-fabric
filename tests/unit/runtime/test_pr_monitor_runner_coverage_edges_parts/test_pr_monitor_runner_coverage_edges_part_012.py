"""Continuation coverage tests for PR monitor runner helper edges."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_mock
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.types import BaseFetchError
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
async def test_fetch_base_wraps_broken_ref_repair_exceptions_as_base_fetch_error(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    fetch_base_once = mocker.patch.object(
        runner,
        "_fetch_base_once",
        mocker.AsyncMock(
            return_value=CommandResult(
                returncode=1,
                stdout="",
                stderr="fatal: bad object refs/heads/awf/ws_old",
            )
        ),
    )
    repair = mocker.patch.object(
        runner,
        "_repair_orphaned_broken_awf_ref",
        mocker.AsyncMock(side_effect=RuntimeError("database unavailable")),
    )

    with pytest.raises(BaseFetchError, match="broken AWF ref repair failed") as exc:
        await runner._fetch_base(
            workspace_id="ws_current",
            worktree_path=tmp_path / "worktrees" / "ws_current",
            base_branch="development",
        )

    assert "database unavailable" in str(exc.value)
    assert fetch_base_once.await_count == 1
    repair.assert_awaited_once()


@pytest.mark.unit
async def test_missing_workspace_terminal_helpers_return_without_side_effects(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    with pytest.raises(RuntimeError, match="disappeared"):
        await runner._load_workspace("ws_missing")
    await runner._persist_state("ws_missing", MonitorState(last_push_sha="abc"))
    await runner._terminate_failed("ws_missing", message="missing")
    await runner._terminate_completed("ws_missing", pr_merge_sha="abc")
