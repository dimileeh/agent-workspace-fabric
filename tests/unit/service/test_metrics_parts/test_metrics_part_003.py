"""Workspace reliability summary service tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from awf.common.config import Settings
from awf.db.enums import FailureReason, OperationStatus, OperationType, WorkspaceStatus
from awf.db.repositories import (
    ResourceReservationRepository,
    TaskAttemptRepository,
    TaskRepository,
    WorkspaceRepository,
)
from awf.db.session import make_session_factory
from awf.service.disk import DiskCheck
from awf.service.resource_capacity import LocalCapacityLimits
from tests.unit.helpers import create_operation, create_workspace

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
    assert summary.local_capacity.cpu_cores == 24.0
    assert summary.local_capacity.memory_gb == 96.0
    assert summary.local_capacity.source == "operator_config"
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
@pytest.mark.parametrize(
    (
        "cpu_configured",
        "memory_configured",
        "expected_source",
        "expected_reason_code",
        "expected_detail",
    ),
    (
        (True, True, "operator_config", None, None),
        (True, False, "mixed", "DOCKER_INFO_UNAVAILABLE", "docker daemon down"),
        (False, True, "mixed", "DOCKER_INFO_UNAVAILABLE", "docker daemon down"),
    ),
)
async def test_resource_saturation_omits_docker_errors_for_operator_overrides(
    session_factory: async_sessionmaker[AsyncSession],
    cpu_configured: bool,
    memory_configured: bool,
    expected_source: str,
    expected_reason_code: str | None,
    expected_detail: str | None,
) -> None:
    from awf.service.metrics import summarize_resource_saturation

    settings = Settings(
        _env_file=None,
        work_dir="/tmp/awf-work",
        local_capacity_cpu_cores=24.0 if cpu_configured else None,
        local_capacity_memory_gb=96.0 if memory_configured else None,
        local_capacity_dind_slots=1,
    )
    now = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)

    summary = await summarize_resource_saturation(
        session_factory,
        settings=settings,
        disk_check=_disk_check(free_bytes=16 * 1024 * _MIB),
        detected_local_capacity=LocalCapacityLimits(
            cpu_cores=8.0,
            memory_gb=16.0,
            reason_code="DOCKER_INFO_UNAVAILABLE",
            detail="docker daemon down",
        ),
        now=now,
    )

    assert summary.local_capacity.source == expected_source
    assert summary.local_capacity.cpu_cores == (24.0 if cpu_configured else 8.0)
    assert summary.local_capacity.memory_gb == (96.0 if memory_configured else 16.0)
    assert summary.local_capacity.reason_code == expected_reason_code
    assert summary.local_capacity.detail == expected_detail


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
    import awf.service.metrics_resources as metrics_resources_mod
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

    monkeypatch.setattr(metrics_resources_mod.asyncio, "to_thread", fake_to_thread)
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
    assert func is metrics_resources_mod.check_disk_space
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
async def test_recovering_excluded_from_reliability_stuck_count(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # ``recovering`` is an intentional auto-retry pause (#612): even an
    # active row past the 2×SLA cutoff with no failure_reason must not be
    # reported as a stuck workspace by /v1/metrics/reliability. This mirrors
    # the same exclusion applied to the SLO ``_count_stuck_detailed`` query.
    from awf.service.metrics import summarize_workspace_reliability

    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    settings = Settings(_env_file=None, agent_wall_timeout_seconds=3600)
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.recovering,
        updated_at=now - timedelta(hours=2),
        created_at=now - timedelta(hours=3),
    )

    summary = await summarize_workspace_reliability(session_factory, settings=settings, now=now)

    assert summary.stuck_count == 0


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
async def test_recovering_excluded_from_stuck_detection(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # ``recovering`` is an intentional auto-retry pause (#612): even past the
    # 2×SLA cutoff it must not surface as stuck_running/stuck_with_reason. This
    # guards the actual ``_count_stuck_detailed`` query, not just the unused
    # SLO ``EXECUTION_IN_USE_STATUSES`` set.
    from awf.service.metrics import summarize_slo_metrics_for_session

    now = datetime(2026, 4, 28, 12, 0, tzinfo=UTC)
    settings = Settings(_env_file=None, agent_wall_timeout_seconds=3600)
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.recovering,
        created_at=now - timedelta(hours=3),
        updated_at=now - timedelta(hours=2),
    )
    await create_workspace(
        session_factory,
        status=WorkspaceStatus.recovering,
        created_at=now - timedelta(hours=3),
        updated_at=now - timedelta(hours=2),
        failure_reason="idle_timeout",
    )

    async with session_factory() as session:
        summary = await summarize_slo_metrics_for_session(
            session,
            settings=settings,
            now=now,
        )

    assert summary.stuck_running_count == 0
    assert summary.stuck_with_reason_count == 0


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
