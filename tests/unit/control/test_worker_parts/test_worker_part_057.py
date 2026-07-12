"""ControlWorker stale-active recovery tests continued.

Split from ``test_worker_part_016.py`` to keep first-party test files under the
maintainability line limit.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import ControlWorker, WorkerConfig
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import WorkspaceEventRepository, WorkspaceRepository
from awf.runtime.inspection import RuntimeSnapshot
from tests.unit.control.test_worker_parts.test_worker_part_016 import (
    _create_active_execution,
    _RecordingExecutor,
    _RecordingRuntimeInspector,
    _TransitioningProvisioner,
    origin_repo,
    session_factory,
)

_IMPORTED_FIXTURES = (origin_repo, session_factory)


class TestRunOnceStaleActiveExecutionRecoveryPart002:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "pr_url",
        [
            None,
            "https://github.com/example/repo/pull/not-a-number",
        ],
    )
    async def test_stale_hosted_pr_adoption_without_attachable_pr_falls_back_to_runtime_failure(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
        pr_url: str | None,
    ) -> None:
        task_policy = {"pr_adoption": {"execution": {"mode": "hosted"}}}
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "hosted-pr-adoption-stale-without-attachable-pr",
            WorkspaceStatus.running,
            persist_compose_project=False,
            task_policy=task_policy,
            node_id="node-a",
        )
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            assert ws is not None
            ws.pr_url = pr_url
            ws.pr_number = None
            ws.execution_claimed_by = "hosted-worker-before-restart"
            ws.execution_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

        inspector = _RecordingRuntimeInspector(
            {
                None: RuntimeSnapshot(
                    stack_state="stopped",
                    reason="compose project has no running containers",
                    services=[],
                )
            }
        )
        executor = _RecordingExecutor()
        worker = ControlWorker(
            session_factory=session_factory,
            provisioner=_TransitioningProvisioner(session_factory),  # type: ignore[arg-type]
            executor=executor,
            runtime_inspector=inspector,
            config=WorkerConfig(
                poll_interval_seconds=0.01,
                max_concurrent_executions=0,
                node_id="node-a",
            ),
        )

        await worker._recover_stale_active_executions()  # noqa: SLF001

        assert inspector.calls == [None]
        assert executor.resume_calls == []
        async with session_factory() as session:
            ws = await WorkspaceRepository(session).get(workspace_id)
            assert ws is not None
            assert ws.status == WorkspaceStatus.failed.value
            assert ws.failure_reason == "infrastructure_failure"
            assert ws.failure_message is not None
            assert "no managed runtime containers were found" in ws.failure_message
            assert ws.execution_claimed_by is None
            assert ws.execution_claim_expires_at is None
            events = await WorkspaceEventRepository(session).list(workspace_id=workspace_id)

        assert any(
            event.event_type == "workspace.state_changed"
            and event.reason_code == "STRANDED_WORKSPACE"
            for event in events
        )
        assert any(
            event.event_type == "workspace.runtime_stranded_detected"
            and event.reason_code == "STRANDED_WORKSPACE"
            for event in events
        )

    @pytest.mark.unit
    async def test_stale_running_with_missing_compose_project_fails(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        origin_repo: Path,
    ) -> None:
        workspace_id = await _create_active_execution(
            session_factory,
            origin_repo,
            "stale-running",
            WorkspaceStatus.running,
            persist_compose_project=False,
        )
        inspector = _RecordingRuntimeInspector(
            {
                None: RuntimeSnapshot(
                    stack_state="unknown",
                    reason="workspace has no compose project",
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
            assert "active execution was lost after a service or Docker restart" in (
                ws.failure_message
            )
            assert "preserved" in ws.failure_message
            events = await WorkspaceEventRepository(s).list(workspace_id=workspace_id)
            assert any(
                event.event_type == "workspace.state_changed"
                and event.reason_code == "STRANDED_WORKSPACE"
                for event in events
            )
            assert any(
                event.event_type == "workspace.runtime_stranded_detected"
                and event.reason_code == "STRANDED_WORKSPACE"
                for event in events
            )
        assert inspector.calls == [None]
