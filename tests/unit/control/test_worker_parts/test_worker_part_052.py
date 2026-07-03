"""ControlWorker monitor recovery active-execution claim tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import ControlWorker, WorkerConfig
from awf.db.enums import OperationStatus, OperationType, WorkspaceStatus
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
    async def test_deferred_active_execution_claim_recorder_ignores_non_positive_limit(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        """Skip deferred-claim recording when the caller passes a non-positive limit."""
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "monitor-deferred-recorder-limit-zero",
            pr_number=458,
        )
        async with session_factory() as s:
            ws = await WorkspaceRepository(s).get(monitor_id)
            assert ws is not None
            ws.execution_claimed_by = "live-execution-worker"
            ws.execution_claim_expires_at = datetime.now(UTC) + timedelta(minutes=10)
            await WorkerHeartbeatRepository(s).record_heartbeat(
                worker_id="live-execution-worker",
                node_id="worker-node-a",
                started_at=datetime.now(UTC) - timedelta(minutes=1),
                last_heartbeat_at=datetime.now(UTC),
                poll_interval_seconds=0.01,
            )
            await s.commit()

        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=1,
                node_id="worker-node-a",
            ),
        )

        await worker._record_monitor_recovery_deferred_active_execution_claims(limit=0)  # noqa: SLF001

        async with session_factory() as s:
            deferred_events = await WorkspaceEventRepository(s).list(
                workspace_id=monitor_id,
                event_type="workspace.monitor_recovery_deferred",
            )
        assert deferred_events == []

    @pytest.mark.unit
    async def test_monitor_claim_returns_false_when_workspace_disappears_after_lost_claim(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Return False when monitor claim loss is followed by a missing workspace row."""
        monitor_id = await _create_monitoring_pr(
            session_factory,
            origin_repo,
            "monitor-disappears-after-lost-claim",
            pr_number=458,
        )
        original_get = WorkspaceRepository.get
        get_calls = 0

        async def get_once_then_missing(
            repo: WorkspaceRepository,
            workspace_id: str,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            """Return the workspace once, then simulate a concurrent delete."""
            nonlocal get_calls
            get_calls += 1
            if get_calls == 1:
                return await original_get(repo, workspace_id, *args, **kwargs)
            return None

        async def lose_monitor_claim(*args: Any, **kwargs: Any) -> bool:
            """Simulate losing the monitor claim CAS."""
            return False

        monkeypatch.setattr(WorkspaceRepository, "get", get_once_then_missing)
        monkeypatch.setattr(WorkspaceRepository, "claim_monitoring_pr", lose_monitor_claim)

        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=_RecordingExecutor(),
            runtime_inspector=_HealthyRuntimeInspector(),
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=1,
                node_id="worker-node-a",
            ),
        )

        claimed = await worker._claim_monitoring_pr(monitor_id)  # noqa: SLF001

        assert claimed is False
        assert get_calls == 2

    @pytest.mark.unit
    async def test_restart_recovery_defers_when_unexpired_execution_claim_has_different_owner(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        """Defer monitor recovery while another worker holds an unexpired execution claim."""
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
            await WorkspaceRepository(s).add_event(
                ws,
                event_type="workspace.monitor_recovery_deferred",
                reason_code="MONITOR_RECOVERY_DEFERRED_ACTIVE_EXECUTION_CLAIM",
                payload={
                    "execution_claim": "malformed prior payload",
                    "reason_code": "MONITOR_RECOVERY_DEFERRED_ACTIVE_EXECUTION_CLAIM",
                },
            )
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
        assert len(deferred_events) == 2
        malformed_event = next(
            event
            for event in deferred_events
            if event.payload is not None
            and event.payload.get("execution_claim") == "malformed prior payload"
        )
        valid_event = next(
            event
            for event in deferred_events
            if event.payload is not None and isinstance(event.payload.get("execution_claim"), dict)
        )
        assert malformed_event.reason_code == "MONITOR_RECOVERY_DEFERRED_ACTIVE_EXECUTION_CLAIM"
        assert valid_event.reason_code == "MONITOR_RECOVERY_DEFERRED_ACTIVE_EXECUTION_CLAIM"
        assert valid_event.payload is not None
        assert valid_event.payload["execution_claim"] == {
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
        assert len(deferred_events) == 2

    @pytest.mark.unit
    async def test_restart_recovery_same_owner_unexpired_execution_claim_can_resume(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        """Allow monitor recovery when this worker already owns the execution claim."""
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


@pytest.mark.unit
async def test_claim_monitoring_pr_reuses_pending_recovery_operation_without_duplicate(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    """Verify claim monitoring pr reuses pending recovery operation without duplicate."""
    monitor_id = await _create_monitoring_pr(
        session_factory,
        origin_repo,
        "monitor-pending-recovery-reclaim",
        pr_number=460,
    )
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
        executor=_RecordingExecutor(),
        runtime_inspector=_HealthyRuntimeInspector(),
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_executions=1,
            node_id="worker-node-a",
        ),
    )

    assert await worker._claim_monitoring_pr(monitor_id) is True  # noqa: SLF001
    first_operation_id = worker._monitor_recovery_operation_ids[monitor_id]  # noqa: SLF001

    async with session_factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=monitor_id)
    remonitor_operations = [
        operation for operation in operations if operation.type == OperationType.remonitor.value
    ]
    assert len(remonitor_operations) == 1

    assert await worker._claim_monitoring_pr(monitor_id) is True  # noqa: SLF001
    assert worker._monitor_recovery_operation_ids[monitor_id] == first_operation_id  # noqa: SLF001

    async with session_factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=monitor_id)
    remonitor_operations = [
        operation for operation in operations if operation.type == OperationType.remonitor.value
    ]
    assert len(remonitor_operations) == 1
    assert remonitor_operations[0].id == first_operation_id
    assert remonitor_operations[0].status == OperationStatus.running.value


@pytest.mark.unit
async def test_claim_monitoring_pr_reuses_active_salvage_recovery_registers_cooldown_tracking(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    """Reused in-flight active-salvage remonitor ops must register salvage cooldown tracking."""
    monitor_id = await _create_monitoring_pr(
        session_factory,
        origin_repo,
        "monitor-reuse-active-salvage-recovery",
        pr_number=463,
    )
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(monitor_id)
        assert ws is not None
        await repo.add_event(
            ws,
            event_type="workspace.active_execution_salvage_monitor_attached",
            reason_code="ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED",
            payload={
                "source": "worker_restart",
                "reason_code": "ACTIVE_EXECUTION_SALVAGE_MONITOR_ATTACHED",
                "workspace_status": WorkspaceStatus.monitoring_pr.value,
                "decision": "attach_pr_monitor",
            },
        )
        await session.commit()

    worker_a = ControlWorker(
        session_factory=session_factory,
        provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
        executor=_RecordingExecutor(),
        runtime_inspector=_HealthyRuntimeInspector(),
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_executions=1,
            node_id="worker-node-a",
        ),
    )
    worker_b = ControlWorker(
        session_factory=session_factory,
        provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
        executor=_RecordingExecutor(),
        runtime_inspector=_HealthyRuntimeInspector(),
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_executions=1,
            node_id="worker-node-b",
        ),
    )

    assert await worker_a._claim_monitoring_pr(monitor_id) is True  # noqa: SLF001
    first_operation_id = worker_a._monitor_recovery_operation_ids[monitor_id]  # noqa: SLF001
    assert first_operation_id in worker_a._active_salvage_monitor_recovery_operation_ids  # noqa: SLF001

    await worker_a._release_monitoring_pr_claim(monitor_id)  # noqa: SLF001

    assert await worker_b._claim_monitoring_pr(monitor_id) is True  # noqa: SLF001
    assert worker_b._monitor_recovery_operation_ids[monitor_id] == first_operation_id  # noqa: SLF001
    assert first_operation_id in worker_b._active_salvage_monitor_recovery_operation_ids  # noqa: SLF001


@pytest.mark.unit
async def test_claim_monitoring_pr_reuses_db_pending_recovery_without_in_memory_handle(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    """Verify claim monitoring pr reuses db pending recovery without in memory handle."""
    monitor_id = await _create_monitoring_pr(
        session_factory,
        origin_repo,
        "monitor-db-pending-recovery-reclaim",
        pr_number=461,
    )
    worker_a = ControlWorker(
        session_factory=session_factory,
        provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
        executor=_RecordingExecutor(),
        runtime_inspector=_HealthyRuntimeInspector(),
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_executions=1,
            node_id="worker-node-a",
        ),
    )
    worker_b = ControlWorker(
        session_factory=session_factory,
        provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
        executor=_RecordingExecutor(),
        runtime_inspector=_HealthyRuntimeInspector(),
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_executions=1,
            node_id="worker-node-b",
        ),
    )

    assert await worker_a._claim_monitoring_pr(monitor_id) is True  # noqa: SLF001
    first_operation_id = worker_a._monitor_recovery_operation_ids[monitor_id]  # noqa: SLF001

    await worker_a._release_monitoring_pr_claim(monitor_id)  # noqa: SLF001
    worker_b._monitor_recovery_operation_ids.pop(monitor_id, None)  # noqa: SLF001

    assert await worker_b._claim_monitoring_pr(monitor_id) is True  # noqa: SLF001
    assert worker_b._monitor_recovery_operation_ids[monitor_id] == first_operation_id  # noqa: SLF001

    async with session_factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=monitor_id)
    remonitor_operations = [
        operation for operation in operations if operation.type == OperationType.remonitor.value
    ]
    assert len(remonitor_operations) == 1
    assert remonitor_operations[0].id == first_operation_id
    assert remonitor_operations[0].status == OperationStatus.running.value


@pytest.mark.unit
async def test_claim_monitoring_pr_creates_fresh_recovery_when_cached_operation_terminal(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    """Stale in-memory recovery handles must not skip creating a fresh remonitor op."""
    monitor_id = await _create_monitoring_pr(
        session_factory,
        origin_repo,
        "monitor-stale-cached-recovery-reclaim",
        pr_number=462,
    )
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
        executor=_RecordingExecutor(),
        runtime_inspector=_HealthyRuntimeInspector(),
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_executions=1,
            node_id="worker-node-a",
        ),
    )

    assert await worker._claim_monitoring_pr(monitor_id) is True  # noqa: SLF001
    first_operation_id = worker._monitor_recovery_operation_ids[monitor_id]  # noqa: SLF001

    async with session_factory() as session:
        operation_repo = OperationRepository(session)
        operation = await operation_repo.get(first_operation_id)
        assert operation is not None
        await operation_repo.finish(
            operation,
            status=OperationStatus.succeeded,
            result={"requested_action": OperationType.remonitor.value},
        )
        await session.commit()

    assert await worker._claim_monitoring_pr(monitor_id) is True  # noqa: SLF001
    second_operation_id = worker._monitor_recovery_operation_ids[monitor_id]  # noqa: SLF001
    assert second_operation_id != first_operation_id

    async with session_factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=monitor_id)
    remonitor_operations = [
        operation for operation in operations if operation.type == OperationType.remonitor.value
    ]
    assert len(remonitor_operations) == 2
    remonitor_by_id = {operation.id: operation for operation in remonitor_operations}
    assert remonitor_by_id[first_operation_id].status == OperationStatus.succeeded.value
    assert remonitor_by_id[second_operation_id].status == OperationStatus.running.value


@pytest.mark.unit
async def test_claim_monitoring_pr_creates_fresh_recovery_when_fresh_worker_lease_expired(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    """Do not adopt a live worker's remonitor op after an expired monitor lease takeover."""
    monitor_id = await _create_monitoring_pr(
        session_factory,
        origin_repo,
        "monitor-fresh-worker-expired-lease",
        pr_number=464,
    )
    worker_a = ControlWorker(
        session_factory=session_factory,
        provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
        executor=_RecordingExecutor(),
        runtime_inspector=_HealthyRuntimeInspector(),
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_executions=1,
            node_id="worker-node-a",
        ),
    )
    worker_b = ControlWorker(
        session_factory=session_factory,
        provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
        executor=_RecordingExecutor(),
        runtime_inspector=_HealthyRuntimeInspector(),
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_executions=1,
            node_id="worker-node-b",
        ),
    )

    assert await worker_a._claim_monitoring_pr(monitor_id) is True  # noqa: SLF001
    first_operation_id = worker_a._monitor_recovery_operation_ids[monitor_id]  # noqa: SLF001

    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(monitor_id)
        assert ws is not None
        ws.monitor_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await WorkerHeartbeatRepository(session).record_heartbeat(
            worker_id=worker_a._worker_id,  # noqa: SLF001
            node_id="worker-node-a",
            started_at=datetime.now(UTC) - timedelta(minutes=1),
            last_heartbeat_at=datetime.now(UTC),
            poll_interval_seconds=0.01,
        )
        await session.commit()

    worker_b._monitor_recovery_operation_ids.pop(monitor_id, None)  # noqa: SLF001
    assert await worker_b._claim_monitoring_pr(monitor_id) is True  # noqa: SLF001
    second_operation_id = worker_b._monitor_recovery_operation_ids[monitor_id]  # noqa: SLF001
    assert second_operation_id != first_operation_id

    async with session_factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=monitor_id)
    remonitor_operations = [
        operation for operation in operations if operation.type == OperationType.remonitor.value
    ]
    assert len(remonitor_operations) == 2
    remonitor_by_id = {operation.id: operation for operation in remonitor_operations}
    assert remonitor_by_id[first_operation_id].status == OperationStatus.running.value
    assert remonitor_by_id[second_operation_id].status == OperationStatus.running.value

    assert (
        await worker_a._finish_monitor_recovery_operation(  # noqa: SLF001
            monitor_id,
            operation_id=first_operation_id,
            status=OperationStatus.succeeded,
        )
        is True
    )

    async with session_factory() as session:
        operations = await OperationRepository(session).list_all(workspace_id=monitor_id)
    remonitor_by_id = {
        operation.id: operation
        for operation in operations
        if operation.type == OperationType.remonitor.value
    }
    assert remonitor_by_id[first_operation_id].status == OperationStatus.cancelled.value
    assert remonitor_by_id[second_operation_id].status == OperationStatus.running.value


@pytest.mark.unit
async def test_finish_monitor_recovery_operation_skips_when_monitor_claim_lost(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    """Verify finish monitor recovery operation skips when monitor claim lost."""
    monitor_id = await _create_monitoring_pr(
        session_factory,
        origin_repo,
        "monitor-finish-lost-claim",
        pr_number=465,
    )
    worker_a = ControlWorker(
        session_factory=session_factory,
        provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
        executor=_RecordingExecutor(),
        runtime_inspector=_HealthyRuntimeInspector(),
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_executions=1,
            node_id="worker-node-a",
        ),
    )
    worker_b = ControlWorker(
        session_factory=session_factory,
        provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
        executor=_RecordingExecutor(),
        runtime_inspector=_HealthyRuntimeInspector(),
        config=WorkerConfig(
            poll_interval_seconds=0.01,
            max_concurrent_executions=1,
            node_id="worker-node-b",
        ),
    )

    assert await worker_a._claim_monitoring_pr(monitor_id) is True  # noqa: SLF001
    operation_id = worker_a._monitor_recovery_operation_ids[monitor_id]  # noqa: SLF001

    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        ws = await repo.get(monitor_id)
        assert ws is not None
        ws.monitor_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

    assert await worker_b._claim_monitoring_pr(monitor_id) is True  # noqa: SLF001

    assert (
        await worker_a._finish_monitor_recovery_operation(  # noqa: SLF001
            monitor_id,
            operation_id=operation_id,
            status=OperationStatus.succeeded,
        )
        is False
    )

    async with session_factory() as session:
        operation = await OperationRepository(session).get(operation_id)
        assert operation is not None
        assert operation.status == OperationStatus.running.value

    assert (
        await worker_b._finish_monitor_recovery_operation(  # noqa: SLF001
            monitor_id,
            operation_id=operation_id,
            status=OperationStatus.succeeded,
        )
        is True
    )

    async with session_factory() as session:
        operation = await OperationRepository(session).get(operation_id)
        assert operation is not None
        assert operation.status == OperationStatus.succeeded.value
