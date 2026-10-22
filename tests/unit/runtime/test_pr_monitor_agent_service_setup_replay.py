"""PR monitor agent-service recovery replays setup after a recreate (#787)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_mock
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunError
from awf.adapters.provider_failures import AGENT_IDLE_TIMEOUT
from awf.common.commands import CommandResult, FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.control.executor import runtime_setup_replay
from awf.control.executor.constants import (
    MONITOR_RUNTIME_SETUP_REPLAY_RESUME_PHASE,
    RUNTIME_SETUP_REPLAY_FAILED_EVENT_TYPE,
    RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE,
    RUNTIME_SETUP_REPLAYED_EVENT_TYPE,
)
from awf.control.executor.state_ops import RESUME_SETUP_FAILURE_BLOCK_TYPE
from awf.db.enums import AgentRuntime, FailureReason, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
from awf.db.session import make_session_factory
from awf.profiles.resolver import ProfileResolutionError
from awf.runtime.pr_monitor_runner.types import (
    _MonitorAgentServiceRecoveryFailedError,
    _MonitorAgentServiceRecoverySupersededError,
    _MonitorHeadObjectMissingError,
)
from tests.postgres import postgres_test_engine
from tests.unit.control.runtime_setup_replay_fakes import (
    TOOL_PATH,
    FakeAgentContainer,
    FakeToolValidation,
    allow_ownership_repair,
    patch_identity,
)
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


@pytest.fixture(autouse=True)
def _ownership_repair_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runtime_setup_replay, "repair_agent_runtime_ownership", allow_ownership_repair
    )


class _OrderedValidation(FakeToolValidation):
    """Record how many agent runs had happened when setup ran."""

    def __init__(self, container: FakeAgentContainer, adapter: FakeAdapter, **kwargs: Any):
        super().__init__(container, **kwargs)
        self._adapter = adapter
        self.agent_calls_at_setup: list[int] = []

    async def run_profile_phases(self, *, phase_names: tuple[str, ...], **kwargs: Any) -> Any:
        if "setup" in phase_names:
            self.agent_calls_at_setup.append(len(self._adapter.calls))
        return await super().run_profile_phases(phase_names=phase_names, **kwargs)


def _idle_timeout_adapter(*, retry_stdout: str | None = "AWF-VERDICT: FIXED: retried") -> Any:
    adapter = FakeAdapter()
    adapter.queue(
        exc=AgentRunError(
            agent=AgentRuntime.claude_code,
            result=CommandResult(returncode=1, stdout="", stderr="idle while agent service down"),
            reason_code=AGENT_IDLE_TIMEOUT,
            details={"provider": "google", "model": "gemini-2.5-pro"},
        )
    )
    if retry_stdout is not None:
        adapter.queue(stdout=retry_stdout)
    return adapter


def _patch_compose(
    mocker: pytest_mock.MockerFixture, container: FakeAgentContainer, *, recreate: bool
) -> Any:
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.probe_agent_service_health",
        return_value=False,
    )

    def _up(**_kwargs: Any) -> None:
        if recreate:
            container.recreate()

    return mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery.ComposeManager.ensure_project_up",
        side_effect=_up,
    )


def _compose_file(tmp_path: Path) -> Path:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    return compose_file


async def _run(runner: Any, workspace_id: str, tmp_path: Path) -> Any:
    return await runner._run_monitor_agent_with_service_recovery(
        workspace_id=workspace_id,
        compose_project="proj",
        compose_file=_compose_file(tmp_path),
        prompt="fix the comment",
        log_source="recovery",
        command_evidence=[],
    )


async def _assert_parked_blocked(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> dict[str, Any]:
    """A failed monitor-side replay parks ``blocked`` (#743) instead of failing."""
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.blocked.value
        assert workspace.block_type == RESUME_SETUP_FAILURE_BLOCK_TYPE
        assert workspace.block_reason_code == RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
        assert workspace.block_resume_phase == MONITOR_RUNTIME_SETUP_REPLAY_RESUME_PHASE
        failed = [
            event for event in workspace.events if event.new_state == WorkspaceStatus.failed.value
        ]
        replay_failed = [
            event
            for event in workspace.events
            if event.event_type == RUNTIME_SETUP_REPLAY_FAILED_EVENT_TYPE
        ]
    assert failed == []
    assert [event.reason_code for event in replay_failed] == [
        RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
    ]
    payload = replay_failed[0].payload
    assert payload is not None
    return payload


def _provisioned_container() -> FakeAgentContainer:
    container = FakeAgentContainer()
    container.tmp.add(TOOL_PATH)
    return container


@pytest.mark.unit
async def test_recreated_agent_service_replays_setup_before_agent_retry(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    adapter = _idle_timeout_adapter()
    validation = _OrderedValidation(container, adapter)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        validation=validation,
    )
    up = _patch_compose(mocker, container, recreate=True)

    result = await _run(runner, workspace_id, tmp_path)

    assert result.stdout == "AWF-VERDICT: FIXED: retried"
    up.assert_awaited_once()
    assert up.await_args.kwargs["force_recreate"] is True
    assert validation.phase_calls == [("setup", "pre_agent")]
    # Setup ran after the failed run and before the retried one.
    assert validation.agent_calls_at_setup == [1]
    assert len(adapter.calls) == 2
    assert TOOL_PATH in container.tmp
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        replayed = [
            event
            for event in workspace.events
            if event.event_type == RUNTIME_SETUP_REPLAYED_EVENT_TYPE
        ]
    assert len(replayed) == 1
    assert replayed[0].payload is not None
    assert replayed[0].payload["trigger"] == "monitor_agent_service_recovery"


@pytest.mark.unit
async def test_unchanged_agent_identity_does_not_replay_setup(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    adapter = _idle_timeout_adapter()
    validation = _OrderedValidation(container, adapter)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        validation=validation,
    )
    _patch_compose(mocker, container, recreate=False)

    await _run(runner, workspace_id, tmp_path)

    assert validation.phase_calls == []
    assert len(adapter.calls) == 2


@pytest.mark.unit
async def test_runner_without_validation_fails_closed_when_agent_recreated(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    adapter = _idle_timeout_adapter(retry_stdout=None)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    _patch_compose(mocker, container, recreate=True)

    with pytest.raises(_MonitorAgentServiceRecoveryFailedError) as raised:
        await _run(runner, workspace_id, tmp_path)

    assert raised.value.reason_code == RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
    assert raised.value.details is not None
    assert raised.value.details["trigger"] == "monitor_agent_service_recovery"
    assert raised.value.details["restart_attempts"] == 1
    # The agent is never retried in a runtime whose setup was not replayed.
    assert len(adapter.calls) == 1
    payload = await _assert_parked_blocked(factory, workspace_id)
    assert payload["failure_reason"] == FailureReason.infrastructure_failure.value


@pytest.mark.unit
async def test_runner_without_validation_retries_when_agent_unchanged(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    adapter = _idle_timeout_adapter()
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    _patch_compose(mocker, container, recreate=False)

    result = await _run(runner, workspace_id, tmp_path)

    assert result.stdout == "AWF-VERDICT: FIXED: retried"
    assert len(adapter.calls) == 2


@pytest.mark.unit
async def test_failed_replay_parks_monitor_blocked_with_reason_code(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    adapter = _idle_timeout_adapter(retry_stdout=None)
    validation = _OrderedValidation(container, adapter, setup_returncode=1)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        validation=validation,
    )
    up = _patch_compose(mocker, container, recreate=True)

    with pytest.raises(_MonitorAgentServiceRecoveryFailedError) as raised:
        await _run(runner, workspace_id, tmp_path)

    assert raised.value.reason_code == RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
    assert raised.value.details is not None
    assert raised.value.details["trigger"] == "monitor_agent_service_recovery"
    assert raised.value.details["command"].startswith("install-tool")
    # Operation handlers persist exc.details as the agent-service recovery payload.
    assert raised.value.details["restart_attempts"] == 1
    assert "source_reason_code" in raised.value.details
    assert "service_healthy" in raised.value.details
    up.assert_awaited_once()
    assert len(adapter.calls) == 1
    assert validation.setup_runs() == 1
    payload = await _assert_parked_blocked(factory, workspace_id)
    assert payload["details"]["agent_service_recovery"]["restart_attempts"] == 1
    assert payload["details"]["command"].startswith("install-tool")


@pytest.mark.unit
async def test_unresolvable_profile_parks_monitor_with_replay_reason_code(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    adapter = _idle_timeout_adapter(retry_stdout=None)
    validation = _OrderedValidation(container, adapter)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        validation=validation,
    )
    _patch_compose(mocker, container, recreate=True)
    mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_setup_replay._profile_for_workspace",
        side_effect=ProfileResolutionError("unknown workspace profile_ref: gone"),
    )

    with pytest.raises(_MonitorAgentServiceRecoveryFailedError) as raised:
        await _run(runner, workspace_id, tmp_path)

    assert raised.value.reason_code == RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
    assert raised.value.details is not None
    assert raised.value.details["trigger"] == "monitor_agent_service_recovery"
    assert validation.phase_calls == []
    assert len(adapter.calls) == 1
    payload = await _assert_parked_blocked(factory, workspace_id)
    assert payload["failure_reason"] == FailureReason.profile_resolution_failure.value


@pytest.mark.unit
@pytest.mark.parametrize("head_recovered", [True, False])
async def test_replay_cleanup_failure_runs_monitor_head_recovery(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
    head_recovered: bool,
) -> None:
    workspace_id = await seed_monitoring_workspace(factory)
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    adapter = _idle_timeout_adapter(retry_stdout=None)
    validation = _OrderedValidation(
        container,
        adapter,
        setup_error=ComposeExecCleanupError(
            invocation_id="inv", source="validation", label="setup", message="stuck"
        ),
    )
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        validation=validation,
    )
    _patch_compose(mocker, container, recreate=True)
    repair = mocker.patch(
        "awf.runtime.pr_monitor_runner.agent_service_recovery."
        "_repair_monitor_git_after_recoverable_agent_cleanup_failure",
        side_effect=None
        if head_recovered
        else _MonitorHeadObjectMissingError("HEAD_OBJECT_MISSING_UNRECOVERABLE", "gone"),
    )

    with pytest.raises(_MonitorAgentServiceRecoveryFailedError) as raised:
        await _run(runner, workspace_id, tmp_path)

    repair.assert_awaited_once()
    assert repair.await_args.kwargs["workspace_id"] == workspace_id
    assert raised.value.details is not None
    assert raised.value.details["head_recovered"] is head_recovered
    assert len(adapter.calls) == 1


@pytest.mark.unit
async def test_failed_replay_does_not_park_when_monitor_claim_moved(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The park is fenced by ``monitor_owner_id``: a superseded monitor keeps its hands off."""
    workspace_id = await seed_monitoring_workspace(factory)
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.monitor_claimed_by = "worker-old"
        await session.commit()
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    adapter = _idle_timeout_adapter(retry_stdout=None)
    validation = _OrderedValidation(container, adapter, setup_returncode=1)
    runner = make_runner(
        factory=factory,
        cmd=FakeCommandRunner(),
        adapter=adapter,
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
        validation=validation,
    )
    runner._monitor_owner_id = "worker-old"
    _patch_compose(mocker, container, recreate=True)
    original_run = validation.run_profile_phases

    async def _setup_outlives_lease(**kwargs: Any) -> Any:
        async with factory() as session:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            assert workspace is not None
            workspace.monitor_claimed_by = "worker-new"
            await session.commit()
        return await original_run(**kwargs)

    monkeypatch.setattr(validation, "run_profile_phases", _setup_outlives_lease)

    with pytest.raises(_MonitorAgentServiceRecoverySupersededError) as raised:
        await _run(runner, workspace_id, tmp_path)

    assert raised.value.reason_code == RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
    async with factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        assert workspace.status == WorkspaceStatus.monitoring_pr.value
        assert workspace.monitor_claimed_by == "worker-new"
        assert not [
            event
            for event in workspace.events
            if event.event_type == RUNTIME_SETUP_REPLAY_FAILED_EVENT_TYPE
        ]
