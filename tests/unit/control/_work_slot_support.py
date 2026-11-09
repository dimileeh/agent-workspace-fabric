"""Shared fixtures for the slot-release (work-slot lease) test suites."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.db.enums import ResourceReservationPhase, WorkspaceStatus
from awf.db.models import WorkSlotLease, WorkspaceEvent
from awf.db.repositories import (
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)


async def create_slot_workspace(
    factory: async_sessionmaker[AsyncSession],
    *,
    status: WorkspaceStatus = WorkspaceStatus.requested,
    steady: float = 1.0,
    peak: float = 2.0,
    reservation_phase: str | None = ResourceReservationPhase.workspace_lifecycle.value,
    node_id: str | None = None,
    title: str = "slot",
) -> str:
    """Create a workspace with a task attempt and (optionally) one reservation."""
    async with factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@example.com:org/repo.git",
            branch_base="main",
            task_title=title,
            task_prompt="p",
            agent="codex",
            test_commands=[],
        )
        workspace.node_id = node_id
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
            task=task, workspace=workspace
        )
        if reservation_phase is not None:
            await ResourceReservationRepository(session).create(
                workspace_id=workspace.id,
                attempt_id=attempt.id,
                node_id="local",
                steady_cpu=steady,
                steady_memory_gb=steady,
                peak_cpu=peak,
                peak_memory_gb=peak,
                disk_mb=None,
                dind_slots=0,
                phase=reservation_phase,
            )
        if status is not WorkspaceStatus.requested:
            workspace.status = status.value
        await session.commit()
        return workspace.id


async def lease_rows(
    factory: async_sessionmaker[AsyncSession],
    *,
    workspace_id: str | None = None,
) -> list[WorkSlotLease]:
    async with factory() as session:
        stmt = select(WorkSlotLease).order_by(WorkSlotLease.ticket_at)
        if workspace_id is not None:
            stmt = stmt.where(WorkSlotLease.workspace_id == workspace_id)
        return list((await session.execute(stmt)).scalars())


async def workspace_events(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    event_type: str,
) -> list[WorkspaceEvent]:
    async with factory() as session:
        stmt = select(WorkspaceEvent).where(
            WorkspaceEvent.workspace_id == workspace_id,
            WorkspaceEvent.event_type == event_type,
        )
        return list((await session.execute(stmt)).scalars())
