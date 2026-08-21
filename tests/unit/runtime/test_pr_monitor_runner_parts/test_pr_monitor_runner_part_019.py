"""Unit tests for focused ``pr_monitor_runner`` provider-recovery run paths."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_mock
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.db.enums import WorkspaceStatus
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor import MonitorState
from awf.runtime.pr_monitor_runner.types import (
    ProviderRecoveryFallbackError,
    ProviderRecoveryRetryError,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
)

from .test_pr_monitor_runner_part_004 import _green_status


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("error_cls", "terminates"),
    [
        (ProviderRecoveryRetryError, False),
        (ProviderRecoveryFallbackError, True),
    ],
)
async def test_run_handles_provider_recovery_exceptions_without_crashing(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    error_cls: type[Exception],
    terminates: bool,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    workspace_id = "ws_provider_recovery_run"
    state = MonitorState(started_at=0.0)
    workspace = SimpleNamespace(
        status=WorkspaceStatus.monitoring_pr.value,
        monitor_started_at=datetime.now(UTC),
        repo_url="git@github.com:dimileeh/aira-web.git",
        pr_number=42,
        branch_base="development",
        remote_push_branch="awf/ws_provider_recovery_run",
        task_kind="feature_branch_pr",
        branch_name="awf/ws_provider_recovery_run",
        task_policy={},
    )

    async def _raise_provider_error(**_kwargs: object) -> bool:
        raise error_cls()

    mocker.patch.object(runner, "_open_monitor_log", mocker.AsyncMock(return_value=None))
    write_log = mocker.patch.object(runner, "_write_monitor_log", mocker.AsyncMock())
    mocker.patch.object(runner, "_load_workspace", mocker.AsyncMock(return_value=workspace))
    mocker.patch.object(runner, "_load_state", return_value=state)
    mocker.patch.object(
        runner,
        "_fetch_status_for_decision",
        mocker.AsyncMock(return_value=_green_status()),
    )
    mocker.patch.object(runner, "_execute", _raise_provider_error)
    persist_state = mocker.patch.object(runner, "_persist_state", mocker.AsyncMock())
    terminate_failed = mocker.patch.object(
        runner,
        "_terminate_failed",
        mocker.AsyncMock(),
    )

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    persist_state.assert_awaited_once_with(workspace_id, state)
    logged_events = [call.args[1]["event"] for call in write_log.await_args_list]
    if terminates:
        assert "monitor.provider_fallback" in logged_events
        terminate_failed.assert_awaited_once_with(
            workspace_id,
            message="monitor: provider recovery fallback triggered",
            reason_code="PROVIDER_FALLBACK",
        )
    else:
        assert "monitor.provider_retry" in logged_events
        terminate_failed.assert_not_awaited()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("error_cls", "terminates"),
    [
        (ProviderRecoveryRetryError, False),
        (ProviderRecoveryFallbackError, True),
    ],
)
async def test_run_handles_provider_recovery_before_state_is_loaded(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    error_cls: type[Exception],
    terminates: bool,
) -> None:
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    workspace_id = "ws_provider_recovery_early"
    mocker.patch.object(runner, "_open_monitor_log", mocker.AsyncMock(return_value=None))
    write_log = mocker.patch.object(runner, "_write_monitor_log", mocker.AsyncMock())
    mocker.patch.object(runner, "_load_workspace", mocker.AsyncMock(side_effect=error_cls()))
    persist_state = mocker.patch.object(runner, "_persist_state", mocker.AsyncMock())
    terminate_failed = mocker.patch.object(
        runner,
        "_terminate_failed",
        mocker.AsyncMock(),
    )

    await runner.run(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=tmp_path / "compose.yml",
    )

    persist_state.assert_not_awaited()
    logged_events = [call.args[1]["event"] for call in write_log.await_args_list]
    if terminates:
        assert "monitor.provider_fallback" in logged_events
        terminate_failed.assert_awaited_once_with(
            workspace_id,
            message="monitor: provider recovery fallback triggered",
            reason_code="PROVIDER_FALLBACK",
        )
    else:
        assert "monitor.provider_retry" in logged_events
        terminate_failed.assert_not_awaited()
