"""Durable scheduler decision and reservation records."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from awf.db.enums import AgentRuntime
from awf.db.repositories import (
    QueueDecisionCreate,
    QueueDecisionRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from tests.postgres import postgres_test_session


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with postgres_test_session() as s:
        yield s


async def _attempt(session: AsyncSession) -> tuple[str, str, str]:
    workspace = await WorkspaceRepository(session).create(
        repo_url="git@github.com:example/scheduler.git",
        branch_base="main",
        task_title="Persist scheduler records",
        task_prompt="Make admission decisions durable.",
        agent=AgentRuntime.codex.value,
        task_class="migration_task",
        owned_paths=["migrations/**"],
        test_commands=["pytest -q"],
    )
    task = await TaskRepository(session).create_or_get(
        repo_url=workspace.repo_url,
        base_branch=workspace.branch_base,
        title=workspace.task_title,
        prompt=workspace.task_prompt,
        external_id="SCHED-1",
        idempotency_key=None,
        task_class=workspace.task_class,
        owned_paths=list(workspace.owned_paths),
    )
    attempt = await TaskAttemptRepository(session).create_for_workspace(
        task=task,
        workspace=workspace,
    )
    return workspace.id, task.id, attempt.id


@pytest.mark.unit
async def test_queue_decision_repository_creates_and_lists_decisions(
    session: AsyncSession,
) -> None:
    workspace_id, task_id, attempt_id = await _attempt(session)
    decided_at = datetime(2026, 4, 26, 12, 30, tzinfo=UTC)

    decision = await QueueDecisionRepository(session).create(
        workspace_id=workspace_id,
        task_id=task_id,
        attempt_id=attempt_id,
        decision="admitted",
        reason_code="ADMITTED_LOCAL",
        class_priority=5,
        computed_priority=57,
        age_boost=2,
        retry_bonus=3,
        score_summary={
            "base_priority": 37,
            "class_bias": 15,
            "age_boost": 2,
            "retry_bonus": 3,
            "human_boost": 0,
            "effective_score": 57,
            "queued_at": "2026-04-26T12:00:00+00:00",
        },
        resource_summary={
            "node_id": "local",
            "steady_cpu": 3.0,
            "steady_memory_gb": 10.0,
            "peak_cpu": 6.0,
            "peak_memory_gb": 16.0,
        },
        overlap_risk_summary={
            "warning_code": "OWNED_PATH_OVERLAP_RISK",
            "overlap_count": 1,
            "workspace_ids": ["ws_existing"],
        },
        decided_at=decided_at,
    )
    await session.commit()

    rows = await QueueDecisionRepository(session).list_for_workspace(workspace_id)

    assert [row.id for row in rows] == [decision.id]
    assert decision.id.startswith("qd_")
    assert rows[0].workspace_id == workspace_id
    assert rows[0].task_id == task_id
    assert rows[0].attempt_id == attempt_id
    assert rows[0].decision == "admitted"
    assert rows[0].reason_code == "ADMITTED_LOCAL"
    assert rows[0].class_priority == 5
    assert rows[0].computed_priority == 57
    assert rows[0].age_boost == 2
    assert rows[0].retry_bonus == 3
    assert rows[0].score_summary["effective_score"] == 57
    assert rows[0].score_summary["human_boost"] == 0
    assert rows[0].resource_summary["peak_cpu"] == 6.0
    assert rows[0].overlap_risk_summary["workspace_ids"] == ["ws_existing"]
    assert rows[0].decided_at == decided_at


@pytest.mark.unit
async def test_queue_decision_repository_batches_create_and_latest_lookup(
    session: AsyncSession,
) -> None:
    first_workspace_id, first_task_id, first_attempt_id = await _attempt(session)
    second_workspace_id, second_task_id, second_attempt_id = await _attempt(session)
    repo = QueueDecisionRepository(session)

    rows = await repo.create_many(
        [
            QueueDecisionCreate(
                workspace_id=first_workspace_id,
                task_id=first_task_id,
                attempt_id=first_attempt_id,
                decision="deferred",
                reason_code="PROVIDER_RECOVERY_NOT_BEFORE",
                class_priority=5,
                computed_priority=30,
                age_boost=0,
                retry_bonus=0,
                resource_summary={"node_id": "old"},
                overlap_risk_summary={},
                score_summary={"effective_score": 30},
                decided_at=datetime(2026, 4, 26, 12, 0, tzinfo=UTC),
            ),
            QueueDecisionCreate(
                workspace_id=first_workspace_id,
                task_id=first_task_id,
                attempt_id=first_attempt_id,
                decision="ordered",
                reason_code="ORDERED_READY_EXECUTION",
                class_priority=5,
                computed_priority=40,
                age_boost=1,
                retry_bonus=0,
                resource_summary={"node_id": "new"},
                overlap_risk_summary={"overlap_count": 0},
                score_summary={"effective_score": 40},
                decided_at=datetime(2026, 4, 26, 12, 5, tzinfo=UTC),
            ),
            QueueDecisionCreate(
                workspace_id=second_workspace_id,
                task_id=second_task_id,
                attempt_id=second_attempt_id,
                decision="ordered",
                reason_code="ORDERED_READY_EXECUTION",
                class_priority=5,
                computed_priority=35,
                age_boost=0,
                retry_bonus=0,
                resource_summary={"node_id": "second"},
                overlap_risk_summary={},
                score_summary={"effective_score": 35},
                decided_at=datetime(2026, 4, 26, 12, 3, tzinfo=UTC),
            ),
        ]
    )
    await session.commit()

    latest = await repo.latest_by_workspace_ids(
        [first_workspace_id, second_workspace_id, first_workspace_id]
    )

    assert latest[first_workspace_id].id == rows[1].id
    assert latest[first_workspace_id].resource_summary["node_id"] == "new"
    assert latest[second_workspace_id].id == rows[2].id


@pytest.mark.unit
async def test_queue_decision_repository_empty_batch_helpers_short_circuit(
    session: AsyncSession,
) -> None:
    repo = QueueDecisionRepository(session)

    assert await repo.create_many([]) == []
    assert await repo.latest_by_workspace_ids([]) == {}


@pytest.mark.unit
async def test_resource_reservation_repository_tracks_active_and_released_rows(
    session: AsyncSession,
) -> None:
    workspace_id, _task_id, attempt_id = await _attempt(session)
    reserved_at = datetime(2026, 4, 26, 13, 0, tzinfo=UTC)
    released_at = datetime(2026, 4, 26, 14, 0, tzinfo=UTC)

    reservation = await ResourceReservationRepository(session).create(
        workspace_id=workspace_id,
        attempt_id=attempt_id,
        node_id="local",
        steady_cpu=4.0,
        steady_memory_gb=12.0,
        peak_cpu=8.0,
        peak_memory_gb=24.0,
        disk_mb=4096,
        phase="workspace_lifecycle",
        reserved_at=reserved_at,
    )
    assert reservation.id.startswith("rr_")
    assert reservation.released_at is None
    assert (await ResourceReservationRepository(session).active_for_workspace(workspace_id)).id == (
        reservation.id
    )

    released = await ResourceReservationRepository(session).release_active_for_workspace(
        workspace_id,
        released_at=released_at,
    )
    rows = await ResourceReservationRepository(session).list_for_workspace(workspace_id)

    assert [row.id for row in released] == [reservation.id]
    assert rows[0].node_id == "local"
    assert rows[0].steady_cpu == 4.0
    assert rows[0].steady_memory_gb == 12.0
    assert rows[0].peak_cpu == 8.0
    assert rows[0].peak_memory_gb == 24.0
    assert rows[0].disk_mb == 4096
    assert rows[0].dind_slots == 0
    assert rows[0].phase == "workspace_lifecycle"
    assert rows[0].reserved_at == reserved_at
    assert rows[0].released_at == released_at
    assert await ResourceReservationRepository(session).active_for_workspace(workspace_id) is None


@pytest.mark.unit
async def test_resource_reservation_repository_persists_dind_and_releases_idempotently(
    session: AsyncSession,
) -> None:
    workspace_id, _task_id, attempt_id = await _attempt(session)
    reserved_at = datetime(2026, 4, 26, 13, 0, tzinfo=UTC)
    released_at = datetime(2026, 4, 26, 14, 0, tzinfo=UTC)
    second_release_at = datetime(2026, 4, 26, 15, 0, tzinfo=UTC)

    reservation = await ResourceReservationRepository(session).create(
        workspace_id=workspace_id,
        attempt_id=attempt_id,
        node_id="local",
        steady_cpu=4.0,
        steady_memory_gb=12.0,
        peak_cpu=8.0,
        peak_memory_gb=24.0,
        disk_mb=4096,
        dind_slots=1,
        phase="workspace_lifecycle",
        reserved_at=reserved_at,
    )
    await session.commit()

    first_release = await ResourceReservationRepository(session).release_active_for_workspace(
        workspace_id,
        released_at=released_at,
    )
    second_release = await ResourceReservationRepository(session).release_active_for_workspace(
        workspace_id,
        released_at=second_release_at,
    )
    rows = await ResourceReservationRepository(session).list_for_workspace(workspace_id)

    assert [row.id for row in first_release] == [reservation.id]
    assert second_release == []
    assert rows[0].dind_slots == 1
    assert rows[0].released_at == released_at
    assert await ResourceReservationRepository(session).active_for_workspace(workspace_id) is None


@pytest.mark.unit
async def test_resource_reservation_release_uses_single_update_returning(
    session: AsyncSession,
) -> None:
    workspace_id, _task_id, attempt_id = await _attempt(session)
    reserved_at = datetime(2026, 4, 26, 13, 0, tzinfo=UTC)
    released_at = datetime(2026, 4, 26, 14, 0, tzinfo=UTC)

    reservation = await ResourceReservationRepository(session).create(
        workspace_id=workspace_id,
        attempt_id=attempt_id,
        node_id="local",
        steady_cpu=4.0,
        steady_memory_gb=12.0,
        peak_cpu=8.0,
        peak_memory_gb=24.0,
        disk_mb=4096,
        phase="workspace_lifecycle",
        reserved_at=reserved_at,
    )
    await session.commit()

    statements: list[str] = []

    def record_sql(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        del conn, cursor, parameters, context, executemany
        statements.append(" ".join(statement.lower().split()))

    engine = session.bind
    assert engine is not None
    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        released = await ResourceReservationRepository(session).release_active_for_workspace(
            workspace_id,
            released_at=released_at,
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)

    assert [row.id for row in released] == [reservation.id]
    assert len(statements) == 1
    assert statements[0].startswith("update resource_reservations ")
    assert " returning " in statements[0]
