"""PR monitor agent compose-service recovery exhaustion tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_mock
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunError
from awf.adapters.provider_failures import AGENT_IDLE_TIMEOUT, AGENT_SERVICE_UNHEALTHY
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.db.enums import AgentRuntime, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor_runner import agent_service_recovery
from awf.runtime.pr_monitor_runner.types import (
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorAgentServiceRecoverySupersededError,
)
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
async def test_monitor_agent_service_recovery_exhaustion_terminates_workspace(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    adapter = FakeAdapter()
    for _ in range(3):
        adapter.queue(
            exc=AgentRunError(
                agent=AgentRuntime.claude_code,
                result=CommandResult(
                    returncode=1,
                    stdout="",
                    stderr="monitor idle timeout while agent service stayed down",
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
        return_value=None,
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
        unhealthy_events = [
            event for event in workspace.events if event.reason_code == AGENT_SERVICE_UNHEALTHY
        ]

    assert adapter.calls == ["fix the comment", "fix the comment", "fix the comment"]
    assert ensure_project_up.await_count == 2
    assert workspace.status == WorkspaceStatus.failed.value
    assert unhealthy_events[-1].payload["details"]["agent_service_recovery"] == {
        "reason_code": AGENT_SERVICE_UNHEALTHY,
        "source_reason_code": AGENT_IDLE_TIMEOUT,
        "service_healthy": False,
        "restart_attempts": 2,
    }


@pytest.mark.unit
async def test_monitor_agent_service_recovery_superseded_when_workspace_missing(
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

    with pytest.raises(_MonitorAgentServiceRecoverySupersededError) as raised:
        await agent_service_recovery._raise_if_monitor_agent_service_recovery_was_superseded(
            runner,
            workspace_id="ws_missing",
            source_reason_code=AGENT_IDLE_TIMEOUT,
            service_healthy=False,
            restart_attempts=1,
        )

    assert raised.value.details == {
        "reason_code": AGENT_SERVICE_UNHEALTHY,
        "source_reason_code": AGENT_IDLE_TIMEOUT,
        "service_healthy": False,
        "restart_attempts": 1,
        "superseded_reason": "workspace_missing",
    }


@pytest.mark.unit
def test_monitor_agent_recovery_helpers_ignore_invalid_timeout_details() -> None:
    timeout_error = AgentRunError(
        agent=AgentRuntime.claude_code,
        result=CommandResult(returncode=1, stdout="", stderr="timeout"),
        reason_code=AGENT_IDLE_TIMEOUT,
        details=None,
    )

    assert agent_service_recovery._provider_from_error(timeout_error) is None
    assert agent_service_recovery._model_from_error(timeout_error) is None

    cleanup_error = ComposeExecCleanupError(
        invocation_id="awf-test-cleanup",
        source="agent",
        label="monitor",
        message='service "agent" is not running',
        cleanup_result=CommandResult(
            returncode=1, stdout="", stderr='service "agent" is not running'
        ),
    )
    cleanup_error.reason_code = "OTHER_REASON"  # type: ignore[attr-defined]

    assert not agent_service_recovery._cleanup_failure_indicates_agent_service_down(cleanup_error)


@pytest.mark.unit
def test_cleanup_failure_indicates_agent_down_falls_back_to_str_when_no_result() -> None:
    """A cleanup failure with no captured result still inspects the exception text.

    When ``cleanup_result is None`` the helper falls back to ``str(exc)`` so a
    cleanup failure carrying the agent-down marker in its message (but no
    separate result object) is still classified as an agent-service-down
    signal. The message rendered by ``ComposeExecCleanupError`` embeds the
    operator-supplied message, so a marker placed there is detected.
    """
    cleanup_error = ComposeExecCleanupError(
        invocation_id="awf-test-cleanup",
        source="agent",
        label="monitor",
        message='service "agent" is not running',
        cleanup_result=None,
    )
    assert agent_service_recovery._cleanup_failure_indicates_agent_service_down(cleanup_error)


@pytest.mark.unit
def test_provider_and_model_from_error_read_provider_recovery_mapping() -> None:
    """``_provider_from_error`` / ``_model_from_error`` read the nested mapping.

    A provider-recovery mapping carrying a non-string (or blank) provider/model
    must fall through to ``None`` rather than surfacing the raw value — only a
    non-empty string is a usable provider/model identity. This exercises the
    branch where ``provider_recovery`` is a Mapping but its inner field is not a
    usable string, distinct from the top-level ``details is None`` path.
    """
    timeout_error = AgentRunError(
        agent=AgentRuntime.claude_code,
        result=CommandResult(returncode=124, stdout="", stderr=""),
        reason_code=AGENT_IDLE_TIMEOUT,
        details={
            "provider_recovery": {"provider": None, "model": "  "},
        },
    )

    assert agent_service_recovery._provider_from_error(timeout_error) is None
    assert agent_service_recovery._model_from_error(timeout_error) is None


@pytest.mark.unit
def test_provider_and_model_from_error_return_recovery_mapping_strings() -> None:
    """A non-empty string inside ``provider_recovery`` is returned trimmed.

    When the top-level ``provider``/``model`` fields are absent/blank but the
    nested ``provider_recovery`` mapping carries a usable string, that value is
    returned (trimmed) — mirroring the provider-recovery contract used by the
    Compose-path classifier.
    """
    timeout_error = AgentRunError(
        agent=AgentRuntime.claude_code,
        result=CommandResult(returncode=124, stdout="", stderr=""),
        reason_code=AGENT_IDLE_TIMEOUT,
        details={
            "provider_recovery": {"provider": "  anthropic  ", "model": "claude-foo"},
        },
    )

    assert agent_service_recovery._provider_from_error(timeout_error) == "anthropic"
    assert agent_service_recovery._model_from_error(timeout_error) == "claude-foo"
