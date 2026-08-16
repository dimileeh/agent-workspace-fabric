"""ControlWorker stale active execution container runtime tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import ControlWorker, WorkerConfig
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceEventRepository, WorkspaceRepository
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
from tests.unit.control.test_worker_parts.test_worker_part_016 import (
    _create_active_execution,
    _RecordingExecutor,
    _RecordingRuntimeInspector,
    _TransitioningProvisioner,
)
from tests.unit.control.test_worker_parts.test_worker_part_016 import (
    origin_repo as origin_repo,
)
from tests.unit.control.test_worker_parts.test_worker_part_016 import (
    session_factory as session_factory,
)


class TestRunOnceStaleActiveExecutionRecoveryPart002:
    @pytest.mark.unit
    async def test_stale_running_with_missing_agent_container_fails_with_structured_reason(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "missing-agent",
            WorkspaceStatus.running,
            compose_project_name="awf_missing_agent",
        )
        inspector = _RecordingRuntimeInspector(
            {
                "awf_missing_agent": RuntimeSnapshot(
                    stack_state="running",
                    services=[
                        RuntimeService(
                            name="postgres",
                            container_id="pg",
                            image="postgres:16",
                            state="running",
                        )
                    ],
                )
            }
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=1),
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.failure_message is not None
            assert "AGENT_CONTAINER_MISSING" in ws.failure_message
            events = await WorkspaceEventRepository(s).list(workspace_id=workspace_id)
            runtime_events = [
                event
                for event in events
                if event.event_type == "workspace.runtime_stranded_detected"
            ]
            assert len(runtime_events) == 1
            assert runtime_events[0].reason_code == "AGENT_CONTAINER_MISSING"
            assert runtime_events[0].payload is not None
            assert runtime_events[0].payload["decision"] == "fail_workspace"
            assert runtime_events[0].payload["runtime"]["services"][0]["name"] == "postgres"

    @pytest.mark.unit
    async def test_stale_running_with_exited_agent_container_fails_with_structured_reason(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "exited-agent",
            WorkspaceStatus.running,
            compose_project_name="awf_exited_agent",
        )
        inspector = _RecordingRuntimeInspector(
            {
                "awf_exited_agent": RuntimeSnapshot(
                    stack_state="stopped",
                    services=[
                        RuntimeService(
                            name="agent",
                            container_id="agent",
                            image="awf-agent:latest",
                            state="exited",
                            status="Exited (1) 2 minutes ago",
                        )
                    ],
                )
            }
        )
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=inspector,
            config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=1),
        )

        assert await worker.run_once() == 0

        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.failure_message is not None
            assert "AGENT_CONTAINER_EXITED" in ws.failure_message
            events = await WorkspaceEventRepository(s).list(workspace_id=workspace_id)
            assert any(
                event.event_type == "workspace.runtime_stranded_detected"
                and event.reason_code == "AGENT_CONTAINER_EXITED"
                for event in events
            )
