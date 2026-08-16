"""ControlWorker stale active execution cleanup tests split from part 033."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import ControlWorker, WorkerConfig
from awf.control.worker.types import _ActiveExecutionCandidate
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceEventRepository, WorkspaceRepository
from awf.node.cleanup import (
    CLEANUP_PARTIAL,
    WorkspaceCleanupResult,
    WorkspaceCleanupStepResult,
)
from awf.runtime.inspection import RuntimeSnapshot
from tests.unit.control.test_worker_parts.test_worker_part_033 import (
    _create_active_execution,
    _RecordingExecutor,
    _RecordingRuntimeCleaner,
    _TransitioningProvisioner,
)
from tests.unit.control.test_worker_parts.test_worker_part_033 import (
    origin_repo as origin_repo,
)
from tests.unit.control.test_worker_parts.test_worker_part_033 import (
    session_factory as session_factory,
)


@pytest.mark.unit
async def test_stale_active_execution_cleanup_failure_keeps_row_active(
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    workspace_id = await _create_active_execution(
        session_factory,
        origin_repo,
        "stale-running-cleanup-fail",
        WorkspaceStatus.running,
        compose_project_name="awf_stale_cleanup_fail",
    )
    cleaner = _RecordingRuntimeCleaner(
        WorkspaceCleanupResult(
            status="partial",
            reason_code=CLEANUP_PARTIAL,
            steps=(
                WorkspaceCleanupStepResult(
                    name="compose_down",
                    status="failed",
                    reason_code="DOCKER_UNAVAILABLE",
                    error="cannot connect to docker",
                ),
            ),
        )
    )
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
        executor=_RecordingExecutor(),
        runtime_cleaner=cleaner,
        config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_executions=1),
    )

    candidate = _ActiveExecutionCandidate(
        workspace_id=workspace_id,
        status=WorkspaceStatus.running,
        compose_project_name="awf_stale_cleanup_fail",
        repo_url=str(origin_repo),
    )
    snapshot = RuntimeSnapshot(stack_state="running", reason="lost worker task")
    assert await worker._record_stale_active_execution_detected(candidate, snapshot)

    await worker._cleanup_and_fail_stale_active_execution(candidate, snapshot)

    async with session_factory() as s:
        ws = await WorkspaceRepository(s).get(workspace_id)
        assert ws is not None
        assert ws.status == WorkspaceStatus.running.value
        assert ws.failure_reason is None
        assert ws.execution_claimed_by is None
        cleanup_events = await WorkspaceEventRepository(s).list(
            workspace_id=workspace_id,
            event_type="workspace.stale_active_execution_cleanup_failed",
        )
        assert len(cleanup_events) == 1
        assert cleanup_events[0].reason_code == "STALE_ACTIVE_EXECUTION_CLEANUP_FAILED"
        assert cleanup_events[0].payload["cleanup"]["reason_code"] == CLEANUP_PARTIAL
