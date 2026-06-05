"""Shared fakes and DB helpers for worker admission/scheduler regressions.

Extracted from ``test_worker_scheduler_admission.py`` to keep that test module
under the first-party line-count guardrail. These are plain helpers imported by
the test module; nothing here is collected as a test.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.control.worker import ControlWorker
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceEventRepository,
    WorkspaceRepository,
)
from awf.runtime.inspection import RuntimeSnapshot


class _RecordingProvisioner:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def provision_claimed(
        self, workspace_id: str, execution_claim_epoch: int | None = None
    ) -> None:
        self.calls.append(workspace_id)


class _UnusedExecutor:
    async def execute(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("saturated worker must not dispatch execution")

    async def resume_pr_monitor(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("saturated worker must not resume monitors")


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, workspace_id: str, *_args: object, **_kwargs: object) -> None:
        self.calls.append(workspace_id)

    async def resume_pr_monitor(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("ready redispatch must not resume monitors")


class _RecordingRuntimeInspector:
    def __init__(self, snapshots: dict[str | None, RuntimeSnapshot]) -> None:
        self._snapshots = snapshots
        self.calls: list[str | None] = []

    async def inspect(self, compose_project_name: str | None) -> RuntimeSnapshot:
        self.calls.append(compose_project_name)
        return self._snapshots[compose_project_name]


class _NonPostgresSession:
    def __init__(self) -> None:
        self.executed = False

    def get_bind(self) -> SimpleNamespace:
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    async def execute(self, *_args: object, **_kwargs: object) -> None:
        self.executed = True


async def _never_finishes() -> None:
    await asyncio.Event().wait()


async def _raises_execution_failure() -> None:
    raise RuntimeError("execution failed")


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
    node_id: str | None,
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
        if status != WorkspaceStatus.provisioning:
            await repo.transition(
                workspace,
                to=WorkspaceStatus.ready,
                reason_code="TEST_READY",
            )
        if status not in {WorkspaceStatus.provisioning, WorkspaceStatus.ready}:
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


async def _workspace_node_id(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> str | None:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        return workspace.node_id


async def _workspace_execution_claim(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> tuple[str | None, datetime | None]:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        return workspace.execution_claimed_by, workspace.execution_claim_expires_at


async def _workspace_execution_epoch(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> int:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        return workspace.execution_claim_epoch


async def _reset_to_requested(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> None:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.status = WorkspaceStatus.requested.value
        await session.commit()


async def _stale_events(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
) -> list[object]:
    async with session_factory() as session:
        return await WorkspaceEventRepository(session).list(
            workspace_id=workspace_id,
            event_type="workspace.stale_active_execution_detected",
        )


def _gate_admission_prechecks(
    monkeypatch: pytest.MonkeyPatch,
    workers: list[ControlWorker],
) -> tuple[asyncio.Event, asyncio.Event]:
    observed = 0
    all_observed = asyncio.Event()
    release = asyncio.Event()

    for worker in workers:
        original: Callable[[], Awaitable[int]] = worker._requested_admission_row_slots  # noqa: SLF001

        async def _gated(original: Callable[[], Awaitable[int]] = original) -> int:
            nonlocal observed
            slots = await original()
            observed += 1
            if observed == len(workers):
                all_observed.set()
            await release.wait()
            return slots

        monkeypatch.setattr(worker, "_requested_admission_row_slots", _gated)

    return all_observed, release
