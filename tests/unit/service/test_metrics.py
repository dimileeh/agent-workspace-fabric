"""Workspace reliability summary service tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.common.config import Settings
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.repositories import (
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.service.disk import DiskCheck


@pytest.fixture
async def session_factory(
    engine: AsyncEngine,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    yield make_session_factory(engine)


async def _workspace(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: WorkspaceStatus,
    updated_at: datetime,
    failure_reason: FailureReason | str | None = None,
    created_at: datetime | None = None,
    repo_url: str = "git@github.com:example/metrics.git",
    branch_base: str = "main",
    task_title: str | None = None,
    agent: str = "codex",
    failure_message: str | None = None,
    pr_url: str | None = None,
    task_policy: dict | None = None,
) -> str:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url=repo_url,
            branch_base=branch_base,
            task_title=task_title or f"{status.value} workspace",
            task_prompt="Collect workspace reliability metrics.",
            agent=agent,
            test_commands=[],
        )
        workspace.status = status.value
        if created_at is not None:
            workspace.created_at = created_at
        workspace.updated_at = updated_at
        workspace.failure_reason = (
            failure_reason.value if isinstance(failure_reason, FailureReason) else failure_reason
        )
        workspace.failure_message = failure_message
        workspace.pr_url = pr_url
        if task_policy is not None:
            workspace.task_policy = task_policy
        await session.commit()
        return workspace.id


async def _reservation_for_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_id: str,
    *,
    steady_cpu: float,
    steady_memory_gb: float,
    peak_cpu: float,
    peak_memory_gb: float,
    disk_mb: int | None = None,
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
            node_id="local",
            steady_cpu=steady_cpu,
            steady_memory_gb=steady_memory_gb,
            peak_cpu=peak_cpu,
            peak_memory_gb=peak_memory_gb,
            disk_mb=disk_mb,
            phase="workspace_lifecycle",
            reserved_at=reserved_at,
        )
        reservation.released_at = released_at
        await session.commit()


def _zero_status_counts() -> dict[str, int]:
    return {status.value: 0 for status in WorkspaceStatus}


def _disk_check(
    *,
    ok: bool = True,
    free_bytes: int = 900,
    threshold_bytes: int = 400,
    reason: str = "SUFFICIENT_DISK",
) -> DiskCheck:
    return DiskCheck(
        path="/tmp/awf-work",
        checked_path="/tmp",
        total_bytes=1000,
        used_bytes=1000 - free_bytes,
        free_bytes=free_bytes,
        percent_free=round(free_bytes / 1000 * 100, 2),
        threshold_bytes=threshold_bytes,
        ok=ok,
        status="ok" if ok else "fail",
        reason=reason,
        detail=None if ok else "Free disk is below the configured admission threshold.",
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
    assert summary.status_counts == _zero_status_counts()
    assert summary.failure_reason_counts == {}
    assert summary.active_count == 0
    assert summary.destroying_count == 0
    assert summary.completed_count == 0
    assert summary.failed_count == 0
    assert summary.cancelled_count == 0
    assert summary.destroyed_count == 0
    assert summary.cleanup_failure_count == 0


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
        await _workspace(session_factory, status=status, updated_at=now - timedelta(minutes=10))
    await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=10),
        failure_reason=FailureReason.agent_failure,
    )
    await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=10),
        failure_reason=FailureReason.cleanup_failure,
    )

    summary = await summarize_workspace_reliability(session_factory, settings=Settings(), now=now)

    expected_status_counts = _zero_status_counts()
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
    await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=7),
    )
    await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=5),
        failure_reason=FailureReason.validation_failure,
    )

    summary = await summarize_workspace_reliability(session_factory, settings=Settings(), since_hours=6, now=now)

    expected_status_counts = _zero_status_counts()
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
    await _workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now - timedelta(hours=30),
    )
    await _workspace(
        session_factory,
        status=WorkspaceStatus.destroying,
        updated_at=now - timedelta(hours=30),
    )
    await _workspace(
        session_factory,
        status=WorkspaceStatus.completed,
        updated_at=now - timedelta(hours=30),
    )

    summary = await summarize_workspace_reliability(session_factory, settings=Settings(), now=now)

    assert summary.status_counts == _zero_status_counts()
    assert summary.active_count == 2
    assert summary.destroying_count == 1


@pytest.mark.unit
async def test_failure_analysis_groups_failed_workspaces_and_latest_examples(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_failure_analysis

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=25),
        failure_reason=FailureReason.agent_failure,
    )
    await _workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now - timedelta(minutes=4),
        failure_reason=FailureReason.validation_failure,
    )
    missing_reason_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=1),
        created_at=now - timedelta(minutes=7),
        task_title="Missing reason failure",
        failure_message="The executor failed before a reason was recorded.",
    )
    raw_unknown_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=2),
        created_at=now - timedelta(minutes=8),
        failure_reason="new_failure_reason",
        task_title="Unknown reason failure",
    )
    latest_validation_id = await _workspace(
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
    infrastructure_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=10),
        created_at=now - timedelta(minutes=20),
        failure_reason=FailureReason.infrastructure_failure,
    )
    middle_validation_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(minutes=30),
        failure_reason=FailureReason.validation_failure,
    )
    await _workspace(
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
    await _workspace(
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
        "from workspace_events",
        "from workspace_log_streams",
        "from task_attempts",
    )
    assert not [
        statement
        for statement in statements
        if any(table in statement for table in relationship_tables)
    ]


@pytest.mark.unit
async def test_failure_analysis_filters_by_since_hours(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_failure_analysis

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=3),
        failure_reason=FailureReason.validation_failure,
    )
    await _workspace(
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
            await _workspace(
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
        await _workspace(session_factory, status=status, updated_at=now)

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
    assert summary.admission.status == "saturated"
    assert summary.admission.reason == "WORKER_EXECUTION_CONCURRENCY_SATURATED"


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
    reserved_workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
    )
    legacy_workspace_id = await _workspace(
        session_factory,
        status=WorkspaceStatus.ready,
        updated_at=now,
    )
    released_workspace_id = await _workspace(
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
    workspace_id = await _workspace(
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
    await _workspace(session_factory, status=WorkspaceStatus.provisioning, updated_at=now)
    await _workspace(session_factory, status=WorkspaceStatus.running, updated_at=now)

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
    await _workspace(session_factory, status=WorkspaceStatus.provisioning, updated_at=now)

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
    await _workspace(
        session_factory,
        status=WorkspaceStatus.running,
        updated_at=now,
        created_at=now - timedelta(hours=1),
    )
    # 2. Active, older than 2x SLA (4 hours), no reason -> Stuck
    await _workspace(
        session_factory,
        status=WorkspaceStatus.monitoring_pr,
        updated_at=now - timedelta(hours=5),
        created_at=now - timedelta(hours=5),
    )
    # 3. Active, older than 2x SLA, has reason -> Not stuck (already classified)
    await _workspace(
        session_factory,
        status=WorkspaceStatus.pushing,
        updated_at=now - timedelta(hours=5),
        created_at=now - timedelta(hours=5),
        failure_reason="network_hiccup",
    )
    # 4. Terminal, older than 2x SLA -> Not active, so not stuck
    await _workspace(
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
    await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now,
        failure_reason=FailureReason.agent_failure,
    )
    # 2. Destroying with actionable reason in window
    await _workspace(
        session_factory,
        status=WorkspaceStatus.destroying,
        updated_at=now,
        failure_reason=FailureReason.cleanup_failure,
    )
    # 3. Failed with unknown reason in window
    await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now,
        failure_reason="unknown_error_code",
    )
    # 4. Cancelled with NO reason in window
    await _workspace(
        session_factory,
        status=WorkspaceStatus.cancelled,
        updated_at=now,
    )
    # 5. Failed outside window -> should not be counted
    await _workspace(
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
    await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=1),
        failure_reason=FailureReason.agent_failure,
        failure_message="Some error ... AGENT_AUTH_FAILED ...",
        agent="gemini",
    )
    # 2. model not found or 404
    await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=1),
        failure_reason=FailureReason.agent_failure,
        failure_message="404 model not found",
        agent="claude",
    )
    # 3. missing worktree
    await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=1),
        failure_reason=FailureReason.validation_failure,
        failure_message="missing managed worktree during fix loop",
        agent="gemini",
    )
    # 4. coverage threshold failure
    await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=1),
        failure_reason=FailureReason.validation_failure,
        failure_message="coverage threshold failure: expected 80%, got 75%",
        agent="gemini",
    )
    # 5. syntax/import errors during validation
    await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=1),
        failure_reason=FailureReason.validation_failure,
        failure_message="SyntaxError: invalid syntax",
        agent="opencode",
    )
    # 6. GitHub transient/auth errors
    await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=1),
        failure_reason=FailureReason.agent_failure,
        failure_message="GitHub auth/PR creation failed",
        agent="gemini",
    )
    # 7. unknown validation failure
    await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=1),
        failure_reason=FailureReason.validation_failure,
        failure_message="Some random pytest failure",
        agent="gemini",
    )
    # 8. Another syntax error to test grouping
    await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=1),
        failure_reason=FailureReason.validation_failure,
        failure_message="ImportError: No module named xxx",
        agent="opencode",
    )

    summary = await summarize_failure_analysis(session_factory, now=now)
    
    assert len(summary.root_cause_clusters) == 7
    cluster_reasons = [c.likely_cause for c in summary.root_cause_clusters]
    
    assert "Agent Auth Failed" in cluster_reasons
    assert "Model Not Found / 404" in cluster_reasons
    assert "Missing Managed Worktree" in cluster_reasons
    assert "Coverage Threshold Failure" in cluster_reasons
    assert "Syntax or Import Error" in cluster_reasons
    assert "GitHub Transient/Auth Error" in cluster_reasons
    assert "Unknown Validation Failure" in cluster_reasons

    # Check grouping
    syntax_cluster = next(c for c in summary.root_cause_clusters if c.likely_cause == "Syntax or Import Error")
    assert syntax_cluster.count == 2
    assert syntax_cluster.agent == "opencode"


@pytest.mark.unit
async def test_root_cause_clusters_agent_model_extraction(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_failure_analysis

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    
    await _workspace(
        session_factory,
        status=WorkspaceStatus.failed,
        updated_at=now - timedelta(hours=1),
        failure_reason=FailureReason.validation_failure,
        failure_message="SyntaxError",
        agent="gemini",
        task_policy={"agent_model": "gemini-1.5-pro"},
    )
    
    await _workspace(
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
    syntax_clusters = [c for c in summary.root_cause_clusters if c.likely_cause == "Syntax or Import Error"]
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
    
    await _workspace(
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

