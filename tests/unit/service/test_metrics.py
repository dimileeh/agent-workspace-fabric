"""Workspace reliability summary service tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.common.config import Settings
from awf.db.enums import FailureReason, OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import (
    ProviderModelCircuitBreakerRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.planning import PLAN_CONFORMANCE_UNSATISFIED
from awf.service.disk import DiskCheck
from awf.service.orphan_resources import (
    WorkspaceIdView,
    build_orphan_resource_summary,
    scan_docker_resources,
    scan_managed_worktrees,
)
from tests.unit.helpers import create_operation, create_workspace, zero_status_counts

_MIB = 1024 * 1024


@pytest.mark.unit
def test_metrics_to_utc_accepts_naive_datetime() -> None:
    from awf.service import metrics

    naive = datetime(2026, 5, 7, 14, 45)

    assert metrics._to_utc(naive) == naive.replace(tzinfo=UTC)


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


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
def test_failure_actions_cover_every_known_failure_reason() -> None:
    from awf.service import metrics

    assert set(metrics._FAILURE_ACTIONS) == {reason.value for reason in FailureReason}


@pytest.mark.unit
def test_failure_action_coverage_guard_reports_missing_reason() -> None:
    from awf.service import metrics

    removed = metrics._FAILURE_ACTIONS.pop(FailureReason.agent_failure.value)
    try:
        with pytest.raises(RuntimeError, match=FailureReason.agent_failure.value):
            metrics._validate_failure_action_coverage()
    finally:
        metrics._FAILURE_ACTIONS[FailureReason.agent_failure.value] = removed


@pytest.mark.unit
async def test_empty_db_returns_zero_workspace_reliability_summary(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_workspace_reliability

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)

    summary = await summarize_workspace_reliability(session_factory, settings=Settings(), now=now)

    assert summary.generated_at == now
    assert summary.window_start == now - timedelta(hours=24)
    assert summary.status_counts == zero_status_counts()
    assert summary.failure_reason_counts == {}
    assert summary.active_count == 0
    assert summary.destroying_count == 0
    assert summary.completed_count == 0
    assert summary.failed_count == 0
    assert summary.cancelled_count == 0
    assert summary.destroyed_count == 0
    assert summary.cleanup_failure_count == 0


@pytest.mark.unit
async def test_failure_analysis_exposes_specific_conformance_reason_and_details(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_failure_analysis

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        workspace = await repo.create(
            repo_url="git@github.com:example/project.git",
            branch_base="main",
            task_title="Finish planned work",
            task_prompt="Implement the saved plan.",
            agent="codex",
            test_commands=[],
        )
        await repo.transition(workspace, to=WorkspaceStatus.provisioning, reason_code="TEST")
        workspace.failure_reason = FailureReason.agent_failure.value
        workspace.failure_message = "plan conformance was not satisfied"
        workspace.updated_at = now
        await repo.transition(
            workspace,
            to=WorkspaceStatus.failed,
            reason_code=PLAN_CONFORMANCE_UNSATISFIED,
            payload={
                "details": {
                    "conformance": {
                        "summary": "Missing planned checks.",
                        "gaps": ["Run mypy", "Add API test"],
                        "reason_code": PLAN_CONFORMANCE_UNSATISFIED,
                        "iterations_used": 0,
                        "max_iterations": 0,
                        "plan_path": "docs/awf-plans/ws.md",
                        "report_path": "docs/awf-plans/ws.conformance.json",
                    }
                },
                "salvage": {"branch_name": "awf/ws_failed"},
            },
        )
        workspace.updated_at = now
        await session.commit()
        workspace_id = workspace.id

    summary = await summarize_failure_analysis(session_factory, now=now)

    assert summary.failure_groups[0].failure_reason == FailureReason.agent_failure.value
    assert summary.latest_examples[0].workspace_id == workspace_id
    assert summary.latest_examples[0].reason_code == PLAN_CONFORMANCE_UNSATISFIED
    assert summary.latest_examples[0].details["conformance"]["gaps"] == [
        "Run mypy",
        "Add API test",
    ]
    assert summary.latest_examples[0].salvage == {"branch_name": "awf/ws_failed"}
    assert summary.root_cause_clusters[0].reason_code == PLAN_CONFORMANCE_UNSATISFIED
    assert summary.root_cause_clusters[0].likely_cause == "Plan Conformance Unsatisfied"
    assert (
        summary.root_cause_clusters[0].actionable_next_action
        == "Retry with the final conformance gaps and finish the remaining planned work."
    )


@pytest.mark.unit
async def test_mixed_statuses_and_failure_reasons_roll_up_counts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_workspace_reliability

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    for status in (
        WorkspaceStatus.requested,
        WorkspaceStatus.running,
        WorkspaceStatus.monitoring_pr,
        WorkspaceStatus.destroying,
        WorkspaceStatus.completed,
        WorkspaceStatus.cancelled,
        WorkspaceStatus.destroyed,
    ):
        await create_workspace(
            session_factory, status=status, updated_at=now - timedelta(minutes=10)
        )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=10),
        failure_reason=FailureReason.agent_failure,
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=10),
        failure_reason=FailureReason.cleanup_failure,
    )

    summary = await summarize_workspace_reliability(session_factory, settings=Settings(), now=now)

    expected_status_counts = zero_status_counts()
    expected_status_counts.update(
        {
            WorkspaceStatus.requested.value: 1,
            WorkspaceStatus.running.value: 1,
            WorkspaceStatus.monitoring_pr.value: 1,
            WorkspaceStatus.destroying.value: 1,
            WorkspaceStatus.completed.value: 1,
            WorkspaceStatus.failed.value: 2,
            WorkspaceStatus.cancelled.value: 1,
            WorkspaceStatus.destroyed.value: 1,
        }
    )
    assert summary.status_counts == expected_status_counts
    assert summary.failure_reason_counts == {
        FailureReason.agent_failure.value: 1,
        FailureReason.cleanup_failure.value: 1,
    }
    assert summary.active_count == 4
    assert summary.destroying_count == 1
    assert summary.completed_count == 1
    assert summary.failed_count == 2
    assert summary.cancelled_count == 1
    assert summary.destroyed_count == 1
    assert summary.cleanup_failure_count == 1


@pytest.mark.unit
async def test_since_hours_filters_by_workspace_updated_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_workspace_reliability

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=7),
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=5),
        failure_reason=FailureReason.validation_failure,
    )

    summary = await summarize_workspace_reliability(
        session_factory, settings=Settings(), since_hours=6, now=now
    )

    expected_status_counts = zero_status_counts()
    expected_status_counts[WorkspaceStatus.failed.value] = 1
    assert summary.window_start == now - timedelta(hours=6)
    assert summary.status_counts == expected_status_counts
    assert summary.failure_reason_counts == {FailureReason.validation_failure.value: 1}
    assert summary.completed_count == 0
    assert summary.failed_count == 1
    assert summary.active_count == 0
    assert summary.destroying_count == 0


@pytest.mark.unit
async def test_current_counts_include_workspaces_outside_updated_at_window(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_workspace_reliability

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now - timedelta(hours=30),
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.destroying,
        updated_at=now - timedelta(hours=30),
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=30),
    )

    summary = await summarize_workspace_reliability(session_factory, settings=Settings(), now=now)

    assert summary.status_counts == zero_status_counts()
    assert summary.active_count == 2
    assert summary.destroying_count == 1


@pytest.mark.unit
async def test_failure_analysis_groups_failed_workspaces_and_latest_examples(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_failure_analysis

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=25),
        failure_reason=FailureReason.agent_failure,
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now - timedelta(minutes=4),
        failure_reason=FailureReason.validation_failure,
    )
    missing_reason_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=1),
        created_at=now - timedelta(minutes=7),
        task_title="Missing reason failure",
        failure_message="The executor failed before a reason was recorded.",
    )
    raw_unknown_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=2),
        created_at=now - timedelta(minutes=8),
        failure_reason="new_failure_reason",
        task_title="Unknown reason failure",
    )
    latest_validation_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=5),
        created_at=now - timedelta(minutes=12),
        failure_reason=FailureReason.validation_failure,
        repo_url="git@github.com:example/api.git",
        branch_base="development",
        task_title="Validation broke",
        agent="claude_code",
        failure_message="pytest failed",
        pr_url="https://github.com/example/api/pull/42",
    )
    infrastructure_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=10),
        created_at=now - timedelta(minutes=20),
        failure_reason=FailureReason.infrastructure_failure,
    )
    middle_validation_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=30),
        failure_reason=FailureReason.validation_failure,
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=45),
        failure_reason=FailureReason.validation_failure,
    )

    summary = await summarize_failure_analysis(session_factory, now=now)

    assert summary.generated_at == now
    assert summary.window_start == now - timedelta(hours=24)
    assert summary.since_hours == 24
    assert summary.total_failed_workspaces == 6
    assert [
        (
            group.failure_reason,
            group.count,
            group.retryable,
            group.recommended_action,
        )
        for group in summary.failure_groups
    ] == [
        (
            FailureReason.validation_failure.value,
            3,
            False,
            "Review validation output and fix failing checks before retrying.",
        ),
        (
            "unknown",
            2,
            False,
            "Inspect workspace logs and classify the failure_reason before retrying.",
        ),
        (
            FailureReason.infrastructure_failure.value,
            1,
            True,
            "Retry after confirming infrastructure health and worker capacity.",
        ),
    ]
    assert [example.workspace_id for example in summary.latest_examples] == [
        missing_reason_id,
        raw_unknown_id,
        latest_validation_id,
        infrastructure_id,
        middle_validation_id,
    ]
    latest_validation = summary.latest_examples[2]
    assert latest_validation.title == "Validation broke"
    assert latest_validation.repo_url == "git@github.com:example/api.git"
    assert latest_validation.branch_base == "development"
    assert latest_validation.agent == "claude_code"
    assert latest_validation.status == WorkspaceStatus.failed.value
    assert latest_validation.failure_reason == FailureReason.validation_failure.value
    assert latest_validation.failure_message == "pytest failed"
    assert latest_validation.pr_url == "https://github.com/example/api/pull/42"
    assert latest_validation.created_at == now - timedelta(minutes=12)
    assert latest_validation.updated_at == now - timedelta(minutes=5)


@pytest.mark.unit
async def test_failure_analysis_latest_examples_do_not_load_workspace_relationships(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_failure_analysis

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=1),
        failure_reason=FailureReason.validation_failure,
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

    event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
    try:
        await summarize_failure_analysis(session_factory, now=now)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record_sql)

    relationship_tables = (
        "from operations",
        "from workspace_log_streams",
        "from task_attempts",
    )
    assert not [
        statement
        for statement in statements
        if any(table in statement for table in relationship_tables)
    ]
    workspace_event_queries = [
        statement for statement in statements if "from workspace_events" in statement
    ]
    assert len(workspace_event_queries) == 1
    workspace_event_query = workspace_event_queries[0]
    # Failure details intentionally read workspace_events, but must keep using the
    # explicit filtered batch query instead of an ORM relationship load.
    assert "workspace_events.event_type = " in workspace_event_query
    assert "workspace_events.new_state = " in workspace_event_query
    assert (
        "order by workspace_events.workspace_id, workspace_events.occurred_at desc"
        in workspace_event_query
    )


@pytest.mark.unit
async def test_failure_analysis_filters_by_since_hours(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_failure_analysis

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=3),
        failure_reason=FailureReason.validation_failure,
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=30),
        failure_reason=FailureReason.phase_timeout,
    )

    summary = await summarize_failure_analysis(session_factory, since_hours=1, now=now)

    assert summary.window_start == now - timedelta(hours=1)
    assert summary.total_failed_workspaces == 1
    assert [(group.failure_reason, group.count) for group in summary.failure_groups] == [
        (FailureReason.phase_timeout.value, 1),
    ]
    assert len(summary.latest_examples) == 1


@pytest.mark.unit
async def test_failure_analysis_accepts_example_limit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_failure_analysis

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    workspace_ids: list[str] = []
    for index in range(6):
        workspace_ids.append(
            await create_workspace(
                session_factory,
                status=WorkspaceStatus.failed,
                updated_at=now - timedelta(minutes=index),
                failure_reason=FailureReason.infrastructure_failure,
            )
        )

    summary = await summarize_failure_analysis(
        session_factory,
        failure_example_limit=6,
        now=now,
    )

    assert [example.workspace_id for example in summary.latest_examples] == workspace_ids


@pytest.mark.unit
@pytest.mark.parametrize("failure_example_limit", [0, 26])
async def test_failure_analysis_validates_example_limit(
    session_factory: async_sessionmaker[AsyncSession],
    failure_example_limit: int,
) -> None:
    from awf.service.metrics import summarize_failure_analysis

    with pytest.raises(ValueError, match="failure_example_limit must be between 1 and 25"):
        await summarize_failure_analysis(
            session_factory,
            failure_example_limit=failure_example_limit,
        )


@pytest.mark.unit
async def test_resource_saturation_reports_active_counts_and_configured_defaults(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_resource_saturation

    settings = Settings(
        _env_file=None,
        work_dir="/tmp/awf-work",
        worker_max_concurrent_provisions=2,
        worker_max_concurrent_executions=4,
        workspace_steady_cpu=2.5,
        workspace_steady_memory_gb=8.0,
        workspace_peak_cpu=5.0,
        workspace_peak_memory_gb=14.0,
    )
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    for status in (
        WorkspaceStatus.requested,
        WorkspaceStatus.provisioning,
        WorkspaceStatus.ready,
        WorkspaceStatus.running,
        WorkspaceStatus.validating,
        WorkspaceStatus.pushing,
        WorkspaceStatus.monitoring_pr,
        WorkspaceStatus.destroying,
        WorkspaceStatus.completed,
    ):
        await create_workspace(session_factory, status=status, updated_at=now)

    summary = await summarize_resource_saturation(
        session_factory,
        settings=settings,
        disk_check=_disk_check(),
        now=now,
    )

    assert summary.generated_at == now
    assert summary.workspace_counts.active_total == 8
    assert summary.workspace_counts.requested == 1
    assert summary.workspace_counts.provisioning == 1
    assert summary.workspace_counts.ready == 1
    assert summary.workspace_counts.running == 1
    assert summary.workspace_counts.validating == 1
    assert summary.workspace_counts.pushing == 1
    assert summary.workspace_counts.monitoring_pr == 1
    assert summary.workspace_counts.destroying == 1
    assert summary.workspace_counts.by_status[WorkspaceStatus.completed.value] == 1
    assert summary.worker.max_concurrent_provisions == 2
    assert summary.worker.max_concurrent_executions == 4
    assert summary.resource_defaults.steady_cpu == 2.5
    assert summary.resource_defaults.steady_memory_gb == 8.0
    assert summary.resource_defaults.peak_cpu == 5.0
    assert summary.resource_defaults.peak_memory_gb == 14.0
    assert summary.reserved_resources.active_workspace_count == 8
    assert summary.reserved_resources.steady_cpu == 20.0
    assert summary.reserved_resources.steady_memory_gb == 64.0
    assert summary.reserved_resources.peak_cpu == 40.0
    assert summary.reserved_resources.peak_memory_gb == 112.0
    assert summary.concurrency.provision.in_use == 1
    assert summary.concurrency.provision.queued == 1
    assert summary.concurrency.provision.available == 1
    assert summary.concurrency.execution.in_use == 4
    assert summary.concurrency.execution.queued == 1
    assert summary.concurrency.execution.available == 0
    assert summary.disk.reason == "SUFFICIENT_DISK"
    assert summary.admission.ok is True


@pytest.mark.unit
async def test_resource_saturation_scopes_capacity_view_to_local_node(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_resource_saturation

    settings = Settings(
        _env_file=None,
        work_dir="/tmp/awf-work",
        worker_node_id="node-a",
        local_capacity_dind_slots=1,
    )
    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    local_running_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
    )
    sibling_running_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
    )
    local_requested_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.requested,
        updated_at=now,
        created_at=now - timedelta(minutes=5),
    )
    sibling_requested_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.requested,
        updated_at=now,
        created_at=now - timedelta(minutes=10),
    )

    async with session_factory() as session:
        node_ids = {
            local_running_id: "node-a",
            local_requested_id: "node-a",
            sibling_running_id: "node-b",
            sibling_requested_id: "node-b",
        }
        for workspace_id, node_id in node_ids.items():
            workspace = await WorkspaceRepository(session).get(workspace_id)
            assert workspace is not None
            workspace.node_id = node_id
        await session.commit()

    await _reservation_for_workspace(
        session_factory,
        local_running_id,
        node_id="node-a",
        steady_cpu=2.0,
        steady_memory_gb=4.0,
        peak_cpu=4.0,
        peak_memory_gb=8.0,
        dind_slots=1,
    )
    await _reservation_for_workspace(
        session_factory,
        sibling_running_id,
        node_id="node-b",
        steady_cpu=20.0,
        steady_memory_gb=40.0,
        peak_cpu=40.0,
        peak_memory_gb=80.0,
        dind_slots=1,
    )
    await _reservation_for_workspace(
        session_factory,
        local_requested_id,
        node_id="node-a",
        steady_cpu=1.0,
        steady_memory_gb=2.0,
        peak_cpu=3.0,
        peak_memory_gb=6.0,
        dind_slots=1,
    )
    await _reservation_for_workspace(
        session_factory,
        sibling_requested_id,
        node_id="node-b",
        steady_cpu=30.0,
        steady_memory_gb=60.0,
        peak_cpu=60.0,
        peak_memory_gb=120.0,
        dind_slots=1,
    )

    summary = await summarize_resource_saturation(
        session_factory,
        settings=settings,
        disk_check=_disk_check(),
        now=now,
    )

    assert summary.workspace_counts.running == 1
    assert summary.workspace_counts.requested == 1
    assert summary.reserved_resources.active_workspace_count == 2
    assert summary.reserved_resources.steady_cpu == 3.0
    assert summary.allocated_resources.active_workspace_count == 1
    assert summary.allocated_resources.steady_cpu == 2.0
    assert summary.capacity_queue.queued_workspace_count == 1
    assert summary.capacity_queue.oldest_workspace_id == local_requested_id
    assert summary.capacity_queue.planned_resources.steady_cpu == 1.0
    assert summary.capacity_queue.planned_resources.dind_slots == 1
    assert summary.capacity_queue.blocked_reason_counts == {"DIND_CAPACITY_SATURATED": 1}


@pytest.mark.unit
async def test_resource_saturation_exposes_open_provider_circuit_breakers(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_resource_saturation

    settings = Settings(_env_file=None, work_dir="/tmp/awf-work")
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        await ProviderModelCircuitBreakerRepository(session).record_failure(
            provider="google",
            model="gemini-2.5-pro",
            reason_code="AGENT_PROVIDER_CAPACITY_EXHAUSTED",
            failure_fingerprint="capacity:fingerprint",
            workspace_id="ws_capacity",
            attempt_id=None,
            now=now,
            failure_threshold=1,
            cooldown_seconds=600,
        )
        await session.commit()

    summary = await summarize_resource_saturation(
        session_factory,
        settings=settings,
        disk_check=_disk_check(),
        now=now + timedelta(seconds=30),
    )

    assert len(summary.provider_circuit_breakers) == 1
    breaker = summary.provider_circuit_breakers[0]
    assert breaker.provider == "google"
    assert breaker.model == "gemini-2.5-pro"
    assert breaker.state == "open"
    assert breaker.failure_count == 1
    assert breaker.last_workspace_id == "ws_capacity"


@pytest.mark.unit
async def test_resource_saturation_provider_recovery_aggregates_via_sql(
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
        status=WorkspaceStatus.running,
        updated_at=now,
        task_policy={PROVIDER_RECOVERY_STATE_KEY: {"action": "retry"}},
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
        task_policy={
            PROVIDER_RECOVERY_STATE_KEY: {
                "action": "retry",
                "not_before": (now + timedelta(hours=1)).isoformat(),
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
                "not_before": (now - timedelta(hours=1)).isoformat(),
            },
        },
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
        task_policy={
            PROVIDER_RECOVERY_STATE_KEY: {"action": "fallback"},
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
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now,
        task_policy={
            PROVIDER_RECOVERY_STATE_KEY: {
                "action": "terminal",
                "decision_reason_code": "PROVIDER_RECOVERY_ATTEMPTS_EXHAUSTED",
                "source_reason_code": "PROVIDER_RETRY_DELAYED",
            },
        },
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now,
        task_policy={PROVIDER_RECOVERY_STATE_KEY: {"action": "retry"}},
    )

    summary = await summarize_resource_saturation(
        session_factory,
        settings=settings,
        disk_check=_disk_check(),
        now=now,
    )

    prs = summary.provider_recovery_state_summary
    assert prs.pending_retry == 2
    assert prs.pending_fallback == 1
    assert prs.in_cooldown == 1
    assert prs.terminal_no_loop == 1
    assert prs.terminal_exhausted == 1
    assert prs.circuit_breakers_open == 0


@pytest.mark.unit
async def test_provider_recovery_cooldown_uses_timestamp_not_string_comparison(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_resource_saturation
    from awf.service.provider_recovery import PROVIDER_RECOVERY_STATE_KEY

    settings = Settings(_env_file=None, work_dir="/tmp/awf-work")
    now = datetime(2026, 5, 1, 11, 30, tzinfo=UTC)

    not_before_edt = (
        datetime(2026, 5, 1, 8, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
    ).isoformat()

    await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
        task_policy={
            PROVIDER_RECOVERY_STATE_KEY: {
                "action": "retry",
                "not_before": not_before_edt,
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
    assert prs.pending_retry == 0
    assert prs.in_cooldown == 1


@pytest.mark.unit
async def test_resource_saturation_malformed_not_before_does_not_crash_query(
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
                "not_before": "not-a-date",
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
async def test_capacity_queue_blocked_reason_counts_aggregates_requested_demands_in_sql(
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
        "PEAK_CPU_CAPACITY_SATURATED": 2,
        "PEAK_MEMORY_CAPACITY_SATURATED": 1,
    }
    assert len(statements) == 1
    assert "sum(case" in statements[0]
    assert "row_number() over" in statements[0]
    assert "left outer join" in statements[0]
    assert "select workspaces.id, workspaces.repo_url" not in statements[0]


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
    assert summary.capacity_queue.planned_resources.active_workspace_count == 1
    assert summary.capacity_queue.planned_resources.dind_slots == 1
    assert summary.capacity_queue.blocked_reason_counts == {"DIND_CAPACITY_SATURATED": 1}


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


@pytest.mark.unit
async def test_resource_saturation_reports_reserved_disk_dind_and_available_capacity(
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
        local_capacity_cpu_cores=24.0,
        local_capacity_memory_gb=96.0,
        local_capacity_dind_slots=4,
    )
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    workspace_id = await create_workspace(
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
        workspace_id,
        steady_cpu=4.0,
        steady_memory_gb=12.0,
        peak_cpu=8.0,
        peak_memory_gb=24.0,
        disk_mb=4096,
        dind_slots=1,
    )
    await _reservation_for_workspace(
        session_factory,
        released_workspace_id,
        steady_cpu=100.0,
        steady_memory_gb=100.0,
        peak_cpu=100.0,
        peak_memory_gb=100.0,
        disk_mb=8192,
        dind_slots=3,
        released_at=now,
    )

    summary = await summarize_resource_saturation(
        session_factory,
        settings=settings,
        disk_check=_disk_check(free_bytes=16 * 1024 * _MIB),
        now=now,
    )

    assert legacy_workspace_id
    assert summary.reserved_resources.active_workspace_count == 2
    assert summary.reserved_resources.steady_cpu == 7.0
    assert summary.reserved_resources.peak_cpu == 14.0
    assert summary.reserved_resources.steady_memory_gb == 22.0
    assert summary.reserved_resources.peak_memory_gb == 40.0
    assert summary.reserved_resources.disk_mb == 4096
    assert summary.reserved_resources.dind_slots == 1
    assert summary.capacity.peak_cpu.limit == 24.0
    assert summary.capacity.peak_cpu.available == 10.0
    assert summary.capacity.peak_memory_gb.limit == 96.0
    assert summary.capacity.peak_memory_gb.available == 56.0
    assert summary.capacity.disk_mb.limit == 16 * 1024
    assert summary.capacity.disk_mb.available == 12 * 1024
    assert summary.capacity.dind_slots.limit == 4
    assert summary.capacity.dind_slots.available == 3
    assert summary.capacity.pressure_reasons == ()


@pytest.mark.unit
async def test_resource_saturation_reports_pressure_reasons_per_dimension(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_resource_saturation

    settings = Settings(
        _env_file=None,
        work_dir="/tmp/awf-work",
        local_capacity_cpu_cores=8.0,
        local_capacity_memory_gb=20.0,
        local_capacity_dind_slots=1,
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
        peak_cpu=9.0,
        peak_memory_gb=21.0,
        disk_mb=4096,
        dind_slots=1,
    )

    summary = await summarize_resource_saturation(
        session_factory,
        settings=settings,
        disk_check=_disk_check(free_bytes=2 * 1024 * _MIB),
        now=now,
    )

    assert summary.capacity.peak_cpu.reason_code == "PEAK_CPU_CAPACITY_SATURATED"
    assert summary.capacity.peak_memory_gb.reason_code == "PEAK_MEMORY_CAPACITY_SATURATED"
    assert summary.capacity.disk_mb.reason_code == "DISK_RESERVATION_PRESSURE"
    assert summary.capacity.dind_slots.reason_code == "DIND_CAPACITY_SATURATED"
    assert summary.capacity.pressure_reasons == (
        "PEAK_CPU_CAPACITY_SATURATED",
        "PEAK_MEMORY_CAPACITY_SATURATED",
        "DISK_RESERVATION_PRESSURE",
        "DIND_CAPACITY_SATURATED",
    )


@pytest.mark.unit
async def test_resource_saturation_admission_reports_both_saturated_concurrency_lanes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_resource_saturation

    settings = Settings(
        _env_file=None,
        work_dir="/tmp/awf-work",
        worker_max_concurrent_provisions=1,
        worker_max_concurrent_executions=1,
    )
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    await create_workspace(session_factory, status=WorkspaceStatus.provisioning, updated_at=now)
    await create_workspace(session_factory, status=WorkspaceStatus.running, updated_at=now)

    summary = await summarize_resource_saturation(
        session_factory,
        settings=settings,
        disk_check=_disk_check(),
        now=now,
    )

    assert summary.concurrency.provision.available == 0
    assert summary.concurrency.execution.available == 0
    assert summary.admission.ok is True
    assert summary.admission.status == "saturated"
    assert summary.admission.reason == "WORKER_PROVISION_AND_EXECUTION_CONCURRENCY_SATURATED"
    assert summary.admission.detail is not None
    assert "Provisioning and execution workers" in summary.admission.detail


@pytest.mark.unit
async def test_resource_saturation_admission_reports_provision_only_saturation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_resource_saturation

    settings = Settings(
        _env_file=None,
        work_dir="/tmp/awf-work",
        worker_max_concurrent_provisions=1,
        worker_max_concurrent_executions=3,
    )
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    await create_workspace(session_factory, status=WorkspaceStatus.provisioning, updated_at=now)

    summary = await summarize_resource_saturation(
        session_factory,
        settings=settings,
        disk_check=_disk_check(),
        now=now,
    )

    assert summary.concurrency.provision.available == 0
    assert summary.concurrency.execution.available == 3
    assert summary.admission.ok is True
    assert summary.admission.status == "saturated"
    assert summary.admission.reason == "WORKER_PROVISION_CONCURRENCY_SATURATED"


@pytest.mark.unit
async def test_resource_saturation_admission_blocks_on_disk_pressure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_resource_saturation

    settings = Settings(
        _env_file=None,
        work_dir="/tmp/awf-work",
        worker_max_concurrent_provisions=3,
        worker_max_concurrent_executions=3,
    )
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)

    summary = await summarize_resource_saturation(
        session_factory,
        settings=settings,
        disk_check=_disk_check(
            ok=False,
            free_bytes=100,
            threshold_bytes=400,
            reason="INSUFFICIENT_DISK",
        ),
        now=now,
    )

    assert summary.workspace_counts.active_total == 0
    assert summary.disk.ok is False
    assert summary.disk.reason == "INSUFFICIENT_DISK"
    assert summary.admission.ok is False
    assert summary.admission.status == "blocked"
    assert summary.admission.reason == "INSUFFICIENT_DISK"
    assert summary.admission.detail is not None


@pytest.mark.unit
async def test_resource_saturation_runs_fallback_disk_check_in_thread(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import awf.service.metrics as metrics_mod
    from awf.service.metrics import summarize_resource_saturation

    class _Usage:
        total = 1000
        used = 100
        free = 900

    def fake_disk_usage(_path: object) -> _Usage:
        return _Usage()

    to_thread_calls: list[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]] = []

    async def fake_to_thread(
        func: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        to_thread_calls.append((func, args, kwargs))
        return func(*args, **kwargs)

    monkeypatch.setattr(metrics_mod.asyncio, "to_thread", fake_to_thread)
    settings = Settings(
        _env_file=None,
        work_dir="/tmp/awf-work",
        min_free_disk_bytes=400,
    )

    summary = await summarize_resource_saturation(
        session_factory,
        settings=settings,
        disk_usage=fake_disk_usage,
    )

    assert summary.disk.reason == "SUFFICIENT_DISK"
    assert len(to_thread_calls) == 1
    func, args, kwargs = to_thread_calls[0]
    assert func is metrics_mod.check_disk_space
    assert args == (settings.work_dir,)
    assert kwargs["min_free_bytes"] == settings.min_free_disk_bytes
    assert kwargs["disk_usage"] is fake_disk_usage


@pytest.mark.unit
async def test_workspace_reliability_reports_stuck_count(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_workspace_reliability

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    settings = Settings(_env_file=None, agent_wall_timeout_seconds=7200)

    # 1. Active, fresh -> Not stuck
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
        created_at=now - timedelta(hours=1),
    )
    # 2. Active, older than 2x SLA (4 hours), no reason -> Stuck
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.monitoring_pr,
        updated_at=now - timedelta(hours=5),
        created_at=now - timedelta(hours=5),
    )
    # 3. Active, older than 2x SLA, has reason -> Not stuck (already classified)
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.pushing,
        updated_at=now - timedelta(hours=5),
        created_at=now - timedelta(hours=5),
        failure_reason="network_hiccup",
    )
    # 4. Terminal, older than 2x SLA -> Not active, so not stuck
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=5),
        created_at=now - timedelta(hours=5),
    )

    summary = await summarize_workspace_reliability(session_factory, settings=settings, now=now)

    assert summary.stuck_count == 1
    assert summary.active_count == 3


@pytest.mark.unit
async def test_workspace_reliability_reports_cleanup_failure_reason_code_coverage(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_workspace_reliability

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    settings = Settings(_env_file=None)

    # 1. Failed with actionable reason in window
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now,
        failure_reason=FailureReason.agent_failure,
    )
    # 2. Destroying with actionable reason in window
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.destroying,
        updated_at=now,
        failure_reason=FailureReason.cleanup_failure,
    )
    # 3. Failed with unknown reason in window
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now,
        failure_reason="unknown_error_code",
    )
    # 4. Cancelled with NO reason in window
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.cancelled,
        updated_at=now,
    )
    # 5. Failed outside window -> should not be counted
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=48),
        failure_reason=FailureReason.agent_failure,
    )

    summary = await summarize_workspace_reliability(session_factory, settings=settings, now=now)

    assert summary.actionable_reason_count == 2
    assert summary.unactionable_reason_count == 2


@pytest.mark.unit
@pytest.mark.parametrize("since_hours", [0, 169])
async def test_workspace_reliability_validates_since_hours(
    session_factory: async_sessionmaker[AsyncSession],
    since_hours: int,
) -> None:
    from awf.service.metrics import summarize_workspace_reliability

    with pytest.raises(ValueError, match="since_hours must be between"):
        await summarize_workspace_reliability(
            session_factory,
            settings=Settings(_env_file=None),
            since_hours=since_hours,
        )


@pytest.mark.unit
async def test_root_cause_clusters_mixed_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_failure_analysis

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)

    # 1. AGENT_AUTH_FAILED
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=1),
        failure_reason=FailureReason.agent_failure,
        failure_message="Some error ... AGENT_AUTH_FAILED ...",
        agent="gemini",
    )
    # 2. model not found or 404
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=1),
        failure_reason=FailureReason.agent_failure,
        failure_message="404 model not found",
        agent="claude",
    )
    # 3. missing worktree
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=1),
        failure_reason=FailureReason.validation_failure,
        failure_message="missing managed worktree during fix loop",
        agent="gemini",
    )
    # 4. coverage threshold failure
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=1),
        failure_reason=FailureReason.validation_failure,
        failure_message="coverage threshold failure: expected 80%, got 75%",
        agent="gemini",
    )
    # 5. syntax/import errors during validation
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=1),
        failure_reason=FailureReason.validation_failure,
        failure_message="SyntaxError: invalid syntax",
        agent="opencode",
    )
    # 6. GitHub transient/auth errors
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=1),
        failure_reason=FailureReason.agent_failure,
        failure_message="GitHub auth/PR creation failed",
        agent="gemini",
    )
    # 7. unknown validation failure
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=1),
        failure_reason=FailureReason.validation_failure,
        failure_message="Some random pytest failure",
        agent="gemini",
    )
    # 8. Another syntax error to test grouping
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=1),
        failure_reason=FailureReason.validation_failure,
        failure_message="ImportError: No module named xxx",
        agent="opencode",
    )
    # 9. unknown agent failure
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=1),
        failure_reason=FailureReason.agent_failure,
        failure_message="Agent exited without a structured reason",
        agent="codex",
    )

    summary = await summarize_failure_analysis(session_factory, now=now)

    assert len(summary.root_cause_clusters) == 8
    cluster_reasons = [c.likely_cause for c in summary.root_cause_clusters]

    assert "Agent Auth Failed" in cluster_reasons
    assert "Model Not Found / 404" in cluster_reasons
    assert "Missing Managed Worktree" in cluster_reasons
    assert "Coverage Threshold Failure" in cluster_reasons
    assert "Syntax or Import Error" in cluster_reasons
    assert "GitHub Transient/Auth Error" in cluster_reasons
    assert "Unknown Agent Failure" in cluster_reasons
    assert "Unknown Validation Failure" in cluster_reasons

    # Check grouping
    syntax_cluster = next(
        c for c in summary.root_cause_clusters if c.likely_cause == "Syntax or Import Error"
    )
    assert syntax_cluster.count == 2
    assert syntax_cluster.agent == "opencode"


@pytest.mark.unit
async def test_root_cause_clusters_agent_model_extraction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_failure_analysis

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)

    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=1),
        failure_reason=FailureReason.validation_failure,
        failure_message="SyntaxError",
        agent="gemini",
        task_policy={"agent_model": "gemini-1.5-pro"},
    )

    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=1),
        failure_reason=FailureReason.validation_failure,
        failure_message="SyntaxError",
        agent="gemini",
        task_policy={},
    )

    summary = await summarize_failure_analysis(session_factory, now=now)

    # Groups should be split by agent_model
    syntax_clusters = [
        c for c in summary.root_cause_clusters if c.likely_cause == "Syntax or Import Error"
    ]
    assert len(syntax_clusters) == 2

    models = {c.agent_model for c in syntax_clusters}
    assert models == {"gemini-1.5-pro", None}


@pytest.mark.unit
async def test_root_cause_clusters_empty_history(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_failure_analysis

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    summary = await summarize_failure_analysis(session_factory, now=now)

    assert summary.root_cause_clusters == []


@pytest.mark.unit
async def test_existing_failure_groups_unaffected(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_failure_analysis

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)

    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=1),
        failure_reason=FailureReason.agent_failure,
        failure_message="AGENT_AUTH_FAILED",
        agent="gemini",
    )

    summary = await summarize_failure_analysis(session_factory, now=now)

    assert len(summary.failure_groups) == 1
    assert summary.failure_groups[0].failure_reason == FailureReason.agent_failure.value
    assert len(summary.latest_examples) == 1


@pytest.mark.unit
async def test_slo_summary_returns_zero_counts_for_empty_db(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_slo_metrics_for_session

    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        summary = await summarize_slo_metrics_for_session(
            session,
            settings=Settings(_env_file=None),
            now=now,
        )

    assert summary.generated_at == now
    assert summary.window_start == now - timedelta(hours=24)
    assert summary.since_hours == 24
    assert summary.creation_total == 0
    assert summary.creation_succeeded == 0
    assert summary.creation_failed == 0
    assert summary.creation_cancelled == 0
    assert summary.cleanup_total == 0
    assert summary.cleanup_succeeded == 0
    assert summary.cleanup_failure_count == 0
    assert summary.stuck_running_count == 0
    assert summary.stuck_with_reason_count == 0
    assert summary.recovery_total == 0
    assert summary.recovery_succeeded == 0
    assert summary.recovery_failed_count == 0
    assert summary.monitor_completed_total == 0
    assert summary.completed_after_monitor_count == 0
    assert summary.monitor_stuck_count == 0
    assert summary.actionable_failure_count == 0
    assert summary.unactionable_failure_count == 0


@pytest.mark.unit
async def test_creation_metrics_windowed_by_created_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_slo_metrics_for_session

    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        created_at=now - timedelta(hours=2),
        updated_at=now - timedelta(hours=1),
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        created_at=now - timedelta(hours=3),
        updated_at=now - timedelta(hours=2),
        failure_reason=FailureReason.agent_failure,
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.cancelled,
        created_at=now - timedelta(hours=4),
        updated_at=now - timedelta(hours=3),
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        created_at=now - timedelta(hours=30),
        updated_at=now - timedelta(hours=29),
    )

    async with session_factory() as session:
        summary = await summarize_slo_metrics_for_session(
            session,
            settings=Settings(_env_file=None),
            since_hours=24,
            now=now,
        )

    assert summary.creation_total == 3
    assert summary.creation_succeeded == 1
    assert summary.creation_failed == 1
    assert summary.creation_cancelled == 1


@pytest.mark.unit
async def test_cleanup_metrics_from_destroy_operations(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_slo_metrics_for_session

    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    ws_id_1 = await create_workspace(
        session_factory,
        status=WorkspaceStatus.destroyed,
        created_at=now - timedelta(hours=5),
        updated_at=now - timedelta(hours=1),
    )
    ws_id_2 = await create_workspace(
        session_factory,
        status=WorkspaceStatus.destroyed,
        created_at=now - timedelta(hours=6),
        updated_at=now - timedelta(hours=1),
    )
    ws_id_3 = await create_workspace(
        session_factory,
        status=WorkspaceStatus.destroyed,
        created_at=now - timedelta(hours=30),
        updated_at=now - timedelta(hours=29),
    )
    await create_operation(
        session_factory,
        ws_id_1,
        operation_type=OperationType.destroy,
        status=OperationStatus.succeeded,
        finished_at=now - timedelta(hours=1),
    )
    await create_operation(
        session_factory,
        ws_id_2,
        operation_type=OperationType.destroy,
        status=OperationStatus.failed,
        finished_at=now - timedelta(hours=1),
    )
    await create_operation(
        session_factory,
        ws_id_3,
        operation_type=OperationType.destroy,
        status=OperationStatus.succeeded,
        finished_at=now - timedelta(hours=29),
    )

    async with session_factory() as session:
        summary = await summarize_slo_metrics_for_session(
            session,
            settings=Settings(_env_file=None),
            since_hours=24,
            now=now,
        )

    assert summary.cleanup_total == 2
    assert summary.cleanup_succeeded == 1
    assert summary.cleanup_failure_count == 1


@pytest.mark.unit
async def test_stuck_state_splits_by_reason_code_presence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_slo_metrics_for_session

    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    settings = Settings(_env_file=None, agent_wall_timeout_seconds=3600)
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        created_at=now - timedelta(hours=3),
        updated_at=now - timedelta(hours=2),
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        created_at=now - timedelta(hours=3),
        updated_at=now - timedelta(hours=2),
        failure_reason="network_hiccup",
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        created_at=now - timedelta(minutes=30),
        updated_at=now - timedelta(minutes=20),
    )

    async with session_factory() as session:
        summary = await summarize_slo_metrics_for_session(
            session,
            settings=settings,
            now=now,
        )

    assert summary.stuck_running_count == 1
    assert summary.stuck_with_reason_count == 1


@pytest.mark.unit
async def test_recovery_metrics_from_operations(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_slo_metrics_for_session

    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    ws_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        created_at=now - timedelta(hours=5),
        updated_at=now - timedelta(hours=1),
        failure_reason=FailureReason.agent_failure,
    )
    await create_operation(
        session_factory,
        ws_id,
        operation_type=OperationType.remonitor,
        status=OperationStatus.succeeded,
        created_at=now - timedelta(hours=1),
        finished_at=now - timedelta(minutes=50),
    )
    await create_operation(
        session_factory,
        ws_id,
        operation_type=OperationType.rebase,
        status=OperationStatus.failed,
        created_at=now - timedelta(hours=1),
        finished_at=now - timedelta(minutes=40),
    )
    await create_operation(
        session_factory,
        ws_id,
        operation_type=OperationType.retry,
        status=OperationStatus.succeeded,
        created_at=now - timedelta(hours=1),
        finished_at=now - timedelta(minutes=30),
    )
    await create_operation(
        session_factory,
        ws_id,
        operation_type=OperationType.remonitor,
        status=OperationStatus.succeeded,
        created_at=now - timedelta(hours=30),
        finished_at=now - timedelta(hours=29),
    )

    async with session_factory() as session:
        summary = await summarize_slo_metrics_for_session(
            session,
            settings=Settings(_env_file=None),
            since_hours=24,
            now=now,
        )

    assert summary.recovery_total == 3
    assert summary.recovery_succeeded == 2
    assert summary.recovery_failed_count == 1


@pytest.mark.unit
async def test_count_monitor_completions_uses_single_aggregate_execute(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service.metrics import _count_monitor_completions

    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    sla_seconds = 3600
    window_start = now - timedelta(hours=24)
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.monitoring_pr,
        created_at=now - timedelta(minutes=30),
        updated_at=now - timedelta(minutes=10),
        pr_url="https://github.com/example/repo/pull/1",
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        created_at=now - timedelta(hours=10),
        updated_at=now - timedelta(hours=1),
        pr_url="https://github.com/example/repo/pull/2",
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        created_at=now - timedelta(hours=1),
        updated_at=now - timedelta(minutes=30),
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        created_at=now - timedelta(hours=30),
        updated_at=now - timedelta(hours=25),
        pr_url="https://github.com/example/repo/pull/3",
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.monitoring_pr,
        created_at=now - timedelta(hours=3),
        updated_at=now - timedelta(minutes=5),
        pr_url="https://github.com/example/repo/pull/4",
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

    async def fail_scalar(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("_count_monitor_completions must not call session.scalar")

    async with session_factory() as session:
        monkeypatch.setattr(session, "scalar", fail_scalar)
        event.listen(engine.sync_engine, "before_cursor_execute", record_sql)
        try:
            counts = await _count_monitor_completions(
                session,
                window_start=window_start,
                sla_seconds=sla_seconds,
                now=now,
            )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record_sql)

    assert counts == (3, 1, 1)
    assert len(statements) == 1
    assert statements[0].startswith("select")
    assert " from workspaces where " in statements[0]
    where_clause = statements[0].split(" from workspaces where ", 1)[1]
    assert "workspaces.updated_at >= " in where_clause
    assert "workspaces.pr_url is not null" in where_clause
    assert " or " in where_clause
    assert "workspaces.status = " in where_clause
    assert "workspaces.created_at < " in where_clause


@pytest.mark.unit
async def test_monitor_metrics_counts_completed_and_stuck(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_slo_metrics_for_session

    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    settings = Settings(_env_file=None, agent_wall_timeout_seconds=3600)
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.monitoring_pr,
        created_at=now - timedelta(hours=3),
        updated_at=now - timedelta(hours=2),
        pr_url="https://github.com/example/repo/pull/1",
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        created_at=now - timedelta(hours=10),
        updated_at=now - timedelta(hours=1),
        pr_url="https://github.com/example/repo/pull/2",
    )

    async with session_factory() as session:
        summary = await summarize_slo_metrics_for_session(
            session,
            settings=settings,
            since_hours=24,
            now=now,
        )

    assert summary.monitor_stuck_count == 1
    assert summary.completed_after_monitor_count == 1
    assert summary.monitor_completed_total == 2
    assert summary.monitor_completed_total != summary.completed_after_monitor_count


@pytest.mark.unit
async def test_monitoring_pr_not_counted_in_stuck_detailed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_slo_metrics_for_session

    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    settings = Settings(_env_file=None, agent_wall_timeout_seconds=3600)
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.monitoring_pr,
        created_at=now - timedelta(hours=3),
        updated_at=now - timedelta(hours=2),
        pr_url="https://github.com/example/repo/pull/1",
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.monitoring_pr,
        created_at=now - timedelta(hours=3),
        updated_at=now - timedelta(hours=2),
        pr_url="https://github.com/example/repo/pull/2",
        failure_reason="network_hiccup",
    )

    async with session_factory() as session:
        summary = await summarize_slo_metrics_for_session(
            session,
            settings=settings,
            now=now,
        )

    assert summary.stuck_running_count == 0
    assert summary.stuck_with_reason_count == 0
    assert summary.monitor_stuck_count == 2


@pytest.mark.unit
async def test_actionable_vs_unactionable_failure_counts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_slo_metrics_for_session

    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        created_at=now - timedelta(hours=5),
        updated_at=now - timedelta(hours=1),
        failure_reason=FailureReason.agent_failure,
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        created_at=now - timedelta(hours=5),
        updated_at=now - timedelta(hours=1),
        failure_reason=FailureReason.validation_failure,
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        created_at=now - timedelta(hours=5),
        updated_at=now - timedelta(hours=1),
        failure_reason="unknown_error_code",
    )

    async with session_factory() as session:
        summary = await summarize_slo_metrics_for_session(
            session,
            settings=Settings(_env_file=None),
            since_hours=24,
            now=now,
        )

    assert summary.actionable_failure_count == 2
    assert summary.unactionable_failure_count == 1


@pytest.mark.unit
@pytest.mark.parametrize("since_hours", [0, 169])
async def test_since_hours_validation_rejected(
    session_factory: async_sessionmaker[AsyncSession],
    since_hours: int,
) -> None:
    from awf.service.metrics import summarize_slo_metrics_for_session

    async with session_factory() as session:
        with pytest.raises(ValueError, match="since_hours must be between"):
            await summarize_slo_metrics_for_session(
                session,
                settings=Settings(_env_file=None),
                since_hours=since_hours,
            )


@pytest.mark.unit
async def test_slo_summary_defaults_to_24_hour_window(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_slo_metrics_for_session

    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        summary = await summarize_slo_metrics_for_session(
            session,
            settings=Settings(_env_file=None),
            now=now,
        )

    assert summary.since_hours == 24
    assert summary.window_start == now - timedelta(hours=24)


@pytest.mark.unit
async def test_slo_summary_session_factory_wrapper(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_slo_metrics

    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    summary = await summarize_slo_metrics(
        session_factory, settings=Settings(_env_file=None), now=now
    )

    assert summary.generated_at == now
    assert summary.since_hours == 24
    assert summary.creation_total == 0


@pytest.mark.unit
async def test_cleanup_and_recovery_include_cancelled_operations(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_slo_metrics_for_session

    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    ws_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.destroyed,
        created_at=now - timedelta(hours=5),
        updated_at=now - timedelta(hours=1),
    )
    await create_operation(
        session_factory,
        ws_id,
        operation_type=OperationType.destroy,
        status=OperationStatus.succeeded,
        finished_at=now - timedelta(hours=1),
    )
    await create_operation(
        session_factory,
        ws_id,
        operation_type=OperationType.destroy,
        status=OperationStatus.cancelled,
        finished_at=now - timedelta(hours=1),
    )
    await create_operation(
        session_factory,
        ws_id,
        operation_type=OperationType.remonitor,
        status=OperationStatus.succeeded,
        created_at=now - timedelta(hours=1),
        finished_at=now - timedelta(minutes=50),
    )
    await create_operation(
        session_factory,
        ws_id,
        operation_type=OperationType.retry,
        status=OperationStatus.cancelled,
        created_at=now - timedelta(hours=1),
    )

    async with session_factory() as session:
        summary = await summarize_slo_metrics_for_session(
            session,
            settings=Settings(_env_file=None),
            since_hours=24,
            now=now,
        )

    assert summary.cleanup_total == 2
    assert summary.cleanup_succeeded == 1
    assert summary.cleanup_failure_count == 0
    assert summary.recovery_total == 2
    assert summary.recovery_succeeded == 1
    assert summary.recovery_failed_count == 0


@pytest.mark.unit
class TestIso8601PostgresGuardRegex:
    from awf.service.metrics import _ISO8601_TS_PG

    _PATTERN = _ISO8601_TS_PG

    @pytest.mark.parametrize(
        "value",
        [
            "2026-05-01T11:00:00Z",
            "2026-05-01T11:00:00+00:00",
            "2026-05-01T11:00:00-04:00",
            "2026-05-01T11:00:00+0000",
            "2026-05-01T23:59:59Z",
            "2026-12-31T00:00:00Z",
            "2026-01-01T00:00:00Z",
            "2026-05-01T11:00:00.123456+00:00",
            "2026-05-01T11:00:00.123456Z",
            "2026-05-01T11:00:00.1-04:00",
            "2026-05-01T12:00:00.0+00:00",
        ],
    )
    def test_valid_timestamps_match(self, value: str) -> None:
        import re

        assert re.match(self._PATTERN, value), f"Expected valid timestamp to match: {value}"

    @pytest.mark.parametrize(
        "value",
        [
            "2026-99-99T99:99:99",
            "2026-13-01T11:00:00Z",
            "2026-00-01T11:00:00Z",
            "2026-05-01T25:00:00Z",
            "2026-05-01T11:60:00Z",
            "2026-05-01T11:00:60Z",
            "2026-05-01T11:00:00Zextra",
            "2026-05-01T11:00:00 Z",
            "not-a-date",
            "",
            "2026-05-01",
            "2026-05-01T11:00",
        ],
    )
    def test_invalid_timestamps_rejected(self, value: str) -> None:
        import re

        assert not re.match(self._PATTERN, value), (
            f"Expected invalid timestamp to be rejected: {value}"
        )

    @pytest.mark.parametrize(
        "value",
        [
            "2026-02-30T11:00:00Z",
            "2026-04-31T11:00:00Z",
            "2025-02-29T11:00:00Z",
        ],
    )
    def test_calendar_invalid_but_syntactically_valid_timestamps_match(self, value: str) -> None:
        import re

        assert re.match(self._PATTERN, value), (
            "Regex accepts syntactically valid timestamps even if calendar-invalid; "
            "safe because not_before is always produced by datetime.isoformat()"
        )
