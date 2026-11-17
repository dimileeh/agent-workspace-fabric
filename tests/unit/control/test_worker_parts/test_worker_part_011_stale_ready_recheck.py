"""ControlWorker stale ready-list recheck tests split from part 011."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import ControlWorker, WorkerConfig
from awf.db.enums import WorkspaceStatus
from tests.unit.control.test_worker_parts.test_worker_part_011 import (
    _create_ready,
    _move_to_operator_control_status,
    _RecordingExecutor,
    _TransitioningProvisioner,
)
from tests.unit.control.test_worker_parts.test_worker_part_011 import (
    origin_repo as origin_repo,
)
from tests.unit.control.test_worker_parts.test_worker_part_011 import (
    session_factory as session_factory,
)


@pytest.mark.unit
@pytest.mark.parametrize("final_status", [WorkspaceStatus.cancelled, WorkspaceStatus.destroyed])
async def test_stale_ready_list_entry_is_rechecked_before_dispatch(
    final_status: WorkspaceStatus,
    session_factory: async_sessionmaker[AsyncSession],
    origin_repo: Path,
) -> None:
    ready_id = await _create_ready(session_factory, origin_repo, "stale-ready")
    await _move_to_operator_control_status(session_factory, ready_id, final_status)

    executor = _RecordingExecutor()
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
        executor=executor,
        config=WorkerConfig(poll_interval_seconds=0.01, max_concurrent_provisions=3),
    )

    async def _stale_ready_list(
        *,
        limit: int | None = None,
        exclude_ids: set[str] | None = None,
    ) -> list[str]:
        del limit, exclude_ids
        return [ready_id]

    worker._list_ready = _stale_ready_list  # type: ignore[method-assign]

    assert await worker.run_once() == 0
    await worker.wait_for_execution_tasks()

    assert executor.calls == []
