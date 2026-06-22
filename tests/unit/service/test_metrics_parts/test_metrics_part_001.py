"""Workspace reliability summary service tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.common.config import Settings
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.repositories import (
    ProviderModelCircuitBreakerRepository,
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.runtime.planning import PLAN_CONFORMANCE_UNSATISFIED
from awf.service import metrics_resources
from awf.service.disk import DiskCheck
from awf.service.metrics_resources import ALLOCATED_RESOURCE_RESERVATION_STATUSES
from awf.service.metrics_types import _AllocatedResourceAuxiliaryCounts
from awf.service.resource_capacity import (
    LocalCapacityLimits,
    ReservedResources,
    WorkspaceResourceDefaults,
)
from tests.unit.helpers import create_workspace, zero_status_counts

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
def test_metrics_to_utc_accepts_naive_datetime() -> None:
    from awf.service import metrics

    naive = datetime(2026, 5, 7, 14, 45)

    assert metrics._to_utc(naive) == naive.replace(tzinfo=UTC)


@pytest.mark.unit
async def test_allocated_resource_helpers_load_auxiliary_counts_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_metrics_globals = metrics_resources._allocated_resources_for_session.__globals__  # noqa: SLF001

    calls: list[tuple[str, str]] = []

    async def metrics_totals(
        _session: object,
        *,
        statuses: object,
        node_id: str,
    ) -> dict[str, float | int]:
        calls.append(("metrics", node_id))
        return _empty_reservation_totals()

    async def scheduler_totals(
        _session: object,
        *,
        statuses: object,
        node_id: str,
    ) -> dict[str, float | int]:
        calls.append(("scheduler", node_id))
        return _empty_reservation_totals()

    async def auxiliary_counts(
        _session: object,
        *,
        node_id: str,
    ) -> _AllocatedResourceAuxiliaryCounts:
        calls.append(("auxiliary", node_id))
        return _AllocatedResourceAuxiliaryCounts(
            unreserved_workspace_count=2,
            defaulted_dind_slots=1,
        )

    monkeypatch.setitem(
        live_metrics_globals,
        "_active_latest_totals_for_metrics_allocation_scope",
        metrics_totals,
    )
    monkeypatch.setitem(
        live_metrics_globals,
        "_active_latest_totals_for_scheduler_allocation_scope",
        scheduler_totals,
    )
    monkeypatch.setitem(
        live_metrics_globals,
        "_allocated_resource_auxiliary_counts_for_session",
        auxiliary_counts,
    )
    defaults = WorkspaceResourceDefaults(
        steady_cpu=1.0,
        steady_memory_gb=2.0,
        peak_cpu=3.0,
        peak_memory_gb=4.0,
    )

    allocated_resources = await metrics_resources._allocated_resources_for_session(  # noqa: SLF001
        object(),  # type: ignore[arg-type]
        node_id="node-a",
        resource_defaults=defaults,
    )
    scheduler_resources = await metrics_resources._scheduler_allocated_resources_for_session(  # noqa: SLF001
        object(),  # type: ignore[arg-type]
        node_id="node-b",
        resource_defaults=defaults,
    )

    assert allocated_resources.active_workspace_count == 2
    assert allocated_resources.steady_cpu == 2.0
    assert scheduler_resources.active_workspace_count == 2
    assert scheduler_resources.dind_slots == 1
    assert calls == [
        ("metrics", "node-a"),
        ("auxiliary", "node-a"),
        ("scheduler", "node-b"),
        ("auxiliary", "node-b"),
    ]


@pytest.mark.unit
async def test_capacity_metrics_helpers_short_circuit_empty_inputs() -> None:
    from awf.service import metrics

    assert metrics._workspace_status_filter(()) is None  # noqa: SLF001
    assert (
        await metrics._defaulted_dind_slots_for_session(  # noqa: SLF001
            object(),  # type: ignore[arg-type]
            statuses=(),
        )
        == 0
    )
    assert (
        await metrics._unreserved_workspace_count_for_session(  # noqa: SLF001
            object(),  # type: ignore[arg-type]
            statuses=(),
        )
        == 0
    )
    assert (
        await metrics._provider_recovery_eligible_capacity_queue_candidates(  # noqa: SLF001
            object(),  # type: ignore[arg-type]
            [],
            scoring_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        == []
    )


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
async def test_resource_saturation_allocated_capacity_matches_scheduler_null_node_rules(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_resource_saturation

    settings = Settings(
        _env_file=None,
        work_dir="/tmp/awf-work",
        worker_node_id="node-a",
        local_capacity_cpu_cores=6.0,
        local_capacity_dind_slots=1,
    )
    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    local_mismatched_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
    )
    null_remote_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
    )
    requested_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.requested,
        updated_at=now,
    )

    async with session_factory() as session:
        node_ids = {
            local_mismatched_id: "node-a",
            null_remote_id: None,
            requested_id: "node-a",
        }
        for workspace_id, node_id in node_ids.items():
            workspace = await WorkspaceRepository(session).get(workspace_id)
            assert workspace is not None
            workspace.node_id = node_id
        await session.commit()

    await _reservation_for_workspace(
        session_factory,
        local_mismatched_id,
        node_id="node-b",
        steady_cpu=1.0,
        steady_memory_gb=2.0,
        peak_cpu=2.0,
        peak_memory_gb=4.0,
        dind_slots=0,
    )
    await _reservation_for_workspace(
        session_factory,
        null_remote_id,
        node_id="node-b",
        steady_cpu=3.0,
        steady_memory_gb=8.0,
        peak_cpu=6.0,
        peak_memory_gb=16.0,
        dind_slots=1,
    )
    await _reservation_for_workspace(
        session_factory,
        requested_id,
        node_id="node-a",
        steady_cpu=2.0,
        steady_memory_gb=4.0,
        peak_cpu=4.0,
        peak_memory_gb=8.0,
        dind_slots=1,
    )

    summary = await summarize_resource_saturation(
        session_factory,
        settings=settings,
        disk_check=_disk_check(),
        now=now,
    )

    assert summary.reserved_resources.active_workspace_count == 3
    assert summary.capacity_queue.planned_resources.active_workspace_count == 1
    assert summary.allocated_resources.active_workspace_count == 1
    assert summary.allocated_resources.peak_cpu == 2.0
    assert summary.allocated_resources.dind_slots == 0
    assert summary.capacity_queue.blocked_reason_counts == {}


@pytest.mark.unit
async def test_capacity_queue_summary_skips_scheduler_allocation_when_unconstrained(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import metrics

    settings = Settings(_env_file=None, work_dir="/tmp/awf-work")
    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    requested_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.requested,
        updated_at=now,
        created_at=now - timedelta(minutes=2),
    )

    async def fail_scheduler_allocation(*args: Any, **kwargs: Any) -> ReservedResources:
        raise AssertionError("scheduler allocation should not be loaded without capacity limits")

    monkeypatch.setattr(
        metrics_resources,
        "_scheduler_allocated_resources_for_session",
        fail_scheduler_allocation,
    )

    async with session_factory() as session:
        summary = await metrics._capacity_queue_summary(  # noqa: SLF001
            session,
            settings=settings,
            node_id=metrics._local_capacity_node_id(settings),  # noqa: SLF001
            resource_defaults=WorkspaceResourceDefaults(
                steady_cpu=1.0,
                steady_memory_gb=2.0,
                peak_cpu=3.0,
                peak_memory_gb=4.0,
            ),
            detected_local_capacity=LocalCapacityLimits(cpu_cores=1.0, memory_gb=1.0),
            now=now,
        )

    assert summary.queued_workspace_count == 1
    assert summary.oldest_workspace_id == requested_id
    assert summary.oldest_wait_seconds == 120
    assert summary.planned_resources.active_workspace_count == 1
    assert summary.planned_resources.steady_cpu == 1.0
    assert summary.planned_resources.steady_memory_gb == 2.0
    assert summary.planned_resources.peak_cpu == 3.0
    assert summary.planned_resources.peak_memory_gb == 4.0
    assert summary.blocked_reason_counts == {}


@pytest.mark.unit
async def test_resource_saturation_reuses_allocation_auxiliary_counts_for_capacity_gate(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from awf.service import metrics

    settings = Settings(
        _env_file=None,
        work_dir="/tmp/awf-work",
        worker_node_id="node-a",
        local_capacity_cpu_cores=100.0,
    )
    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.requested,
        updated_at=now,
    )
    unreserved_calls: list[tuple[tuple[str, ...] | None, str | None]] = []
    defaulted_dind_calls: list[tuple[tuple[str, ...] | None, str | None]] = []

    def status_values(statuses: Any) -> tuple[str, ...] | None:
        if statuses is None:
            return None
        return tuple(
            status.value if isinstance(status, WorkspaceStatus) else str(status)
            for status in statuses
        )

    async def record_unreserved_count(
        session: AsyncSession,
        *,
        statuses: Any,
        node_id: str | None,
    ) -> int:
        del session
        unreserved_calls.append((status_values(statuses), node_id))
        return 0

    async def record_defaulted_dind_slots(
        session: AsyncSession,
        *,
        statuses: Any = None,
        node_id: str | None = None,
    ) -> int:
        del session
        defaulted_dind_calls.append((status_values(statuses), node_id))
        return 0

    monkeypatch.setattr(
        metrics_resources,
        "_unreserved_workspace_count_for_session",
        record_unreserved_count,
    )
    monkeypatch.setattr(
        metrics_resources,
        "_defaulted_dind_slots_for_session",
        record_defaulted_dind_slots,
    )

    await metrics.summarize_resource_saturation(
        session_factory,
        settings=settings,
        disk_check=_disk_check(),
        now=now,
    )

    allocated_statuses = ALLOCATED_RESOURCE_RESERVATION_STATUSES
    assert unreserved_calls.count((allocated_statuses, "node-a")) == 1
    assert defaulted_dind_calls.count((allocated_statuses, "node-a")) == 1


@pytest.mark.unit
async def test_capacity_queue_uses_scheduler_allocation_scope_for_migrating_reservation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_resource_saturation

    settings = Settings(
        _env_file=None,
        work_dir="/tmp/awf-work",
        worker_node_id="node-a",
        local_capacity_cpu_cores=6.0,
    )
    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    migrating_workspace_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
    )
    requested_id = await create_workspace(
        session_factory,
        status=WorkspaceStatus.requested,
        updated_at=now,
    )

    async with session_factory() as session:
        node_ids = {
            migrating_workspace_id: "node-b",
            requested_id: "node-a",
        }
        for workspace_id, node_id in node_ids.items():
            workspace = await WorkspaceRepository(session).get(workspace_id)
            assert workspace is not None
            workspace.node_id = node_id
        await session.commit()

    await _reservation_for_workspace(
        session_factory,
        migrating_workspace_id,
        node_id="node-a",
        steady_cpu=1.0,
        steady_memory_gb=2.0,
        peak_cpu=5.0,
        peak_memory_gb=4.0,
    )
    await _reservation_for_workspace(
        session_factory,
        requested_id,
        node_id="node-a",
        steady_cpu=2.0,
        steady_memory_gb=4.0,
        peak_cpu=2.0,
        peak_memory_gb=8.0,
    )

    summary = await summarize_resource_saturation(
        session_factory,
        settings=settings,
        disk_check=_disk_check(),
        now=now,
    )

    assert summary.allocated_resources.active_workspace_count == 0
    assert summary.capacity_queue.queued_workspace_count == 1
    assert summary.capacity_queue.blocked_reason_counts == {
        "PEAK_CPU_CAPACITY_SATURATED": 1,
    }


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
async def test_count_awaiting_human_node_agnostic_spans_all_nodes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The helper's default node_id=None counts awaiting-human monitoring_pr
    # workspaces across every node; a node_id scopes the count to that node
    # (plus unassigned rows). Unflagged monitoring_pr rows are excluded either way.
    now = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
    local_flagged = await create_workspace(
        session_factory, status=WorkspaceStatus.monitoring_pr, updated_at=now
    )
    remote_flagged = await create_workspace(
        session_factory, status=WorkspaceStatus.monitoring_pr, updated_at=now
    )
    await create_workspace(session_factory, status=WorkspaceStatus.monitoring_pr, updated_at=now)
    async with session_factory() as session:
        repo = WorkspaceRepository(session)
        for workspace_id, node in ((local_flagged, "local"), (remote_flagged, "remote")):
            workspace = await repo.get(workspace_id)
            assert workspace is not None
            workspace.node_id = node
            await repo.set_workspace_attention(workspace_id, reason="needs a human", now=now)
        await session.commit()

    async with session_factory() as session:
        # node_id=None spans both nodes (the node-agnostic branch)...
        assert await metrics_resources._count_awaiting_human(session, node_id=None) == 2  # noqa: SLF001
        # ...while a node scope excludes the workspace pinned to another node.
        assert (
            await metrics_resources._count_awaiting_human(session, node_id="local") == 1  # noqa: SLF001
        )
