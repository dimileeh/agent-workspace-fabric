"""Workspace reliability summary service tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.common.config import Settings
from awf.db.enums import WorkspaceStatus
from awf.db.repositories import (
    ProviderModelCircuitBreakerRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.service import metrics_capacity
from awf.service.disk import DiskCheck
from awf.service.orphan_resources import (
    WorkspaceIdView,
    build_orphan_resource_summary,
    scan_docker_resources,
    scan_managed_worktrees,
)
from tests.unit.helpers import create_workspace

_MIB = 1024 * 1024


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


def _empty_reservation_totals() -> dict[str, float | int]:
    return {
        "workspace_count": 0,
        "steady_cpu": 0.0,
        "steady_memory_gb": 0.0,
        "peak_cpu": 0.0,
        "peak_memory_gb": 0.0,
        "disk_mb": 0,
        "dind_slots": 0,
    }


async def _reservation_for_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    node_id: str = "local",
    steady_cpu: float,
    steady_memory_gb: float,
    peak_cpu: float,
    peak_memory_gb: float,
    disk_mb: int | None = None,
    dind_slots: int = 0,
    reserved_at: datetime | None = None,
    released_at: datetime | None = None,
) -> None:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        task = await TaskRepository(session).create_or_get(
            repo_url=workspace.repo_url,
            base_branch=workspace.branch_base,
            title=workspace.task_title,
            prompt=workspace.task_prompt,
            external_id=None,
            idempotency_key=f"metrics-reservation:{workspace.id}",
            task_class=workspace.task_class,
            owned_paths=list(workspace.owned_paths),
        )
        attempt_repo = TaskAttemptRepository(session)
        attempt = await attempt_repo.get_by_workspace_id(workspace.id)
        if attempt is None:
            attempt = await attempt_repo.create_for_workspace(
                task=task,
                workspace=workspace,
            )
        reservation = await ResourceReservationRepository(session).create(
            workspace_id=workspace.id,
            attempt_id=attempt.id,
            node_id=node_id,
            steady_cpu=steady_cpu,
            steady_memory_gb=steady_memory_gb,
            peak_cpu=peak_cpu,
            peak_memory_gb=peak_memory_gb,
            disk_mb=disk_mb,
            dind_slots=dind_slots,
            phase="workspace_lifecycle",
            reserved_at=reserved_at,
        )
        reservation.released_at = released_at
        await session.commit()


def _disk_check(
    *,
    ok: bool = True,
    free_bytes: int = 900,
    threshold_bytes: int = 400,
    reason: str = "SUFFICIENT_DISK",
) -> DiskCheck:
    total_bytes = max(1000, free_bytes + 1)
    return DiskCheck(
        path="/tmp/awf-work",
        checked_path="/tmp",
        total_bytes=total_bytes,
        used_bytes=total_bytes - free_bytes,
        free_bytes=free_bytes,
        percent_free=round(free_bytes / total_bytes * 100, 2),
        threshold_bytes=threshold_bytes,
        ok=ok,
        status="ok" if ok else "fail",
        reason=reason,
        detail=None if ok else "Free disk is below the configured admission threshold.",
    )


def _empty_run(args: list[str], **_kwargs: object) -> Any:
    if args[:3] in (
        ["docker", "ps", "-a"],
        ["docker", "network", "ls"],
        ["docker", "volume", "ls"],
    ):
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    raise AssertionError(f"unexpected subprocess call: {args}")


@pytest.mark.unit
async def test_resource_saturation_structurally_invalid_timestamps_rejected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_resource_saturation
    from awf.service.provider_recovery import PROVIDER_RECOVERY_STATE_KEY

    settings = Settings(_env_file=None, work_dir="/tmp/awf-work")
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
        task_policy={
            PROVIDER_RECOVERY_STATE_KEY: {
                "action": "retry",
                "not_before": "2026-99-99T99:99:99",
            },
        },
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
        task_policy={
            PROVIDER_RECOVERY_STATE_KEY: {
                "action": "retry",
                "not_before": "2026-05-01T11:00:00Zextra",
            },
        },
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
        task_policy={
            PROVIDER_RECOVERY_STATE_KEY: {
                "action": "retry",
                "not_before": "2026-13-01T11:00:00Z",
            },
        },
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
        task_policy={
            PROVIDER_RECOVERY_STATE_KEY: {
                "action": "retry",
                "not_before": "2026-05-01T25:00:00Z",
            },
        },
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
        task_policy={
            PROVIDER_RECOVERY_STATE_KEY: {
                "action": "retry",
                "not_before": "2026-05-01T11:00:00Z",
            },
        },
    )

    summary = await summarize_resource_saturation(
        session_factory,
        settings=settings,
        disk_check=_disk_check(),
        now=now,
    )

    prs = summary.provider_recovery_state_summary
    assert prs.pending_retry == 1
    assert prs.in_cooldown == 0


@pytest.mark.unit
async def test_resource_saturation_terminal_uses_decision_reason_code_over_source_reason_code(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_resource_saturation
    from awf.service.provider_recovery import (
        PROVIDER_RECOVERY_NO_LOOP_REASON,
        PROVIDER_RECOVERY_STATE_KEY,
    )

    settings = Settings(_env_file=None, work_dir="/tmp/awf-work")
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now,
        task_policy={
            PROVIDER_RECOVERY_STATE_KEY: {
                "action": "terminal",
                "source_reason_code": "PROVIDER_RETRY_DELAYED",
            },
        },
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now,
        task_policy={
            PROVIDER_RECOVERY_STATE_KEY: {
                "action": "terminal",
                "decision_reason_code": PROVIDER_RECOVERY_NO_LOOP_REASON,
                "source_reason_code": "PROVIDER_RETRY_DELAYED",
            },
        },
    )

    summary = await summarize_resource_saturation(
        session_factory,
        settings=settings,
        disk_check=_disk_check(),
        now=now,
    )
    prs = summary.provider_recovery_state_summary
    assert prs.terminal_no_loop == 1
    assert prs.terminal_exhausted == 1


@pytest.mark.unit
async def test_resource_saturation_terminal_null_source_reason_code_counted_as_exhausted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_resource_saturation
    from awf.service.provider_recovery import (
        PROVIDER_RECOVERY_STATE_KEY,
    )

    settings = Settings(_env_file=None, work_dir="/tmp/awf-work")
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now,
        task_policy={
            PROVIDER_RECOVERY_STATE_KEY: {
                "action": "terminal",
            },
        },
    )

    summary = await summarize_resource_saturation(
        session_factory,
        settings=settings,
        disk_check=_disk_check(),
        now=now,
    )
    prs = summary.provider_recovery_state_summary
    assert prs.terminal_no_loop == 0
    assert prs.terminal_exhausted == 1


@pytest.mark.unit
async def test_resource_saturation_includes_orphan_resource_summary(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path,
) -> None:
    from awf.service.metrics import summarize_resource_saturation

    (tmp_path / "git" / "worktrees" / "ws_dead").mkdir(parents=True)
    orphan_summary = build_orphan_resource_summary(
        docker_scan=scan_docker_resources(
            docker_host="unix:///var/run/docker.sock",
            run_subprocess=_empty_run,
        ),
        worktree_scan=scan_managed_worktrees(tmp_path),
        workspace_view=WorkspaceIdView(
            active_ids=frozenset(),
            terminal_ids=frozenset({"ws_dead"}),
            available=True,
        ),
    )

    summary = await summarize_resource_saturation(
        session_factory,
        settings=Settings(_env_file=None, work_dir=str(tmp_path)),
        disk_check=_disk_check(),
        orphan_resources=orphan_summary,
    )

    assert summary.orphan_resources.orphan_count == 1
    assert summary.orphan_resources.orphan_counts_by_kind["worktree"] == 1
    assert summary.orphan_resources.cleanup_readiness.reason == "ORPHAN_RESOURCES_PRESENT"


@pytest.mark.unit
async def test_resource_saturation_includes_runtime_health_counts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_resource_saturation
    from awf.service.workspace_runtime_health import (
        RuntimeResource,
        RuntimeWorkspace,
        summarize_runtime_health,
    )

    runtime_health = summarize_runtime_health(
        workspaces=(
            RuntimeWorkspace(
                workspace_id="ws_missing_stack",
                status=WorkspaceStatus.running.value,
                compose_project_name="awf_ws_missing_stack",
                compose_file_path="/tmp/ws_missing_stack/compose.yml",
            ),
            RuntimeWorkspace(
                workspace_id="ws_exited",
                status=WorkspaceStatus.running.value,
                compose_project_name="awf_ws_exited",
                compose_file_path="/tmp/ws_exited/compose.yml",
            ),
            RuntimeWorkspace(
                workspace_id="ws_monitor",
                status=WorkspaceStatus.monitoring_pr.value,
                compose_project_name="awf_ws_monitor",
                compose_file_path="/tmp/ws_monitor/compose.yml",
                pr_url="https://github.com/example/repo/pull/42",
            ),
        ),
        resources=(
            RuntimeResource(
                resource_kind="container",
                workspace_id="ws_exited",
                compose_project="awf_ws_exited",
                service="agent",
                state="exited",
                container_id="agent",
            ),
        ),
    )

    summary = await summarize_resource_saturation(
        session_factory,
        settings=Settings(_env_file=None, work_dir="/tmp/awf-work"),
        disk_check=_disk_check(),
        runtime_health=runtime_health,
    )

    assert summary.runtime_health.stranded_count == 3
    assert summary.runtime_health.fail_candidate_count == 2
    assert summary.runtime_health.recoverable_count == 1
    assert summary.runtime_health.reason_counts == {
        "AGENT_CONTAINER_EXITED": 1,
        "STRANDED_WORKSPACE": 2,
    }


@pytest.mark.unit
async def test_capacity_queue_blocked_reason_counts_loads_latest_requested_demands_once(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import _capacity_queue_blocked_reason_counts
    from awf.service.resource_capacity import ReservedResources, WorkspaceResourceDefaults

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.requested,
        updated_at=now,
    )
    reserved_workspace_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.requested,
        updated_at=now,
    )
    await _reservation_for_workspace(
        session_factory,
        reserved_workspace_id,
        steady_cpu=20.0,
        steady_memory_gb=40.0,
        peak_cpu=20.0,
        peak_memory_gb=40.0,
        dind_slots=0,
        reserved_at=now - timedelta(minutes=5),
    )
    await _reservation_for_workspace(
        session_factory,
        reserved_workspace_id,
        steady_cpu=1.0,
        steady_memory_gb=4.0,
        peak_cpu=7.0,
        peak_memory_gb=25.0,
        dind_slots=1,
        reserved_at=now,
    )

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

    async with session_factory() as session:
        event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
        try:
            counts = await _capacity_queue_blocked_reason_counts(
                session,
                settings=Settings(
                    _env_file=None,
                    local_capacity_cpu_cores=8.0,
                    local_capacity_memory_gb=24.0,
                    local_capacity_dind_slots=1,
                ),
                node_id="local",
                allocated_resources=ReservedResources(
                    active_workspace_count=1,
                    steady_cpu=2.0,
                    steady_memory_gb=4.0,
                    peak_cpu=4.0,
                    peak_memory_gb=8.0,
                    disk_mb=0,
                    dind_slots=1,
                ),
                resource_defaults=WorkspaceResourceDefaults(
                    steady_cpu=3.0,
                    steady_memory_gb=8.0,
                    peak_cpu=6.0,
                    peak_memory_gb=16.0,
                ),
                detected_local_capacity=None,
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record_sql)

    assert counts == {
        "DIND_CAPACITY_SATURATED": 1,
        "PEAK_CPU_CAPACITY_SATURATED": 1,
        "PEAK_MEMORY_CAPACITY_SATURATED": 1,
    }
    assert len(statements) == 1
    assert "sum(case" not in statements[0]
    assert "row_number() over" in statements[0]
    assert "left outer join" in statements[0]
    assert "select workspaces.id as queue_workspace_id" in statements[0]
    assert "workspaces.repo_url" not in statements[0]
    assert " limit " in statements[0]


@pytest.mark.unit
async def test_capacity_queue_candidates_prefilter_reservations_to_requested_scope(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import _capacity_queue_blocked_reason_counts
    from awf.service.resource_capacity import ReservedResources, WorkspaceResourceDefaults

    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    requested_workspace_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.requested,
        updated_at=now,
    )
    running_workspace_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
    )
    async with session_factory() as session:
        for workspace_id in (requested_workspace_id, running_workspace_id):
            workspace = await WorkspaceRepository(session).get(workspace_id)
            assert workspace is not None
            workspace.node_id = "local"
        await session.commit()
    for workspace_id in (requested_workspace_id, running_workspace_id):
        await _reservation_for_workspace(
            session_factory,
            workspace_id,
            steady_cpu=1.0,
            steady_memory_gb=2.0,
            peak_cpu=1.0,
            peak_memory_gb=2.0,
            dind_slots=0,
            reserved_at=now,
        )

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

    async with session_factory() as session:
        event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
        try:
            counts = await _capacity_queue_blocked_reason_counts(
                session,
                settings=Settings(_env_file=None, local_capacity_cpu_cores=2.0),
                node_id="local",
                allocated_resources=ReservedResources(
                    active_workspace_count=0,
                    steady_cpu=0.0,
                    steady_memory_gb=0.0,
                    peak_cpu=0.0,
                    peak_memory_gb=0.0,
                    disk_mb=0,
                    dind_slots=0,
                ),
                resource_defaults=WorkspaceResourceDefaults(
                    steady_cpu=1.0,
                    steady_memory_gb=2.0,
                    peak_cpu=1.0,
                    peak_memory_gb=2.0,
                ),
                detected_local_capacity=None,
                scoring_at=now,
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record_sql)

    assert counts == {}
    assert len(statements) == 1
    assert "join workspaces as requested_reservation_workspace" in statements[0]
    assert "requested_reservation_workspace.status =" in statements[0]
    assert "requested_reservation_workspace.node_id =" in statements[0]


@pytest.mark.unit
async def test_capacity_queue_blocked_reason_counts_limits_after_scheduler_priority(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import metrics
    from awf.service.resource_capacity import ReservedResources, WorkspaceResourceDefaults

    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(metrics_capacity, "DEFAULT_CAPACITY_QUEUE_BLOCKER_SCAN_LIMIT", 2)
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.requested,
        updated_at=now,
        created_at=now - timedelta(minutes=30),
        task_policy={"scheduler": {"base_priority": 0}},
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.requested,
        updated_at=now,
        created_at=now - timedelta(minutes=29),
        task_policy={"scheduler": {"base_priority": 0}},
    )
    high_priority_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.requested,
        updated_at=now,
        created_at=now - timedelta(minutes=1),
        task_policy={"scheduler": {"base_priority": 100}},
    )
    await _reservation_for_workspace(
        session_factory,
        high_priority_id,
        steady_cpu=1.0,
        steady_memory_gb=2.0,
        peak_cpu=5.0,
        peak_memory_gb=2.0,
        dind_slots=0,
        reserved_at=now,
    )

    async with session_factory() as session:
        counts = await metrics._capacity_queue_blocked_reason_counts(
            session,
            settings=Settings(
                _env_file=None,
                local_capacity_cpu_cores=4.0,
            ),
            node_id="local",
            allocated_resources=ReservedResources(
                active_workspace_count=0,
                steady_cpu=0.0,
                steady_memory_gb=0.0,
                peak_cpu=0.0,
                peak_memory_gb=0.0,
                disk_mb=0,
                dind_slots=0,
            ),
            resource_defaults=WorkspaceResourceDefaults(
                steady_cpu=1.0,
                steady_memory_gb=2.0,
                peak_cpu=1.0,
                peak_memory_gb=2.0,
            ),
            detected_local_capacity=None,
            scoring_at=now,
        )

    assert counts == {"PEAK_CPU_CAPACITY_SATURATED": 1}


@pytest.mark.unit
async def test_capacity_queue_blocked_reason_counts_accumulates_fifo_demands(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import _capacity_queue_blocked_reason_counts
    from awf.service.resource_capacity import ReservedResources, WorkspaceResourceDefaults

    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    first_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.requested,
        updated_at=now,
        created_at=now,
    )
    second_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.requested,
        updated_at=now,
        created_at=now + timedelta(seconds=1),
    )
    async with session_factory() as session:
        for workspace_id in (first_id, second_id):
            workspace = await WorkspaceRepository(session).get(workspace_id)
            assert workspace is not None
            workspace.resolved_profile = {"docker": {"mode": "dind"}}
        await session.commit()

    async with session_factory() as session:
        counts = await _capacity_queue_blocked_reason_counts(
            session,
            settings=Settings(
                _env_file=None,
                local_capacity_dind_slots=1,
            ),
            node_id="local",
            allocated_resources=ReservedResources(
                active_workspace_count=0,
                steady_cpu=0.0,
                steady_memory_gb=0.0,
                peak_cpu=0.0,
                peak_memory_gb=0.0,
                disk_mb=0,
                dind_slots=0,
            ),
            resource_defaults=WorkspaceResourceDefaults(
                steady_cpu=1.0,
                steady_memory_gb=2.0,
                peak_cpu=1.0,
                peak_memory_gb=2.0,
            ),
            detected_local_capacity=None,
        )

    assert counts == {"DIND_CAPACITY_SATURATED": 1}


@pytest.mark.unit
async def test_capacity_queue_blocked_reason_counts_collapses_fifo_deferred_frontier(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import _capacity_queue_blocked_reason_counts
    from awf.service.resource_capacity import ReservedResources, WorkspaceResourceDefaults

    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    workspace_ids = [
        await create_workspace(
            session_factory,
            status=WorkspaceStatus.requested,
            updated_at=now,
            created_at=now + timedelta(seconds=index),
        )
        for index in range(3)
    ]
    async with session_factory() as session:
        for workspace_id in workspace_ids:
            workspace = await WorkspaceRepository(session).get(workspace_id)
            assert workspace is not None
            workspace.resolved_profile = {"docker": {"mode": "dind"}}
        await session.commit()

    async with session_factory() as session:
        counts = await _capacity_queue_blocked_reason_counts(
            session,
            settings=Settings(
                _env_file=None,
                local_capacity_dind_slots=1,
            ),
            node_id="local",
            allocated_resources=ReservedResources(
                active_workspace_count=1,
                steady_cpu=0.0,
                steady_memory_gb=0.0,
                peak_cpu=0.0,
                peak_memory_gb=0.0,
                disk_mb=0,
                dind_slots=1,
            ),
            resource_defaults=WorkspaceResourceDefaults(
                steady_cpu=1.0,
                steady_memory_gb=2.0,
                peak_cpu=1.0,
                peak_memory_gb=2.0,
            ),
            detected_local_capacity=None,
        )

    assert counts == {"DIND_CAPACITY_SATURATED": 1}


@pytest.mark.unit
async def test_capacity_queue_blocked_reason_counts_uses_stale_node_reservation_demand(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import _capacity_queue_blocked_reason_counts
    from awf.service.resource_capacity import ReservedResources, WorkspaceResourceDefaults

    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    workspace_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.requested,
        updated_at=now,
    )
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).get(workspace_id)
        assert workspace is not None
        workspace.node_id = "local"
        await session.commit()
    await _reservation_for_workspace(
        session_factory,
        workspace_id,
        node_id="prior-node",
        steady_cpu=2.0,
        steady_memory_gb=4.0,
        peak_cpu=9.0,
        peak_memory_gb=8.0,
        dind_slots=0,
        reserved_at=now,
    )

    async with session_factory() as session:
        counts = await _capacity_queue_blocked_reason_counts(
            session,
            settings=Settings(
                _env_file=None,
                local_capacity_cpu_cores=8.0,
            ),
            node_id="local",
            allocated_resources=ReservedResources(
                active_workspace_count=0,
                steady_cpu=0.0,
                steady_memory_gb=0.0,
                peak_cpu=0.0,
                peak_memory_gb=0.0,
                disk_mb=0,
                dind_slots=0,
            ),
            resource_defaults=WorkspaceResourceDefaults(
                steady_cpu=1.0,
                steady_memory_gb=2.0,
                peak_cpu=1.0,
                peak_memory_gb=2.0,
            ),
            detected_local_capacity=None,
        )

    assert counts == {"PEAK_CPU_CAPACITY_SATURATED": 1}


@pytest.mark.unit
async def test_capacity_queue_blocked_reason_counts_ignores_detected_cpu_and_memory_limits(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import _capacity_queue_blocked_reason_counts
    from awf.service.resource_capacity import (
        LocalCapacityLimits,
        ReservedResources,
        WorkspaceResourceDefaults,
    )

    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    workspace_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.requested,
        updated_at=now,
    )
    await _reservation_for_workspace(
        session_factory,
        workspace_id,
        steady_cpu=2.0,
        steady_memory_gb=4.0,
        peak_cpu=3.0,
        peak_memory_gb=5.0,
        dind_slots=1,
        reserved_at=now,
    )

    async with session_factory() as session:
        counts = await _capacity_queue_blocked_reason_counts(
            session,
            settings=Settings(
                _env_file=None,
                local_capacity_cpu_cores=None,
                local_capacity_memory_gb=None,
                local_capacity_dind_slots=1,
            ),
            node_id="local",
            allocated_resources=ReservedResources(
                active_workspace_count=0,
                steady_cpu=0.0,
                steady_memory_gb=0.0,
                peak_cpu=0.0,
                peak_memory_gb=0.0,
                disk_mb=0,
                dind_slots=1,
            ),
            resource_defaults=WorkspaceResourceDefaults(
                steady_cpu=2.0,
                steady_memory_gb=4.0,
                peak_cpu=3.0,
                peak_memory_gb=5.0,
            ),
            detected_local_capacity=LocalCapacityLimits(cpu_cores=1.0, memory_gb=1.0),
        )

    assert counts == {"DIND_CAPACITY_SATURATED": 1}


@pytest.mark.unit
async def test_capacity_queue_blocked_reason_counts_excludes_provider_cooldown_candidates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import _capacity_queue_blocked_reason_counts
    from awf.service.provider_recovery import PROVIDER_RECOVERY_STATE_KEY
    from awf.service.resource_capacity import ReservedResources, WorkspaceResourceDefaults

    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    workspace_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.requested,
        updated_at=now,
        task_policy={
            PROVIDER_RECOVERY_STATE_KEY: {
                "not_before": (now + timedelta(minutes=5)).isoformat(),
            }
        },
    )
    await _reservation_for_workspace(
        session_factory,
        workspace_id,
        steady_cpu=1.0,
        steady_memory_gb=2.0,
        peak_cpu=1.0,
        peak_memory_gb=2.0,
        dind_slots=1,
        reserved_at=now,
    )

    async with session_factory() as session:
        counts = await _capacity_queue_blocked_reason_counts(
            session,
            settings=Settings(
                _env_file=None,
                local_capacity_dind_slots=1,
            ),
            node_id="local",
            allocated_resources=ReservedResources(
                active_workspace_count=1,
                steady_cpu=0.0,
                steady_memory_gb=0.0,
                peak_cpu=0.0,
                peak_memory_gb=0.0,
                disk_mb=0,
                dind_slots=1,
            ),
            resource_defaults=WorkspaceResourceDefaults(
                steady_cpu=1.0,
                steady_memory_gb=2.0,
                peak_cpu=1.0,
                peak_memory_gb=2.0,
            ),
            detected_local_capacity=None,
            scoring_at=now,
        )

    assert counts == {}


@pytest.mark.unit
async def test_capacity_queue_blocked_reason_counts_excludes_open_provider_circuit_candidates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import _capacity_queue_blocked_reason_counts
    from awf.service.resource_capacity import ReservedResources, WorkspaceResourceDefaults

    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    model = "gpt-5.3-codex-spark"
    workspace_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.requested,
        updated_at=now,
        task_policy={"agent_model": model},
    )
    await _reservation_for_workspace(
        session_factory,
        workspace_id,
        steady_cpu=1.0,
        steady_memory_gb=2.0,
        peak_cpu=1.0,
        peak_memory_gb=2.0,
        dind_slots=1,
        reserved_at=now,
    )
    async with session_factory() as session:
        await ProviderModelCircuitBreakerRepository(session).record_failure(
            provider="openai",
            model=model,
            reason_code="PROVIDER_MODEL_CIRCUIT_OPEN",
            failure_fingerprint="provider-capacity",
            workspace_id=workspace_id,
            attempt_id=None,
            now=now,
            failure_threshold=1,
            cooldown_seconds=600,
        )
        await session.commit()

    async with session_factory() as session:
        counts = await _capacity_queue_blocked_reason_counts(
            session,
            settings=Settings(
                _env_file=None,
                local_capacity_dind_slots=1,
            ),
            node_id="local",
            allocated_resources=ReservedResources(
                active_workspace_count=1,
                steady_cpu=0.0,
                steady_memory_gb=0.0,
                peak_cpu=0.0,
                peak_memory_gb=0.0,
                disk_mb=0,
                dind_slots=1,
            ),
            resource_defaults=WorkspaceResourceDefaults(
                steady_cpu=1.0,
                steady_memory_gb=2.0,
                peak_cpu=1.0,
                peak_memory_gb=2.0,
            ),
            detected_local_capacity=None,
            scoring_at=now,
        )

    assert counts == {}


@pytest.mark.unit
async def test_capacity_queue_blocked_reason_counts_refills_after_provider_suppression(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import metrics
    from awf.service.provider_recovery import PROVIDER_RECOVERY_STATE_KEY
    from awf.service.resource_capacity import ReservedResources, WorkspaceResourceDefaults

    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(metrics_capacity, "DEFAULT_CAPACITY_QUEUE_BLOCKER_SCAN_LIMIT", 2)
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.requested,
        updated_at=now,
        created_at=now,
        task_policy={
            "scheduler": {"base_priority": 100},
            PROVIDER_RECOVERY_STATE_KEY: {
                "not_before": (now + timedelta(minutes=5)).isoformat(),
            },
        },
    )
    model = "gpt-5.3-codex-spark"
    circuit_workspace_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.requested,
        updated_at=now,
        created_at=now + timedelta(seconds=1),
        task_policy={
            "agent_model": model,
            "scheduler": {"base_priority": 90},
        },
    )
    eligible_workspace_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.requested,
        updated_at=now,
        created_at=now + timedelta(seconds=2),
        task_policy={"scheduler": {"base_priority": 1}},
    )
    async with session_factory() as session:
        await ProviderModelCircuitBreakerRepository(session).record_failure(
            provider="openai",
            model=model,
            reason_code="PROVIDER_MODEL_CIRCUIT_OPEN",
            failure_fingerprint="provider-capacity",
            workspace_id=circuit_workspace_id,
            attempt_id=None,
            now=now,
            failure_threshold=1,
            cooldown_seconds=600,
        )
        await session.commit()
    await _reservation_for_workspace(
        session_factory,
        eligible_workspace_id,
        steady_cpu=1.0,
        steady_memory_gb=2.0,
        peak_cpu=1.0,
        peak_memory_gb=2.0,
        dind_slots=1,
        reserved_at=now,
    )

    async with session_factory() as session:
        counts = await metrics._capacity_queue_blocked_reason_counts(
            session,
            settings=Settings(
                _env_file=None,
                local_capacity_dind_slots=1,
            ),
            node_id="local",
            allocated_resources=ReservedResources(
                active_workspace_count=1,
                steady_cpu=0.0,
                steady_memory_gb=0.0,
                peak_cpu=0.0,
                peak_memory_gb=0.0,
                disk_mb=0,
                dind_slots=1,
            ),
            resource_defaults=WorkspaceResourceDefaults(
                steady_cpu=1.0,
                steady_memory_gb=2.0,
                peak_cpu=1.0,
                peak_memory_gb=2.0,
            ),
            detected_local_capacity=None,
            scoring_at=now,
        )

    assert counts == {"DIND_CAPACITY_SATURATED": 1}


@pytest.mark.unit
async def test_capacity_queue_blocked_reason_counts_caps_provider_suppression_refill_pages(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import metrics
    from awf.service.provider_recovery import PROVIDER_RECOVERY_STATE_KEY
    from awf.service.resource_capacity import ReservedResources, WorkspaceResourceDefaults

    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(metrics_capacity, "DEFAULT_CAPACITY_QUEUE_BLOCKER_SCAN_LIMIT", 2)
    monkeypatch.setattr(
        metrics_capacity,
        "DEFAULT_CAPACITY_QUEUE_BLOCKER_REFILL_PAGE_LIMIT",
        2,
        raising=False,
    )
    for index in range(5):
        await create_workspace(
            session_factory,
            status=WorkspaceStatus.requested,
            updated_at=now,
            created_at=now + timedelta(seconds=index),
            task_policy={
                "scheduler": {"base_priority": 100 - index},
                PROVIDER_RECOVERY_STATE_KEY: {
                    "not_before": (now + timedelta(minutes=5)).isoformat(),
                },
            },
        )

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

    async with session_factory() as session:
        event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
        try:
            counts = await metrics._capacity_queue_blocked_reason_counts(
                session,
                settings=Settings(
                    _env_file=None,
                    local_capacity_dind_slots=1,
                ),
                node_id="local",
                allocated_resources=ReservedResources(
                    active_workspace_count=1,
                    steady_cpu=0.0,
                    steady_memory_gb=0.0,
                    peak_cpu=0.0,
                    peak_memory_gb=0.0,
                    disk_mb=0,
                    dind_slots=1,
                ),
                resource_defaults=WorkspaceResourceDefaults(
                    steady_cpu=1.0,
                    steady_memory_gb=2.0,
                    peak_cpu=1.0,
                    peak_memory_gb=2.0,
                ),
                detected_local_capacity=None,
                scoring_at=now,
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record_sql)

    queue_page_reads = [
        statement
        for statement in statements
        if "select workspaces.id as queue_workspace_id" in statement
    ]
    assert counts == {}
    assert len(queue_page_reads) == 2


@pytest.mark.unit
async def test_resource_saturation_defaulted_dind_profiles_are_counted_everywhere(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_resource_saturation

    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    allocated_workspace_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
    )
    requested_workspace_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.requested,
        updated_at=now,
    )
    async with session_factory() as session:
        for workspace_id in (allocated_workspace_id, requested_workspace_id):
            workspace = await WorkspaceRepository(session).get(workspace_id)
            assert workspace is not None
            workspace.resolved_profile = {"docker": {"mode": "dind"}}
        await session.commit()

    summary = await summarize_resource_saturation(
        session_factory,
        settings=Settings(
            _env_file=None,
            work_dir="/tmp/awf-work",
            local_capacity_dind_slots=1,
        ),
        disk_check=_disk_check(),
        now=now,
    )

    assert summary.reserved_resources.active_workspace_count == 2
    assert summary.reserved_resources.dind_slots == 2
    assert summary.allocated_resources.active_workspace_count == 1
    assert summary.allocated_resources.dind_slots == 1
    assert summary.capacity_queue.queued_workspace_count == 1
    assert summary.capacity_queue.planned_resources.dind_slots == 1
    assert summary.capacity_queue.blocked_reason_counts == {"DIND_CAPACITY_SATURATED": 1}


@pytest.mark.unit
async def test_defaulted_dind_slots_are_aggregated_without_profile_materialization(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import _defaulted_dind_slots_for_session

    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    dind_workspace_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
    )
    host_workspace_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
    )
    reserved_workspace_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
    )
    remote_workspace_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
    )
    async with session_factory() as session:
        profiles = {
            dind_workspace_id: ("local", {"docker": {"mode": "dind"}}),
            host_workspace_id: ("local", {"docker": {"mode": "host"}}),
            reserved_workspace_id: ("local", {"docker": {"mode": "dind"}}),
            remote_workspace_id: ("remote", {"docker": {"mode": "dind"}}),
        }
        for workspace_id, (node_id, profile) in profiles.items():
            workspace = await WorkspaceRepository(session).get(workspace_id)
            assert workspace is not None
            workspace.node_id = node_id
            workspace.resolved_profile = profile
        await session.commit()
    await _reservation_for_workspace(
        session_factory,
        reserved_workspace_id,
        steady_cpu=1.0,
        steady_memory_gb=1.0,
        peak_cpu=1.0,
        peak_memory_gb=1.0,
        reserved_at=now,
    )

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

    async with session_factory() as session:
        event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
        try:
            slots = await _defaulted_dind_slots_for_session(
                session,
                statuses=(WorkspaceStatus.running,),
                node_id="local",
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record_sql)

    assert slots == 1
    assert len(statements) == 1
    assert "sum(case" in statements[0]
    assert "select workspaces.resolved_profile" not in statements[0]


@pytest.mark.unit
async def test_resource_saturation_prefers_active_reservations_and_falls_back_for_old_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_resource_saturation

    settings = Settings(
        _env_file=None,
        work_dir="/tmp/awf-work",
        workspace_steady_cpu=3.0,
        workspace_steady_memory_gb=10.0,
        workspace_peak_cpu=6.0,
        workspace_peak_memory_gb=16.0,
    )
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    reserved_workspace_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
    )
    legacy_workspace_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.ready,
        updated_at=now,
    )
    released_workspace_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now,
    )
    await _reservation_for_workspace(
        session_factory,
        reserved_workspace_id,
        steady_cpu=4.0,
        steady_memory_gb=12.0,
        peak_cpu=8.0,
        peak_memory_gb=24.0,
        disk_mb=4096,
    )
    await _reservation_for_workspace(
        session_factory,
        released_workspace_id,
        steady_cpu=100.0,
        steady_memory_gb=100.0,
        peak_cpu=100.0,
        peak_memory_gb=100.0,
        disk_mb=8192,
        released_at=now,
    )

    summary = await summarize_resource_saturation(
        session_factory,
        settings=settings,
        disk_check=_disk_check(),
        now=now,
    )

    assert legacy_workspace_id
    assert summary.workspace_counts.active_total == 2
    assert summary.reserved_resources.active_workspace_count == 2
    assert summary.reserved_resources.steady_cpu == 7.0
    assert summary.reserved_resources.steady_memory_gb == 22.0
    assert summary.reserved_resources.peak_cpu == 14.0
    assert summary.reserved_resources.peak_memory_gb == 40.0
    assert summary.reserved_resources.disk_mb == 4096
    assert summary.reserved_resources.dind_slots == 0


@pytest.mark.unit
async def test_resource_saturation_uses_latest_active_reservation_per_workspace(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_resource_saturation

    settings = Settings(
        _env_file=None,
        work_dir="/tmp/awf-work",
        workspace_steady_cpu=3.0,
        workspace_steady_memory_gb=10.0,
        workspace_peak_cpu=6.0,
        workspace_peak_memory_gb=16.0,
    )
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    workspace_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
    )
    await _reservation_for_workspace(
        session_factory,
        workspace_id,
        steady_cpu=4.0,
        steady_memory_gb=12.0,
        peak_cpu=8.0,
        peak_memory_gb=24.0,
        disk_mb=4096,
        reserved_at=now - timedelta(minutes=5),
    )
    await _reservation_for_workspace(
        session_factory,
        workspace_id,
        steady_cpu=6.0,
        steady_memory_gb=14.0,
        peak_cpu=10.0,
        peak_memory_gb=28.0,
        disk_mb=4096,
        reserved_at=now,
    )

    summary = await summarize_resource_saturation(
        session_factory,
        settings=settings,
        disk_check=_disk_check(),
        now=now,
    )

    assert summary.reserved_resources.active_workspace_count == 1
    assert summary.reserved_resources.steady_cpu == 6.0
    assert summary.reserved_resources.steady_memory_gb == 14.0
    assert summary.reserved_resources.peak_cpu == 10.0
    assert summary.reserved_resources.peak_memory_gb == 28.0
    assert summary.reserved_resources.disk_mb == 4096
