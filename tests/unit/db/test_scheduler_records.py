"""Durable scheduler decision and reservation records."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from awf.common.ids import new_resource_reservation_id
from awf.db.enums import AgentRuntime, WorkspaceStatus
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
async def test_repository_empty_capacity_inputs_short_circuit_without_database() -> None:
    reservation_repo = ResourceReservationRepository(object())  # type: ignore[arg-type]
    attempt_repo = TaskAttemptRepository(object(), dialect_name="sqlite")  # type: ignore[arg-type]

    assert await attempt_repo.get_by_workspace_ids([]) == {}
    assert await reservation_repo.active_latest_by_workspace_ids([]) == {}
    assert await reservation_repo.active_latest_totals(statuses=()) == {
        "workspace_count": 0,
        "steady_cpu": 0.0,
        "steady_memory_gb": 0.0,
        "peak_cpu": 0.0,
        "peak_memory_gb": 0.0,
        "disk_mb": 0,
        "dind_slots": 0,
    }
    assert await reservation_repo.active_latest_totals_for_workspace_scope(statuses=()) == {
        "workspace_count": 0,
        "steady_cpu": 0.0,
        "steady_memory_gb": 0.0,
        "peak_cpu": 0.0,
        "peak_memory_gb": 0.0,
        "disk_mb": 0,
        "dind_slots": 0,
    }
    assert await reservation_repo.active_latest_totals_for_scheduler_allocation_scope(
        statuses=(),
        node_id="local",
    ) == {
        "workspace_count": 0,
        "steady_cpu": 0.0,
        "steady_memory_gb": 0.0,
        "peak_cpu": 0.0,
        "peak_memory_gb": 0.0,
        "disk_mb": 0,
        "dind_slots": 0,
    }
    assert await reservation_repo.active_latest_totals_for_metrics_allocation_scope(
        statuses=(),
        node_id="local",
    ) == {
        "workspace_count": 0,
        "steady_cpu": 0.0,
        "steady_memory_gb": 0.0,
        "peak_cpu": 0.0,
        "peak_memory_gb": 0.0,
        "disk_mb": 0,
        "dind_slots": 0,
    }


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


@pytest.mark.unit
async def test_resource_reservation_active_latest_by_workspace_ids_uses_window_query(
    session: AsyncSession,
) -> None:
    first_workspace_id, _first_task_id, first_attempt_id = await _attempt(session)
    second_workspace_id, _second_task_id, second_attempt_id = await _attempt(session)
    reserved_at = datetime(2026, 4, 26, 13, 0, tzinfo=UTC)
    repo = ResourceReservationRepository(session)
    older = await repo.create(
        workspace_id=first_workspace_id,
        attempt_id=first_attempt_id,
        node_id="node-a",
        steady_cpu=1.0,
        steady_memory_gb=2.0,
        peak_cpu=3.0,
        peak_memory_gb=4.0,
        disk_mb=100,
        dind_slots=0,
        phase="workspace_lifecycle",
        reserved_at=reserved_at,
    )
    latest = await repo.create(
        workspace_id=first_workspace_id,
        attempt_id=first_attempt_id,
        node_id="node-a",
        steady_cpu=5.0,
        steady_memory_gb=6.0,
        peak_cpu=7.0,
        peak_memory_gb=8.0,
        disk_mb=200,
        dind_slots=1,
        phase="workspace_lifecycle",
        reserved_at=reserved_at + timedelta(minutes=1),
    )
    released_newer = await repo.create(
        workspace_id=first_workspace_id,
        attempt_id=first_attempt_id,
        node_id="node-a",
        steady_cpu=9.0,
        steady_memory_gb=10.0,
        peak_cpu=11.0,
        peak_memory_gb=12.0,
        disk_mb=300,
        dind_slots=2,
        phase="workspace_lifecycle",
        reserved_at=reserved_at + timedelta(minutes=2),
    )
    second_latest = await repo.create(
        workspace_id=second_workspace_id,
        attempt_id=second_attempt_id,
        node_id="node-b",
        steady_cpu=13.0,
        steady_memory_gb=14.0,
        peak_cpu=15.0,
        peak_memory_gb=16.0,
        disk_mb=400,
        dind_slots=3,
        phase="workspace_lifecycle",
        reserved_at=reserved_at,
    )
    released_newer.released_at = reserved_at + timedelta(minutes=3)
    older_id = older.id
    latest_id = latest.id
    released_newer_id = released_newer.id
    second_latest_id = second_latest.id
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
        rows = await ResourceReservationRepository(session).active_latest_by_workspace_ids(
            [first_workspace_id, second_workspace_id, first_workspace_id]
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)

    assert rows[first_workspace_id].id == latest_id
    assert rows[second_workspace_id].id == second_latest_id
    assert older_id not in {row.id for row in rows.values()}
    assert released_newer_id not in {row.id for row in rows.values()}
    assert len(statements) == 1
    assert "row_number() over" in statements[0]
    assert "partition by resource_reservations.workspace_id" in statements[0]
    assert "reservation_rank = " in statements[0]


@pytest.mark.unit
async def test_resource_reservation_active_latest_totals_can_filter_by_node_id(
    session: AsyncSession,
) -> None:
    first_workspace_id, _first_task_id, first_attempt_id = await _attempt(session)
    second_workspace_id, _second_task_id, second_attempt_id = await _attempt(session)
    requested_workspace_id, _requested_task_id, requested_attempt_id = await _attempt(session)
    workspace_repo = WorkspaceRepository(session)
    for workspace_id in (first_workspace_id, second_workspace_id):
        workspace = await workspace_repo.get(workspace_id)
        assert workspace is not None
        await workspace_repo.transition(
            workspace,
            to=WorkspaceStatus.provisioning,
            reason_code="SEED",
        )

    reserved_at = datetime(2026, 4, 26, 13, 0, tzinfo=UTC)
    repo = ResourceReservationRepository(session)
    await repo.create(
        workspace_id=first_workspace_id,
        attempt_id=first_attempt_id,
        node_id="node-a",
        steady_cpu=20.0,
        steady_memory_gb=40.0,
        peak_cpu=80.0,
        peak_memory_gb=160.0,
        disk_mb=9999,
        dind_slots=1,
        phase="workspace_lifecycle",
        reserved_at=reserved_at - timedelta(minutes=5),
    )
    await repo.create(
        workspace_id=first_workspace_id,
        attempt_id=first_attempt_id,
        node_id="node-a",
        steady_cpu=2.0,
        steady_memory_gb=4.0,
        peak_cpu=8.0,
        peak_memory_gb=16.0,
        disk_mb=1000,
        dind_slots=1,
        phase="workspace_lifecycle",
        reserved_at=reserved_at,
    )
    await repo.create(
        workspace_id=second_workspace_id,
        attempt_id=second_attempt_id,
        node_id="node-b",
        steady_cpu=3.0,
        steady_memory_gb=6.0,
        peak_cpu=12.0,
        peak_memory_gb=24.0,
        disk_mb=2000,
        dind_slots=0,
        phase="workspace_lifecycle",
        reserved_at=reserved_at,
    )
    await repo.create(
        workspace_id=requested_workspace_id,
        attempt_id=requested_attempt_id,
        node_id="node-a",
        steady_cpu=100.0,
        steady_memory_gb=100.0,
        peak_cpu=100.0,
        peak_memory_gb=100.0,
        disk_mb=100,
        dind_slots=1,
        phase="workspace_lifecycle",
        reserved_at=reserved_at,
    )

    global_totals = await repo.active_latest_totals(statuses=(WorkspaceStatus.provisioning,))
    node_a_totals = await repo.active_latest_totals(
        statuses=(WorkspaceStatus.provisioning,),
        node_id="node-a",
    )
    node_b_totals = await repo.active_latest_totals(
        statuses=(WorkspaceStatus.provisioning,),
        node_id="node-b",
    )

    assert global_totals == {
        "workspace_count": 2,
        "steady_cpu": 5.0,
        "steady_memory_gb": 10.0,
        "peak_cpu": 20.0,
        "peak_memory_gb": 40.0,
        "disk_mb": 3000,
        "dind_slots": 1,
    }
    assert node_a_totals == {
        "workspace_count": 1,
        "steady_cpu": 2.0,
        "steady_memory_gb": 4.0,
        "peak_cpu": 8.0,
        "peak_memory_gb": 16.0,
        "disk_mb": 1000,
        "dind_slots": 1,
    }
    assert node_b_totals == {
        "workspace_count": 1,
        "steady_cpu": 3.0,
        "steady_memory_gb": 6.0,
        "peak_cpu": 12.0,
        "peak_memory_gb": 24.0,
        "disk_mb": 2000,
        "dind_slots": 0,
    }


@pytest.mark.unit
async def test_resource_reservation_active_latest_totals_filters_node_after_latest_rank(
    session: AsyncSession,
) -> None:
    workspace_id, _task_id, attempt_id = await _attempt(session)
    workspace_repo = WorkspaceRepository(session)
    workspace = await workspace_repo.get(workspace_id)
    assert workspace is not None
    await workspace_repo.transition(
        workspace,
        to=WorkspaceStatus.provisioning,
        reason_code="SEED",
    )

    reserved_at = datetime(2026, 4, 26, 13, 0, tzinfo=UTC)
    repo = ResourceReservationRepository(session)
    await repo.create(
        workspace_id=workspace_id,
        attempt_id=attempt_id,
        node_id="node-a",
        steady_cpu=20.0,
        steady_memory_gb=40.0,
        peak_cpu=80.0,
        peak_memory_gb=160.0,
        disk_mb=9999,
        dind_slots=1,
        phase="workspace_lifecycle",
        reserved_at=reserved_at - timedelta(minutes=5),
    )
    await repo.create(
        workspace_id=workspace_id,
        attempt_id=attempt_id,
        node_id="node-b",
        steady_cpu=2.0,
        steady_memory_gb=4.0,
        peak_cpu=8.0,
        peak_memory_gb=16.0,
        disk_mb=1000,
        dind_slots=0,
        phase="workspace_lifecycle",
        reserved_at=reserved_at,
    )

    node_a_totals = await repo.active_latest_totals(
        statuses=(WorkspaceStatus.provisioning,),
        node_id="node-a",
    )
    node_b_totals = await repo.active_latest_totals(
        statuses=(WorkspaceStatus.provisioning,),
        node_id="node-b",
    )

    assert node_a_totals == {
        "workspace_count": 0,
        "steady_cpu": 0.0,
        "steady_memory_gb": 0.0,
        "peak_cpu": 0.0,
        "peak_memory_gb": 0.0,
        "disk_mb": 0,
        "dind_slots": 0,
    }
    assert node_b_totals == {
        "workspace_count": 1,
        "steady_cpu": 2.0,
        "steady_memory_gb": 4.0,
        "peak_cpu": 8.0,
        "peak_memory_gb": 16.0,
        "disk_mb": 1000,
        "dind_slots": 0,
    }


@pytest.mark.unit
async def test_resource_reservation_active_latest_totals_for_workspace_scope_uses_workspace_node(
    session: AsyncSession,
) -> None:
    local_workspace_id, _local_task_id, local_attempt_id = await _attempt(session)
    remote_workspace_id, _remote_task_id, remote_attempt_id = await _attempt(session)
    legacy_workspace_id, _legacy_task_id, legacy_attempt_id = await _attempt(session)
    workspace_repo = WorkspaceRepository(session)
    for workspace_id, node_id in (
        (local_workspace_id, "node-a"),
        (remote_workspace_id, "node-b"),
        (legacy_workspace_id, None),
    ):
        workspace = await workspace_repo.get(workspace_id)
        assert workspace is not None
        workspace.node_id = node_id
        await workspace_repo.transition(
            workspace,
            to=WorkspaceStatus.provisioning,
            reason_code="SEED",
        )

    reserved_at = datetime(2026, 5, 20, 13, 0, tzinfo=UTC)
    repo = ResourceReservationRepository(session)
    await repo.create(
        workspace_id=local_workspace_id,
        attempt_id=local_attempt_id,
        node_id="node-a",
        steady_cpu=20.0,
        steady_memory_gb=40.0,
        peak_cpu=80.0,
        peak_memory_gb=160.0,
        disk_mb=9999,
        dind_slots=3,
        phase="workspace_lifecycle",
        reserved_at=reserved_at - timedelta(minutes=5),
    )
    await repo.create(
        workspace_id=local_workspace_id,
        attempt_id=local_attempt_id,
        node_id="node-b",
        steady_cpu=2.0,
        steady_memory_gb=4.0,
        peak_cpu=8.0,
        peak_memory_gb=16.0,
        disk_mb=1000,
        dind_slots=1,
        phase="workspace_lifecycle",
        reserved_at=reserved_at,
    )
    await repo.create(
        workspace_id=remote_workspace_id,
        attempt_id=remote_attempt_id,
        node_id="node-a",
        steady_cpu=100.0,
        steady_memory_gb=200.0,
        peak_cpu=300.0,
        peak_memory_gb=400.0,
        disk_mb=9000,
        dind_slots=4,
        phase="workspace_lifecycle",
        reserved_at=reserved_at,
    )
    await repo.create(
        workspace_id=legacy_workspace_id,
        attempt_id=legacy_attempt_id,
        node_id="node-c",
        steady_cpu=3.0,
        steady_memory_gb=5.0,
        peak_cpu=7.0,
        peak_memory_gb=11.0,
        disk_mb=500,
        dind_slots=1,
        phase="workspace_lifecycle",
        reserved_at=reserved_at,
    )

    totals = await repo.active_latest_totals_for_workspace_scope(
        statuses=(WorkspaceStatus.provisioning,),
        node_id="node-a",
    )

    assert totals == {
        "workspace_count": 2,
        "steady_cpu": 5.0,
        "steady_memory_gb": 9.0,
        "peak_cpu": 15.0,
        "peak_memory_gb": 27.0,
        "disk_mb": 1500,
        "dind_slots": 2,
    }


@pytest.mark.unit
async def test_resource_reservation_active_latest_totals_for_scheduler_allocation_scope(
    session: AsyncSession,
) -> None:
    local_workspace_id, _local_task_id, local_attempt_id = await _attempt(session)
    null_local_id, _null_local_task_id, null_local_attempt_id = await _attempt(session)
    null_remote_id, _null_remote_task_id, null_remote_attempt_id = await _attempt(session)
    remote_local_id, _remote_local_task_id, remote_local_attempt_id = await _attempt(session)
    remote_remote_id, _remote_remote_task_id, remote_remote_attempt_id = await _attempt(session)
    workspace_repo = WorkspaceRepository(session)
    for workspace_id, node_id in (
        (local_workspace_id, "node-a"),
        (null_local_id, None),
        (null_remote_id, None),
        (remote_local_id, "node-b"),
        (remote_remote_id, "node-b"),
    ):
        workspace = await workspace_repo.get(workspace_id)
        assert workspace is not None
        workspace.node_id = node_id
        await workspace_repo.transition(
            workspace,
            to=WorkspaceStatus.provisioning,
            reason_code="SEED",
        )

    reserved_at = datetime(2026, 5, 20, 13, 0, tzinfo=UTC)
    repo = ResourceReservationRepository(session)
    await repo.create(
        workspace_id=local_workspace_id,
        attempt_id=local_attempt_id,
        node_id="node-b",
        steady_cpu=1.0,
        steady_memory_gb=2.0,
        peak_cpu=3.0,
        peak_memory_gb=4.0,
        disk_mb=100,
        dind_slots=0,
        phase="workspace_lifecycle",
        reserved_at=reserved_at,
    )
    await repo.create(
        workspace_id=null_local_id,
        attempt_id=null_local_attempt_id,
        node_id="node-a",
        steady_cpu=5.0,
        steady_memory_gb=6.0,
        peak_cpu=7.0,
        peak_memory_gb=8.0,
        disk_mb=200,
        dind_slots=1,
        phase="workspace_lifecycle",
        reserved_at=reserved_at,
    )
    await repo.create(
        workspace_id=null_remote_id,
        attempt_id=null_remote_attempt_id,
        node_id="node-b",
        steady_cpu=11.0,
        steady_memory_gb=12.0,
        peak_cpu=13.0,
        peak_memory_gb=14.0,
        disk_mb=300,
        dind_slots=2,
        phase="workspace_lifecycle",
        reserved_at=reserved_at,
    )
    await repo.create(
        workspace_id=remote_local_id,
        attempt_id=remote_local_attempt_id,
        node_id="node-a",
        steady_cpu=17.0,
        steady_memory_gb=18.0,
        peak_cpu=19.0,
        peak_memory_gb=20.0,
        disk_mb=400,
        dind_slots=3,
        phase="workspace_lifecycle",
        reserved_at=reserved_at,
    )
    await repo.create(
        workspace_id=remote_remote_id,
        attempt_id=remote_remote_attempt_id,
        node_id="node-b",
        steady_cpu=23.0,
        steady_memory_gb=24.0,
        peak_cpu=25.0,
        peak_memory_gb=26.0,
        disk_mb=500,
        dind_slots=4,
        phase="workspace_lifecycle",
        reserved_at=reserved_at,
    )

    totals = await repo.active_latest_totals_for_scheduler_allocation_scope(
        statuses=(WorkspaceStatus.provisioning,),
        node_id="node-a",
    )

    assert totals == {
        "workspace_count": 3,
        "steady_cpu": 23.0,
        "steady_memory_gb": 26.0,
        "peak_cpu": 29.0,
        "peak_memory_gb": 32.0,
        "disk_mb": 700,
        "dind_slots": 4,
    }


@pytest.mark.unit
async def test_resource_reservation_scheduler_allocation_scope_counts_null_node_reservation(
    session: AsyncSession,
) -> None:
    null_null_id, _null_null_task_id, null_null_attempt_id = await _attempt(session)
    null_remote_id, _null_remote_task_id, null_remote_attempt_id = await _attempt(session)
    workspace_repo = WorkspaceRepository(session)
    for workspace_id in (null_null_id, null_remote_id):
        workspace = await workspace_repo.get(workspace_id)
        assert workspace is not None
        workspace.node_id = None
        await workspace_repo.transition(
            workspace,
            to=WorkspaceStatus.provisioning,
            reason_code="SEED",
        )

    reserved_at = datetime(2026, 5, 20, 13, 0, tzinfo=UTC)
    repo = ResourceReservationRepository(session)
    await session.execute(
        text("ALTER TABLE resource_reservations ALTER COLUMN node_id DROP NOT NULL")
    )
    await session.execute(
        text(
            """
            INSERT INTO resource_reservations (
                id,
                workspace_id,
                attempt_id,
                node_id,
                steady_cpu,
                steady_memory_gb,
                peak_cpu,
                peak_memory_gb,
                disk_mb,
                dind_slots,
                phase,
                reserved_at,
                released_at,
                created_at,
                updated_at
            )
            VALUES (
                :id,
                :workspace_id,
                :attempt_id,
                NULL,
                :steady_cpu,
                :steady_memory_gb,
                :peak_cpu,
                :peak_memory_gb,
                :disk_mb,
                :dind_slots,
                :phase,
                :reserved_at,
                NULL,
                :reserved_at,
                :reserved_at
            )
            """
        ),
        {
            "id": new_resource_reservation_id(),
            "workspace_id": null_null_id,
            "attempt_id": null_null_attempt_id,
            "steady_cpu": 5.0,
            "steady_memory_gb": 6.0,
            "peak_cpu": 7.0,
            "peak_memory_gb": 8.0,
            "disk_mb": 200,
            "dind_slots": 1,
            "phase": "workspace_lifecycle",
            "reserved_at": reserved_at,
        },
    )
    await repo.create(
        workspace_id=null_remote_id,
        attempt_id=null_remote_attempt_id,
        node_id="node-b",
        steady_cpu=11.0,
        steady_memory_gb=12.0,
        peak_cpu=13.0,
        peak_memory_gb=14.0,
        disk_mb=300,
        dind_slots=2,
        phase="workspace_lifecycle",
        reserved_at=reserved_at,
    )

    totals = await repo.active_latest_totals_for_scheduler_allocation_scope(
        statuses=(WorkspaceStatus.provisioning,),
        node_id="node-a",
    )

    assert totals == {
        "workspace_count": 1,
        "steady_cpu": 5.0,
        "steady_memory_gb": 6.0,
        "peak_cpu": 7.0,
        "peak_memory_gb": 8.0,
        "disk_mb": 200,
        "dind_slots": 1,
    }


@pytest.mark.unit
async def test_resource_reservation_metrics_allocation_scope_counts_null_node_reservation(
    session: AsyncSession,
) -> None:
    null_null_id, _null_null_task_id, null_null_attempt_id = await _attempt(session)
    null_remote_id, _null_remote_task_id, null_remote_attempt_id = await _attempt(session)
    workspace_repo = WorkspaceRepository(session)
    for workspace_id in (null_null_id, null_remote_id):
        workspace = await workspace_repo.get(workspace_id)
        assert workspace is not None
        workspace.node_id = None
        await workspace_repo.transition(
            workspace,
            to=WorkspaceStatus.provisioning,
            reason_code="SEED",
        )

    reserved_at = datetime(2026, 5, 20, 13, 0, tzinfo=UTC)
    repo = ResourceReservationRepository(session)
    await session.execute(
        text("ALTER TABLE resource_reservations ALTER COLUMN node_id DROP NOT NULL")
    )
    await session.execute(
        text(
            """
            INSERT INTO resource_reservations (
                id,
                workspace_id,
                attempt_id,
                node_id,
                steady_cpu,
                steady_memory_gb,
                peak_cpu,
                peak_memory_gb,
                disk_mb,
                dind_slots,
                phase,
                reserved_at,
                released_at,
                created_at,
                updated_at
            )
            VALUES (
                :id,
                :workspace_id,
                :attempt_id,
                NULL,
                :steady_cpu,
                :steady_memory_gb,
                :peak_cpu,
                :peak_memory_gb,
                :disk_mb,
                :dind_slots,
                :phase,
                :reserved_at,
                NULL,
                :reserved_at,
                :reserved_at
            )
            """
        ),
        {
            "id": new_resource_reservation_id(),
            "workspace_id": null_null_id,
            "attempt_id": null_null_attempt_id,
            "steady_cpu": 5.0,
            "steady_memory_gb": 6.0,
            "peak_cpu": 7.0,
            "peak_memory_gb": 8.0,
            "disk_mb": 200,
            "dind_slots": 1,
            "phase": "workspace_lifecycle",
            "reserved_at": reserved_at,
        },
    )
    await repo.create(
        workspace_id=null_remote_id,
        attempt_id=null_remote_attempt_id,
        node_id="node-b",
        steady_cpu=11.0,
        steady_memory_gb=12.0,
        peak_cpu=13.0,
        peak_memory_gb=14.0,
        disk_mb=300,
        dind_slots=2,
        phase="workspace_lifecycle",
        reserved_at=reserved_at,
    )

    totals = await repo.active_latest_totals_for_metrics_allocation_scope(
        statuses=(WorkspaceStatus.provisioning,),
        node_id="node-a",
    )

    assert totals == {
        "workspace_count": 1,
        "steady_cpu": 5.0,
        "steady_memory_gb": 6.0,
        "peak_cpu": 7.0,
        "peak_memory_gb": 8.0,
        "disk_mb": 200,
        "dind_slots": 1,
    }
