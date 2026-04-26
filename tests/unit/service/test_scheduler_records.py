"""Workspace service persistence for admission decisions and reservations."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.api.schemas import WorkspaceCreateV2Request
from awf.db.base import Base
from awf.db.repositories import (
    QueueDecisionRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
)
from awf.db.session import make_engine, make_session_factory
from awf.service.workspaces import WorkspaceService


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield make_session_factory(engine)
    finally:
        await engine.dispose()


def _request() -> WorkspaceCreateV2Request:
    return WorkspaceCreateV2Request(
        repo={"url": "git@github.com:example/service.git", "base_branch": "main"},
        task={
            "title": "Persist admission",
            "prompt": "Persist scheduler admission and reservation state.",
            "agent": "codex",
            "kind": "feature_branch_pr",
            "task_class": "dependency_task",
            "priority": 25,
            "owned_paths": ["pyproject.toml", "uv.lock"],
        },
        workspace={"profile_ref": "auto", "profile": None},
        validation={"commands": ["uv run pytest -q"], "requested_tier": 2},
        resources={
            "steady_state_cpu_cores": 4.0,
            "steady_state_memory_gb": 12.0,
            "peak_cpu_cores": 8.0,
            "peak_memory_gb": 24.0,
            "disk_mb": 4096,
        },
    )


@pytest.mark.unit
async def test_create_v2_writes_admitted_decision_and_local_reservation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceService(factory)

    created = await service.create_v2(_request())

    async with factory() as session:
        attempt = await TaskAttemptRepository(session).get_by_workspace_id(created.id)
        decisions = await QueueDecisionRepository(session).list_for_workspace(created.id)
        reservations = await ResourceReservationRepository(session).list_for_workspace(created.id)

    assert attempt is not None
    assert len(decisions) == 1
    assert decisions[0].workspace_id == created.id
    assert decisions[0].task_id == attempt.task_id
    assert decisions[0].attempt_id == attempt.id
    assert decisions[0].decision == "admitted"
    assert decisions[0].reason_code == "ADMITTED_LOCAL"
    assert decisions[0].class_priority == 4
    assert decisions[0].computed_priority == 37
    assert decisions[0].age_boost == 0
    assert decisions[0].retry_bonus == 0
    assert decisions[0].resource_summary == {
        "node_id": "local",
        "steady_cpu": 4.0,
        "steady_memory_gb": 12.0,
        "peak_cpu": 8.0,
        "peak_memory_gb": 24.0,
        "disk_mb": 4096,
        "phase": "workspace_lifecycle",
    }
    assert decisions[0].overlap_risk_summary == {
        "warning_code": None,
        "overlap_count": 0,
        "workspace_ids": [],
        "overlaps": [],
    }

    assert len(reservations) == 1
    assert reservations[0].workspace_id == created.id
    assert reservations[0].attempt_id == attempt.id
    assert reservations[0].node_id == "local"
    assert reservations[0].steady_cpu == 4.0
    assert reservations[0].steady_memory_gb == 12.0
    assert reservations[0].peak_cpu == 8.0
    assert reservations[0].peak_memory_gb == 24.0
    assert reservations[0].disk_mb == 4096
    assert reservations[0].phase == "workspace_lifecycle"
    assert reservations[0].released_at is None


@pytest.mark.unit
async def test_terminal_workspace_control_releases_active_reservation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async def noop_stopper(_compose_project_name: str | None) -> None:
        return None

    service = WorkspaceService(factory, project_stopper=noop_stopper)
    created = await service.create_v2(_request())

    await service.cancel_workspace(
        created.id,
        reason="operator cancellation",
        stop_stack=False,
    )

    async with factory() as session:
        reservation = (await ResourceReservationRepository(session).list_for_workspace(created.id))[0]
        active = await ResourceReservationRepository(session).active_for_workspace(created.id)

    assert active is None
    assert reservation.released_at is not None
