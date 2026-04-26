"""Workspace reliability summary service tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.common.config import Settings
from awf.db.enums import FailureReason, WorkspaceStatus
from awf.db.repositories import WorkspaceRepository
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
    failure_reason: FailureReason | None = None,
) -> None:
    async with session_factory() as session:
        workspace = await WorkspaceRepository(session).create(
            repo_url="git@github.com:example/metrics.git",
            branch_base="main",
            task_title=f"{status.value} workspace",
            task_prompt="Collect workspace reliability metrics.",
            agent="codex",
            test_commands=[],
        )
        workspace.status = status.value
        workspace.updated_at = updated_at
        workspace.failure_reason = failure_reason.value if failure_reason is not None else None
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
async def test_empty_db_returns_zero_workspace_reliability_summary(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from awf.service.metrics import summarize_workspace_reliability

    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)

    summary = await summarize_workspace_reliability(session_factory, now=now)

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

    summary = await summarize_workspace_reliability(session_factory, now=now)

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

    summary = await summarize_workspace_reliability(session_factory, since_hours=6, now=now)

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

    summary = await summarize_workspace_reliability(session_factory, now=now)

    assert summary.status_counts == _zero_status_counts()
    assert summary.active_count == 2
    assert summary.destroying_count == 1


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
