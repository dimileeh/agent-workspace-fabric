"""Focused regressions for worker admission vs execution-slot capacity."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import ControlWorker, WorkerConfig
from awf.control.worker.types import _ExecutionTaskKind
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.inspection import RuntimeService, RuntimeSnapshot
from tests.postgres import postgres_test_engine


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


class _RecordingProvisioner:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def provision_claimed(self, workspace_id: str) -> None:
        self.calls.append(workspace_id)


class _UnusedExecutor:
    async def execute(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("saturated worker must not dispatch execution")

    async def resume_pr_monitor(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("saturated worker must not resume monitors")


class _RecordingRuntimeInspector:
    def __init__(self, snapshots: dict[str | None, RuntimeSnapshot]) -> None:
        self._snapshots = snapshots
        self.calls: list[str | None] = []

    async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
        self.calls.append(compose_project_name)
        return self._snapshots[compose_project_name]


async def _never_finishes() -> None:
    await asyncio.Event().wait()


async def _create_requested(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    create_attempt: bool,
    node_id: str | None = None,
) -> str:
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@example.com:org/repo.git",
            branch_base="development",
            task_title="queued when saturated",
            task_prompt="do the narrow thing",
            agent="codex",
            test_commands=[],
        )
        workspace.node_id = node_id
        if create_attempt:
            task = await TaskRepository(session).create_or_get(
                repo_url=workspace.repo_url,
                base_branch=workspace.branch_base,
                title=workspace.task_title,
                prompt=workspace.task_prompt,
                external_id=None,
                idempotency_key=None,
                task_class=workspace.task_class,
                owned_paths=list(workspace.owned_paths),
            )
            attempt = await TaskAttemptRepository(session).create_for_workspace(
                task=task,
                workspace=workspace,
            )
            await ResourceReservationRepository(session).create(
                workspace_id=workspace.id,
                attempt_id=attempt.id,
                node_id="local",
                steady_cpu=1.0,
                steady_memory_gb=1.0,
                peak_cpu=1.0,
                peak_memory_gb=1.0,
                disk_mb=None,
                dind_slots=0,
                phase="workspace_lifecycle",
            )
        await session.commit()
        return workspace.id


async def _create_active_slot(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    node_id: str,
    status: WorkspaceStatus = WorkspaceStatus.running,
) -> str:
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@example.com:org/repo.git",
            branch_base="development",
            task_title="active slot consumer",
            task_prompt="already active",
            agent="codex",
            test_commands=[],
        )
        workspace.node_id = node_id
        workspace.compose_project_name = f"awf_{workspace.id}"
        workspace.compose_file_path = f"/tmp/{workspace.id}/compose.yml"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.provisioning,
            reason_code="TEST_PROVISIONING",
        )
        await repo.transition(
            workspace,
            to=WorkspaceStatus.ready,
            reason_code="TEST_READY",
        )
        if status != WorkspaceStatus.ready:
            await repo.transition(workspace, to=status, reason_code="TEST_ACTIVE")
        await session.commit()
        return workspace.id


async def _create_ready_with_runtime_metadata(
    session_factory: async_sessionmaker[AsyncSession],
) -> str:
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@example.com:org/repo.git",
            branch_base="development",
            task_title="ready and waiting",
            task_prompt="do the narrow thing",
            agent="codex",
            test_commands=[],
        )
        workspace.compose_project_name = f"awf_{workspace.id}"
        workspace.compose_file_path = f"/tmp/{workspace.id}/compose.yml"
        await repo.transition(
            workspace,
            to=WorkspaceStatus.provisioning,
            reason_code="TEST_PROVISIONING",
        )
        await repo.transition(
            workspace,
            to=WorkspaceStatus.ready,
            reason_code="TEST_READY",
        )
        await session.commit()
        return workspace.id


async def _workspace_status(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> str:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        return workspace.status


async def _stale_events(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> list[object]:
    async with session_factory() as session:
        return await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.stale_active_execution_detected",
        )


@pytest.mark.unit
async def test_requested_workspace_stays_queued_when_execution_slots_are_saturated(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _create_requested(session_factory, create_attempt=False)
    provisioner = _RecordingProvisioner()
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner,  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(max_concurrent_provisions=5, max_concurrent_executions=1),
    )
    worker._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001
    existing_task = asyncio.create_task(_never_finishes())
    worker._track_execution_task(  # noqa: SLF001
        "ws_existing",
        existing_task,
        kind=_ExecutionTaskKind.READY,
    )

    try:
        assert await worker.run_once() == 0
        assert provisioner.calls == []
        assert (
            await _workspace_status(session_factory, workspace_id)
            == WorkspaceStatus.requested.value
        )
    finally:
        existing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await existing_task


@pytest.mark.unit
async def test_local_capacity_claims_also_wait_for_execution_slot_capacity(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _create_requested(session_factory, create_attempt=True)
    provisioner = _RecordingProvisioner()
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner,  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=5,
            max_concurrent_executions=1,
            local_capacity_cpu_cores=100.0,
        ),
    )
    worker._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001
    existing_task = asyncio.create_task(_never_finishes())
    worker._track_execution_task(  # noqa: SLF001
        "ws_existing",
        existing_task,
        kind=_ExecutionTaskKind.READY,
    )

    try:
        assert await worker.run_once() == 0
        assert provisioner.calls == []
        assert (
            await _workspace_status(session_factory, workspace_id)
            == WorkspaceStatus.requested.value
        )
    finally:
        existing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await existing_task


@pytest.mark.unit
async def test_requested_workspace_stays_queued_when_node_active_rows_fill_slots(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _create_active_slot(session_factory, node_id="local")
    workspace_id = await _create_requested(
        session_factory,
        create_attempt=False,
        node_id="local",
    )
    provisioner = _RecordingProvisioner()
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=provisioner,  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        config=WorkerConfig(
            max_concurrent_provisions=5,
            max_concurrent_executions=1,
            node_id="local",
        ),
    )
    worker._next_stale_active_execution_scan_at = float("inf")  # noqa: SLF001

    assert await worker.run_once() == 0

    assert provisioner.calls == []
    assert await _workspace_status(session_factory, workspace_id) == WorkspaceStatus.requested.value


@pytest.mark.unit
async def test_healthy_ready_workspace_waiting_for_slot_is_not_stale_execution(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    workspace_id = await _create_ready_with_runtime_metadata(session_factory)
    compose_project_name = f"awf_{workspace_id}"
    inspector = _RecordingRuntimeInspector(
        {
            compose_project_name: RuntimeSnapshot(
                stack_state="running",
                services=[
                    RuntimeService(
                        name="agent",
                        container_id="agent-1",
                        image="awf-agent-runtime:latest",
                        state="running",
                    )
                ],
            )
        }
    )
    worker = ControlWorker(
        session_factory=session_factory,
        provisioner=_RecordingProvisioner(),  # type: ignore[arg-type]
        executor=_UnusedExecutor(),  # type: ignore[arg-type]
        runtime_inspector=inspector,
        config=WorkerConfig(max_concurrent_executions=0),
    )

    await worker._recover_stale_active_executions()  # noqa: SLF001

    assert inspector.calls == [compose_project_name]
    assert await _workspace_status(session_factory, workspace_id) == WorkspaceStatus.ready.value
    assert await _stale_events(session_factory, workspace_id) == []
