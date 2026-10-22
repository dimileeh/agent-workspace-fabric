"""Monitor resume replays profile setup when the runtime was recreated (#787, #1006).

Restart recovery, remonitor and guide-unblock all resume through
``resume_pr_monitor_handoff``, which brings the runtime back with
``compose up --force-recreate``. Setup that installed a tool under the container's
``/tmp`` must run again before the monitor's pre-push validation needs it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populate registry
from awf.common.commands import FakeCommandRunner
from awf.control.executor import runtime_setup_replay
from awf.control.executor.constants import (
    RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE,
    RUNTIME_SETUP_REPLAYED_EVENT_TYPE,
)
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from tests.unit.control.runtime_setup_replay_fakes import (
    TOOL_PATH,
    FakeAgentContainer,
    FakeCompose,
    FakeToolValidation,
    allow_ownership_repair,
    patch_identity,
)
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_005 import (
    _make_executor,
    factory,
    fake,
)
from tests.unit.control.test_executor_error_paths_parts.test_executor_error_paths_part_006 import (
    _seed_monitoring_pr,
)

_IMPORTED_FIXTURES = (factory, fake)
_SECRET = "ghp_" + "B" * 36


class ValidatingMonitor:
    """Monitor double whose loop runs the local pre-push validate phases."""

    def __init__(self, validation: FakeToolValidation) -> None:
        self.validation = validation
        self.validate_returncodes: list[int] = []

    async def run(self, *, workspace_id: str, **_kwargs: Any) -> None:
        self.validation.trace.append("monitor.run")
        result = await self.validation.run_profile_phases(
            workspace_id=workspace_id, phase_names=("post_agent", "validate")
        )
        self.validate_returncodes.extend(command.returncode for command in result.commands)


@pytest.fixture(autouse=True)
def _ownership_repair_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runtime_setup_replay, "repair_agent_runtime_ownership", allow_ownership_repair
    )


def _executor(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    *,
    container: FakeAgentContainer,
    validation: FakeToolValidation,
    monitor: ValidatingMonitor,
    recreate: bool = True,
) -> tuple[Any, FakeCompose]:
    compose = FakeCompose(container, recreate=recreate)
    executor = _make_executor(
        fake,
        factory,
        tmp_path,
        compose=compose,
        validation=validation,
        pr_monitor_factory=lambda *_a, **_k: monitor,
    )
    return executor, compose


async def _events(
    factory: async_sessionmaker[AsyncSession], workspace_id: str, event_type: str
) -> list[WorkspaceEvent]:
    async with factory() as session:
        rows = await session.execute(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.event_type == event_type,
            )
        )
        return list(rows.scalars())


async def _failed_transitions(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> list[WorkspaceEvent]:
    async with factory() as session:
        rows = await session.execute(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.event_type == "workspace.state_changed",
                WorkspaceEvent.new_state == WorkspaceStatus.failed.value,
            )
        )
        return list(rows.scalars())


def _provisioned_container() -> FakeAgentContainer:
    """A container whose initial handoff setup already installed the tool."""
    container = FakeAgentContainer()
    container.tmp.add(TOOL_PATH)
    return container


@pytest.mark.unit
async def test_restart_recovery_replays_setup_before_monitor_validation(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    validation = FakeToolValidation(container)
    monitor = ValidatingMonitor(validation)
    ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(tmp_path / "compose.yml"))
    executor, compose = _executor(
        fake, factory, tmp_path, container=container, validation=validation, monitor=monitor
    )

    await executor.resume_pr_monitor(ws_id)

    # Recreate flags are unchanged; the replay happens because identity changed.
    assert [call["force_recreate"] for call in compose.calls] == [True]
    assert validation.trace == [
        "phases:setup,pre_agent",
        "monitor.run",
        "phases:post_agent,validate",
    ]
    assert monitor.validate_returncodes == [0]
    events = await _events(factory, ws_id, RUNTIME_SETUP_REPLAYED_EVENT_TYPE)
    assert len(events) == 1
    assert events[0].payload is not None
    assert events[0].payload["trigger"] == "monitor_resume"
    assert events[0].payload["previous_container_id"] == "agent-0"
    assert events[0].payload["container_id"] == "agent-1"


@pytest.mark.unit
async def test_without_replay_recreated_runtime_reproduces_missing_tool(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduce #787: identity reported unchanged, yet ``up`` wiped ``/tmp`` -> 127."""
    container = _provisioned_container()
    stale = container.identity()

    async def _stale_identity(_project: str | None, **_kwargs: Any) -> Any:
        return stale

    monkeypatch.setattr(runtime_setup_replay, "capture_agent_container_identity", _stale_identity)
    validation = FakeToolValidation(container)
    monitor = ValidatingMonitor(validation)
    ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(tmp_path / "compose.yml"))
    executor, _compose = _executor(
        fake, factory, tmp_path, container=container, validation=validation, monitor=monitor
    )

    await executor.resume_pr_monitor(ws_id)

    assert validation.setup_runs() == 0
    assert monitor.validate_returncodes == [127]


@pytest.mark.unit
async def test_unchanged_identity_does_not_replay_setup(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    validation = FakeToolValidation(container)
    monitor = ValidatingMonitor(validation)
    ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(tmp_path / "compose.yml"))
    executor, compose = _executor(
        fake,
        factory,
        tmp_path,
        container=container,
        validation=validation,
        monitor=monitor,
        recreate=False,
    )

    await executor.resume_pr_monitor(ws_id)

    assert len(compose.calls) == 1
    assert validation.setup_runs() == 0
    assert validation.probe_calls == 0
    assert monitor.validate_returncodes == [0]
    assert await _events(factory, ws_id, RUNTIME_SETUP_REPLAYED_EVENT_TYPE) == []


@pytest.mark.unit
async def test_released_runtime_gets_setup_on_resume(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1006: the block released the runtime, so there is no agent before ``up``."""
    container = _provisioned_container()
    container.release()
    patch_identity(monkeypatch, container)
    validation = FakeToolValidation(container)
    monitor = ValidatingMonitor(validation)
    ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(tmp_path / "compose.yml"))
    executor, _compose = _executor(
        fake, factory, tmp_path, container=container, validation=validation, monitor=monitor
    )

    await executor.resume_pr_monitor(ws_id)

    assert validation.setup_runs() == 1
    assert monitor.validate_returncodes == [0]
    events = await _events(factory, ws_id, RUNTIME_SETUP_REPLAYED_EVENT_TYPE)
    assert [event.payload["basis"] for event in events if event.payload] == [
        "agent_container_created"
    ]


@pytest.mark.unit
async def test_failed_replay_fails_workspace_with_reason_code_and_no_retry(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    validation = FakeToolValidation(
        container, setup_returncode=1, setup_stdout=f"auth token={_SECRET} rejected"
    )
    monitor = ValidatingMonitor(validation)
    ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(tmp_path / "compose.yml"))
    executor, compose = _executor(
        fake, factory, tmp_path, container=container, validation=validation, monitor=monitor
    )

    handoff = await executor.resume_pr_monitor_handoff(ws_id)

    assert handoff is None
    assert len(compose.calls) == 1
    assert validation.setup_runs() == 1
    assert monitor.validate_returncodes == []
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.failed.value
        assert ws.failure_reason == FailureReason.service_startup_failure.value
        assert ws.failure_message is not None
        assert ws.failure_message.startswith("monitor recovery: runtime setup replay failed")
    transitions = await _failed_transitions(factory, ws_id)
    assert len(transitions) == 1
    assert transitions[0].reason_code == RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
    payload = transitions[0].payload
    assert payload is not None
    assert payload["details"]["trigger"] == "monitor_resume"
    assert "rejected" in payload["details"]["stdout_tail"]
    assert _SECRET not in repr(payload)
    assert await _events(factory, ws_id, RUNTIME_SETUP_REPLAYED_EVENT_TYPE) == []


@pytest.mark.unit
async def test_replay_failure_skips_mark_failed_when_resume_was_superseded(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    validation = FakeToolValidation(container, setup_returncode=1)
    monitor = ValidatingMonitor(validation)
    ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(tmp_path / "compose.yml"))
    executor, _compose = _executor(
        fake, factory, tmp_path, container=container, validation=validation, monitor=monitor
    )
    original_recheck = executor._recheck_status

    async def _recheck_status(workspace_id: str, *, action: str, **kwargs: Any) -> bool:
        if action == "resume_setup_replay_failed":
            return False
        return bool(await original_recheck(workspace_id, action=action, **kwargs))

    monkeypatch.setattr(executor, "_recheck_status", _recheck_status)

    assert await executor.resume_pr_monitor_handoff(ws_id) is None

    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value


@pytest.mark.unit
async def test_replay_is_skipped_when_status_changed_before_replay(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    validation = FakeToolValidation(container)
    monitor = ValidatingMonitor(validation)
    ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(tmp_path / "compose.yml"))
    executor, _compose = _executor(
        fake, factory, tmp_path, container=container, validation=validation, monitor=monitor
    )
    original_recheck = executor._recheck_status

    async def _recheck_status(workspace_id: str, *, action: str, **kwargs: Any) -> bool:
        if action == "resume_setup_replay":
            return False
        return bool(await original_recheck(workspace_id, action=action, **kwargs))

    monkeypatch.setattr(executor, "_recheck_status", _recheck_status)

    assert await executor.resume_pr_monitor_handoff(ws_id) is None
    assert validation.setup_runs() == 0


@pytest.mark.unit
async def test_replay_without_resolved_profile_fails_with_reason_code(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    validation = FakeToolValidation(container)
    monitor = ValidatingMonitor(validation)
    ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(tmp_path / "compose.yml"))
    executor, _compose = _executor(
        fake, factory, tmp_path, container=container, validation=validation, monitor=monitor
    )

    def _profile_unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("profile unavailable")

    monkeypatch.setattr(
        "awf.control.executor.monitor_handoff._profile_for_workspace", _profile_unavailable
    )

    assert await executor.resume_pr_monitor_handoff(ws_id) is None

    assert validation.setup_runs() == 0
    transitions = await _failed_transitions(factory, ws_id)
    assert [event.reason_code for event in transitions] == [RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE]
