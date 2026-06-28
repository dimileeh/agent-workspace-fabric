"""PR monitor agent compose-service restart failure tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_mock
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunError
from awf.adapters.provider_failures import AGENT_IDLE_TIMEOUT, AGENT_SERVICE_UNHEALTHY
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.node.compose_manager import ComposeOperationError
from awf.runtime.pr_monitor_runner.types import _MonitorAgentServiceRecoveryFailedError
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
    seed_monitoring_workspace,
)


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _write_compose_file(tmp_path: Path) -> Path:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    return compose_file


@pytest.mark.unit
async def test_monitor_agent_service_restart_failure_terminates_without_provider_recovery(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.claude_code,
            result=CommandResult(
                returncode=1,
                stdout="",
                stderr="monitor idle timeout while agent service was down",
            ),
            reason_code=AGENT_IDLE_TIMEOUT,
            details={"provider": "google", "model": "gemini-2.5-pro"},
        )
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        side_effect=ComposeOperationError(
            operation="up",
            returncode=1,
            stdout="",
            stderr="compose unavailable",
        ),
    )
    compose_file = _write_compose_file(tmp_path)

    with pytest.raises(_MonitorAgentServiceRecoveryFailedError):
        await runner._run_monitor_agent_with_service_recovery(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=compose_file,
            prompt="fix the comment",
            log_source="recovery",
            command_evidence=[],
        )

    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        event_types = [event.event_type for event in workspace.events]
        unhealthy_events = [
            event for event in workspace.events if event.reason_code == AGENT_SERVICE_UNHEALTHY
        ]

    assert workspace.status == WorkspaceStatus.failed.value
    assert workspace.failure_reason == "infrastructure_failure"
    assert "workspace.provider_recovery_requested" not in event_types
    assert len(unhealthy_events) == 1
    assert unhealthy_events[0].event_type == "workspace.state_changed"
    assert unhealthy_events[0].payload["details"]["agent_service_recovery"] == {
        "reason_code": AGENT_SERVICE_UNHEALTHY,
        "source_reason_code": AGENT_IDLE_TIMEOUT,
        "service_healthy": False,
        "restart_attempts": 1,
    }


@pytest.mark.unit
async def test_monitor_agent_service_restart_unexpected_error_propagates(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.claude_code,
            result=CommandResult(
                returncode=1,
                stdout="",
                stderr="monitor idle timeout while agent service was down",
            ),
            reason_code=AGENT_IDLE_TIMEOUT,
            details={"provider": "google", "model": "gemini-2.5-pro"},
        )
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )
    ensure_project_up = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        side_effect=RuntimeError("unexpected bug"),
    )
    compose_file = _write_compose_file(tmp_path)

    with pytest.raises(RuntimeError, match="unexpected bug"):
        await runner._run_monitor_agent_with_service_recovery(
            workspace_id=workspace_id,
            compose_project="proj",
            compose_file=compose_file,
            prompt="fix the comment",
            log_source="recovery",
            command_evidence=[],
        )

    ensure_project_up.assert_awaited_once()
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.monitoring_pr.value
