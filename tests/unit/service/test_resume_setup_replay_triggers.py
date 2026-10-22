"""Remonitor and guide-unblock resume into a runtime that has setup replayed (#787, #1006).

Both operator triggers move the workspace back to ``monitoring_pr``; the worker
then resumes it through ``resume_pr_monitor_handoff``. The runtime was released
when the workspace failed or blocked, so the resume's ``compose up`` creates a
fresh agent container that never ran the profile's setup.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import FakeCommandRunner
from awf.control.executor import runtime_setup_replay
from awf.control.executor.constants import RUNTIME_SETUP_REPLAYED_EVENT_TYPE
from awf.db.enums import WorkspaceStatus
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
from tests.unit.control.test_executor_monitor_recovery_parts.test_monitor_resume_setup_replay import (  # noqa: E501
    ValidatingMonitor,
)
from tests.unit.service.test_controls_lifecycle_parts.controls_lifecycle_helpers import _service

_IMPORTED_FIXTURES = (factory, fake)


@pytest.fixture(autouse=True)
def _ownership_repair_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runtime_setup_replay, "repair_agent_runtime_ownership", allow_ownership_repair
    )


async def _seed(factory: async_sessionmaker[AsyncSession], tmp_path: Path) -> str:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    return await _seed_monitoring_pr(factory, compose_file_path=str(compose_file))


async def _resume_and_validate(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workspace_id: str,
    container: FakeAgentContainer,
) -> tuple[FakeToolValidation, ValidatingMonitor]:
    patch_identity(monkeypatch, container)
    validation = FakeToolValidation(container)
    monitor = ValidatingMonitor(validation)
    executor = _make_executor(
        fake,
        factory,
        tmp_path,
        compose=FakeCompose(container),
        validation=validation,
        pr_monitor_factory=lambda *_a, **_k: monitor,
    )
    await executor.resume_pr_monitor(workspace_id)
    return validation, monitor


async def _replay_events(
    factory: async_sessionmaker[AsyncSession], workspace_id: str
) -> list[WorkspaceEvent]:
    async with factory() as session:
        rows = await session.execute(
            select(WorkspaceEvent).where(
                WorkspaceEvent.workspace_id == workspace_id,
                WorkspaceEvent.event_type == RUNTIME_SETUP_REPLAYED_EVENT_TYPE,
            )
        )
        return list(rows.scalars())


@pytest.mark.unit
async def test_remonitor_resume_replays_setup_before_validate(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws_id = await _seed(factory, tmp_path)
    container = FakeAgentContainer()
    container.tmp.add(TOOL_PATH)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(ws_id)
        assert ws is not None
        await repo.transition(ws, to=WorkspaceStatus.failed, reason_code="COMMAND_FAILED")
        await session.commit()
    container.release()  # terminal runtime release on failure
    async with factory() as session:
        service, _stopper, _cleaner = _service(session)
        response = await service.remonitor_workspace(ws_id, reason="tool came back")
        await session.commit()
    assert response.status == WorkspaceStatus.monitoring_pr

    validation, monitor = await _resume_and_validate(
        fake, factory, tmp_path, monkeypatch, ws_id, container
    )

    assert validation.setup_runs() == 1
    assert monitor.validate_returncodes == [0]
    assert len(await _replay_events(factory, ws_id)) == 1


@pytest.mark.unit
async def test_guide_unblock_resume_replays_setup_before_validate(
    fake: FakeCommandRunner,
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1006: guide on a monitor-origin protected-scope block resumes into a fresh runtime."""
    ws_id = await _seed(factory, tmp_path)
    container = FakeAgentContainer()
    container.tmp.add(TOOL_PATH)
    async with factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(ws_id)
        assert ws is not None
        await repo.transition(
            ws, to=WorkspaceStatus.blocked, reason_code="QUALITY_GATE_POLICY_CHANGED"
        )
        ws.block_reason_code = "QUALITY_GATE_POLICY_CHANGED"
        ws.block_type = "protected_quality_gate"
        ws.block_epoch = 1
        ws.block_resume_phase = "monitor_protected_scope_push"
        ws.block_violations = [
            {"path": ".github/workflows/validate.yml", "reason": "protected path"}
        ]
        ws.monitor_last_commit_sha = "h" * 40
        await session.commit()
    container.release()  # the block released the runtime
    async with factory() as session:
        service, _stopper, _cleaner = _service(session)
        response = await service.guide_workspace(
            ws_id,
            directive="discard the protected edit and resolve the thread",
            reason="operator reviewed the protected-scope pause",
        )
        await session.commit()
    assert response.status == WorkspaceStatus.monitoring_pr

    validation, monitor = await _resume_and_validate(
        fake, factory, tmp_path, monkeypatch, ws_id, container
    )

    assert validation.setup_runs() == 1
    assert monitor.validate_returncodes == [0]
    events = await _replay_events(factory, ws_id)
    assert len(events) == 1
    assert events[0].payload is not None
    assert events[0].payload["basis"] == "agent_container_created"
