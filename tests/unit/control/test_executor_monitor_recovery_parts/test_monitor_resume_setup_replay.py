"""Monitor resume replays profile setup when the runtime was recreated (#787, #1006).

Restart recovery, remonitor and guide-unblock all resume through
``resume_pr_monitor_handoff``, which brings the runtime back with
``compose up --force-recreate``. Setup that installed a tool under the container's
``/tmp`` must run again before the monitor's pre-push validation needs it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters import registry as _registry  # noqa: F401 — populate registry
from awf.common.commands import FakeCommandRunner
from awf.common.compose_exec import ComposeExecCleanupError
from awf.control.blocked_transition import is_monitor_origin_block_resume_phase
from awf.control.executor import (
    monitor_handoff,
    monitor_resume_setup_replay,
    runtime_setup_replay,
)
from awf.control.executor.constants import (
    MONITOR_RUNTIME_SETUP_REPLAY_RESUME_PHASE,
    RUNTIME_SETUP_REPLAY_FAILED_EVENT_TYPE,
    RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE,
    RUNTIME_SETUP_REPLAYED_EVENT_TYPE,
)
from awf.control.executor.state_ops import RESUME_SETUP_FAILURE_BLOCK_TYPE
from awf.control.worker.dispatch_methods import _monitor_recovery_handoff_failure_error
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.models import WorkspaceEvent
from awf.db.repositories import WorkspaceRepository
from awf.node.compose_manager import ComposeOperationError
from awf.profiles.resolver import ProfileResolutionError
from awf.runtime.worktree_writer_lock import hold_exclusive_worktree_writer_lock
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
async def test_restart_recovery_brings_up_only_dependencies_before_agent_recreate(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    validation = FakeToolValidation(container)
    monitor = ValidatingMonitor(validation)
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        "services:\n"
        "  agent:\n"
        "    image: agent\n"
        "  postgres:\n"
        "    image: postgres\n"
        "  debug:\n"
        "    image: debug\n"
        "    profiles: [debug]\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("COMPOSE_PROFILES", raising=False)
    ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(compose_file))
    executor, compose = _executor(
        fake, factory, tmp_path, container=container, validation=validation, monitor=monitor
    )

    await executor.resume_pr_monitor(ws_id)

    assert [
        (call.get("no_recreate", False), call.get("force_recreate", False), call.get("services"))
        for call in compose.calls
    ] == [(True, False, ("postgres",)), (False, True, ("agent",))]


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

    # Only the agent is recreated (#936); the replay happens because identity changed.
    assert [
        (call.get("no_recreate", False), call.get("force_recreate", False), call.get("services"))
        for call in compose.calls
    ] == [(True, False, None), (False, True, ("agent",))]
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
async def test_resume_restart_and_replay_wait_for_worktree_writer_lock(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reclaimed monitor's agent-service recovery replays setup under the writer
    lock; resume must not restart the runtime or replay setup underneath it."""
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    validation = FakeToolValidation(container)
    monitor = ValidatingMonitor(validation)
    ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(tmp_path / "compose.yml"))
    executor, compose = _executor(
        fake, factory, tmp_path, container=container, validation=validation, monitor=monitor
    )
    held = asyncio.Event()
    release = asyncio.Event()

    async def _old_monitor_recovery() -> None:
        async with hold_exclusive_worktree_writer_lock(executor._config.worktrees_root / ws_id):
            held.set()
            await release.wait()

    holder = asyncio.create_task(_old_monitor_recovery())
    await held.wait()
    resume = asyncio.create_task(executor.resume_pr_monitor(ws_id))
    await asyncio.sleep(0.3)

    assert compose.calls == []
    assert validation.setup_runs() == 0
    release.set()
    await holder
    await resume
    assert len(compose.calls) == 2  # dependencies, then the agent (#936)
    assert validation.setup_runs() == 1


@pytest.mark.unit
async def test_resume_rechecks_monitor_claim_after_acquiring_worktree_writer_lock(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A monitor claim reclaimed while resume waits for the writer lock must stop the
    superseded resume before it force-recreates the new owner's runtime."""
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    validation = FakeToolValidation(container)
    monitor = ValidatingMonitor(validation)
    ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(tmp_path / "compose.yml"))
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        ws.monitor_claimed_by = "worker-old"
        await session.commit()
    executor, compose = _executor(
        fake, factory, tmp_path, container=container, validation=validation, monitor=monitor
    )
    held = asyncio.Event()
    release = asyncio.Event()

    async def _old_monitor_recovery() -> None:
        async with hold_exclusive_worktree_writer_lock(executor._config.worktrees_root / ws_id):
            held.set()
            await release.wait()

    holder = asyncio.create_task(_old_monitor_recovery())
    await held.wait()
    resume = asyncio.create_task(executor.resume_pr_monitor_handoff(ws_id))
    await asyncio.sleep(0.3)
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        ws.monitor_claimed_by = "worker-new"
        await session.commit()
    release.set()
    await holder

    assert await resume is None
    assert compose.calls == []
    assert validation.setup_runs() == 0


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

    assert len(compose.calls) == 2  # dependencies, then the agent (#936)
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


async def _replay_failed_events(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> list[WorkspaceEvent]:
    return await _events(factory, workspace_id, RUNTIME_SETUP_REPLAY_FAILED_EVENT_TYPE)


async def _assert_parked_blocked(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> WorkspaceEvent:
    """A failed monitor-side replay parks ``blocked`` (#743) instead of failing."""
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.blocked.value
        assert ws.block_type == RESUME_SETUP_FAILURE_BLOCK_TYPE
        assert ws.block_reason_code == RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
        assert ws.block_resume_phase == MONITOR_RUNTIME_SETUP_REPLAY_RESUME_PHASE
        assert is_monitor_origin_block_resume_phase(ws.block_resume_phase)
        assert ws.failure_reason is None
    assert await _failed_transitions(factory, workspace_id) == []
    events = await _replay_failed_events(factory, workspace_id)
    assert len(events) == 1
    assert events[0].reason_code == RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
    return events[0]


@pytest.mark.unit
async def test_failed_replay_parks_monitor_blocked_and_preserves_worktree(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient setup failure (registry 429) must not kill a healthy PR monitor."""
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    validation = FakeToolValidation(
        container, setup_returncode=1, setup_stdout=f"429 Too Many Requests token={_SECRET}"
    )
    monitor = ValidatingMonitor(validation)
    ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(tmp_path / "compose.yml"))
    executor, compose = _executor(
        fake, factory, tmp_path, container=container, validation=validation, monitor=monitor
    )
    worktree = executor._config.worktrees_root / ws_id
    worktree.mkdir(parents=True)
    (worktree / "unpushed.txt").write_text("keep me\n", encoding="utf-8")

    handoff = await executor.resume_pr_monitor_handoff(ws_id)

    assert handoff is None
    assert len(compose.calls) == 2  # dependencies, then the agent (#936)
    assert validation.setup_runs() == 1
    assert monitor.validate_returncodes == []
    event = await _assert_parked_blocked(factory, ws_id)
    payload = event.payload
    assert payload is not None
    assert payload["message"].startswith("monitor recovery: runtime setup replay failed")
    assert payload["failure_reason"] == FailureReason.service_startup_failure.value
    assert payload["details"]["trigger"] == "monitor_resume"
    assert "429 Too Many Requests" in payload["details"]["stdout_tail"]
    assert _SECRET not in repr(payload)
    assert (worktree / "unpushed.txt").read_text(encoding="utf-8") == "keep me\n"
    assert await _events(factory, ws_id, RUNTIME_SETUP_REPLAYED_EVENT_TYPE) == []
    error_code, message = await _monitor_recovery_handoff_failure_error(
        SimpleNamespace(_session_factory=factory), ws_id
    )
    assert error_code == RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
    assert message.startswith("monitor recovery: runtime setup replay failed")


@pytest.mark.unit
async def test_failed_replay_does_not_park_when_monitor_claim_moved(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The park is fenced by ``monitor_owner_id``: a newer claimant keeps the row."""
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    validation = FakeToolValidation(container, setup_returncode=1)
    monitor = ValidatingMonitor(validation)
    ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(tmp_path / "compose.yml"))
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        ws.monitor_claimed_by = "worker-old"
        await session.commit()
    executor, _compose = _executor(
        fake, factory, tmp_path, container=container, validation=validation, monitor=monitor
    )
    original_run = validation.run_profile_phases

    async def _setup_outlives_lease(**kwargs: Any) -> Any:
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None
            ws.monitor_claimed_by = "worker-new"
            await session.commit()
        return await original_run(**kwargs)

    async def _recheck_passes(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(validation, "run_profile_phases", _setup_outlives_lease)
    monkeypatch.setattr(executor, "_recheck_status", _recheck_passes)

    assert await executor.resume_pr_monitor_handoff(ws_id) is None

    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        assert ws.monitor_claimed_by == "worker-new"
    assert await _replay_failed_events(factory, ws_id) == []


@pytest.mark.unit
async def test_successful_replay_rechecks_monitor_claim_before_monitor_build(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup that outlives the lease must stop the superseded resume before its
    monitor-build failure paths can fail the newer claimant's row."""
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    validation = FakeToolValidation(container)
    ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(tmp_path / "compose.yml"))
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        ws.monitor_claimed_by = "worker-old"
        await session.commit()
    factory_calls: list[Any] = []

    def _failing_monitor_factory(*args: Any, **kwargs: Any) -> Any:
        factory_calls.append((args, kwargs))
        raise RuntimeError("monitor build failed")

    executor = _make_executor(
        fake,
        factory,
        tmp_path,
        compose=FakeCompose(container, recreate=True),
        validation=validation,
        pr_monitor_factory=_failing_monitor_factory,
    )
    original_run = validation.run_profile_phases

    async def _setup_outlives_lease(**kwargs: Any) -> Any:
        result = await original_run(**kwargs)
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None
            ws.monitor_claimed_by = "worker-new"
            await session.commit()
        return result

    monkeypatch.setattr(validation, "run_profile_phases", _setup_outlives_lease)

    assert await executor.resume_pr_monitor_handoff(ws_id) is None

    assert validation.setup_runs() == 1
    assert factory_calls == []
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        assert ws.monitor_claimed_by == "worker-new"
    assert await _failed_transitions(factory, ws_id) == []


@pytest.mark.unit
async def test_unchanged_identity_rechecks_monitor_claim_before_monitor_build(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed-but-usable restart that kept the container must still stop a
    superseded resume before its monitor-build failure paths fail the row."""
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    validation = FakeToolValidation(container)
    ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(tmp_path / "compose.yml"))
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        ws.monitor_claimed_by = "worker-old"
        await session.commit()
    factory_calls: list[Any] = []

    def _failing_monitor_factory(*args: Any, **kwargs: Any) -> Any:
        factory_calls.append((args, kwargs))
        raise RuntimeError("monitor build failed")

    compose = FakeCompose(container, recreate=False)
    executor = _make_executor(
        fake,
        factory,
        tmp_path,
        compose=compose,
        validation=validation,
        pr_monitor_factory=_failing_monitor_factory,
    )

    async def _up_fails_while_lease_moves(**kwargs: Any) -> None:
        compose.calls.append(kwargs)
        async with factory() as session:
            ws = await WorkspaceRepository(session).get(ws_id)
            assert ws is not None
            ws.monitor_claimed_by = "worker-new"
            await session.commit()
        raise ComposeOperationError(
            operation="up", returncode=1, stdout="", stderr="companion unhealthy"
        )

    async def _runtime_usable(*_args: Any) -> bool:
        return True

    monkeypatch.setattr(compose, "ensure_project_up", _up_fails_while_lease_moves)
    monkeypatch.setattr(
        monitor_handoff, "_compose_runtime_usable_after_restart_failure", _runtime_usable
    )

    assert await executor.resume_pr_monitor_handoff(ws_id) is None

    assert validation.setup_runs() == 0
    assert factory_calls == []
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(ws_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.monitoring_pr.value
        assert ws.monitor_claimed_by == "worker-new"
    assert await _failed_transitions(factory, ws_id) == []


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
@pytest.mark.parametrize(
    "resolve_error",
    [ValueError("profile unavailable"), ProfileResolutionError("unknown workspace profile_ref")],
)
async def test_replay_without_resolved_profile_fails_with_reason_code(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resolve_error: Exception,
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
        raise resolve_error

    for module in ("monitor_handoff", "monitor_resume_setup_replay"):
        monkeypatch.setattr(
            f"awf.control.executor.{module}._profile_for_workspace", _profile_unavailable
        )

    assert await executor.resume_pr_monitor_handoff(ws_id) is None

    assert validation.setup_runs() == 0
    event = await _assert_parked_blocked(factory, ws_id)
    assert event.payload is not None
    assert event.payload["failure_reason"] == FailureReason.profile_resolution_failure.value


@pytest.mark.unit
async def test_replay_re_resolves_profile_when_resume_sync_failed(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient profile-sync failure earlier in the resume must not block the replay."""
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    validation = FakeToolValidation(container)
    monitor = ValidatingMonitor(validation)
    ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(tmp_path / "compose.yml"))
    executor, _compose = _executor(
        fake, factory, tmp_path, container=container, validation=validation, monitor=monitor
    )
    original_sync = monitor_handoff._sync_resolved_profile
    calls = {"n": 0}

    async def _sync_fails_once(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient sync failure")
        return await original_sync(*args, **kwargs)

    monkeypatch.setattr(monitor_handoff, "_sync_resolved_profile", _sync_fails_once)

    await executor.resume_pr_monitor(ws_id)

    assert validation.setup_runs() == 1
    assert monitor.validate_returncodes == [0]
    assert await _failed_transitions(factory, ws_id) == []


@pytest.mark.unit
async def test_recovery_operation_reports_replay_failure_after_usable_compose_failure(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker must attribute the recovery failure to the replay, not to compose."""
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    validation = FakeToolValidation(container, setup_returncode=1)
    monitor = ValidatingMonitor(validation)
    ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(tmp_path / "compose.yml"))
    executor, compose = _executor(
        fake, factory, tmp_path, container=container, validation=validation, monitor=monitor
    )

    async def _up_partially_fails(**kwargs: Any) -> None:
        compose.calls.append(kwargs)
        container.recreate()
        raise ComposeOperationError(
            operation="up", returncode=1, stdout="", stderr="companion unhealthy"
        )

    async def _runtime_usable(*_args: Any) -> bool:
        return True

    monkeypatch.setattr(compose, "ensure_project_up", _up_partially_fails)
    monkeypatch.setattr(
        monitor_handoff, "_compose_runtime_usable_after_restart_failure", _runtime_usable
    )

    assert await executor.resume_pr_monitor_handoff(ws_id) is None

    error_code, message = await _monitor_recovery_handoff_failure_error(
        SimpleNamespace(_session_factory=factory), ws_id
    )
    assert error_code == RUNTIME_SETUP_REPLAY_FAILED_REASON_CODE
    assert "runtime setup replay failed" in message


def _cleanup_failure() -> ComposeExecCleanupError:
    return ComposeExecCleanupError(
        invocation_id="inv", source="validation", label="setup", message="stuck"
    )


async def _set_monitor_last_commit(
    factory: async_sessionmaker[AsyncSession], workspace_id: str, sha: str
) -> None:
    async with factory() as session:
        ws = await WorkspaceRepository(session).get(workspace_id)
        assert ws is not None
        ws.monitor_last_commit_sha = sha
        await session.commit()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("monitor_last_commit_sha", "expected_anchor"), [("c" * 40, "c" * 40), (None, None)]
)
async def test_replay_cleanup_failure_recovers_missing_head_onto_pr_head(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    monitor_last_commit_sha: str | None,
    expected_anchor: str | None,
) -> None:
    """HEAD recovery must rebuild on the pushed PR head, never flatten onto base_commit."""
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    validation = FakeToolValidation(container, setup_error=_cleanup_failure())
    monitor = ValidatingMonitor(validation)
    ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(tmp_path / "compose.yml"))
    if monitor_last_commit_sha is not None:
        await _set_monitor_last_commit(factory, ws_id, monitor_last_commit_sha)
    executor, _compose = _executor(
        fake, factory, tmp_path, container=container, validation=validation, monitor=monitor
    )
    recover = AsyncMock(return_value=True)
    monkeypatch.setattr(executor, "_recover_missing_git_head_or_mark_failed", recover)

    async def _head_missing(_worktree_path: Path) -> bool:
        return False

    monkeypatch.setattr(monitor_resume_setup_replay, "verify_head_object_exists", _head_missing)

    assert await executor.resume_pr_monitor_handoff(ws_id) is None

    if expected_anchor is None:
        recover.assert_not_awaited()
        return
    recover.assert_awaited_once()
    kwargs = recover.await_args.kwargs
    assert kwargs["base_commit"] == expected_anchor
    assert kwargs["branch_name"] == "awf/x"
    assert kwargs["from_status"] == WorkspaceStatus.monitoring_pr
    assert kwargs["stage"] == "runtime_setup_replay_cleanup_failure"
    assert kwargs["mark_failed_on_failure"] is False


@pytest.mark.unit
async def test_replay_cleanup_failure_skips_head_recovery_when_head_present(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _provisioned_container()
    patch_identity(monkeypatch, container)
    validation = FakeToolValidation(container, setup_error=_cleanup_failure())
    monitor = ValidatingMonitor(validation)
    ws_id = await _seed_monitoring_pr(factory, compose_file_path=str(tmp_path / "compose.yml"))
    executor, _compose = _executor(
        fake, factory, tmp_path, container=container, validation=validation, monitor=monitor
    )
    recover = AsyncMock(return_value=True)
    monkeypatch.setattr(executor, "_recover_missing_git_head_or_mark_failed", recover)

    async def _head_present(_worktree_path: Path) -> bool:
        return True

    monkeypatch.setattr(monitor_resume_setup_replay, "verify_head_object_exists", _head_present)

    assert await executor.resume_pr_monitor_handoff(ws_id) is None
    recover.assert_not_awaited()
