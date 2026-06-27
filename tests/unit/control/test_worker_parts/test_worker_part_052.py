"""ControlWorker monitor recovery active-execution claim tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import ControlWorker, WorkerConfig
from awf.db.enums import OperationType, WorkspaceStatus
from awf.db.repositories import (
    OperationRepository,
    WorkerHeartbeatRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from tests.unit.control.test_worker_parts.test_worker_part_014 import (
    _create_monitoring_pr,
    _HealthyRuntimeInspector,
    _RecordingExecutor,
    _TransitioningProvisioner,
)
from tests.unit.control.test_worker_parts.test_worker_part_014 import (
    origin_repo as origin_repo,
)
from tests.unit.control.test_worker_parts.test_worker_part_014 import (
    session_factory as session_factory,
)


class TestRunOnceMonitorRecoveryActiveExecutionClaimPart002:
    @pytest.mark.unit
    async def test_restart_recovery_defers_when_unexpired_execution_claim_has_different_owner(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        execution_expires_at = datetime.now(UTC) + timedelta(minutes=10)
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "monitor-with-unexpired-execution-claim",
            task_policy={"scheduler": {"base_priority": 100}},
            pr_number=459,
        )
        eligible_monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "eligible-monitor-after-deferred-execution-claim",
            task_policy={"scheduler": {"base_priority": 1}},
            pr_number=460,
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            ws.execution_claimed_by = "live-execution-worker"
            ws.execution_claim_expires_at = execution_expires_at
            await WorkerHeartbeatRepository(s).record_heartbeat(
                worker_id="live-execution-worker",
                node_id="worker-node-a",
                started_at=execution_expires_at - timedelta(minutes=1),
                last_heartbeat_at=datetime.now(UTC),
                poll_interval_seconds=0.01,
            )
            await s.commit()

        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=1,
                node_id="worker-node-a",
            ),
        )

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        assert executor.calls == []
        assert executor.resume_calls == [eligible_monitor_id]
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.monitoring_pr.value
            assert ws.execution_claimed_by == "live-execution-worker"
            assert ws.execution_claim_expires_at is not None
            assert ws.execution_claim_expires_at.replace(tzinfo=UTC) == execution_expires_at
            assert ws.monitor_claimed_by is None
            assert ws.monitor_claim_expires_at is None
            operations = await OperationRepository(s).list_all(workspace_id=monitor_id)
            recovery_events = await WorkspaceEventRepository(s).list(
                workspace_id=monitor_id,
                event_type="workspace.monitor_recovery_started",
            )
            deferred_events = await WorkspaceEventRepository(s).list(
                workspace_id=monitor_id,
                event_type="workspace.monitor_recovery_deferred",
            )

        remonitor_operations = [
            operation for operation in operations if operation.type == OperationType.remonitor.value
        ]
        assert remonitor_operations == []
        assert recovery_events == []
        assert len(deferred_events) == 1
        assert deferred_events[0].reason_code == "MONITOR_RECOVERY_DEFERRED_ACTIVE_EXECUTION_CLAIM"
        assert deferred_events[0].payload is not None
        assert deferred_events[0].payload["execution_claim"] == {
            "action": "preserved_unexpired",
            "reason_code": "UNEXPIRED_EXECUTION_CLAIM_PRESERVED_DURING_MONITOR_RECOVERY",
            "previous_claimed_by": "live-execution-worker",
            "previous_expires_at": execution_expires_at.isoformat(),
        }

        refreshed_execution_expires_at = execution_expires_at + timedelta(minutes=5)
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            ws.execution_claim_expires_at = refreshed_execution_expires_at
            await s.commit()

        await worker.run_once()
        await worker.wait_for_execution_tasks()
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            assert ws.execution_claim_expires_at is not None
            assert ws.execution_claim_expires_at.replace(tzinfo=UTC) == (
                refreshed_execution_expires_at
            )
            deferred_events = await WorkspaceEventRepository(s).list(
                workspace_id=monitor_id,
                event_type="workspace.monitor_recovery_deferred",
            )
        assert len(deferred_events) == 1

    @pytest.mark.unit
    async def test_restart_recovery_same_owner_unexpired_execution_claim_can_resume(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        execution_expires_at = datetime.now(UTC) + timedelta(minutes=10)
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "monitor-with-same-owner-execution-claim",
            pr_number=461,
        )

        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=1,
                node_id="worker-node-a",
            ),
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            ws.execution_claimed_by = worker._worker_id
            ws.execution_claim_expires_at = execution_expires_at
            await s.commit()

        assert await worker.run_once() == 1
        await worker.wait_for_execution_tasks()

        assert executor.resume_calls == [monitor_id]
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            assert ws.execution_claimed_by == worker._worker_id
            assert ws.execution_claim_expires_at is not None
            assert ws.execution_claim_expires_at.replace(tzinfo=UTC) == execution_expires_at
            recovery_events = await WorkspaceEventRepository(s).list(
                workspace_id=monitor_id,
                event_type="workspace.monitor_recovery_started",
            )
            deferred_events = await WorkspaceEventRepository(s).list(
                workspace_id=monitor_id,
                event_type="workspace.monitor_recovery_deferred",
            )

        assert len(recovery_events) == 1
        assert deferred_events == []
